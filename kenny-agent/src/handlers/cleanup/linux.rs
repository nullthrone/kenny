//! Linux cleanup: file-descriptor-relative primitives and the Linux targets.
//!
//! Every directory is held as an fd opened with `O_NOFOLLOW|O_DIRECTORY|O_CLOEXEC`
//! relative to its parent fd, every entry is read with `fstatat(AT_SYMLINK_NOFOLLOW)`
//! and removed with `unlinkat` on the pinned parent. Nothing below a root is ever
//! resolved by path, so swapping a directory for a symlink between enumeration and
//! deletion cannot redirect the walk. The walk stays on the root's mount: every entry
//! and every opened directory must carry the root's device **and** mount ID (`statx`
//! `STATX_MNT_ID`, else `mnt_id` from `/proc/self/fdinfo`) — a bind mount shares the
//! device, so the device alone would let the walk cross into it. Each directory's real
//! location (`/proc/self/fd`) is re-checked against the root before anything in it is
//! removed.

use std::ffi::{CStr, CString, OsStr, OsString};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use super::{
    journal_archive_rules, journal_total_rules, key_under, linux_logs_rules, package_cache_rules,
    path_key, sweep_root, tmp_rules, Cancel, CatalogEntry, FsError, Kind, Meta, Ops, Session, Stop,
    LIST_CAP, VAR_TMP_MIN_AGE_DAYS,
};
use crate::protocol::ErrorCode;
use crate::telemetry::collectors::proc::{run_command, ProcOutput};
use crate::telemetry::collectors::ProbeFailure;

/// An open, pinned directory.
pub struct FdDir {
    fd: OwnedFd,
    dev: u64,
    /// The root's mount ID; every directory on the walk carries the same one.
    mnt: u64,
    root: Arc<String>,
}

/// The fd-relative primitives.
pub struct FdOps;

fn errno() -> i32 {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

fn map_errno(e: i32) -> FsError {
    match e {
        libc::ENOENT => FsError::NotFound,
        libc::EACCES | libc::EPERM | libc::EROFS => FsError::Denied,
        libc::EBUSY | libc::ETXTBSY => FsError::InUse,
        libc::ELOOP => FsError::Reparse,
        libc::ENOTEMPTY | libc::EEXIST => FsError::NotEmpty,
        libc::ENOTDIR => FsError::Changed,
        _ => FsError::Other,
    }
}

fn map_io(e: &std::io::Error) -> FsError {
    e.raw_os_error().map(map_errno).unwrap_or(FsError::Other)
}

fn c_name(name: &OsStr) -> Result<CString, FsError> {
    if !super::is_plain_name(name) {
        return Err(FsError::Other);
    }
    CString::new(name.as_bytes()).map_err(|_| FsError::Other)
}

fn fstatat_nofollow(dirfd: RawFd, name: &CStr) -> Result<libc::stat, FsError> {
    // SAFETY: `name` is NUL-terminated; `st` is a valid out-pointer.
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::fstatat(dirfd, name.as_ptr(), &mut st, libc::AT_SYMLINK_NOFOLLOW) };
    if rc != 0 {
        return Err(map_errno(errno()));
    }
    Ok(st)
}

fn fstat(fd: RawFd) -> Result<libc::stat, FsError> {
    // SAFETY: `fd` is open; `st` is a valid out-pointer.
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } != 0 {
        return Err(map_errno(errno()));
    }
    Ok(st)
}

#[allow(clippy::unnecessary_cast)] // field widths differ between libc targets
fn time_of(sec: i64, nsec: i64) -> SystemTime {
    if sec < 0 {
        return UNIX_EPOCH;
    }
    UNIX_EPOCH + Duration::new(sec as u64, nsec.clamp(0, 999_999_999) as u32)
}

/// Age counts from the newest of mtime, ctime and atime (as `systemd-tmpfiles` does), so
/// a freshly extracted archive with old mtimes or a file still being read is not old.
/// A directory ignores atime: listing it (as every scan does) refreshes that, and would
/// keep an emptied directory forever young.
#[allow(clippy::unnecessary_cast)] // field widths differ between libc targets
fn meta_of(st: &libc::stat) -> Meta {
    let kind = match st.st_mode & libc::S_IFMT {
        libc::S_IFREG => Kind::File,
        libc::S_IFDIR => Kind::Dir,
        libc::S_IFLNK => Kind::Link,
        _ => Kind::Special,
    };
    let changed = time_of(st.st_mtime as i64, st.st_mtime_nsec as i64)
        .max(time_of(st.st_ctime as i64, st.st_ctime_nsec as i64));
    let newest = if kind == Kind::Dir {
        changed
    } else {
        changed.max(time_of(st.st_atime as i64, st.st_atime_nsec as i64))
    };
    Meta {
        kind,
        size: st.st_size.max(0) as u64,
        newest,
        id: (st.st_dev as u64, st.st_ino as u64),
        owner: Some(st.st_uid as u32),
    }
}

/// The mount ID of `name` in `dirfd` (or of `dirfd` itself with an empty `name` and
/// `AT_EMPTY_PATH`), never following a link or triggering an automount. `None` where the
/// kernel does not report one (`statx` before 4.11, `STATX_MNT_ID` before 5.8). Issued as
/// a raw syscall so the static musl build does not depend on the C library's wrapper.
fn statx_mnt_id(dirfd: RawFd, name: &CStr, flags: libc::c_int) -> Option<u64> {
    // SAFETY: `name` is NUL-terminated; `stx` is a valid, zeroed out-buffer.
    let mut stx: libc::statx = unsafe { std::mem::zeroed() };
    let rc = unsafe {
        libc::syscall(
            libc::SYS_statx,
            dirfd,
            name.as_ptr(),
            flags | libc::AT_SYMLINK_NOFOLLOW | libc::AT_NO_AUTOMOUNT,
            libc::STATX_MNT_ID,
            &mut stx as *mut libc::statx,
        )
    };
    (rc == 0 && stx.stx_mask & libc::STATX_MNT_ID != 0).then_some(stx.stx_mnt_id)
}

/// `mnt_id` from a `/proc/self/fdinfo/<fd>` document.
pub(super) fn parse_fdinfo_mnt_id(doc: &str) -> Option<u64> {
    doc.lines()
        .find_map(|l| l.strip_prefix("mnt_id:"))
        .and_then(|v| v.trim().parse().ok())
}

/// The mount ID of an open fd: `statx`, else `/proc/self/fdinfo`.
fn fd_mnt_id(fd: RawFd) -> Option<u64> {
    statx_mnt_id(fd, c"", libc::AT_EMPTY_PATH).or_else(|| {
        std::fs::read_to_string(format!("/proc/self/fdinfo/{fd}"))
            .ok()
            .and_then(|d| parse_fdinfo_mnt_id(&d))
    })
}

/// An object whose mount ID differs from the root's — or cannot be told — is on another
/// mount (a mount point or bind mount below the root) and is not entered.
pub(super) fn require_same_mount(root: u64, got: Option<u64>) -> Result<(), FsError> {
    match got {
        Some(m) if m == root => Ok(()),
        _ => Err(FsError::OtherDevice),
    }
}

fn open_dir_at(dirfd: RawFd, name: &CStr) -> Result<OwnedFd, FsError> {
    // SAFETY: `name` is NUL-terminated; the returned fd is owned below.
    let fd = unsafe {
        libc::openat(
            dirfd,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(map_errno(errno()));
    }
    // SAFETY: `fd` was just returned by `openat` and is owned by nobody else.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// Re-check that an open directory still lives under the root. Uses the kernel's view
/// of the fd's current path; where `/proc` is unavailable the fd walk alone holds.
fn verify_under_root(dir: &FdDir) -> Result<(), FsError> {
    let link = format!("/proc/self/fd/{}", dir.fd.as_raw_fd());
    let Ok(real) = std::fs::read_link(&link) else {
        return Ok(());
    };
    let key = path_key(&real);
    if key.ends_with(" (deleted)") {
        return Err(FsError::Changed);
    }
    if key_under(&key, &dir.root) {
        Ok(())
    } else {
        Err(FsError::Escaped)
    }
}

impl FdOps {
    fn child(&self, parent: &FdDir, name: &OsStr, expect: &Meta) -> Result<FdDir, FsError> {
        let c = c_name(name)?;
        let fd = match open_dir_at(parent.fd.as_raw_fd(), &c) {
            Ok(fd) => fd,
            // `O_DIRECTORY|O_NOFOLLOW` on a symlink fails with ENOTDIR: say what it is.
            Err(FsError::Changed) => {
                let now = fstatat_nofollow(parent.fd.as_raw_fd(), &c).map(|st| meta_of(&st));
                return Err(match now {
                    Ok(m) if m.kind == Kind::Link => FsError::Reparse,
                    _ => FsError::Changed,
                });
            }
            Err(e) => return Err(e),
        };
        let m = meta_of(&fstat(fd.as_raw_fd())?);
        if m.kind != Kind::Dir || m.id != expect.id {
            return Err(FsError::Changed);
        }
        if m.id.0 != parent.dev {
            return Err(FsError::OtherDevice);
        }
        require_same_mount(parent.mnt, fd_mnt_id(fd.as_raw_fd()))?;
        let child = FdDir {
            fd,
            dev: parent.dev,
            mnt: parent.mnt,
            root: parent.root.clone(),
        };
        verify_under_root(&child)?;
        Ok(child)
    }

    /// Re-read `name` and require it to still be the examined object of `kind`.
    fn recheck(&self, dir: &FdDir, c: &CStr, expect: &Meta, kind: Kind) -> Result<(), FsError> {
        let m = meta_of(&fstatat_nofollow(dir.fd.as_raw_fd(), c)?);
        if m.kind == Kind::Link {
            return Err(FsError::Reparse);
        }
        if m.kind != kind || m.id != expect.id {
            return Err(FsError::Changed);
        }
        verify_under_root(dir)
    }
}

impl Ops for FdOps {
    type Dir = FdDir;

    fn open_root(&self, base: &Path, chain: &[&str]) -> Result<(FdDir, PathBuf), FsError> {
        let md = std::fs::symlink_metadata(base).map_err(|e| map_io(&e))?;
        if md.file_type().is_symlink() {
            return Err(FsError::Reparse);
        }
        if !md.is_dir() {
            return Err(FsError::Other);
        }
        let canon = std::fs::canonicalize(base).map_err(|e| map_io(&e))?;
        let c = CString::new(canon.as_os_str().as_bytes()).map_err(|_| FsError::Other)?;
        // SAFETY: `c` is NUL-terminated; the fd is owned below.
        let raw = unsafe {
            libc::open(
                c.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if raw < 0 {
            return Err(map_errno(errno()));
        }
        // SAFETY: just opened, owned by nobody else.
        let fd = unsafe { OwnedFd::from_raw_fd(raw) };
        let m = meta_of(&fstat(fd.as_raw_fd())?);
        // Without the root's mount ID the walk could not tell a bind mount: refuse.
        let mnt = fd_mnt_id(fd.as_raw_fd()).ok_or(FsError::Other)?;
        let mut dir = FdDir {
            fd,
            dev: m.id.0,
            mnt,
            root: Arc::new(path_key(&canon)),
        };
        let mut path = canon;
        for comp in chain {
            let name = OsStr::new(comp);
            let m = self.stat(&dir, name)?;
            match m.kind {
                Kind::Link => return Err(FsError::Reparse),
                Kind::Dir => {}
                _ => return Err(FsError::Other),
            }
            dir = self.child(&dir, name, &m)?;
            path.push(comp);
        }
        dir.root = Arc::new(path_key(&path));
        Ok((dir, path))
    }

    fn list(&self, dir: &FdDir) -> Result<Vec<OsString>, FsError> {
        // SAFETY: duplicate the pinned fd; `fdopendir` takes ownership of the duplicate
        // and `closedir` releases it on every path below.
        let dup = unsafe { libc::fcntl(dir.fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
        if dup < 0 {
            return Err(map_errno(errno()));
        }
        let dirp = unsafe { libc::fdopendir(dup) };
        if dirp.is_null() {
            let e = errno();
            unsafe { libc::close(dup) };
            return Err(map_errno(e));
        }
        unsafe { libc::rewinddir(dirp) };
        let mut names = Vec::new();
        let result = loop {
            // SAFETY: errno is thread-local; reset it so end-of-stream and error differ.
            unsafe { *libc::__errno_location() = 0 };
            let ent = unsafe { libc::readdir(dirp) };
            if ent.is_null() {
                let e = errno();
                break if e == 0 { Ok(()) } else { Err(map_errno(e)) };
            }
            // SAFETY: `d_name` is NUL-terminated within the entry readdir returned.
            let name = unsafe { CStr::from_ptr((*ent).d_name.as_ptr()) }.to_bytes();
            if name == b"." || name == b".." {
                continue;
            }
            names.push(OsString::from_vec(name.to_vec()));
            if names.len() >= LIST_CAP {
                break Ok(());
            }
        };
        unsafe { libc::closedir(dirp) };
        result.map(|()| names)
    }

    fn stat(&self, dir: &FdDir, name: &OsStr) -> Result<Meta, FsError> {
        let c = c_name(name)?;
        let m = meta_of(&fstatat_nofollow(dir.fd.as_raw_fd(), &c)?);
        if m.id.0 != dir.dev {
            return Err(FsError::OtherDevice);
        }
        // A mount point (bind mounts included) reports the mounted root's ID. Where the
        // kernel cannot say, `open_dir` still checks the opened directory's own.
        if m.kind != Kind::Link {
            if let Some(mnt) = statx_mnt_id(dir.fd.as_raw_fd(), &c, 0) {
                require_same_mount(dir.mnt, Some(mnt))?;
            }
        }
        Ok(m)
    }

    fn open_dir(&self, parent: &FdDir, name: &OsStr, expect: &Meta) -> Result<FdDir, FsError> {
        self.child(parent, name, expect)
    }

    fn remove_file(
        &self,
        dir: &FdDir,
        name: &OsStr,
        expect: &Meta,
        commit: Option<&Cancel>,
    ) -> Result<(), FsError> {
        let c = c_name(name)?;
        self.recheck(dir, &c, expect, Kind::File)?;
        let Some(cancel) = commit else {
            return Ok(());
        };
        let Some(_permit) = cancel.permit() else {
            return Err(FsError::Cancelled);
        };
        // SAFETY: `c` is NUL-terminated; `dir.fd` is the pinned parent.
        if unsafe { libc::unlinkat(dir.fd.as_raw_fd(), c.as_ptr(), 0) } != 0 {
            return Err(map_errno(errno()));
        }
        Ok(())
    }

    fn remove_dir(
        &self,
        dir: &FdDir,
        name: &OsStr,
        expect: &Meta,
        commit: Option<&Cancel>,
    ) -> Result<(), FsError> {
        let c = c_name(name)?;
        self.recheck(dir, &c, expect, Kind::Dir)?;
        let Some(cancel) = commit else {
            return Ok(());
        };
        let Some(_permit) = cancel.permit() else {
            return Err(FsError::Cancelled);
        };
        // SAFETY: as above; AT_REMOVEDIR only removes an empty directory.
        if unsafe { libc::unlinkat(dir.fd.as_raw_fd(), c.as_ptr(), libc::AT_REMOVEDIR) } != 0 {
            return Err(map_errno(errno()));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------------

/// Minimal `PATH` for a fixed-argv child (package managers spawn helpers).
const CHILD_PATH: &str = "/usr/sbin:/usr/bin:/sbin:/bin";

/// Run an absolute `program` with fixed `args`: no shell, environment cleared to a
/// minimal `PATH` and C locale, working directory `/`, stdin null, killed past `budget`.
pub(crate) fn run_fixed(
    program: &str,
    args: &[&str],
    budget: Duration,
) -> Result<ProcOutput, ProbeFailure> {
    debug_assert!(program.starts_with('/'), "run_fixed needs an absolute path");
    let mut cmd = Command::new(program);
    cmd.args(args)
        .env_clear()
        .env("PATH", CHILD_PATH)
        .env("LANG", "C")
        .env("LC_ALL", "C")
        .current_dir("/");
    run_command(cmd, budget)
}

/// The first of `candidates` that exists as a file.
fn first_existing(candidates: &[&'static str]) -> Option<&'static str> {
    candidates.iter().copied().find(|p| Path::new(p).is_file())
}

/// The package caches `fs_cleanup_scan` sums for `package_cache`.
const PACKAGE_CACHE_DIRS: &[&str] = &[
    "/var/cache/apt/archives",
    "/var/cache/dnf",
    "/var/cache/zypp/packages",
];

/// The journal directories a vacuum shrinks.
const JOURNAL_DIRS: &[&str] = &["/var/log/journal", "/run/log/journal"];

/// The package manager's own clean, as a fixed argv.
fn package_manager() -> Option<(&'static str, &'static [&'static str])> {
    const MANAGERS: &[(&str, &[&str])] = &[
        ("/usr/bin/apt-get", &["clean"]),
        ("/usr/bin/dnf", &["clean", "packages"]),
        ("/usr/bin/zypper", &["clean"]),
    ];
    MANAGERS
        .iter()
        .copied()
        .find(|(p, _)| Path::new(p).is_file())
}

/// Sweep `dirs` read-only into a fresh measuring session.
fn measure_into<'c>(s: &Session<'c>, dirs: &[&str], rules: &super::Rules) -> Session<'c> {
    let mut m = s.measuring();
    for d in dirs {
        sweep_root(&FdOps, &mut m, Path::new(d), &[], rules);
    }
    m
}

/// Run a cleanup command and account for it as the size delta of `dirs`.
fn command_cleanup(
    s: &mut Session,
    program: &str,
    args: &[&str],
    dirs: &[&str],
    rules: &super::Rules,
) {
    let tool = Path::new(program)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    command_cleanup_with(
        s,
        &tool,
        |budget| run_fixed(program, args, budget),
        dirs,
        rules,
    );
}

/// [`command_cleanup`] with the command injected (`run` gets the time left).
pub(super) fn command_cleanup_with(
    s: &mut Session,
    tool: &str,
    run: impl FnOnce(Duration) -> Result<ProcOutput, ProbeFailure>,
    dirs: &[&str],
    rules: &super::Rules,
) {
    let before = measure_into(s, dirs, rules);
    // A cancelled call starts no command (its own budget bounds one already running).
    let Some(permit) = s.cancel().permit() else {
        s.stopped.get_or_insert(Stop::Time);
        return;
    };
    drop(permit);
    let budget = s.time_left();
    match run(budget) {
        Ok(out) if out.success => {}
        Ok(out) => {
            let code = out
                .code
                .map_or_else(|| "a signal".to_string(), |c| format!("code {c}"));
            s.error("command_failed", format!("{tool} exited with {code}"));
        }
        Err(ProbeFailure::Timeout(_)) => {
            s.stopped = Some(Stop::Time);
            s.error(
                "command_timeout",
                format!("{tool} did not finish in time and was stopped"),
            );
        }
        Err(_) => s.error("command_failed", format!("{tool} could not be started")),
    }
    let after = measure_into(s, dirs, rules);
    account_delta(s, before, after);
}

/// Account a command-driven cleanup as `before - after`. An incomplete `after`
/// measurement under-counts what is left and would over-report what was freed, so the
/// call then reports nothing freed, `complete: false` and an error entry. (An incomplete
/// `before` only under-reports.)
pub(super) fn account_delta(s: &mut Session, before: Session, after: Session) {
    s.examined += before.examined + after.examined;
    s.skipped = after.skipped;
    for e in after.errors {
        s.error(e.code, e.message);
    }
    if after.stopped.is_some() {
        s.error(
            "measure_incomplete",
            "the space left after the cleanup could not be measured in time; nothing freed is reported",
        );
    } else {
        s.files += before.files.saturating_sub(after.files);
        s.bytes += before.bytes.saturating_sub(after.bytes);
    }
    if s.stopped.is_none() {
        s.stopped = before.stopped.or(after.stopped);
    }
}

/// Run one Linux target.
pub(super) fn execute(entry: &CatalogEntry, s: &mut Session) -> Result<(), (ErrorCode, String)> {
    match entry.target {
        "tmp" => {
            sweep_root(
                &FdOps,
                s,
                Path::new("/tmp"),
                &[],
                &tmp_rules(entry.min_age_days),
            );
            if s.stopped.is_none() {
                sweep_root(
                    &FdOps,
                    s,
                    Path::new("/var/tmp"),
                    &[],
                    &tmp_rules(VAR_TMP_MIN_AGE_DAYS),
                );
            }
        }
        "logs_old" => sweep_root(
            &FdOps,
            s,
            Path::new("/var/log"),
            &[],
            &linux_logs_rules(entry.min_age_days),
        ),
        "package_cache" => {
            let Some((program, args)) = package_manager() else {
                return Err((
                    ErrorCode::ExecFailed,
                    "no supported package manager (apt-get, dnf, zypper) found".to_string(),
                ));
            };
            let rules = package_cache_rules();
            if s.dry_run {
                for d in PACKAGE_CACHE_DIRS {
                    sweep_root(&FdOps, s, Path::new(d), &[], &rules);
                }
            } else {
                command_cleanup(s, program, args, PACKAGE_CACHE_DIRS, &rules);
            }
        }
        "journal" => {
            let Some(program) = first_existing(&["/usr/bin/journalctl", "/bin/journalctl"]) else {
                return Err((ErrorCode::ExecFailed, "journalctl not found".to_string()));
            };
            if s.dry_run {
                let rules = journal_archive_rules(entry.min_age_days);
                for d in JOURNAL_DIRS {
                    sweep_root(&FdOps, s, Path::new(d), &[], &rules);
                }
            } else {
                command_cleanup(
                    s,
                    program,
                    &["--vacuum-time=14d"],
                    JOURNAL_DIRS,
                    &journal_total_rules(),
                );
            }
        }
        other => {
            return Err((
                ErrorCode::BadArgs,
                format!("unknown cleanup target {other:?}"),
            ))
        }
    }
    Ok(())
}

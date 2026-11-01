//! `fs_cleanup_scan` / `fs_cleanup` — free disk space from a fixed, agent-owned catalog.
//!
//! `target` is the only argument and names one catalog entry; every root is derived from
//! the agent's own environment, never from the request. The catalog (names, order and
//! `min_age_days` per OS) is pinned by `docs/fixtures/vectors/fs_cleanup_targets.json`.
//!
//! Layout:
//!
//! * **Core (this file, cfg-free).** A walker that takes an injected root, [`Rules`]
//!   (age threshold, depth, name filters), [`Budgets`], a [`Clock`] and a `dry_run`
//!   flag. `fs_cleanup_scan` runs it with `dry_run = true`, `fs_cleanup` with
//!   `dry_run = false` — one code path, so a scan reports what a cleanup removes.
//! * **Primitives ([`Ops`]).** Each platform supplies a pinned directory walk:
//!   Linux walks file descriptors (`openat(O_NOFOLLOW|O_DIRECTORY)`, `fstatat(
//!   AT_SYMLINK_NOFOLLOW)`, `unlinkat`) and stays on the root's device; Windows holds
//!   directory handles opened with `FILE_FLAG_OPEN_REPARSE_POINT` and without
//!   `FILE_SHARE_DELETE`, verifies each object's final path is under the root, and
//!   deletes through the very handle it verified (`FileDispositionInfo`).
//! * **Platform targets (`cleanup/linux.rs`, `cleanup/windows.rs`).** Resolve the roots
//!   and the per-target extras (service stop/restore, package manager, journal vacuum).
//!
//! Safety rules the walker guarantees: links/junctions/reparse points are never
//! followed or deleted (counted under `skipped.reparse`); a root that is itself a link is
//! refused; every candidate stays under its canonical root; a root is never deleted; an
//! emptied subdirectory is removed only when it was older than the threshold; sockets,
//! FIFOs and devices are never removed; the agent's own log directory and state files are
//! excluded by an explicit candidate check. Work is bounded by [`CLEANUP_BUDGET`] /
//! [`SCAN_BUDGET`], [`MAX_EXAMINED`] and [`MAX_DELETIONS`]; one cleanup or service restart
//! runs at a time per host ([`try_host_lock`]).

use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard, TryLockError};
use std::time::{Duration, Instant, SystemTime};

use serde_json::{json, Value};

use crate::protocol::ErrorCode;

#[cfg(target_os = "linux")]
mod linux;
#[cfg(windows)]
mod windows;

#[cfg(target_os = "linux")]
pub(crate) use linux::run_fixed;

/// Wall-clock bound of one `fs_cleanup` call (walk, deletions and any command it runs).
pub const CLEANUP_BUDGET: Duration = Duration::from_secs(90);

/// Wall-clock bound of one `fs_cleanup_scan` call.
pub const SCAN_BUDGET: Duration = Duration::from_secs(60);

/// Entries one call may examine before it stops with `complete: false`.
pub const MAX_EXAMINED: u64 = 50_000;

/// Deletions one call may perform (a scan counts the deletions it would perform).
pub const MAX_DELETIONS: u64 = 20_000;

/// Cap on the `errors` array of a result.
pub const MAX_ERRORS: usize = 20;

/// Deepest directory level the walker descends to, so a hostile tree cannot exhaust
/// the stack or the handle table.
pub const MAX_DEPTH: usize = 64;

/// Most names one directory listing collects. Past [`MAX_EXAMINED`] the call stops
/// anyway; the cap keeps a directory with millions of entries from costing that much
/// memory first.
pub const LIST_CAP: usize = MAX_EXAMINED as usize + 1;

/// The message a call that finds the host busy fails with (`exec_failed`).
pub const BUSY_MESSAGE: &str = "another cleanup or service restart is running";

// ---------------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------------

/// One catalog entry: a target name and its fixed age threshold.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CatalogEntry {
    pub target: &'static str,
    pub min_age_days: u32,
}

const fn entry(target: &'static str, min_age_days: u32) -> CatalogEntry {
    CatalogEntry {
        target,
        min_age_days,
    }
}

/// The Windows catalog, in contract order.
pub const WINDOWS_CATALOG: &[CatalogEntry] = &[
    entry("temp_system", 7),
    entry("temp_users", 7),
    entry("windows_update_cache", 1),
    entry("recycle_bin", 30),
    entry("logs_old", 30),
];

/// The Linux catalog, in contract order. `tmp`'s threshold is the `/tmp` one;
/// `/var/tmp` uses [`VAR_TMP_MIN_AGE_DAYS`].
pub const LINUX_CATALOG: &[CatalogEntry] = &[
    entry("tmp", 10),
    entry("package_cache", 0),
    entry("journal", 14),
    entry("logs_old", 30),
];

/// `/var/tmp` threshold for the Linux `tmp` target.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // Linux targets; exercised by the tests everywhere
pub const VAR_TMP_MIN_AGE_DAYS: u32 = 30;

/// The OS family whose catalog applies.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CatalogOs {
    Windows,
    Linux,
    /// Any other OS (macOS, BSD): both tools are `unsupported`.
    Other,
}

impl CatalogOs {
    /// The family this build runs on.
    pub fn native() -> Self {
        if cfg!(windows) {
            CatalogOs::Windows
        } else if cfg!(target_os = "linux") {
            CatalogOs::Linux
        } else {
            CatalogOs::Other
        }
    }
}

/// Resolve a request's `args` to a catalog entry for `os`.
///
/// Another OS's target is `unsupported`, an unknown or missing one `bad_args`, and on an
/// OS without a catalog every call is `unsupported`.
pub fn lookup(os: CatalogOs, args: &Value) -> Result<CatalogEntry, (ErrorCode, String)> {
    let (native, other) = match os {
        CatalogOs::Windows => (WINDOWS_CATALOG, LINUX_CATALOG),
        CatalogOs::Linux => (LINUX_CATALOG, WINDOWS_CATALOG),
        CatalogOs::Other => {
            return Err((
                ErrorCode::Unsupported,
                "disk cleanup is not supported on this platform".to_string(),
            ))
        }
    };
    let Some(target) = args.get("target").and_then(Value::as_str) else {
        return Err((
            ErrorCode::BadArgs,
            "`target` must be a string naming a catalog entry".to_string(),
        ));
    };
    if let Some(e) = native.iter().find(|e| e.target == target) {
        return Ok(*e);
    }
    if other.iter().any(|e| e.target == target) {
        return Err((
            ErrorCode::Unsupported,
            format!("cleanup target {target:?} is not available on this OS"),
        ));
    }
    Err((ErrorCode::BadArgs, "unknown cleanup target".to_string()))
}

// ---------------------------------------------------------------------------------
// Host lock (shared with service_restart)
// ---------------------------------------------------------------------------------

static HOST_LOCK: Mutex<()> = Mutex::new(());

/// Take the per-host lock that serialises cleanups and service restarts, or fail with
/// `exec_failed` when another one holds it. A poisoned lock is still a free lock: the
/// panic that poisoned it has already ended that call.
pub fn try_host_lock() -> Result<MutexGuard<'static, ()>, (ErrorCode, String)> {
    match HOST_LOCK.try_lock() {
        Ok(guard) => Ok(guard),
        Err(TryLockError::Poisoned(p)) => Ok(p.into_inner()),
        Err(TryLockError::WouldBlock) => Err((ErrorCode::ExecFailed, BUSY_MESSAGE.to_string())),
    }
}

/// Hold the host lock for a test (blocking), so a handler under test sees a busy host.
#[cfg(test)]
pub(crate) fn hold_host_lock_for_test() -> MutexGuard<'static, ()> {
    HOST_LOCK.lock().unwrap_or_else(|p| p.into_inner())
}

// ---------------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------------

/// What kind of object a directory entry is, read without following links.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    File,
    Dir,
    /// A symlink, junction or other reparse point: never followed, never deleted.
    Link,
    /// A socket, FIFO, device or anything else that is not a regular file.
    Special,
}

/// Metadata of one entry, read without following links.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Meta {
    pub kind: Kind,
    pub size: u64,
    /// The newest of the timestamps that count towards age (see each platform).
    pub newest: SystemTime,
    /// `(device, inode)` / `(volume serial, file index)`: the identity a later
    /// operation re-checks so a swapped entry is never acted on.
    pub id: (u64, u64),
}

/// Why a primitive refused or failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FsError {
    /// Vanished in the meantime: nothing to do.
    NotFound,
    /// Held open by another process (`skipped.in_use`).
    InUse,
    /// Not permitted (`skipped.denied`).
    Denied,
    /// The entry is (or turned into) a link or reparse point (`skipped.reparse`).
    Reparse,
    /// A directory that is not empty (a new entry appeared): kept silently.
    NotEmpty,
    /// The entry is no longer the object that was examined.
    Changed,
    /// The object resolved outside its root.
    Escaped,
    /// The entry lives on another filesystem (a mount point under the root).
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    OtherDevice,
    Other,
}

/// The pinned-walk primitives a platform provides. Every method takes an open
/// directory and a single entry name; none ever resolves a caller-supplied path.
pub trait Ops {
    type Dir;

    /// Open `base` (then each `chain` component below it, each refused if it is a link)
    /// as a root. Refuses a `base` that is itself a link. Returns the directory and its
    /// canonical path, which becomes the containment root.
    fn open_root(&self, base: &Path, chain: &[&str]) -> Result<(Self::Dir, PathBuf), FsError>;

    /// Entry names in `dir` (without `.`/`..`).
    fn list(&self, dir: &Self::Dir) -> Result<Vec<OsString>, FsError>;

    /// Metadata of `name` in `dir`, never following a link.
    fn stat(&self, dir: &Self::Dir, name: &OsStr) -> Result<Meta, FsError>;

    /// Open subdirectory `name`, refusing a link and anything not identical to `expect`
    /// or outside the root.
    fn open_dir(
        &self,
        parent: &Self::Dir,
        name: &OsStr,
        expect: &Meta,
    ) -> Result<Self::Dir, FsError>;

    /// Delete regular file `name` after re-verifying it is still `expect` and under the
    /// root. With `dry_run` every check runs and nothing is deleted.
    fn remove_file(
        &self,
        dir: &Self::Dir,
        name: &OsStr,
        expect: &Meta,
        dry_run: bool,
    ) -> Result<(), FsError>;

    /// Delete empty directory `name`, with the same checks as [`Ops::remove_file`].
    fn remove_dir(
        &self,
        dir: &Self::Dir,
        name: &OsStr,
        expect: &Meta,
        dry_run: bool,
    ) -> Result<(), FsError>;
}

/// A single entry name must never smuggle a path: separators, `.` and `..` are refused
/// before any primitive touches the filesystem.
pub(crate) fn is_plain_name(name: &OsStr) -> bool {
    let s = name.to_string_lossy();
    !(s.is_empty()
        || s == "."
        || s == ".."
        || s.contains('/')
        || (cfg!(windows) && s.contains('\\')))
}

/// Normalised comparison key for a path: verbatim prefix stripped and, on Windows,
/// separators unified and case folded.
pub(crate) fn path_key(p: &Path) -> String {
    let s = p.to_string_lossy();
    if cfg!(windows) {
        let s = if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
            format!(r"\\{rest}")
        } else {
            s.strip_prefix(r"\\?\").unwrap_or(&s).to_string()
        };
        s.replace('/', "\\").trim_end_matches('\\').to_lowercase()
    } else {
        s.trim_end_matches('/').to_string()
    }
}

/// Whether `key` is `base` or lies below it (on a component boundary).
pub(crate) fn key_under(key: &str, base: &str) -> bool {
    let sep = if cfg!(windows) { '\\' } else { '/' };
    key == base || (key.starts_with(base) && key[base.len()..].starts_with(sep))
}

// ---------------------------------------------------------------------------------
// Clock, budgets, session
// ---------------------------------------------------------------------------------

/// Time source: the wall clock ages are measured against, and the elapsed time the
/// budget is measured with. Injected so tests can age files and exhaust the budget.
pub trait Clock {
    fn now(&self) -> SystemTime;
    fn elapsed(&self) -> Duration;
}

/// The real clock, started when the call began.
pub struct RealClock {
    wall: SystemTime,
    start: Instant,
}

impl RealClock {
    pub fn start() -> Self {
        Self {
            wall: SystemTime::now(),
            start: Instant::now(),
        }
    }
}

impl Clock for RealClock {
    fn now(&self) -> SystemTime {
        self.wall
    }
    fn elapsed(&self) -> Duration {
        self.start.elapsed()
    }
}

/// Per-call work bounds.
#[derive(Debug, Clone, Copy)]
pub struct Budgets {
    pub time: Duration,
    pub max_examined: u64,
    pub max_deletions: u64,
}

impl Budgets {
    pub fn for_call(dry_run: bool) -> Self {
        Self {
            time: if dry_run { SCAN_BUDGET } else { CLEANUP_BUDGET },
            max_examined: MAX_EXAMINED,
            max_deletions: MAX_DELETIONS,
        }
    }
}

/// Why a call stopped before finishing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stop {
    Time,
    Count,
}

impl Stop {
    fn as_str(self) -> &'static str {
        match self {
            Stop::Time => "time",
            Stop::Count => "count",
        }
    }
}

/// Skipped-entry counters (`skipped` on the wire).
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Skipped {
    pub in_use: u64,
    pub denied: u64,
    pub reparse: u64,
    pub other: u64,
}

/// One `errors` entry. Messages never contain a file name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrEntry {
    pub code: &'static str,
    pub message: String,
}

/// The running state of one call: counters, budgets and exclusions.
pub struct Session<'c> {
    pub dry_run: bool,
    clock: &'c dyn Clock,
    budgets: Budgets,
    excluded: Vec<String>,
    pub files: u64,
    pub bytes: u64,
    pub examined: u64,
    pub deletions: u64,
    pub skipped: Skipped,
    pub errors: Vec<ErrEntry>,
    pub stopped: Option<Stop>,
}

impl<'c> Session<'c> {
    /// `excluded` are paths no candidate may be, or lie under (canonicalised here).
    pub fn new(
        dry_run: bool,
        clock: &'c dyn Clock,
        budgets: Budgets,
        excluded: &[PathBuf],
    ) -> Self {
        Self {
            dry_run,
            clock,
            budgets,
            excluded: excluded
                .iter()
                .map(|p| path_key(&canonical_or_self(p)))
                .collect(),
            files: 0,
            bytes: 0,
            examined: 0,
            deletions: 0,
            skipped: Skipped::default(),
            errors: Vec::new(),
            stopped: None,
        }
    }

    /// A read-only session with the same clock, budgets and exclusions — used to
    /// measure a directory before and after a command-driven cleanup.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn measuring(&self) -> Session<'c> {
        Session {
            dry_run: true,
            clock: self.clock,
            budgets: self.budgets,
            excluded: self.excluded.clone(),
            files: 0,
            bytes: 0,
            examined: 0,
            deletions: 0,
            skipped: Skipped::default(),
            errors: Vec::new(),
            stopped: None,
        }
    }

    /// Wall time left in this call's budget.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn time_left(&self) -> Duration {
        self.budgets.time.saturating_sub(self.clock.elapsed())
    }

    /// Account for examining one more entry; `false` once a budget is spent.
    fn tick(&mut self) -> bool {
        if self.stopped.is_some() {
            return false;
        }
        if self.clock.elapsed() >= self.budgets.time {
            self.stopped = Some(Stop::Time);
            return false;
        }
        if self.examined >= self.budgets.max_examined {
            self.stopped = Some(Stop::Count);
            return false;
        }
        self.examined += 1;
        true
    }

    /// Whether one more deletion fits the budget.
    fn may_delete(&mut self) -> bool {
        if self.stopped.is_some() {
            return false;
        }
        if self.deletions >= self.budgets.max_deletions {
            self.stopped = Some(Stop::Count);
            return false;
        }
        true
    }

    fn old_enough(&self, meta: &Meta, min_age: Duration) -> bool {
        if min_age.is_zero() {
            return true;
        }
        match self.clock.now().duration_since(meta.newest) {
            Ok(age) => age >= min_age,
            // A timestamp in the future is not old.
            Err(_) => false,
        }
    }

    fn is_excluded(&self, path: &Path) -> bool {
        let key = path_key(path);
        self.excluded.iter().any(|ex| key_under(&key, ex))
    }

    /// Record an error entry, deduplicated and capped at [`MAX_ERRORS`].
    pub fn error(&mut self, code: &'static str, message: impl Into<String>) {
        let message = message.into();
        if self.errors.len() >= MAX_ERRORS
            || self
                .errors
                .iter()
                .any(|e| e.code == code && e.message == message)
        {
            return;
        }
        self.errors.push(ErrEntry { code, message });
    }

    /// Count a refused primitive under the matching `skipped` bucket.
    fn skip(&mut self, e: FsError) {
        match e {
            FsError::NotFound | FsError::NotEmpty => {}
            FsError::InUse => self.skipped.in_use += 1,
            FsError::Denied => self.skipped.denied += 1,
            FsError::Reparse => self.skipped.reparse += 1,
            FsError::Escaped => {
                self.skipped.other += 1;
                self.error(
                    "containment",
                    "an entry resolved outside its cleanup root and was skipped",
                );
            }
            FsError::Changed | FsError::OtherDevice | FsError::Other => self.skipped.other += 1,
        }
    }

    /// `complete` on the wire.
    pub fn complete(&self) -> bool {
        self.stopped.is_none()
    }
}

/// Canonicalise `p`, or its parent joined with its file name, or leave it as is — so a
/// not-yet-existing excluded path still compares against canonical candidates.
fn canonical_or_self(p: &Path) -> PathBuf {
    if let Ok(c) = std::fs::canonicalize(p) {
        return c;
    }
    match (p.parent(), p.file_name()) {
        (Some(parent), Some(name)) => std::fs::canonicalize(parent)
            .map(|c| c.join(name))
            .unwrap_or_else(|_| p.to_path_buf()),
        _ => p.to_path_buf(),
    }
}

// ---------------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------------

/// What one root's walk removes.
#[derive(Clone, Copy)]
pub struct Rules {
    /// Files (and emptied directories) younger than this are kept.
    pub min_age: Duration,
    /// Deepest level a file may sit at (1 = directly in the root).
    pub max_depth: usize,
    /// Remove a subdirectory the walk emptied (only if it was older than `min_age`).
    pub remove_empty_dirs: bool,
    /// Which regular files are candidates, by name.
    pub want_file: fn(&str) -> bool,
    /// Entries never touched or descended into: `(name, depth of the listed dir, is_dir)`.
    pub skip_entry: fn(&str, usize, bool) -> bool,
}

pub const fn days(n: u32) -> Duration {
    Duration::from_secs(n as u64 * 86_400)
}

fn any_file(_: &str) -> bool {
    true
}

fn skip_nothing(_: &str, _: usize, _: bool) -> bool {
    false
}

/// Linux `tmp`: everything but the systemd/X11 sockets' homes.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // Linux targets; exercised by the tests everywhere
pub fn tmp_rules(min_age_days: u32) -> Rules {
    fn skip(name: &str, _depth: usize, _is_dir: bool) -> bool {
        name.starts_with("systemd-private-") || name == ".X11-unix" || name == ".ICE-unix"
    }
    Rules {
        min_age: days(min_age_days),
        max_depth: MAX_DEPTH,
        remove_empty_dirs: true,
        want_file: any_file,
        skip_entry: skip,
    }
}

/// Linux `logs_old`: rotated files under `/var/log` up to depth 3, never `journal/`.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // Linux targets; exercised by the tests everywhere
pub fn linux_logs_rules(min_age_days: u32) -> Rules {
    fn skip(name: &str, depth: usize, is_dir: bool) -> bool {
        depth == 0 && is_dir && name == "journal"
    }
    Rules {
        min_age: days(min_age_days),
        max_depth: 3,
        remove_empty_dirs: false,
        want_file: is_rotated_log,
        skip_entry: skip,
    }
}

/// `*.gz`, `*.xz`, `*.bz2`, `*.zst`, `*.[0-9]` or `*.old`.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // Linux targets; exercised by the tests everywhere
pub fn is_rotated_log(name: &str) -> bool {
    let Some((stem, ext)) = name.rsplit_once('.') else {
        return false;
    };
    if stem.is_empty() {
        return false;
    }
    matches!(ext, "gz" | "xz" | "bz2" | "zst" | "old")
        || (ext.len() == 1 && ext.as_bytes()[0].is_ascii_digit())
}

/// Windows temp directories and the update download cache: every file, emptied
/// subdirectories too.
#[cfg_attr(not(windows), allow(dead_code))] // Windows targets; exercised by the tests everywhere
pub fn windows_tree_rules(min_age_days: u32) -> Rules {
    Rules {
        min_age: days(min_age_days),
        max_depth: MAX_DEPTH,
        remove_empty_dirs: true,
        want_file: any_file,
        skip_entry: skip_nothing,
    }
}

/// Windows `logs_old`: `%WINDIR%\Logs\CBS\*.cab` and `CbsPersist_*.log`, top level only.
#[cfg_attr(not(windows), allow(dead_code))] // Windows targets; exercised by the tests everywhere
pub fn windows_cbs_rules(min_age_days: u32) -> Rules {
    fn want(name: &str) -> bool {
        let n = name.to_ascii_lowercase();
        n.ends_with(".cab") || (n.starts_with("cbspersist_") && n.ends_with(".log"))
    }
    Rules {
        min_age: days(min_age_days),
        max_depth: 1,
        remove_empty_dirs: false,
        want_file: want,
        skip_entry: skip_nothing,
    }
}

/// Everything below a directory, regardless of age (a recycle-bin `$R` folder, whose
/// age is its `$I` record's).
#[cfg_attr(not(windows), allow(dead_code))] // Windows targets; exercised by the tests everywhere
pub fn whole_tree_rules() -> Rules {
    Rules {
        min_age: Duration::ZERO,
        max_depth: MAX_DEPTH,
        remove_empty_dirs: true,
        want_file: any_file,
        skip_entry: skip_nothing,
    }
}

/// Every file except a package manager's `lock` (measuring a package cache).
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // Linux targets; exercised by the tests everywhere
pub fn package_cache_rules() -> Rules {
    fn want(name: &str) -> bool {
        name != "lock"
    }
    Rules {
        min_age: Duration::ZERO,
        max_depth: MAX_DEPTH,
        remove_empty_dirs: false,
        want_file: want,
        skip_entry: skip_nothing,
    }
}

/// Archived journal files (`*@*.journal`, `*.journal~`) older than `min_age_days`:
/// what `journalctl --vacuum-time` may remove, for the scan's estimate.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // Linux targets; exercised by the tests everywhere
pub fn journal_archive_rules(min_age_days: u32) -> Rules {
    fn want(name: &str) -> bool {
        (name.contains('@') && name.ends_with(".journal")) || name.ends_with(".journal~")
    }
    Rules {
        min_age: days(min_age_days),
        max_depth: MAX_DEPTH,
        remove_empty_dirs: false,
        want_file: want,
        skip_entry: skip_nothing,
    }
}

/// Every journal file (measuring the size delta of a vacuum).
#[cfg_attr(not(target_os = "linux"), allow(dead_code))] // Linux targets; exercised by the tests everywhere
pub fn journal_total_rules() -> Rules {
    Rules {
        min_age: Duration::ZERO,
        max_depth: MAX_DEPTH,
        remove_empty_dirs: false,
        want_file: any_file,
        skip_entry: skip_nothing,
    }
}

// ---------------------------------------------------------------------------------
// Walker
// ---------------------------------------------------------------------------------

/// Sweep one root: open it (refusing a link), then walk it under `rules`.
pub fn sweep_root<O: Ops>(ops: &O, s: &mut Session, base: &Path, chain: &[&str], rules: &Rules) {
    match ops.open_root(base, chain) {
        Ok((dir, canon)) => {
            if s.is_excluded(&canon) {
                return;
            }
            walk(ops, s, rules, &dir, &canon, 0);
        }
        // A root that does not exist has nothing to clean.
        Err(FsError::NotFound) => {}
        Err(FsError::Reparse) => {
            s.skipped.reparse += 1;
            s.error(
                "root_refused",
                "a cleanup root is a link or reparse point and was refused",
            );
        }
        Err(e) => {
            s.skip(e);
            s.error("root_unavailable", "a cleanup root could not be opened");
        }
    }
}

/// Walk `dir` (at `depth`, root = 0). Returns how many entries remain in it afterwards
/// (or would remain, in a dry run), so the caller knows whether it was emptied.
fn walk<O: Ops>(
    ops: &O,
    s: &mut Session,
    rules: &Rules,
    dir: &O::Dir,
    path: &Path,
    depth: usize,
) -> u64 {
    let names = match ops.list(dir) {
        Ok(n) => n,
        Err(e) => {
            s.skip(e);
            s.error("list_failed", "a directory could not be listed");
            return 1;
        }
    };
    let mut remaining = 0u64;
    for name in names {
        if !s.tick() {
            // Unvisited entries remain.
            remaining += 1;
            break;
        }
        if !is_plain_name(&name) {
            remaining += 1;
            continue;
        }
        let meta = match ops.stat(dir, &name) {
            Ok(m) => m,
            Err(FsError::NotFound) => continue,
            Err(e) => {
                s.skip(e);
                remaining += 1;
                continue;
            }
        };
        let name_str = name.to_string_lossy();
        let child_path = path.join(&name);
        if (rules.skip_entry)(&name_str, depth, meta.kind == Kind::Dir)
            || s.is_excluded(&child_path)
        {
            remaining += 1;
            continue;
        }
        match meta.kind {
            Kind::Link => {
                s.skipped.reparse += 1;
                remaining += 1;
            }
            Kind::Special => remaining += 1,
            Kind::File => {
                if !(rules.want_file)(&name_str) || !s.old_enough(&meta, rules.min_age) {
                    remaining += 1;
                    continue;
                }
                if !s.may_delete() {
                    remaining += 1;
                    break;
                }
                match ops.remove_file(dir, &name, &meta, s.dry_run) {
                    Ok(()) => {
                        s.files += 1;
                        s.bytes += meta.size;
                        s.deletions += 1;
                    }
                    Err(FsError::NotFound) => {}
                    Err(e) => {
                        s.skip(e);
                        remaining += 1;
                    }
                }
            }
            Kind::Dir => {
                // A subdirectory at depth+1 holds files at depth+2.
                if depth + 2 > rules.max_depth {
                    remaining += 1;
                    continue;
                }
                let left = match ops.open_dir(dir, &name, &meta) {
                    Ok(child) => {
                        let left = walk(ops, s, rules, &child, &child_path, depth + 1);
                        // Release the pin before deleting the directory itself.
                        drop(child);
                        left
                    }
                    Err(FsError::NotFound) => continue,
                    Err(e) => {
                        s.skip(e);
                        remaining += 1;
                        continue;
                    }
                };
                if left == 0
                    && rules.remove_empty_dirs
                    && s.old_enough(&meta, rules.min_age)
                    && s.may_delete()
                {
                    match ops.remove_dir(dir, &name, &meta, s.dry_run) {
                        Ok(()) => s.deletions += 1,
                        Err(FsError::NotFound) => {}
                        Err(e) => {
                            s.skip(e);
                            remaining += 1;
                        }
                    }
                } else {
                    remaining += 1;
                }
            }
        }
    }
    remaining
}

/// Sweep one volume's `$Recycle.Bin`: in each per-user folder, every `$I` record older
/// than `min_age` (by its own timestamps) goes together with its `$R` counterpart (a file,
/// or a folder removed through the same walker). The `$R` part goes first, so a failure
/// leaves the entry visible in the bin rather than an orphaned payload.
#[cfg_attr(not(windows), allow(dead_code))] // Windows targets; exercised by the tests everywhere
pub fn sweep_recycle_bin<O: Ops>(ops: &O, s: &mut Session, bin: &Path, min_age: Duration) {
    let (root, canon) = match ops.open_root(bin, &[]) {
        Ok(r) => r,
        Err(FsError::NotFound) => return,
        Err(FsError::Reparse) => {
            s.skipped.reparse += 1;
            s.error(
                "root_refused",
                "a cleanup root is a link or reparse point and was refused",
            );
            return;
        }
        Err(e) => {
            s.skip(e);
            s.error("root_unavailable", "a cleanup root could not be opened");
            return;
        }
    };
    let users = match ops.list(&root) {
        Ok(n) => n,
        Err(e) => {
            s.skip(e);
            s.error("list_failed", "a directory could not be listed");
            return;
        }
    };
    for user in users {
        if !s.tick() {
            return;
        }
        if !is_plain_name(&user) {
            continue;
        }
        let meta = match ops.stat(&root, &user) {
            Ok(m) => m,
            Err(FsError::NotFound) => continue,
            Err(e) => {
                s.skip(e);
                continue;
            }
        };
        let user_path = canon.join(&user);
        match meta.kind {
            Kind::Link => s.skipped.reparse += 1,
            Kind::Dir if !s.is_excluded(&user_path) => match ops.open_dir(&root, &user, &meta) {
                Ok(dir) => sweep_recycle_user(ops, s, &dir, &user_path, min_age),
                Err(FsError::NotFound) => {}
                Err(e) => s.skip(e),
            },
            _ => {}
        }
        if s.stopped.is_some() {
            return;
        }
    }
}

#[cfg_attr(not(windows), allow(dead_code))] // Windows targets; exercised by the tests everywhere
fn sweep_recycle_user<O: Ops>(
    ops: &O,
    s: &mut Session,
    dir: &O::Dir,
    path: &Path,
    min_age: Duration,
) {
    let names = match ops.list(dir) {
        Ok(n) => n,
        Err(e) => {
            s.skip(e);
            s.error("list_failed", "a directory could not be listed");
            return;
        }
    };
    for name in names {
        let n = name.to_string_lossy().into_owned();
        // Only `$I` records drive removal; `$R` payloads follow their record.
        if n.len() < 3 || !n.is_char_boundary(2) || !n[..2].eq_ignore_ascii_case("$I") {
            continue;
        }
        if !s.tick() {
            return;
        }
        if !is_plain_name(&name) {
            continue;
        }
        let info = match ops.stat(dir, &name) {
            Ok(m) => m,
            Err(FsError::NotFound) => continue,
            Err(e) => {
                s.skip(e);
                continue;
            }
        };
        if info.kind == Kind::Link {
            s.skipped.reparse += 1;
            continue;
        }
        if info.kind != Kind::File
            || !s.old_enough(&info, min_age)
            || s.is_excluded(&path.join(&name))
        {
            continue;
        }
        let payload = OsString::from(format!("$R{}", &n[2..]));
        if !s.tick() {
            return;
        }
        // Remove the payload; `true` once it is gone (or never existed).
        let payload_gone = match ops.stat(dir, &payload) {
            Err(FsError::NotFound) => true,
            Err(e) => {
                s.skip(e);
                false
            }
            Ok(m) => match m.kind {
                Kind::Link => {
                    s.skipped.reparse += 1;
                    false
                }
                Kind::Special => false,
                Kind::File => {
                    if !s.may_delete() {
                        return;
                    }
                    match ops.remove_file(dir, &payload, &m, s.dry_run) {
                        Ok(()) => {
                            s.files += 1;
                            s.bytes += m.size;
                            s.deletions += 1;
                            true
                        }
                        Err(FsError::NotFound) => true,
                        Err(e) => {
                            s.skip(e);
                            false
                        }
                    }
                }
                Kind::Dir => {
                    let left = match ops.open_dir(dir, &payload, &m) {
                        Ok(child) => {
                            let left =
                                walk(ops, s, &whole_tree_rules(), &child, &path.join(&payload), 0);
                            drop(child);
                            left
                        }
                        Err(FsError::NotFound) => 0,
                        Err(e) => {
                            s.skip(e);
                            1
                        }
                    };
                    if left == 0 && s.may_delete() {
                        match ops.remove_dir(dir, &payload, &m, s.dry_run) {
                            Ok(()) => {
                                s.deletions += 1;
                                true
                            }
                            Err(FsError::NotFound) => true,
                            Err(e) => {
                                s.skip(e);
                                false
                            }
                        }
                    } else {
                        false
                    }
                }
            },
        };
        if !payload_gone || !s.may_delete() {
            if s.stopped.is_some() {
                return;
            }
            continue;
        }
        match ops.remove_file(dir, &name, &info, s.dry_run) {
            Ok(()) => {
                s.files += 1;
                s.bytes += info.size;
                s.deletions += 1;
            }
            Err(FsError::NotFound) => {}
            Err(e) => s.skip(e),
        }
    }
}

// ---------------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------------

fn skipped_json(s: &Session) -> Value {
    json!({
        "in_use": s.skipped.in_use,
        "denied": s.skipped.denied,
        "reparse": s.skipped.reparse,
        "other": s.skipped.other,
    })
}

fn errors_json(s: &Session) -> Value {
    Value::Array(
        s.errors
            .iter()
            .take(MAX_ERRORS)
            .map(|e| json!({ "code": e.code, "message": e.message }))
            .collect(),
    )
}

/// The `fs_cleanup_scan` result.
pub fn scan_result(entry: &CatalogEntry, s: &Session) -> Value {
    json!({
        "target": entry.target,
        "files": s.files,
        "bytes": s.bytes,
        "min_age_days": entry.min_age_days,
        "complete": s.complete(),
        "skipped": skipped_json(s),
        "errors": errors_json(s),
    })
}

/// The `fs_cleanup` result; `stopped` only when the call did not complete.
pub fn cleanup_result(entry: &CatalogEntry, s: &Session) -> Value {
    let mut v = json!({
        "target": entry.target,
        "files_removed": s.files,
        "bytes_freed": s.bytes,
        "complete": s.complete(),
        "skipped": skipped_json(s),
        "errors": errors_json(s),
    });
    if let Some(stop) = s.stopped {
        v["stopped"] = json!(stop.as_str());
    }
    v
}

// ---------------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------------

/// Paths no cleanup may touch: the agent's log directory, its state files and its own
/// binary. On Linux dev/CI fallbacks these can sit under `/tmp`, hence the explicit check.
fn agent_excluded_paths() -> Vec<PathBuf> {
    let mut v = Vec::new();
    if let Some(d) = crate::log_dir() {
        v.push(d);
    }
    v.push(crate::control::control_path());
    v.push(crate::control::state_dir().join(crate::control::CONTROL_FILE));
    v.push(crate::policy::shell_policy_path());
    v.push(crate::keys::key_path());
    if let Ok(exe) = std::env::current_exe() {
        v.push(exe);
    }
    #[cfg(windows)]
    {
        let program_data =
            std::env::var_os("ProgramData").unwrap_or_else(|| r"C:\ProgramData".into());
        v.push(PathBuf::from(program_data).join("kenny"));
    }
    v
}

/// `fs_cleanup_scan` — report what `fs_cleanup` would remove (read-only).
pub async fn scan(args: Value) -> Result<Value, (ErrorCode, String)> {
    run(args, true).await
}

/// `fs_cleanup` — remove it (mutating, irreversible).
pub async fn cleanup(args: Value) -> Result<Value, (ErrorCode, String)> {
    run(args, false).await
}

async fn run(args: Value, dry_run: bool) -> Result<Value, (ErrorCode, String)> {
    let entry = lookup(CatalogOs::native(), &args)?;
    tokio::task::spawn_blocking(move || {
        let _host = try_host_lock()?;
        let clock = RealClock::start();
        let mut s = Session::new(
            dry_run,
            &clock,
            Budgets::for_call(dry_run),
            &agent_excluded_paths(),
        );
        execute(&entry, &mut s)?;
        Ok(if dry_run {
            scan_result(&entry, &s)
        } else {
            cleanup_result(&entry, &s)
        })
    })
    .await
    .map_err(|_| (ErrorCode::Internal, "cleanup task failed".to_string()))?
}

#[cfg(target_os = "linux")]
fn execute(entry: &CatalogEntry, s: &mut Session) -> Result<(), (ErrorCode, String)> {
    linux::execute(entry, s)
}

#[cfg(windows)]
fn execute(entry: &CatalogEntry, s: &mut Session) -> Result<(), (ErrorCode, String)> {
    windows::execute(entry, s)
}

#[cfg(not(any(windows, target_os = "linux")))]
fn execute(_entry: &CatalogEntry, _s: &mut Session) -> Result<(), (ErrorCode, String)> {
    Err((
        ErrorCode::Unsupported,
        "disk cleanup is not supported on this platform".to_string(),
    ))
}

/// The primitives of this platform, for the core tests.
#[cfg(all(test, target_os = "linux"))]
pub(crate) fn platform_ops() -> linux::FdOps {
    linux::FdOps
}

#[cfg(all(test, windows))]
pub(crate) fn platform_ops() -> windows::WinOps {
    windows::WinOps
}

#[cfg(test)]
mod tests;

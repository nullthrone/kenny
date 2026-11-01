//! Windows cleanup: handle-pinned primitives and the Windows targets.
//!
//! Every directory on the walk is held open with `FILE_FLAG_BACKUP_SEMANTICS |
//! FILE_FLAG_OPEN_REPARSE_POINT` and **without** `FILE_SHARE_DELETE`, so neither it nor
//! (being held all the way down from the root) any ancestor can be renamed or replaced
//! while the walk is below it. Every object is opened with `FILE_FLAG_OPEN_REPARSE_POINT`
//! (a junction or symlink is opened as itself, never followed), its attributes are
//! checked for `FILE_ATTRIBUTE_REPARSE_POINT`, and its final path
//! (`GetFinalPathNameByHandleW`) must lie under the root's. A file is deleted through
//! the very handle that passed those checks (`DELETE` access, `FileDispositionInfo`),
//! opened with no sharing so a file another process holds open is skipped as in use.
//!
//! Subdirectories and deletion candidates are opened with `NtCreateFile` relative to the
//! pinned parent handle and with `FILE_COMPLETE_IF_OPLOCKED`: an open that would have to
//! wait for another process to acknowledge an oplock break returns at once
//! (`STATUS_OPLOCK_BREAK_IN_PROGRESS`), and the entry is skipped as in use. A local user
//! holding an oplock on an old file therefore cannot stall the walk. A dry run opens a
//! candidate for its attributes only, with full sharing, so a scan has no side effect on
//! other processes' handles.

use std::ffi::{OsStr, OsString};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use windows::core::{PCWSTR, PWSTR};
use windows::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows::Wdk::Storage::FileSystem::{
    NtCreateFile, FILE_COMPLETE_IF_OPLOCKED, FILE_DIRECTORY_FILE, FILE_OPEN,
    FILE_OPEN_FOR_BACKUP_INTENT, FILE_OPEN_REPARSE_POINT, FILE_SYNCHRONOUS_IO_NONALERT,
    NTCREATEFILE_CREATE_OPTIONS,
};
use windows::Win32::Foundation::{
    CloseHandle, FILETIME, HANDLE, NTSTATUS, OBJ_CASE_INSENSITIVE, STATUS_OPLOCK_BREAK_IN_PROGRESS,
    UNICODE_STRING,
};
use windows::Win32::Storage::FileSystem::{
    CreateFileW, FileDispositionInfo, GetDriveTypeW, GetFileInformationByHandle,
    GetFinalPathNameByHandleW, GetLogicalDrives, SetFileInformationByHandle,
    BY_HANDLE_FILE_INFORMATION, DELETE, FILE_ACCESS_RIGHTS, FILE_DISPOSITION_INFO,
    FILE_FLAGS_AND_ATTRIBUTES, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_LIST_DIRECTORY, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_MODE, FILE_SHARE_READ,
    FILE_SHARE_WRITE, GETFINALPATHNAMEBYHANDLE_FLAGS, OPEN_EXISTING, SYNCHRONIZE,
};
use windows::Win32::System::IO::IO_STATUS_BLOCK;

use super::{
    days, key_under, path_key, sweep_recycle_bin, sweep_root, windows_cbs_rules,
    windows_tree_rules, Cancel, CatalogEntry, FsError, Kind, Meta, Ops, Session, LIST_CAP,
};
use crate::protocol::ErrorCode;

const ATTR_DIRECTORY: u32 = 0x10;
const ATTR_DEVICE: u32 = 0x40;
const ATTR_REPARSE_POINT: u32 = 0x400;

/// An owned Win32 handle, closed once on drop.
struct Handle(HANDLE);

impl Drop for Handle {
    fn drop(&mut self) {
        // SAFETY: a handle `CreateFileW` returned, closed exactly once here.
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

/// An open, pinned directory, with every ancestor up to its root's base held too.
pub struct WinDir {
    handle: Handle,
    path: PathBuf,
    root: Arc<String>,
    _ancestors: Vec<Handle>,
}

/// The handle-pinned primitives.
pub struct WinOps;

fn wide(p: &Path) -> Vec<u16> {
    p.as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

/// The Win32 error behind a `windows` error (an `HRESULT_FROM_WIN32` value).
fn win32_code(e: &windows::core::Error) -> u32 {
    let hr = e.code().0 as u32;
    if hr >> 16 == 0x8007 {
        hr & 0xFFFF
    } else {
        hr
    }
}

fn map_code(code: u32) -> FsError {
    match code {
        2 | 3 => FsError::NotFound,
        5 => FsError::Denied,
        32 | 33 => FsError::InUse,
        145 => FsError::NotEmpty,
        _ => FsError::Other,
    }
}

fn map_win(e: &windows::core::Error) -> FsError {
    map_code(win32_code(e))
}

fn map_io(e: &std::io::Error) -> FsError {
    e.raw_os_error()
        .map(|c| map_code(c as u32))
        .unwrap_or(FsError::Other)
}

const DIR_ACCESS: u32 = FILE_LIST_DIRECTORY.0 | FILE_READ_ATTRIBUTES.0 | SYNCHRONIZE.0;

/// The `NTSTATUS` failures an open or delete maps to a skip bucket.
fn map_nt(status: NTSTATUS) -> FsError {
    match status.0 as u32 {
        // OBJECT_NAME_NOT_FOUND, OBJECT_PATH_NOT_FOUND, DELETE_PENDING
        0xC000_0034 | 0xC000_003A | 0xC000_0056 => FsError::NotFound,
        // ACCESS_DENIED, CANNOT_DELETE
        0xC000_0022 | 0xC000_0121 => FsError::Denied,
        // SHARING_VIOLATION, FILE_LOCK_CONFLICT
        0xC000_0043 | 0xC000_0054 => FsError::InUse,
        // NOT_A_DIRECTORY, FILE_IS_A_DIRECTORY
        0xC000_0103 | 0xC000_00BA => FsError::Changed,
        // STOPPED_ON_SYMLINK
        0x8000_002D => FsError::Reparse,
        _ => FsError::Other,
    }
}

/// Open entry `name` of the pinned directory `parent` itself (never a reparse target),
/// relative to its handle, so no path is resolved again. `FILE_COMPLETE_IF_OPLOCKED`
/// makes an open that would wait for an oplock break return at once; that entry is
/// skipped as in use (the handle is closed unused).
fn open_relative(
    parent: &Handle,
    name: &OsStr,
    access: u32,
    share: FILE_SHARE_MODE,
    extra: NTCREATEFILE_CREATE_OPTIONS,
) -> Result<Handle, FsError> {
    if !super::is_plain_name(name) {
        return Err(FsError::Other);
    }
    let mut w: Vec<u16> = name.encode_wide().collect();
    let bytes = u16::try_from(w.len() * 2).map_err(|_| FsError::Other)?;
    let object_name = UNICODE_STRING {
        Length: bytes,
        MaximumLength: bytes,
        Buffer: PWSTR(w.as_mut_ptr()),
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.0,
        ObjectName: &object_name,
        Attributes: OBJ_CASE_INSENSITIVE,
        ..Default::default()
    };
    let mut handle = HANDLE::default();
    let mut io = IO_STATUS_BLOCK::default();
    // SAFETY: every pointer refers to a local that outlives the call; `w` backs the name.
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            FILE_ACCESS_RIGHTS(access),
            &attributes,
            &mut io,
            None,
            FILE_FLAGS_AND_ATTRIBUTES(0),
            share,
            FILE_OPEN,
            FILE_OPEN_REPARSE_POINT
                | FILE_OPEN_FOR_BACKUP_INTENT
                | FILE_SYNCHRONOUS_IO_NONALERT
                | FILE_COMPLETE_IF_OPLOCKED
                | extra,
            None,
            0,
        )
    };
    if status == STATUS_OPLOCK_BREAK_IN_PROGRESS {
        drop(Handle(handle));
        return Err(FsError::InUse);
    }
    if status.is_err() {
        return Err(map_nt(status));
    }
    Ok(Handle(handle))
}

/// Open `path` itself (never a reparse target) with `access` and `share`.
fn open(path: &Path, access: u32, share: FILE_SHARE_MODE) -> Result<Handle, FsError> {
    let w = wide(path);
    // SAFETY: `w` is a NUL-terminated wide string that outlives the call.
    unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            access,
            share,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    }
    .map(Handle)
    .map_err(|e| map_win(&e))
}

fn info(h: &Handle) -> Result<BY_HANDLE_FILE_INFORMATION, FsError> {
    let mut i = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: `h` is open; `i` is a valid out-pointer.
    unsafe { GetFileInformationByHandle(h.0, &mut i) }.map_err(|e| map_win(&e))?;
    Ok(i)
}

fn filetime(ft: FILETIME) -> SystemTime {
    // 100 ns intervals since 1601-01-01.
    const UNIX_OFFSET: u64 = 116_444_736_000_000_000;
    let t = ((ft.dwHighDateTime as u64) << 32) | ft.dwLowDateTime as u64;
    match t.checked_sub(UNIX_OFFSET) {
        Some(since) => UNIX_EPOCH + Duration::from_nanos(since.saturating_mul(100)),
        None => UNIX_EPOCH,
    }
}

/// Age counts from the newer of last-write and creation time (an extracted or copied
/// file carries an old write time but a fresh creation time).
fn meta_of(i: &BY_HANDLE_FILE_INFORMATION) -> Meta {
    let a = i.dwFileAttributes;
    let kind = if a & ATTR_REPARSE_POINT != 0 {
        Kind::Link
    } else if a & ATTR_DIRECTORY != 0 {
        Kind::Dir
    } else if a & ATTR_DEVICE != 0 {
        Kind::Special
    } else {
        Kind::File
    };
    Meta {
        kind,
        size: ((i.nFileSizeHigh as u64) << 32) | i.nFileSizeLow as u64,
        newest: filetime(i.ftLastWriteTime).max(filetime(i.ftCreationTime)),
        id: (
            i.dwVolumeSerialNumber as u64,
            ((i.nFileIndexHigh as u64) << 32) | i.nFileIndexLow as u64,
        ),
        owner: None,
    }
}

/// The object's real path as the filesystem resolves it.
fn final_path(h: &Handle) -> Result<PathBuf, FsError> {
    let mut buf = vec![0u16; 512];
    loop {
        // SAFETY: `h` is open; the buffer length is passed with the slice.
        let n =
            unsafe { GetFinalPathNameByHandleW(h.0, &mut buf, GETFINALPATHNAMEBYHANDLE_FLAGS(0)) }
                as usize;
        if n == 0 {
            return Err(FsError::Other);
        }
        if n < buf.len() {
            buf.truncate(n);
            return Ok(PathBuf::from(OsString::from_wide(&buf)));
        }
        if n > 32_768 {
            return Err(FsError::Other);
        }
        buf.resize(n + 1, 0);
    }
}

fn require_under(h: &Handle, root: &str) -> Result<PathBuf, FsError> {
    let p = final_path(h)?;
    if key_under(&path_key(&p), root) {
        Ok(p)
    } else {
        Err(FsError::Escaped)
    }
}

impl WinOps {
    /// Open `name`, verify it is still `expect` of `kind` under the root and, when
    /// committing, mark it for deletion through that handle. Committing opens with
    /// `DELETE` and no sharing (a file another process holds open is in use); a dry run
    /// opens for attributes only, with full sharing.
    fn delete(
        &self,
        dir: &WinDir,
        name: &OsStr,
        expect: &Meta,
        kind: Kind,
        commit: Option<&Cancel>,
    ) -> Result<(), FsError> {
        let (access, share) = if commit.is_some() {
            (
                DELETE.0 | FILE_READ_ATTRIBUTES.0 | SYNCHRONIZE.0,
                FILE_SHARE_MODE(0),
            )
        } else {
            (
                FILE_READ_ATTRIBUTES.0 | SYNCHRONIZE.0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            )
        };
        let h = open_relative(
            &dir.handle,
            name,
            access,
            share,
            NTCREATEFILE_CREATE_OPTIONS(0),
        )?;
        let m = meta_of(&info(&h)?);
        if m.kind == Kind::Link {
            return Err(FsError::Reparse);
        }
        if m.kind != kind || m.id != expect.id {
            return Err(FsError::Changed);
        }
        require_under(&h, &dir.root)?;
        let Some(cancel) = commit else {
            return Ok(());
        };
        let Some(permit) = cancel.permit() else {
            return Err(FsError::Cancelled);
        };
        let disposition = FILE_DISPOSITION_INFO { DeleteFile: true };
        // SAFETY: `h` was opened with DELETE; the buffer is a FILE_DISPOSITION_INFO.
        let marked = unsafe {
            SetFileInformationByHandle(
                h.0,
                FileDispositionInfo,
                &disposition as *const FILE_DISPOSITION_INFO as *const core::ffi::c_void,
                std::mem::size_of::<FILE_DISPOSITION_INFO>() as u32,
            )
        }
        .map_err(|e| map_win(&e));
        // The object goes away when `h` closes, still under the permit.
        drop(h);
        drop(permit);
        marked
    }
}

impl Ops for WinOps {
    type Dir = WinDir;

    fn open_root(&self, base: &Path, chain: &[&str]) -> Result<(WinDir, PathBuf), FsError> {
        let md = std::fs::symlink_metadata(base).map_err(|e| map_io(&e))?;
        if md.file_type().is_symlink() || md.file_attributes() & ATTR_REPARSE_POINT != 0 {
            return Err(FsError::Reparse);
        }
        let handle = open(base, DIR_ACCESS, FILE_SHARE_READ | FILE_SHARE_WRITE)?;
        let m = meta_of(&info(&handle)?);
        match m.kind {
            Kind::Link => return Err(FsError::Reparse),
            Kind::Dir => {}
            _ => return Err(FsError::Other),
        }
        let path = final_path(&handle)?;
        let mut dir = WinDir {
            handle,
            root: Arc::new(path_key(&path)),
            path,
            _ancestors: Vec::new(),
        };
        for comp in chain {
            let name = OsStr::new(comp);
            let m = self.stat(&dir, name)?;
            match m.kind {
                Kind::Link => return Err(FsError::Reparse),
                Kind::Dir => {}
                _ => return Err(FsError::Other),
            }
            let mut child = self.open_dir(&dir, name, &m)?;
            let WinDir {
                handle,
                mut _ancestors,
                ..
            } = dir;
            _ancestors.push(handle);
            child._ancestors = _ancestors;
            dir = child;
        }
        dir.root = Arc::new(path_key(&dir.path));
        let path = dir.path.clone();
        Ok((dir, path))
    }

    fn list(&self, dir: &WinDir) -> Result<Vec<OsString>, FsError> {
        // The directory and every ancestor are pinned, so its path resolves to it; each
        // name is then re-opened and verified on its own.
        let rd = std::fs::read_dir(&dir.path).map_err(|e| map_io(&e))?;
        let mut names = Vec::new();
        for e in rd {
            let e = e.map_err(|e| map_io(&e))?;
            names.push(e.file_name());
            if names.len() >= LIST_CAP {
                break;
            }
        }
        Ok(names)
    }

    fn stat(&self, dir: &WinDir, name: &OsStr) -> Result<Meta, FsError> {
        if !super::is_plain_name(name) {
            return Err(FsError::Other);
        }
        let h = open(
            &dir.path.join(name),
            FILE_READ_ATTRIBUTES.0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        )?;
        Ok(meta_of(&info(&h)?))
    }

    fn open_dir(&self, parent: &WinDir, name: &OsStr, expect: &Meta) -> Result<WinDir, FsError> {
        let handle = open_relative(
            &parent.handle,
            name,
            DIR_ACCESS,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_DIRECTORY_FILE,
        )?;
        let m = meta_of(&info(&handle)?);
        if m.kind == Kind::Link {
            return Err(FsError::Reparse);
        }
        if m.kind != Kind::Dir || m.id != expect.id {
            return Err(FsError::Changed);
        }
        let path = require_under(&handle, &parent.root)?;
        Ok(WinDir {
            handle,
            path,
            root: parent.root.clone(),
            _ancestors: Vec::new(),
        })
    }

    fn remove_file(
        &self,
        dir: &WinDir,
        name: &OsStr,
        expect: &Meta,
        commit: Option<&Cancel>,
    ) -> Result<(), FsError> {
        self.delete(dir, name, expect, Kind::File, commit)
    }

    fn remove_dir(
        &self,
        dir: &WinDir,
        name: &OsStr,
        expect: &Meta,
        commit: Option<&Cancel>,
    ) -> Result<(), FsError> {
        self.delete(dir, name, expect, Kind::Dir, commit)
    }
}

// ---------------------------------------------------------------------------------
// Roots
// ---------------------------------------------------------------------------------

/// `%SystemRoot%` from the service environment (set by the SCM from the machine
/// environment, which only administrators can change), never from a request.
fn windir() -> PathBuf {
    std::env::var_os("SystemRoot")
        .or_else(|| std::env::var_os("windir"))
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| PathBuf::from(r"C:\Windows"))
}

/// The machine's profiles directory (`ProfileList\ProfilesDirectory`, expanded), else
/// `%SystemDrive%\Users`.
fn profiles_dir() -> PathBuf {
    use windows::core::w;
    use windows::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_SZ};
    let mut buf = vec![0u16; 1024];
    let mut len = (buf.len() * 2) as u32;
    // SAFETY: buffer and its byte length are passed together; the value is a string.
    let rc = unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            w!(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"),
            w!("ProfilesDirectory"),
            RRF_RT_REG_SZ,
            None,
            Some(buf.as_mut_ptr() as *mut core::ffi::c_void),
            Some(&mut len),
        )
    };
    if rc.0 == 0 {
        let chars = (len as usize / 2).min(buf.len());
        let s = OsString::from_wide(&buf[..chars]);
        let s = s.to_string_lossy().trim_end_matches('\0').to_string();
        let p = PathBuf::from(s);
        if p.is_absolute() {
            return p;
        }
    }
    let drive = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".to_string());
    PathBuf::from(format!(r"{drive}\Users"))
}

/// Local profiles to clean: not `Public`/`Default`/`Default User`/`All Users`, and not a
/// reparse point (the walk re-verifies every level from the profile down).
fn user_profiles() -> Vec<PathBuf> {
    const SKIP: &[&str] = &["public", "default", "default user", "all users"];
    let Ok(rd) = std::fs::read_dir(profiles_dir()) else {
        return Vec::new();
    };
    rd.filter_map(Result::ok)
        .filter(|e| !SKIP.contains(&e.file_name().to_string_lossy().to_lowercase().as_str()))
        .filter(|e| {
            std::fs::symlink_metadata(e.path()).is_ok_and(|m| {
                m.is_dir()
                    && !m.file_type().is_symlink()
                    && m.file_attributes() & ATTR_REPARSE_POINT == 0
            })
        })
        .map(|e| e.path())
        .collect()
}

/// Root of every fixed local volume (`C:\`, `D:\`, …).
fn fixed_volumes() -> Vec<PathBuf> {
    const DRIVE_FIXED: u32 = 3;
    // SAFETY: no arguments.
    let mask = unsafe { GetLogicalDrives() };
    (0..26u8)
        .filter(|i| mask & (1 << i) != 0)
        .map(|i| PathBuf::from(format!("{}:\\", (b'A' + i) as char)))
        .filter(|root| {
            let w = wide(root);
            // SAFETY: NUL-terminated wide string.
            let kind = unsafe { GetDriveTypeW(PCWSTR(w.as_ptr())) };
            kind == DRIVE_FIXED
        })
        .collect()
}

// ---------------------------------------------------------------------------------
// Windows Update cache
// ---------------------------------------------------------------------------------

/// Services stopped around the update-cache cleanup, in stop order.
const WU_SERVICES: &[&str] = &["wuauserv", "bits"];

/// Wait for a service to stop (each).
const WU_STOP_WAIT: Duration = Duration::from_secs(20);

/// Wait for a service to come back (each).
const WU_START_WAIT: Duration = Duration::from_secs(20);

/// Time set aside at the end of the call's budget to bring every stopped service back.
const WU_RESTORE_RESERVE: Duration =
    Duration::from_secs(WU_START_WAIT.as_secs() * WU_SERVICES.len() as u64);

const POLL: Duration = Duration::from_millis(250);

fn wait_for(
    svc: &windows_service::service::Service,
    want: windows_service::service::ServiceState,
    max: Duration,
) -> bool {
    let deadline = Instant::now() + max;
    loop {
        match svc.query_status() {
            Ok(st) if st.current_state == want => return true,
            Ok(_) => {}
            Err(_) => return false,
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(POLL);
    }
}

/// Bring a service this call stopped back to running within `max`: wait out a stop
/// still in progress, then start it once it is stopped.
fn restore_running(svc: &windows_service::service::Service, max: Duration) -> bool {
    use windows_service::service::ServiceState;
    let deadline = Instant::now() + max;
    let mut start_accepted = false;
    loop {
        match svc.query_status() {
            Ok(st) => match st.current_state {
                ServiceState::Running => return true,
                ServiceState::Stopped if !start_accepted => {
                    start_accepted = svc.start(&[] as &[&OsStr]).is_ok();
                }
                _ => {}
            },
            Err(_) => return false,
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(POLL);
    }
}

/// Restarts every service this call stopped (each was `Running` before) when dropped —
/// on success, error, timeout or abandonment alike.
struct RestoreServices {
    stopped: Vec<(&'static str, windows_service::service::Service)>,
    // Dropped after the services (fields drop in declaration order).
    _manager: windows_service::service_manager::ServiceManager,
}

impl Drop for RestoreServices {
    fn drop(&mut self) {
        for (name, svc) in self.stopped.iter().rev() {
            if !restore_running(svc, WU_START_WAIT) {
                tracing::warn!(service = %name, "service did not return to running after cleanup");
            }
        }
    }
}

fn raw_os_error(e: &windows_service::Error) -> Option<i32> {
    match e {
        windows_service::Error::Winapi(io) => io.raw_os_error(),
        _ => None,
    }
}

/// Whether Windows servicing is installing something right now.
fn servicing_running() -> bool {
    use sysinfo::{ProcessRefreshKind, ProcessesToUpdate, System};
    let mut sys = System::new();
    sys.refresh_processes_specifics(ProcessesToUpdate::All, true, ProcessRefreshKind::nothing());
    sys.processes().values().any(|p| {
        p.name()
            .to_string_lossy()
            .eq_ignore_ascii_case("TiWorker.exe")
    })
}

/// Whether a reboot is pending (CBS or Windows Update). A key that cannot be checked for
/// any reason but absence counts as pending: refusing is the safe answer.
fn reboot_pending() -> bool {
    use windows::core::w;
    use windows::Win32::System::Registry::{
        RegCloseKey, RegOpenKeyExW, HKEY, HKEY_LOCAL_MACHINE, KEY_READ,
    };
    let keys = [
        w!(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"),
        w!(r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"),
    ];
    keys.into_iter().any(|key| {
        let mut hk = HKEY::default();
        // SAFETY: `hk` is a valid out-pointer; closed below when opened.
        let rc = unsafe { RegOpenKeyExW(HKEY_LOCAL_MACHINE, key, None, KEY_READ, &mut hk) };
        if rc.0 == 0 {
            // SAFETY: opened above.
            unsafe {
                let _ = RegCloseKey(hk);
            }
            true
        } else {
            rc.0 != 2
        }
    })
}

/// Run `f` with `wuauserv` and `bits` stopped, restoring each to its prior state after:
/// a service that was `Running` is stopped and started again; one that was `Stopped` is
/// left alone; one in any other state (starting, stopping, paused) refuses the call.
/// Refused while servicing runs or a reboot is pending. A scan only checks the refusals:
/// it never stops a service. If a stop does not complete in time nothing is deleted, and
/// the restore still runs. The walk leaves [`WU_RESTORE_RESERVE`] of the budget for the
/// restore.
fn with_update_services_stopped(
    s: &mut Session,
    f: impl FnOnce(&mut Session),
) -> Result<(), (ErrorCode, String)> {
    use windows_service::service::{ServiceAccess, ServiceState};
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

    if servicing_running() {
        return Err((
            ErrorCode::ExecFailed,
            "Windows servicing (TiWorker.exe) is installing updates; try again later".to_string(),
        ));
    }
    if reboot_pending() {
        return Err((
            ErrorCode::ExecFailed,
            "a reboot is pending; restart the host before cleaning the update cache".to_string(),
        ));
    }
    if s.dry_run {
        f(s);
        return Ok(());
    }
    s.reserve(WU_RESTORE_RESERVE);

    let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)
        .map_err(|_| {
            (
                ErrorCode::ExecFailed,
                "cannot connect to the service control manager".to_string(),
            )
        })?;
    let mut guard = RestoreServices {
        stopped: Vec::new(),
        _manager: manager,
    };
    for name in WU_SERVICES {
        let svc = match guard._manager.open_service(
            name,
            ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::START,
        ) {
            Ok(svc) => svc,
            // ERROR_SERVICE_DOES_NOT_EXIST: nothing to stop.
            Err(e) if raw_os_error(&e) == Some(1060) => continue,
            Err(_) => {
                return Err((
                    ErrorCode::ExecFailed,
                    format!("cannot open the {name} service"),
                ))
            }
        };
        let state = svc.query_status().map(|s| s.current_state).map_err(|_| {
            (
                ErrorCode::ExecFailed,
                format!("cannot query the {name} service"),
            )
        })?;
        match state {
            ServiceState::Stopped => continue,
            ServiceState::Running => {}
            _ => {
                return Err((
                    ErrorCode::ExecFailed,
                    format!("the {name} service is changing state; try again later"),
                ))
            }
        }
        let Some(permit) = s.cancel().permit() else {
            return Err((ErrorCode::Timeout, "the cleanup was abandoned".to_string()));
        };
        match svc.stop() {
            Ok(_) => {}
            // ERROR_SERVICE_NOT_ACTIVE: it stopped on its own; not this call's to restore.
            Err(e) if raw_os_error(&e) == Some(1062) => continue,
            // ERROR_DEPENDENT_SERVICES_RUNNING: never force dependents.
            Err(e) if raw_os_error(&e) == Some(1051) => {
                return Err((
                    ErrorCode::ExecFailed,
                    format!("services depending on {name} are running"),
                ))
            }
            Err(_) => {
                return Err((
                    ErrorCode::ExecFailed,
                    format!("cannot stop the {name} service"),
                ))
            }
        }
        drop(permit);
        guard.stopped.push((name, svc));
        let (_, svc) = guard.stopped.last().expect("just pushed");
        if !wait_for(svc, ServiceState::Stopped, WU_STOP_WAIT.min(s.time_left())) {
            // Nothing is deleted; the guard brings the service back.
            return Err((
                ErrorCode::Timeout,
                format!("the {name} service did not stop in time"),
            ));
        }
    }
    f(s);
    drop(guard);
    Ok(())
}

// ---------------------------------------------------------------------------------
// Targets
// ---------------------------------------------------------------------------------

/// Run one Windows target.
pub(super) fn execute(entry: &CatalogEntry, s: &mut Session) -> Result<(), (ErrorCode, String)> {
    let ops = WinOps;
    match entry.target {
        "temp_system" => sweep_root(
            &ops,
            s,
            &windir().join("Temp"),
            &[],
            &windows_tree_rules(entry.min_age_days),
        ),
        "temp_users" => {
            let rules = windows_tree_rules(entry.min_age_days);
            for profile in user_profiles() {
                sweep_root(&ops, s, &profile, &["AppData", "Local", "Temp"], &rules);
                if s.stopped.is_some() {
                    break;
                }
            }
        }
        "windows_update_cache" => {
            let root = windir().join("SoftwareDistribution").join("Download");
            let rules = windows_tree_rules(entry.min_age_days);
            with_update_services_stopped(s, |s| sweep_root(&ops, s, &root, &[], &rules))?;
        }
        "recycle_bin" => {
            for volume in fixed_volumes() {
                sweep_recycle_bin(
                    &ops,
                    s,
                    &volume.join("$Recycle.Bin"),
                    days(entry.min_age_days),
                );
                if s.stopped.is_some() {
                    break;
                }
            }
        }
        "logs_old" => sweep_root(
            &ops,
            s,
            &windir().join("Logs").join("CBS"),
            &[],
            &windows_cbs_rules(entry.min_age_days),
        ),
        other => {
            return Err((
                ErrorCode::BadArgs,
                format!("unknown cleanup target {other:?}"),
            ))
        }
    }
    Ok(())
}

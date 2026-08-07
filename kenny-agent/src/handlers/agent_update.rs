//! `agent_update` — server-triggered self-update (Windows and Linux service builds).
//!
//! Flow (Windows):
//! 1. Download `url` to a temp file.
//! 2. Verify SHA-256 == `sha256` (abort with `exec_failed` on mismatch/error).
//! 3. Stage it as `kenny-agent.new.exe` next to the running exe.
//! 4. Spawn a detached `finish-update` helper that waits for the service to stop,
//!    swaps the binary (running exe → `.old`, `.new.exe` → exe), and restarts the
//!    service.
//! 5. Return `{ok, staged_version}` **before** triggering the restart, so the
//!    connection drops and the agent reconnects on the new version.
//!
//! Flow (Linux, ADR-0031 Phase 4 / ADR-0034):
//! 1. Download + verify exactly as above, staged as `kenny-agent.new` next to the
//!    running exe (`/opt/kenny/kenny-agent`).
//! 2. `chmod 0o755`, then `rename` it over the running binary. This is atomic
//!    (same dir) and legal on Linux: the live process keeps its open inode.
//! 3. Return `{ok, staged_version}`, then trigger a **detached** systemd restart
//!    (`systemd-run --on-active=2 systemctl restart <unit>`) so the response frame
//!    flushes before systemd stops us. None of the Windows exe-lock / tray-mutex
//!    dance applies here.
//!
//! On other Unixes (macOS/BSD) the agent lacks this capability and returns
//! `error.code = "unsupported"` per the contract.

use serde_json::Value;

use crate::protocol::ErrorCode;

/// Arguments for `agent_update` (`{version, url, sha256}`).
#[derive(serde::Deserialize)]
struct UpdateArgs {
    #[allow(dead_code)]
    version: String,
    #[allow(dead_code)]
    url: String,
    /// Lowercase hex-encoded SHA-256 of the expected binary.
    #[allow(dead_code)]
    sha256: String,
}

/// `agent_update` entry point.
pub async fn update(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: UpdateArgs =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::update(a).await
    }
    #[cfg(target_os = "linux")]
    {
        let a: UpdateArgs =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        linux_impl::update(a).await
    }
    #[cfg(all(not(windows), not(target_os = "linux")))]
    {
        // Validate args even on the stub so malformed calls are caught early.
        let _ = std::mem::size_of::<UpdateArgs>();
        Err((
            ErrorCode::Unsupported,
            "agent_update is only available on the Windows and Linux service builds".to_string(),
        ))
    }
}

/// Download + SHA-256 verification shared by the Windows and Linux update paths.
///
/// Kept in one place so both `windows_impl` and `linux_impl` stage binaries the
/// same way (stream to disk, hash as we go, reject on mismatch).
#[cfg(any(windows, target_os = "linux"))]
mod common {
    use crate::protocol::ErrorCode;
    use sha2::{Digest, Sha256};
    use std::io::Read;
    use std::path::Path;

    /// Cap a download at 256 MiB to avoid unbounded memory use.
    const MAX_DOWNLOAD_BYTES: u64 = 256 * 1024 * 1024;

    /// Stream `url` to `dest`, computing SHA-256 as we go; verify against `expected`
    /// (lowercase hex). Removes the staged file on any error, including a mismatch.
    pub fn download_and_verify(
        url: &str,
        expected: &str,
        dest: &Path,
    ) -> Result<(), (ErrorCode, String)> {
        let resp = ureq::get(url)
            .call()
            .map_err(|e| (ErrorCode::ExecFailed, format!("download failed: {e}")))?;

        let mut reader = resp.into_reader().take(MAX_DOWNLOAD_BYTES);
        let mut file = std::fs::File::create(dest).map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("cannot create staged file: {e}"),
            )
        })?;
        let mut hasher = Sha256::new();
        let mut buf = [0u8; 64 * 1024];
        loop {
            let n = reader
                .read(&mut buf)
                .map_err(|e| (ErrorCode::ExecFailed, format!("download read error: {e}")))?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
            use std::io::Write;
            file.write_all(&buf[..n])
                .map_err(|e| (ErrorCode::ExecFailed, format!("staged write error: {e}")))?;
        }
        file.sync_all().ok();

        let got = hex_encode(&hasher.finalize());
        if got != expected {
            // Don't leave an unverified binary lying around.
            let _ = std::fs::remove_file(dest);
            return Err((
                ErrorCode::ExecFailed,
                format!("sha256 mismatch: expected {expected}, got {got}"),
            ));
        }
        Ok(())
    }

    /// Lowercase hex-encode a byte slice (avoids pulling in another crate).
    pub fn hex_encode(bytes: &[u8]) -> String {
        let mut s = String::with_capacity(bytes.len() * 2);
        for b in bytes {
            use std::fmt::Write;
            let _ = write!(s, "{b:02x}");
        }
        s
    }
}

/// True when two paths refer to the same executable image. Windows file paths are
/// case-insensitive, so compare canonicalized, lowercased strings; fall back to the
/// raw path when canonicalization fails (e.g. a binary that is mid-swap).
#[cfg(any(windows, test))]
fn same_executable(a: &std::path::Path, b: &std::path::Path) -> bool {
    fn norm(p: &std::path::Path) -> String {
        std::fs::canonicalize(p)
            .unwrap_or_else(|_| p.to_path_buf())
            .to_string_lossy()
            .to_lowercase()
    }
    norm(a) == norm(b)
}

/// A fresh process table with executable paths populated, so callers can match
/// processes by the binary they run (the default refresh does not guarantee `exe`).
#[cfg(any(windows, test))]
fn process_snapshot() -> sysinfo::System {
    use sysinfo::{ProcessRefreshKind, ProcessesToUpdate, System, UpdateKind};
    let mut sys = System::new();
    sys.refresh_processes_specifics(
        ProcessesToUpdate::All,
        true,
        ProcessRefreshKind::nothing().with_exe(UpdateKind::Always),
    );
    sys
}

/// Terminate every *other* process whose executable image is `target`.
///
/// A running `.exe` is locked on Windows, so the file cannot be replaced while a
/// process still runs it. The caller has already stopped the service, but the tray
/// runs the same binary in the interactive session (ADR-0011) and would otherwise
/// keep the image locked — the actual cause of "target exe still locked" — aborting
/// the self-update swap (ADR-0013). We skip our own PID (the updater runs a side
/// copy, but be defensive). Best-effort: per-process failures are logged, not fatal.
/// Returns the number of processes signalled.
#[cfg(any(windows, test))]
fn terminate_processes_running(target: &std::path::Path) -> usize {
    let me = sysinfo::get_current_pid().ok();
    let sys = process_snapshot();
    let mut signalled = 0usize;
    for (pid, proc_) in sys.processes() {
        if Some(*pid) == me {
            continue;
        }
        let Some(exe) = proc_.exe() else { continue };
        if !same_executable(exe, target) {
            continue;
        }
        if proc_.kill() {
            signalled += 1;
            tracing::info!(pid = pid.as_u32(), "terminated process locking target exe");
        } else {
            tracing::warn!(
                pid = pid.as_u32(),
                "could not terminate process locking target exe"
            );
        }
    }
    signalled
}

/// Poll until no *other* process runs `target`, or `timeout` elapses. Returns `true`
/// once the image is free of other processes, `false` on timeout.
///
/// `TerminateProcess` (via [`terminate_processes_running`]) is asynchronous: it signals
/// a process but returns before the kernel has reaped it and released its handles —
/// including the tray's per-session single-instance mutex. The self-update swap uses the
/// rename-the-running-exe trick (ADR-0013), so the rename succeeds *immediately* even
/// while the old tray is still alive; nothing in the swap path waits for it to die. We
/// must therefore wait explicitly before restarting the service, otherwise the freshly
/// launched tray loses the mutex race against the still-dying old tray and exits — and
/// the user is left looking at the OLD-version tray while the service runs the new one.
#[cfg(any(windows, test))]
fn wait_for_no_process_running(target: &std::path::Path, timeout: std::time::Duration) -> bool {
    let me = sysinfo::get_current_pid().ok();
    let deadline = std::time::Instant::now() + timeout;
    loop {
        let sys = process_snapshot();
        let still_running = sys.processes().iter().any(|(pid, proc_)| {
            Some(*pid) != me && proc_.exe().is_some_and(|exe| same_executable(exe, target))
        });
        if !still_running {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use serde_json::json;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use tracing::{info, warn};

    use crate::config::SERVICE_NAME;

    /// `CREATE_NO_WINDOW` — keep the detached helper headless.
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    /// `DETACHED_PROCESS` — fully detach from the (dying) service process.
    const DETACHED_PROCESS: u32 = 0x0000_0008;

    pub async fn update(a: UpdateArgs) -> Result<Value, (ErrorCode, String)> {
        let exe = std::env::current_exe().map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("cannot locate current exe: {e}"),
            )
        })?;
        let dir = exe
            .parent()
            .ok_or((ErrorCode::ExecFailed, "exe has no parent dir".to_string()))?
            .to_path_buf();
        let staged = dir.join("kenny-agent.new.exe");

        // Download + verify on a blocking thread (ureq is synchronous; hashing is CPU).
        let url = a.url.clone();
        let expected = a.sha256.to_lowercase();
        let staged_path = staged.clone();
        let result = tokio::task::spawn_blocking(move || {
            super::common::download_and_verify(&url, &expected, &staged_path)
        })
        .await
        .map_err(|e| (ErrorCode::ExecFailed, format!("update task panicked: {e}")))?;
        result?;

        // Hash verified and the new binary is staged. Spawn the detached helper that
        // performs the swap + restart, then answer the server. We launch it BEFORE
        // returning the response struct to the caller, but the helper waits for the
        // service to stop, so the response still reaches the server first.
        spawn_finish_update(&exe, &staged)?;

        info!(version = %a.version, "agent_update staged; restart helper launched");
        Ok(json!({ "ok": true, "staged_version": a.version }))
    }

    /// Launch the detached `finish-update` helper that swaps binaries and restarts
    /// the service once this process (the service) has stopped.
    fn spawn_finish_update(target: &Path, staged: &Path) -> Result<(), (ErrorCode, String)> {
        // The helper runs the SAME (current, soon-to-be-replaced) exe; it only does
        // file ops + SCM calls, so running the old binary is fine.
        let helper: PathBuf = staged_helper_copy(target)?;
        Command::new(&helper)
            .arg("finish-update")
            .arg("--service")
            .arg(SERVICE_NAME)
            .arg("--new")
            .arg(staged)
            .arg("--target")
            .arg(target)
            .creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS)
            .spawn()
            .map_err(|e| {
                (
                    ErrorCode::ExecFailed,
                    format!("spawn finish-update failed: {e}"),
                )
            })?;
        Ok(())
    }

    /// Copy the running exe to a side path so the helper isn't the file being
    /// replaced (you cannot rename a running exe's own image into `.old` from
    /// within itself reliably while it is the swap target). Returns the helper path.
    fn staged_helper_copy(target: &Path) -> Result<PathBuf, (ErrorCode, String)> {
        let dir = target
            .parent()
            .ok_or((ErrorCode::ExecFailed, "exe has no parent dir".to_string()))?;
        let helper = dir.join("kenny-agent.updater.exe");
        std::fs::copy(target, &helper).map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("cannot stage updater helper: {e}"),
            )
        })?;
        Ok(helper)
    }

    /// The `finish-update` helper body: stop the service, swap binaries, restart it.
    ///
    /// Runs as a separate, detached process (the OLD binary). It polls the service
    /// until it is stopped, renames the running target to `.old`, moves the verified
    /// `.new.exe` into place, then starts the service again.
    pub fn finish_update(service: &str, new: &Path, target: &Path) -> anyhow::Result<()> {
        use std::thread::sleep;
        use std::time::Duration;
        use windows_service::service::{ServiceAccess, ServiceState};
        use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

        let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)?;
        let svc = manager.open_service(
            service,
            ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::START,
        )?;

        // Ask the service to stop (it may already be stopping because the server
        // dropped us), then wait for it to actually be Stopped.
        let _ = svc.stop();
        for _ in 0..120 {
            let status = svc.query_status()?;
            if status.current_state == ServiceState::Stopped {
                break;
            }
            sleep(Duration::from_millis(500));
        }

        // The service is stopped, but the tray runs the SAME binary in the user's
        // interactive session (ADR-0011, ADR-0018) and holds the per-session
        // single-instance mutex. Terminate any process still running the target.
        let killed = super::terminate_processes_running(target);
        if killed > 0 {
            info!(
                count = killed,
                "terminated processes locking target exe before swap"
            );
        }

        // Wait for those processes to actually exit before swapping + restarting.
        // TerminateProcess is asynchronous, and the rename-the-running-exe swap below
        // does NOT block on the image (renaming a running exe succeeds on Windows), so
        // without this wait the restarted service would relaunch the tray while the old
        // tray is still alive and holding the single-instance mutex — the new tray would
        // lose the race and exit, leaving the OLD-version tray reporting a stale version
        // (e.g. tray shows v1.0.0 after the service updated to v1.1.2). The poll also
        // lets the cleanup `remove_file(&old)` below actually succeed.
        if !super::wait_for_no_process_running(target, Duration::from_secs(10)) {
            warn!("processes running target exe did not exit before timeout; tray may report a stale version");
        }

        // Swap: running exe → .old, new → exe. Retry briefly in case the image lock
        // hasn't been released yet.
        let old = target.with_extension("old");
        let _ = std::fs::remove_file(&old);
        let mut swapped = false;
        for _ in 0..40 {
            if std::fs::rename(target, &old).is_ok() {
                swapped = true;
                break;
            }
            sleep(Duration::from_millis(500));
        }
        if !swapped {
            warn!("could not move running exe aside; aborting swap");
            anyhow::bail!("target exe still locked");
        }
        if let Err(e) = std::fs::rename(new, target) {
            // Roll back so the service can still start on the old binary.
            let _ = std::fs::rename(&old, target);
            return Err(e.into());
        }

        // Restart the service on the new binary, then clean up.
        svc.start::<&str>(&[])?;
        let _ = std::fs::remove_file(&old);
        info!(service, "service restarted on updated binary");
        Ok(())
    }
}

/// Public re-export used by `main.rs` to run the hidden `finish-update` subcommand.
#[cfg(windows)]
pub fn run_finish_update(service: &str, new: &str, target: &str) -> anyhow::Result<()> {
    windows_impl::finish_update(
        service,
        std::path::Path::new(new),
        std::path::Path::new(target),
    )
}

#[cfg(target_os = "linux")]
mod linux_impl {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;
    use tracing::{info, warn};

    /// ELF `e_machine` values for our two supported release targets (see
    /// `/usr/include/elf.h`: `EM_X86_64`, `EM_AARCH64`).
    const EM_X86_64: u16 = 62;
    const EM_AARCH64: u16 = 183;

    /// Read the ELF `e_machine` field (u16, little-endian, offset 18) from `path`.
    ///
    /// Both supported targets (`x86_64`, `aarch64`) are little-endian, so we don't
    /// need to branch on `EI_DATA`. Split out from [`verify_elf_matches_host`] so the
    /// byte-parsing is unit-testable on synthetic headers.
    pub fn read_e_machine(path: &std::path::Path) -> Result<u16, (ErrorCode, String)> {
        use std::io::Read;
        let mut f = std::fs::File::open(path).map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("cannot open staged binary: {e}"),
            )
        })?;
        let mut hdr = [0u8; 20];
        f.read_exact(&mut hdr).map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("staged binary too short to be ELF: {e}"),
            )
        })?;
        if hdr[0..4] != *b"\x7fELF" {
            return Err((
                ErrorCode::ExecFailed,
                "staged binary is not an ELF file".to_string(),
            ));
        }
        Ok(u16::from_le_bytes([hdr[18], hdr[19]]))
    }

    /// Expected ELF `e_machine` for a normalized `std::env::consts::ARCH`-style string.
    pub fn machine_for_arch(arch: &str) -> u16 {
        match arch {
            "aarch64" => EM_AARCH64,
            _ => EM_X86_64,
        }
    }

    /// Reject a staged binary whose ELF machine type doesn't match this host.
    ///
    /// Defense in depth for #139: the server resolves which binary to push from the
    /// agent's *reported* arch, so a routing bug (or an old agent that never reported
    /// one) could still hand us a wrong-arch download. The sha256 check only proves
    /// the bytes match what the server *served* — it says nothing about whether those
    /// bytes are runnable here. This check runs before the atomic rename, so a
    /// mismatched binary is rejected and the working exe is never touched.
    fn verify_elf_matches_host(path: &std::path::Path) -> Result<(), (ErrorCode, String)> {
        let machine = read_e_machine(path)?;
        let expected = machine_for_arch(std::env::consts::ARCH);
        if machine != expected {
            return Err((
                ErrorCode::ExecFailed,
                format!(
                    "arch mismatch: staged binary e_machine={machine}, host ({}) expects {expected}",
                    std::env::consts::ARCH
                ),
            ));
        }
        Ok(())
    }

    /// Linux self-update: download + verify, atomically swap the running binary,
    /// then trigger a detached systemd restart.
    pub async fn update(a: UpdateArgs) -> Result<Value, (ErrorCode, String)> {
        let exe = std::env::current_exe().map_err(|e| {
            (
                ErrorCode::ExecFailed,
                format!("cannot locate current exe: {e}"),
            )
        })?;
        let dir = exe
            .parent()
            .ok_or((ErrorCode::ExecFailed, "exe has no parent dir".to_string()))?
            .to_path_buf();
        let staged = dir.join("kenny-agent.new");

        // Download + verify on a blocking thread (ureq is synchronous; hashing is CPU).
        let url = a.url.clone();
        let expected = a.sha256.to_lowercase();
        let staged_path = staged.clone();
        let result = tokio::task::spawn_blocking(move || {
            super::common::download_and_verify(&url, &expected, &staged_path)
        })
        .await
        .map_err(|e| (ErrorCode::ExecFailed, format!("update task panicked: {e}")))?;
        result?;

        // Defense in depth: never let a wrong-arch binary reach the atomic rename
        // below, even if routing was wrong (#139). Checked before chmod so a bad
        // download is rejected as early as possible.
        if let Err(e) = verify_elf_matches_host(&staged) {
            let _ = std::fs::remove_file(&staged);
            return Err(e);
        }

        // Make the staged binary executable before it becomes the live exe.
        if let Err(e) = std::fs::set_permissions(&staged, std::fs::Permissions::from_mode(0o755)) {
            let _ = std::fs::remove_file(&staged);
            return Err((
                ErrorCode::ExecFailed,
                format!("cannot chmod staged binary: {e}"),
            ));
        }

        // Atomic swap: same-dir rename replaces the running binary in place. This is
        // legal on Linux — the live process keeps its already-open inode, and the new
        // image only takes effect on the next exec (the systemd restart below).
        if let Err(e) = std::fs::rename(&staged, &exe) {
            let _ = std::fs::remove_file(&staged);
            return Err((
                ErrorCode::ExecFailed,
                format!("cannot replace running binary: {e}"),
            ));
        }

        let unit = std::fs::read_to_string("/proc/self/cgroup")
            .map(|c| unit_name_from_cgroup(&c))
            .unwrap_or_else(|_| "kenny-agent.service".to_string());

        info!(
            version = %a.version,
            unit = %unit,
            "agent_update swapped binary; scheduling detached restart"
        );

        // Build the response BEFORE triggering the restart so we can hand it back to
        // the tunnel to flush; the restart is delayed (2 s) to let that frame go out.
        let response = json!({ "ok": true, "staged_version": a.version });
        trigger_restart(&unit);
        Ok(response)
    }

    /// Schedule a systemd restart of `unit` that survives our own SIGTERM.
    ///
    /// Preferred path: `systemd-run --on-active=2 systemctl restart <unit>` runs the
    /// restart from a transient unit outside our cgroup, so stopping us cannot kill it.
    /// Fallbacks (best-effort) shell out via `setsid`/`sh` with a short sleep.
    fn trigger_restart(unit: &str) {
        match Command::new("systemd-run")
            .arg("--on-active=2")
            .arg("systemctl")
            .arg("restart")
            .arg(unit)
            .spawn()
        {
            Ok(_) => {
                info!(unit, "scheduled restart via systemd-run");
                return;
            }
            Err(e) => {
                warn!(unit, error = %e, "systemd-run spawn failed; falling back to detached sh");
            }
        }

        // Fallback: a detached shell that sleeps (so our response frame flushes) then
        // restarts the unit. `setsid` puts it in its own session; if `setsid` is
        // missing, spawn `sh` directly as a last resort.
        let script = format!("sleep 2; systemctl restart {unit}");
        match Command::new("setsid")
            .arg("sh")
            .arg("-c")
            .arg(&script)
            .spawn()
        {
            Ok(_) => info!(unit, "scheduled restart via setsid sh"),
            Err(_) => match Command::new("sh").arg("-c").arg(&script).spawn() {
                Ok(_) => info!(unit, "scheduled restart via sh"),
                Err(e) => warn!(unit, error = %e, "failed to schedule restart"),
            },
        }
    }

    /// Extract the systemd unit name from `/proc/self/cgroup` contents.
    ///
    /// cgroup v2 lines look like `0::/system.slice/kenny-agent.service`; we take the
    /// last path segment ending in `.service`. Pure function so it is unit-testable.
    /// Falls back to `kenny-agent.service` when nothing parseable is found.
    pub fn unit_name_from_cgroup(contents: &str) -> String {
        for line in contents.lines() {
            if let Some(seg) = line.rsplit('/').find(|s| s.ends_with(".service")) {
                return seg.to_string();
            }
        }
        "kenny-agent.service".to_string()
    }
}

#[cfg(all(test, not(windows)))]
mod tests {
    use super::*;
    #[allow(unused_imports)]
    use serde_json::json;

    #[cfg(all(not(windows), not(target_os = "linux")))]
    #[tokio::test]
    async fn update_is_unsupported_off_windows_and_linux() {
        let res = update(json!({
            "version": "0.2.0",
            "url": "https://example.com/kenny-agent.exe",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        }))
        .await;
        let (code, _msg) = res.expect_err("must be unsupported off Windows and Linux");
        assert_eq!(code, ErrorCode::Unsupported);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn cgroup_v2_service_line() {
        assert_eq!(
            super::linux_impl::unit_name_from_cgroup("0::/system.slice/kenny-agent.service\n"),
            "kenny-agent.service"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn cgroup_custom_service_name() {
        assert_eq!(
            super::linux_impl::unit_name_from_cgroup("0::/system.slice/my-custom-agent.service"),
            "my-custom-agent.service"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn cgroup_nested_slice_takes_deepest_service() {
        assert_eq!(
            super::linux_impl::unit_name_from_cgroup(
                "0::/system.slice/kenny.slice/kenny-agent.service"
            ),
            "kenny-agent.service"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn cgroup_falls_back_when_no_service_segment() {
        assert_eq!(
            super::linux_impl::unit_name_from_cgroup(""),
            "kenny-agent.service"
        );
        assert_eq!(
            super::linux_impl::unit_name_from_cgroup(
                "0::/user.slice/user-1000.slice/session-3.scope"
            ),
            "kenny-agent.service"
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn elf_guard_accepts_matching_machine() {
        use super::linux_impl::{machine_for_arch, read_e_machine};
        let path = write_synthetic_elf(machine_for_arch("aarch64"));
        assert_eq!(read_e_machine(&path).unwrap(), machine_for_arch("aarch64"));
        let _ = std::fs::remove_file(&path);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn elf_guard_rejects_mismatched_machine() {
        use super::linux_impl::machine_for_arch;
        assert_ne!(machine_for_arch("aarch64"), machine_for_arch("x86_64"));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn elf_guard_rejects_non_elf_file() {
        use super::linux_impl::read_e_machine;
        let path = std::env::temp_dir().join(format!(
            "kenny-agent-not-elf-{}-{}",
            std::process::id(),
            "a"
        ));
        std::fs::write(
            &path,
            b"not an elf file at all, just text padding out to 20+",
        )
        .unwrap();
        let err = read_e_machine(&path).expect_err("must reject non-ELF content");
        assert_eq!(err.0, ErrorCode::ExecFailed);
        let _ = std::fs::remove_file(&path);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn elf_guard_rejects_truncated_file() {
        use super::linux_impl::read_e_machine;
        let path = std::env::temp_dir().join(format!(
            "kenny-agent-short-elf-{}-{}",
            std::process::id(),
            "b"
        ));
        std::fs::write(&path, b"\x7fELF\x02\x01").unwrap(); // fewer than 20 bytes
        let err = read_e_machine(&path).expect_err("must reject a too-short file");
        assert_eq!(err.0, ErrorCode::ExecFailed);
        let _ = std::fs::remove_file(&path);
    }

    /// Write a minimal synthetic ELF header (magic + padding + `e_machine` at offset
    /// 18) so the byte-parsing can be tested without a real binary on disk.
    #[cfg(target_os = "linux")]
    fn write_synthetic_elf(machine: u16) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "kenny-agent-synthetic-elf-{}-{}",
            std::process::id(),
            machine
        ));
        let mut hdr = [0u8; 20];
        hdr[0..4].copy_from_slice(b"\x7fELF");
        hdr[18..20].copy_from_slice(&machine.to_le_bytes());
        std::fs::write(&path, hdr).unwrap();
        path
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn hex_encode_matches_known_sha256() {
        use sha2::{Digest, Sha256};
        // SHA-256 of the empty input is a well-known constant.
        let digest = Sha256::new().finalize();
        assert_eq!(
            super::common::hex_encode(&digest),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn same_executable_matches_identical_path() {
        let me = std::env::current_exe().expect("current exe path");
        assert!(same_executable(&me, &me));
    }

    #[test]
    fn same_executable_rejects_distinct_paths() {
        use std::path::Path;
        assert!(!same_executable(
            Path::new("/opt/a/kenny"),
            Path::new("/opt/b/kenny")
        ));
    }

    #[test]
    fn snapshot_populates_exe_for_current_process() {
        // Guards the fix: matching processes by their binary requires `exe()` to be
        // populated by the refresh. If this regresses, the tray would never be matched
        // and the self-update swap would fail again on a locked exe.
        let me = sysinfo::get_current_pid().expect("current pid");
        let sys = process_snapshot();
        let proc_ = sys
            .process(me)
            .expect("current process present in snapshot");
        assert!(
            proc_.exe().is_some(),
            "exe path must be populated by the refresh"
        );
    }

    #[test]
    fn terminate_skips_when_no_process_runs_target() {
        // No process was started from this path, so nothing is terminated.
        let bogus = std::env::temp_dir().join("kenny-agent-not-a-real-binary-zzz");
        assert_eq!(terminate_processes_running(&bogus), 0);
    }

    #[test]
    fn wait_returns_immediately_when_no_process_runs_target() {
        // Nothing runs this path, so the wait succeeds on the first snapshot. A long
        // timeout proves we don't busy-wait to the deadline when the image is free.
        let bogus = std::env::temp_dir().join("kenny-agent-not-a-real-binary-zzz");
        let start = std::time::Instant::now();
        assert!(wait_for_no_process_running(
            &bogus,
            std::time::Duration::from_secs(30)
        ));
        assert!(
            start.elapsed() < std::time::Duration::from_secs(5),
            "must return as soon as no process holds the target, not at the deadline"
        );
    }

    #[test]
    fn wait_times_out_while_another_process_runs_target() {
        // A live process (other than us) holding the target image must make the wait
        // block to its deadline and report `false`. Run a child from a private copy of a
        // long-lived binary so we fully control the path and it isn't our own PID.
        let src = std::path::Path::new("/bin/sleep");
        if !src.exists() {
            return; // No `sleep` on this host; skip rather than fail.
        }
        let target = std::env::temp_dir().join(format!("kenny-wait-test-{}", std::process::id()));
        std::fs::copy(src, &target).expect("copy sleep binary");
        // Make the copy executable for hosts that don't preserve the mode bit.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&target).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&target, perms).unwrap();
        }

        let mut child = std::process::Command::new(&target)
            .arg("30")
            .spawn()
            .expect("spawn child running target");
        // Give the child a moment to appear in the process table.
        std::thread::sleep(std::time::Duration::from_millis(200));

        let start = std::time::Instant::now();
        let freed = wait_for_no_process_running(&target, std::time::Duration::from_millis(300));
        let elapsed = start.elapsed();

        let _ = child.kill();
        let _ = child.wait();
        let _ = std::fs::remove_file(&target);

        assert!(
            !freed,
            "must report not-free while a process still runs target"
        );
        assert!(
            elapsed >= std::time::Duration::from_millis(300),
            "must block until the deadline when the image stays held"
        );
    }
}

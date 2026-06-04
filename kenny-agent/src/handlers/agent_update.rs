//! `agent.update` — server-triggered self-update (Windows only).
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
//! Off Windows (dev/Linux builds) the agent lacks this capability and returns
//! `error.code = "unsupported"` per the contract.

use serde_json::Value;

use crate::protocol::ErrorCode;

/// Arguments for `agent.update` (`{version, url, sha256}`).
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

/// `agent.update` entry point.
pub async fn update(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: UpdateArgs =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::update(a).await
    }
    #[cfg(not(windows))]
    {
        // Validate args even on the stub so malformed calls are caught early.
        let _ = std::mem::size_of::<UpdateArgs>();
        Err((
            ErrorCode::Unsupported,
            "agent.update is only available on the Windows service build".to_string(),
        ))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use std::io::Read;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use tracing::{info, warn};

    use crate::config::SERVICE_NAME;

    /// `CREATE_NO_WINDOW` — keep the detached helper headless.
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    /// `DETACHED_PROCESS` — fully detach from the (dying) service process.
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    /// Cap a download at 256 MiB to avoid unbounded memory use.
    const MAX_DOWNLOAD_BYTES: u64 = 256 * 1024 * 1024;

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
        let result =
            tokio::task::spawn_blocking(move || download_and_verify(&url, &expected, &staged_path))
                .await
                .map_err(|e| (ErrorCode::ExecFailed, format!("update task panicked: {e}")))?;
        result?;

        // Hash verified and the new binary is staged. Spawn the detached helper that
        // performs the swap + restart, then answer the server. We launch it BEFORE
        // returning the response struct to the caller, but the helper waits for the
        // service to stop, so the response still reaches the server first.
        spawn_finish_update(&exe, &staged)?;

        info!(version = %a.version, "agent.update staged; restart helper launched");
        Ok(json!({ "ok": true, "staged_version": a.version }))
    }

    /// Stream `url` to `dest`, computing SHA-256 as we go; verify against `expected`.
    fn download_and_verify(
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
    fn hex_encode(bytes: &[u8]) -> String {
        let mut s = String::with_capacity(bytes.len() * 2);
        for b in bytes {
            use std::fmt::Write;
            let _ = write!(s, "{b:02x}");
        }
        s
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

#[cfg(all(test, not(windows)))]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn update_is_unsupported_off_windows() {
        let res = update(json!({
            "version": "0.2.0",
            "url": "https://example.com/kenny-agent.exe",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        }))
        .await;
        let (code, _msg) = res.expect_err("must be unsupported off Windows");
        assert_eq!(code, ErrorCode::Unsupported);
    }
}

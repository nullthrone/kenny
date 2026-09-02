//! `remotehelp_*` — orchestrate Windows Quick Assist as a remote-help *concierge*.
//!
//! kenny does **not** carry the screen or input — Quick Assist brings its own Microsoft
//! relay, NAT traversal, and encryption. These tools prepare, launch, and tear down a
//! session around it:
//!
//! * `remotehelp_status` (read-only) — is Quick Assist installed, is the internet
//!   reachable, and is there an interactive session to host it?
//! * `remotehelp_start` (mutating) — open Quick Assist on the user's desktop.
//! * `remotehelp_stop` (mutating) — close Quick Assist so no session lingers.
//!
//! The agent runs as a session-0 service with no desktop, so `start` launches the app via
//! the user-session tray helper over an allow-listed named pipe — same delivery mechanism
//! as `screen_capture` (ADR-0018). See ADR-0021. Off Windows, `start`/`stop` return
//! `unsupported` and `status` reports everything not-available.

use serde_json::{json, Value};

use crate::protocol::ErrorCode;

/// Quick Assist's process name — also its key in the tray launch allow-list
/// (`session_launch_ipc::ALLOWED_APPS`), which maps it to the packaged app's AUMID.
#[cfg_attr(not(windows), allow(dead_code))]
const QUICK_ASSIST_EXE: &str = "quickassist.exe";

/// The human-in-the-loop reminder returned by `remotehelp_start`: kenny opens the app but
/// the Microsoft-account sign-in, the security code, and the consent click stay with the
/// people involved (a feature in a family setting).
#[cfg_attr(not(windows), allow(dead_code))]
const START_NOTE: &str = "Quick Assist opened on the user's desktop. A helper must share \
the security code and the person at this PC must accept the connection.";

/// `remotehelp_status` — readiness for a Quick Assist session.
pub async fn status(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::status().await
    }
    #[cfg(not(windows))]
    {
        Ok(json!({
            "installed": false,
            "version": "",
            "internet_ok": false,
            "interactive_session": false,
        }))
    }
}

/// `remotehelp_start` — launch Quick Assist on the interactive user desktop.
pub fn start(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let mut result = crate::session_launch_ipc::launch_via_tray(QUICK_ASSIST_EXE)?;
        // Attach the human-in-the-loop note alongside `{launched, pid}`.
        if let Some(obj) = result.as_object_mut() {
            obj.insert("note".to_string(), json!(START_NOTE));
        }
        Ok(result)
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("remotehelp_start"))
    }
}

/// `remotehelp_stop` — terminate Quick Assist so no session is left open.
pub async fn stop(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::stop().await
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("remotehelp_stop"))
    }
}

#[cfg(not(windows))]
fn unsupported(tool: &str) -> (ErrorCode, String) {
    (
        ErrorCode::Unsupported,
        format!("{tool} is only available on Windows"),
    )
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use std::time::Duration;
    use tokio::process::Command;

    /// Bound on the readiness probe — `Test-NetConnection` can take a few seconds.
    const STATUS_TIMEOUT: Duration = Duration::from_secs(20);

    /// Run a PowerShell script and return its captured stdout, honouring a timeout.
    async fn powershell(script: &str, timeout: Duration) -> Result<String, (ErrorCode, String)> {
        let mut cmd = Command::new("powershell.exe");
        cmd.args(["-NoProfile", "-NonInteractive", "-Command", script]);
        let output = match tokio::time::timeout(timeout, cmd.output()).await {
            Ok(res) => {
                res.map_err(|e| (ErrorCode::ExecFailed, format!("powershell spawn: {e}")))?
            }
            Err(_) => return Err((ErrorCode::Timeout, "remotehelp probe timed out".to_string())),
        };
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    }

    /// Probe Quick Assist install state + internet reachability via PowerShell, then add
    /// `interactive_session` (whether the tray's launch pipe is reachable).
    pub async fn status() -> Result<Value, (ErrorCode, String)> {
        // Emit a single compact JSON object so parsing is trivial and locale-independent.
        let script = "\
$ErrorActionPreference='SilentlyContinue';\
$pkg = Get-AppxPackage -Name 'MicrosoftCorporationII.QuickAssist';\
$net = Test-NetConnection -ComputerName 'login.live.com' -Port 443 -InformationLevel Quiet;\
[pscustomobject]@{ installed = [bool]$pkg; version = if ($pkg) { \"$($pkg.Version)\" } else { '' }; internet_ok = [bool]$net } | ConvertTo-Json -Compress";
        let raw = powershell(script, STATUS_TIMEOUT).await?;
        let parsed: Value = serde_json::from_str(raw.trim()).map_err(|e| {
            (
                ErrorCode::Internal,
                format!("could not parse remotehelp status ({e}): {raw}"),
            )
        })?;
        Ok(json!({
            "installed": parsed.get("installed").and_then(Value::as_bool).unwrap_or(false),
            "version": parsed.get("version").and_then(Value::as_str).unwrap_or(""),
            "internet_ok": parsed.get("internet_ok").and_then(Value::as_bool).unwrap_or(false),
            "interactive_session": crate::session_launch_ipc::tray_available(),
        }))
    }

    /// Best-effort terminate any running Quick Assist process. Idempotent: succeeds even
    /// when none is running (the operator just wants a clean state).
    pub async fn stop() -> Result<Value, (ErrorCode, String)> {
        let script = "Stop-Process -Name 'quickassist' -Force -ErrorAction SilentlyContinue";
        powershell(script, Duration::from_secs(10)).await?;
        Ok(json!({ "stopped": true }))
    }
}

#[cfg(test)]
mod tests {
    // Every test below is `#[cfg(not(windows))]`; gate the import the same way so a
    // Windows build (where this module would otherwise be empty) doesn't warn.
    #[cfg(not(windows))]
    use super::*;

    #[cfg(not(windows))]
    #[tokio::test]
    async fn status_reports_unavailable_off_windows() {
        let v = status(json!({})).await.unwrap();
        assert_eq!(v["installed"], false);
        assert_eq!(v["internet_ok"], false);
        assert_eq!(v["interactive_session"], false);
        assert_eq!(v["version"], "");
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn start_and_stop_unsupported_off_windows() {
        assert_eq!(start(json!({})).unwrap_err().0, ErrorCode::Unsupported);
        assert_eq!(stop(json!({})).await.unwrap_err().0, ErrorCode::Unsupported);
    }
}

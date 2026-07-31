//! Local remote-control kill switch (the tray on/off state).
//!
//! The person sitting at the endpoint can switch remote control **off** from the
//! agent's tray menu. While off, the agent refuses every *mutating* tool (anything
//! that writes to the device) but keeps serving telemetry and read-only diagnostics.
//!
//! State lives in a small JSON **control file** so it can be shared across processes
//! and sessions: the tray helper runs in the interactive user session and *writes* it,
//! while the agent — typically a Windows service in session 0 — *reads* it before
//! running a mutating tool. The choice therefore also survives restarts.
//!
//! Default is **on**: a missing/unreadable/corrupt file reads as enabled, so the
//! agent is fully operable out of the box and a transient read error never silently
//! widens the block. See ADR-0011.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// Environment override for the control-file path (used by tests and flexible
/// deployments). When unset, [`control_path`] falls back to a platform default.
pub const CONTROL_FILE_ENV: &str = "KENNY_CONTROL_FILE";

/// File name of the persisted control state.
pub const CONTROL_FILE: &str = "kenny-agent.control.json";

/// Persisted on/off state of the local remote-control kill switch.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct ControlState {
    pub remote_control_enabled: bool,
}

impl Default for ControlState {
    fn default() -> Self {
        // Remote control ships **on**.
        Self {
            remote_control_enabled: true,
        }
    }
}

/// Resolve the control-file path.
///
/// Precedence: `KENNY_CONTROL_FILE` override → a shared, cross-session location.
/// On Windows that is `%ProgramData%\kenny\` (readable by the LocalSystem service and
/// writable by the user's tray once `install` has granted the ACL). Elsewhere (dev/CI)
/// it is the system temp dir, which keeps single-user foreground runs and tests simple.
pub fn control_path() -> PathBuf {
    if let Some(path) = std::env::var_os(CONTROL_FILE_ENV) {
        return PathBuf::from(path);
    }
    base_dir().join(CONTROL_FILE)
}

#[cfg(windows)]
fn base_dir() -> PathBuf {
    let program_data = std::env::var_os("ProgramData").unwrap_or_else(|| r"C:\ProgramData".into());
    PathBuf::from(program_data).join("kenny")
}

/// Linux base dir: `/var/lib/kenny` when it exists and is writable (the systemd install
/// creates it), else the system temp dir so dev/CI single-user runs and tests stay
/// simple. The gate is "dir exists & writable", **not** "am I root", so a root
/// `cargo test` without `/var/lib/kenny` still uses `temp_dir()`.
#[cfg(target_os = "linux")]
fn base_dir() -> PathBuf {
    let fhs = PathBuf::from("/var/lib/kenny");
    if dir_is_writable(&fhs) {
        fhs
    } else {
        std::env::temp_dir()
    }
}

/// Portable base dir off Windows and Linux (macOS/BSD/etc): the system temp dir.
#[cfg(all(not(windows), not(target_os = "linux")))]
fn base_dir() -> PathBuf {
    std::env::temp_dir()
}

/// Whether `dir` exists and is writable by the current effective user, probed by
/// creating and removing a temporary file. Used to gate the FHS state path.
#[cfg(target_os = "linux")]
fn dir_is_writable(dir: &std::path::Path) -> bool {
    if !dir.is_dir() {
        return false;
    }
    let probe = dir.join(format!(".kenny-write-probe-{}", std::process::id()));
    match std::fs::File::create(&probe) {
        Ok(_) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// Read the current state. Fail-safe to **on** if the file is missing or unreadable.
pub fn read_state() -> ControlState {
    let path = control_path();
    match std::fs::read(&path) {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_default(),
        Err(_) => ControlState::default(),
    }
}

/// `true` if remote control (mutating tools) is currently allowed.
pub fn remote_control_enabled() -> bool {
    read_state().remote_control_enabled
}

/// Persist a new on/off state. Creates the parent directory and writes atomically
/// (temp file + rename) so a concurrent reader never sees a half-written file.
///
/// Only the Windows tray writes this in non-test builds; allow it to look unused on
/// other platforms (it is still exercised by the unit tests everywhere).
#[cfg_attr(not(windows), allow(dead_code))]
pub fn set_remote_control_enabled(enabled: bool) -> std::io::Result<()> {
    let path = control_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let state = ControlState {
        remote_control_enabled: enabled,
    };
    let body = serde_json::to_vec_pretty(&state)?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, &body)?;
    std::fs::rename(&tmp, &path)?;
    Ok(())
}

/// Whether a tool *writes to the device* and is therefore gated by the kill switch.
///
/// Read-only diagnostics (`fs_*`, `diag_*`, `net_config`, `screen_capture`,
/// `remotehelp_status`) and `telemetry_collect` are **not** mutating and keep working
/// while remote control is off. Keep this list in lockstep with the tool catalog in
/// `docs/protocol.md`.
pub fn is_mutating(tool: &str) -> bool {
    matches!(
        tool,
        "powershell_exec"
            | "shell_exec"
            | "winget_install"
            | "winget_uninstall"
            | "winget_update"
            | "net_dns_flush"
            | "net_adapter_reset"
            | "remotehelp_start"
            | "remotehelp_stop"
            | "agent_update"
            | "webfilter_apply"
            | "webfilter_clear"
            | "account_set_enabled"
            | "account_set_admin"
            | "account_set_logon_rights"
            | "account_create"
            | "account_delete"
            | "account_session_action"
            | "password_policy_set"
    )
}

/// Process-wide lock serializing every test that mutates the global
/// `KENNY_CONTROL_FILE` env var — shared across modules (e.g. `dispatch` tests) so they
/// never interleave with each other.
#[cfg(test)]
pub(crate) static TEST_ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[cfg(test)]
mod tests {
    use super::*;

    fn with_control_file<T>(name: &str, f: impl FnOnce(&std::path::Path) -> T) -> T {
        let _guard = TEST_ENV_LOCK.lock().unwrap();
        let path = std::env::temp_dir().join(name);
        let _ = std::fs::remove_file(&path);
        std::env::set_var(CONTROL_FILE_ENV, &path);
        let out = f(&path);
        std::env::remove_var(CONTROL_FILE_ENV);
        let _ = std::fs::remove_file(&path);
        out
    }

    #[test]
    fn defaults_to_enabled_when_file_missing() {
        with_control_file("kenny-test-missing.control.json", |_path| {
            assert!(remote_control_enabled(), "default must be on");
        });
    }

    #[test]
    fn write_then_read_round_trips() {
        with_control_file("kenny-test-roundtrip.control.json", |_path| {
            set_remote_control_enabled(false).unwrap();
            assert!(!remote_control_enabled());
            set_remote_control_enabled(true).unwrap();
            assert!(remote_control_enabled());
        });
    }

    #[test]
    fn corrupt_file_reads_as_enabled() {
        with_control_file("kenny-test-corrupt.control.json", |path| {
            std::fs::write(path, b"not json").unwrap();
            assert!(
                remote_control_enabled(),
                "corrupt file must fail safe to on"
            );
        });
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_base_dir_falls_back_to_temp_without_fhs_dir() {
        // When the FHS state dir is absent (CI/sandbox), the base dir must fall back to
        // the portable temp path — independent of whether the test runs as root.
        if !std::path::Path::new("/var/lib/kenny").exists() {
            assert_eq!(base_dir(), std::env::temp_dir());
        }
        // A guaranteed-absent directory is never writable.
        assert!(!dir_is_writable(std::path::Path::new(
            "/var/lib/kenny-definitely-not-present"
        )));
    }

    #[test]
    fn classifies_mutating_vs_readonly() {
        assert!(is_mutating("powershell_exec"));
        assert!(is_mutating("shell_exec"));
        assert!(is_mutating("winget_install"));
        assert!(is_mutating("agent_update"));
        assert!(is_mutating("remotehelp_start"));
        assert!(is_mutating("remotehelp_stop"));
        assert!(is_mutating("webfilter_apply"));
        assert!(is_mutating("webfilter_clear"));
        // webfilter_status is read-only and must keep working under the kill switch.
        assert!(!is_mutating("webfilter_status"));
        assert!(!is_mutating("telemetry_collect"));
        assert!(!is_mutating("fs_list"));
        assert!(!is_mutating("diag_processes"));
        assert!(!is_mutating("net_config"));
        assert!(!is_mutating("screen_capture"));
        assert!(!is_mutating("remotehelp_status"));
    }
}

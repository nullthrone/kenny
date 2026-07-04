//! `setup` — the self-elevating bootstrap installer.
//!
//! The connection config is resolved from the CLI flags and/or the
//! `kenny-agent.setup.json` sidecar the server ships next to the exe (flags win). On
//! Windows, `setup` elevates itself via UAC when needed, copies the running binary into
//! `%ProgramFiles%\kenny`, and runs `install` from there. See ADR-0033.
//!
//! The config resolution (and its tests) is portable and compiled on every platform so
//! Linux CI exercises it; only the elevation/copy path is `#[cfg(windows)]`.

use std::path::Path;

use crate::config::SetupArgs;
use crate::service::ServiceConfig;

/// File name of the setup sidecar the server ships next to the exe. Carries the
/// connection parameters (including the one-time enroll token) for a hands-off install.
#[cfg_attr(not(windows), allow(dead_code))]
pub const SETUP_FILE: &str = "kenny-agent.setup.json";

/// Connection config resolved from flags and/or the sidecar, ready to hand to `install`.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedSetup {
    pub server: String,
    pub agent_id: String,
    pub enroll_token: Option<String>,
    pub server_pubkey: Option<String>,
    pub telemetry_interval_secs: u64,
    pub service_name: String,
}

/// Resolve the effective setup config: start from the CLI flags, then fill any *unset*
/// fields from the `kenny-agent.setup.json` sidecar in `sidecar_dir` (if it exists and
/// parses). Flags always win over the sidecar. Errors clearly if `server` or `agent_id`
/// cannot be determined from either source.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn resolve_setup_config(args: &SetupArgs, sidecar_dir: &Path) -> anyhow::Result<ResolvedSetup> {
    // Load the sidecar if present; a missing file is fine (flag-driven install).
    let sidecar: Option<ServiceConfig> = {
        let path = sidecar_dir.join(SETUP_FILE);
        match std::fs::read(&path) {
            Ok(bytes) => Some(
                serde_json::from_slice::<ServiceConfig>(&bytes)
                    .map_err(|e| anyhow::anyhow!("failed to parse {}: {e}", path.display()))?,
            ),
            Err(_) => None,
        }
    };

    // Flags win; fall back to the sidecar field by field.
    let server = args
        .run
        .server
        .clone()
        .or_else(|| sidecar.as_ref().map(|s| s.server.clone()));
    let agent_id = args
        .run
        .agent_id
        .clone()
        .or_else(|| sidecar.as_ref().map(|s| s.agent_id.clone()));
    let enroll_token = args
        .run
        .enroll_token
        .clone()
        .or_else(|| sidecar.as_ref().and_then(|s| s.enroll_token.clone()));
    let server_pubkey = args
        .run
        .server_pubkey
        .clone()
        .or_else(|| sidecar.as_ref().and_then(|s| s.server_pubkey.clone()));
    let telemetry_interval_secs = args
        .run
        .telemetry_interval_secs
        .or_else(|| sidecar.as_ref().map(|s| s.telemetry_interval_secs))
        .unwrap_or(900);

    let server = server.ok_or_else(|| {
        anyhow::anyhow!(
            "no server configured: pass --server (or --agent-id) or ship a \
             {SETUP_FILE} sidecar next to the installer"
        )
    })?;
    let agent_id = agent_id.ok_or_else(|| {
        anyhow::anyhow!(
            "no agent-id configured: pass --agent-id (or --server) or ship a \
             {SETUP_FILE} sidecar next to the installer"
        )
    })?;

    Ok(ResolvedSetup {
        server,
        agent_id,
        enroll_token,
        server_pubkey,
        telemetry_interval_secs,
        service_name: args.service_name.clone(),
    })
}

#[cfg(all(not(windows), not(target_os = "linux")))]
pub fn setup(_args: crate::config::SetupArgs) -> anyhow::Result<()> {
    anyhow::bail!("`setup` is only supported on Windows and Linux");
}

#[cfg(windows)]
pub use windows_impl::setup;

#[cfg(target_os = "linux")]
pub use linux_impl::setup;

/// Linux bootstrap installer: copy the running binary into `/opt/kenny` and run
/// `install` from there so the systemd unit's `ExecStart` points at the stable path.
/// The parallel to the Windows UAC-elevated `%ProgramFiles%\kenny` copy (ADR-0035).
#[cfg(target_os = "linux")]
mod linux_impl {
    use std::os::unix::fs::PermissionsExt as _;
    use std::path::Path;

    use anyhow::Context as _;
    use tracing::info;

    use super::{resolve_setup_config, ResolvedSetup};
    use crate::config::SetupArgs;

    /// Stable install directory for the agent binary.
    const INSTALL_DIR: &str = "/opt/kenny";
    /// Installed binary path (the systemd unit's `ExecStart`).
    const INSTALL_BIN: &str = "/opt/kenny/kenny-agent";

    /// `setup` — Linux bootstrap installer entry point.
    pub fn setup(args: SetupArgs) -> anyhow::Result<()> {
        // Requires root to write /opt, /etc/kenny, and the systemd unit.
        crate::service::require_root()?;

        let exe = std::env::current_exe()?;
        let src_dir = exe
            .parent()
            .ok_or_else(|| anyhow::anyhow!("exe has no parent dir"))?
            .to_path_buf();

        let resolved = resolve_setup_config(&args, &src_dir)?;

        std::fs::create_dir_all(INSTALL_DIR).with_context(|| format!("creating {INSTALL_DIR}"))?;
        let dest = Path::new(INSTALL_BIN);
        if same_file(&exe, dest) {
            info!(path = %dest.display(), "already running from install dir; skipping copy");
        } else {
            std::fs::copy(&exe, dest)
                .with_context(|| format!("copying agent binary to {}", dest.display()))?;
            std::fs::set_permissions(dest, std::fs::Permissions::from_mode(0o755))
                .with_context(|| format!("setting mode on {}", dest.display()))?;
            info!(from = %exe.display(), to = %dest.display(), "copied agent binary");
        }

        // Run `install` from the copied binary: it persists the config to /etc/kenny and
        // renders the unit with `ExecStart` = the copied path (its own `current_exe`).
        run_install(dest, &resolved)?;
        info!("setup complete");
        Ok(())
    }

    /// Best-effort same-path check (normalizes via `canonicalize` when both exist).
    fn same_file(a: &Path, b: &Path) -> bool {
        match (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
            (Ok(a), Ok(b)) => a == b,
            _ => a == b,
        }
    }

    /// Run `install` from the copied binary, forwarding the resolved config.
    fn run_install(dest_exe: &Path, r: &ResolvedSetup) -> anyhow::Result<()> {
        let mut cmd = std::process::Command::new(dest_exe);
        cmd.arg("install")
            .arg("--server")
            .arg(&r.server)
            .arg("--agent-id")
            .arg(&r.agent_id)
            .arg("--telemetry-interval-secs")
            .arg(r.telemetry_interval_secs.to_string())
            .arg("--service-name")
            .arg(&r.service_name);
        if let Some(t) = &r.enroll_token {
            cmd.arg("--enroll-token").arg(t);
        }
        if let Some(k) = &r.server_pubkey {
            cmd.arg("--server-pubkey").arg(k);
        }
        let status = cmd
            .status()
            .with_context(|| format!("running {} install", dest_exe.display()))?;
        if !status.success() {
            anyhow::bail!("`install` failed with {status}");
        }
        Ok(())
    }
}

#[cfg(windows)]
mod windows_impl {
    use std::path::{Path, PathBuf};

    use tracing::info;

    use super::{resolve_setup_config, ResolvedSetup, SETUP_FILE};
    use crate::config::SetupArgs;

    /// `setup` — self-elevating bootstrap installer entry point.
    pub fn setup(args: SetupArgs) -> anyhow::Result<()> {
        let exe = std::env::current_exe()?;
        let src_dir = exe
            .parent()
            .ok_or_else(|| anyhow::anyhow!("exe has no parent dir"))?
            .to_path_buf();

        let resolved = resolve_setup_config(&args, &src_dir)?;

        if !is_elevated()? {
            // Not elevated: relaunch ourselves elevated to do the real work, then stop.
            info!("requesting elevation via UAC");
            let code = relaunch_elevated(&exe, &resolved)?;
            if code != 0 {
                anyhow::bail!("elevated setup exited with code {code}");
            }
            return Ok(());
        }

        // Elevated: install into %ProgramFiles%\kenny and run `install` from there.
        let dest_dir = install_dir();
        std::fs::create_dir_all(&dest_dir)?;
        let dest_exe = dest_dir.join("kenny-agent.exe");

        if same_file(&exe, &dest_exe) {
            info!(path = %dest_exe.display(), "already running from install dir; skipping copy");
        } else {
            std::fs::copy(&exe, &dest_exe)?;
            info!(from = %exe.display(), to = %dest_exe.display(), "copied agent binary");
        }

        run_install(&dest_exe, &resolved)?;

        // Token hygiene: the sidecar next to the *source* exe carries the one-time enroll
        // token; remove it once installed. Never remove the copy in the install dir.
        let src_sidecar = src_dir.join(SETUP_FILE);
        let dest_sidecar = dest_dir.join(SETUP_FILE);
        if src_sidecar.exists() && !same_file(&src_sidecar, &dest_sidecar) {
            match std::fs::remove_file(&src_sidecar) {
                Ok(()) => {
                    info!(path = %src_sidecar.display(), "removed setup sidecar (enroll token)")
                }
                Err(e) => {
                    tracing::warn!(error = %e, path = %src_sidecar.display(), "could not remove setup sidecar")
                }
            }
        }

        info!("setup complete");
        Ok(())
    }

    /// Install directory: `%ProgramFiles%\kenny` (fallback `C:\Program Files\kenny`).
    fn install_dir() -> PathBuf {
        let base = std::env::var_os("ProgramFiles")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\Program Files"));
        base.join("kenny")
    }

    /// Best-effort same-path check (normalizes via `canonicalize` when both exist).
    fn same_file(a: &Path, b: &Path) -> bool {
        match (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
            (Ok(a), Ok(b)) => a == b,
            _ => a == b,
        }
    }

    /// Run `install` from the copied binary, forwarding the resolved config.
    fn run_install(dest_exe: &Path, r: &ResolvedSetup) -> anyhow::Result<()> {
        let mut cmd = std::process::Command::new(dest_exe);
        cmd.arg("install")
            .arg("--server")
            .arg(&r.server)
            .arg("--agent-id")
            .arg(&r.agent_id)
            .arg("--telemetry-interval-secs")
            .arg(r.telemetry_interval_secs.to_string())
            .arg("--service-name")
            .arg(&r.service_name);
        if let Some(t) = &r.enroll_token {
            cmd.arg("--enroll-token").arg(t);
        }
        if let Some(k) = &r.server_pubkey {
            cmd.arg("--server-pubkey").arg(k);
        }
        let status = cmd.status()?;
        if !status.success() {
            anyhow::bail!("`install` failed with {status}");
        }
        Ok(())
    }

    /// Build the `setup ...` argument string passed to the elevated relaunch. Each value
    /// is wrapped in double quotes so paths/tokens with spaces survive.
    fn elevated_args(r: &ResolvedSetup) -> String {
        let mut s = format!(
            "setup --server \"{}\" --agent-id \"{}\" --telemetry-interval-secs \"{}\" \
             --service-name \"{}\"",
            r.server, r.agent_id, r.telemetry_interval_secs, r.service_name
        );
        if let Some(t) = &r.enroll_token {
            s.push_str(&format!(" --enroll-token \"{t}\""));
        }
        if let Some(k) = &r.server_pubkey {
            s.push_str(&format!(" --server-pubkey \"{k}\""));
        }
        s
    }

    /// Is the current process running with an elevated (admin) token?
    fn is_elevated() -> anyhow::Result<bool> {
        use windows::Win32::Foundation::{CloseHandle, HANDLE};
        use windows::Win32::Security::{
            GetTokenInformation, TokenElevation, TOKEN_ELEVATION, TOKEN_QUERY,
        };
        use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

        // SAFETY: `OpenProcessToken`/`GetTokenInformation` receive valid out-params and a
        // correctly sized buffer; the token handle is closed on every path.
        unsafe {
            let mut token = HANDLE::default();
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token)?;

            let mut elevation = TOKEN_ELEVATION::default();
            let mut ret_len = 0u32;
            let result = GetTokenInformation(
                token,
                TokenElevation,
                Some(&mut elevation as *mut _ as *mut core::ffi::c_void),
                std::mem::size_of::<TOKEN_ELEVATION>() as u32,
                &mut ret_len,
            );
            let _ = CloseHandle(token);
            result?;
            Ok(elevation.TokenIsElevated != 0)
        }
    }

    /// Relaunch this exe elevated (`runas`) to run `setup` again with an admin token.
    /// Blocks until the elevated child exits and returns its exit code.
    fn relaunch_elevated(exe: &Path, r: &ResolvedSetup) -> anyhow::Result<u32> {
        use windows::core::PCWSTR;
        use windows::Win32::Foundation::{CloseHandle, ERROR_CANCELLED, WAIT_FAILED};
        use windows::Win32::System::Threading::{
            GetExitCodeProcess, WaitForSingleObject, INFINITE,
        };
        use windows::Win32::UI::Shell::{
            ShellExecuteExW, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW,
        };
        use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

        // NUL-terminated UTF-16 buffers that must outlive the ShellExecuteExW call.
        let verb: Vec<u16> = "runas\0".encode_utf16().collect();
        let file: Vec<u16> = exe
            .as_os_str()
            .to_string_lossy()
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();
        let params: Vec<u16> = elevated_args(r)
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();

        // SAFETY: all PCWSTR pointers reference NUL-terminated buffers that outlive the
        // call; `info` is fully initialized with its `cbSize` set; the returned process
        // handle (via SEE_MASK_NOCLOSEPROCESS) is closed before returning.
        unsafe {
            let mut info = SHELLEXECUTEINFOW {
                cbSize: std::mem::size_of::<SHELLEXECUTEINFOW>() as u32,
                fMask: SEE_MASK_NOCLOSEPROCESS,
                lpVerb: PCWSTR(verb.as_ptr()),
                lpFile: PCWSTR(file.as_ptr()),
                lpParameters: PCWSTR(params.as_ptr()),
                nShow: SW_SHOWNORMAL.0,
                ..Default::default()
            };

            if let Err(e) = ShellExecuteExW(&mut info) {
                if e.code() == ERROR_CANCELLED.to_hresult() {
                    anyhow::bail!("UAC elevation was declined or failed");
                }
                return Err(anyhow::anyhow!("ShellExecuteExW (runas) failed: {e}"));
            }

            let handle = info.hProcess;
            if handle.is_invalid() {
                anyhow::bail!("elevated process handle was not returned");
            }

            let wait = WaitForSingleObject(handle, INFINITE);
            if wait == WAIT_FAILED {
                let _ = CloseHandle(handle);
                anyhow::bail!("WaitForSingleObject on elevated process failed");
            }

            let mut code = 0u32;
            let result = GetExitCodeProcess(handle, &mut code);
            let _ = CloseHandle(handle);
            result?;
            Ok(code)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Cli, Command};
    use clap::Parser;
    use std::path::PathBuf;

    /// Create a unique empty scratch directory under the system temp dir.
    fn unique_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "kenny-setup-test-{tag}-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ));
        std::fs::create_dir_all(&dir).expect("create temp dir");
        dir
    }

    /// Parse a `setup` invocation into its [`SetupArgs`].
    fn setup_args(argv: &[&str]) -> crate::config::SetupArgs {
        let mut full = vec!["kenny-agent", "setup"];
        full.extend_from_slice(argv);
        match Cli::parse_from(full).command {
            Some(Command::Setup(a)) => a,
            other => panic!("expected setup, got {other:?}"),
        }
    }

    fn write_sidecar(dir: &std::path::Path, json: &str) {
        std::fs::write(dir.join(SETUP_FILE), json).expect("write sidecar");
    }

    #[test]
    fn resolves_from_sidecar_when_flags_absent() {
        let dir = unique_dir("sidecar");
        write_sidecar(
            &dir,
            r#"{
                "server": "wss://side/agent/ws",
                "agent_id": "side-pc",
                "enroll_token": "one-time",
                "telemetry_interval_secs": 300
            }"#,
        );
        let args = setup_args(&[]);
        let r = resolve_setup_config(&args, &dir).expect("resolves from sidecar");
        assert_eq!(r.server, "wss://side/agent/ws");
        assert_eq!(r.agent_id, "side-pc");
        assert_eq!(r.enroll_token.as_deref(), Some("one-time"));
        assert_eq!(r.telemetry_interval_secs, 300);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn flags_override_sidecar() {
        let dir = unique_dir("override");
        write_sidecar(
            &dir,
            r#"{
                "server": "wss://side/agent/ws",
                "agent_id": "side-pc",
                "telemetry_interval_secs": 300
            }"#,
        );
        let args = setup_args(&[
            "--server",
            "wss://flag/agent/ws",
            "--telemetry-interval-secs",
            "42",
        ]);
        let r = resolve_setup_config(&args, &dir).expect("resolves with override");
        // Flag wins for server; agent_id falls back to the sidecar.
        assert_eq!(r.server, "wss://flag/agent/ws");
        assert_eq!(r.agent_id, "side-pc");
        assert_eq!(r.telemetry_interval_secs, 42);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn defaults_interval_to_900_when_unset() {
        let dir = unique_dir("default-interval");
        let args = setup_args(&["--server", "wss://x/agent/ws", "--agent-id", "pc"]);
        let r = resolve_setup_config(&args, &dir).expect("resolves from flags");
        assert_eq!(r.telemetry_interval_secs, 900);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn errors_when_server_and_agent_id_missing() {
        // Empty dir (no sidecar), no flags.
        let dir = unique_dir("missing");
        let args = setup_args(&[]);
        let err = resolve_setup_config(&args, &dir).expect_err("must error without config");
        let msg = err.to_string();
        assert!(msg.contains("server") || msg.contains("agent-id"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn errors_when_only_server_present() {
        let dir = unique_dir("only-server");
        let args = setup_args(&["--server", "wss://x/agent/ws"]);
        let err = resolve_setup_config(&args, &dir).expect_err("missing agent-id must error");
        assert!(err.to_string().contains("agent-id"));
        std::fs::remove_dir_all(&dir).ok();
    }
}

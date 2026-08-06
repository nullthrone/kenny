//! Windows service lifecycle: `install`, `uninstall`, and `run-service`.
//!
//! All real logic is `#[cfg(windows)]`. On other platforms these are stubs that
//! report `unsupported`, keeping Linux builds/tests green.
//!
//! `install` registers an auto-starting service (with restart-on-failure recovery)
//! that launches `kenny-agent run-service`, and persists the connection config to a
//! JSON file next to the exe so `run-service` can read it. `run-service` registers
//! the SCM control handler and runs the tunnel with a graceful stop signal.

/// Re-exported for the screen-capture path, which relaunches a dead tray on demand.
#[cfg(windows)]
pub(crate) use windows_impl::launch_tray_in_active_session;
#[cfg(windows)]
pub use windows_impl::{install, run_service, uninstall};

// Linux uses the systemd-unit lifecycle (parallel to the Windows SCM path, ADR-0031).
// `require_root` is also reused by the Linux `setup` bootstrap.
#[cfg(target_os = "linux")]
pub(crate) use linux_impl::require_root;
#[cfg(target_os = "linux")]
pub use linux_impl::{install, run_service, uninstall};

// Every remaining non-Windows target (other unix such as macOS/BSD, and any non-unix
// non-windows target) keeps the `unsupported` stub so the crate still builds there.
#[cfg(all(not(windows), not(target_os = "linux")))]
mod stub {
    use crate::config::{InstallArgs, RunServiceArgs, UninstallArgs};

    /// `install` is supported on Windows and Linux only.
    pub fn install(_args: InstallArgs) -> anyhow::Result<()> {
        anyhow::bail!("`install` is only supported on Windows and Linux");
    }

    /// `uninstall` is supported on Windows and Linux only.
    pub fn uninstall(_args: UninstallArgs) -> anyhow::Result<()> {
        anyhow::bail!("`uninstall` is only supported on Windows and Linux");
    }

    /// `run-service` is supported on Windows and Linux only.
    pub fn run_service(_args: RunServiceArgs) -> anyhow::Result<()> {
        anyhow::bail!("`run-service` is only supported on Windows and Linux");
    }
}

#[cfg(all(not(windows), not(target_os = "linux")))]
pub use stub::{install, run_service, uninstall};

/// Persisted service configuration (written by `install`, read by `run-service`).
/// Only constructed on Windows, but defined unconditionally so the shape is shared.
#[cfg_attr(not(windows), allow(dead_code))]
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ServiceConfig {
    pub server: String,
    pub agent_id: String,
    /// Legacy per-agent bearer token (migration window only). Optional from v0.8.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token: Option<String>,
    /// Pinned server Ed25519 public key (standard base64) for the signature path.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub server_pubkey: Option<String>,
    /// One-time enrollment token, persisted so the service's first run can enroll.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enroll_token: Option<String>,
    pub telemetry_interval_secs: u64,
}

/// File name of the persisted config, stored next to the executable.
#[cfg_attr(not(windows), allow(dead_code))]
pub const CONFIG_FILE: &str = "kenny-agent.config.json";

/// Linux systemd-unit lifecycle: the parallel to the Windows SCM path (ADR-0031).
///
/// `install` writes the connection config to `/etc/kenny`, renders a systemd unit into
/// `/etc/systemd/system`, and `enable --now`s it. `run-service` reads the persisted
/// config and runs the tunnel until `SIGTERM` (systemd stop). There is no SCM and no
/// tray/session-0 model here (Linux has no session-0 construct — see ADR-0031).
#[cfg(target_os = "linux")]
mod linux_impl {
    use anyhow::Context as _;
    use tracing::info;

    use super::{ServiceConfig, CONFIG_FILE};
    use crate::config::{Config, InstallArgs, RunServiceArgs, UninstallArgs};

    /// Connection config directory (persisted `ServiceConfig`).
    const ETC_DIR: &str = "/etc/kenny";
    /// Update-stable state directory (key + kill-switch control file).
    const STATE_DIR: &str = "/var/lib/kenny";
    /// Rolling-log directory.
    const LOG_DIR: &str = "/var/log/kenny";
    /// System unit directory for the rendered service file.
    const SYSTEMD_DIR: &str = "/etc/systemd/system";

    /// The systemd unit file name for `service_name` (e.g. `kenny-agent.service`).
    fn unit_file_name(service_name: &str) -> String {
        format!("{service_name}.service")
    }

    /// Absolute path of the rendered unit for `service_name`.
    fn unit_path(service_name: &str) -> std::path::PathBuf {
        std::path::Path::new(SYSTEMD_DIR).join(unit_file_name(service_name))
    }

    /// Render the systemd unit for the agent. Pure and unit-tested.
    ///
    /// `exec_path` is the absolute path to the installed `kenny-agent` binary. The unit
    /// runs `run-service`, which reads the persisted config from `/etc/kenny`.
    pub(crate) fn render_unit(cfg: &ServiceConfig, exec_path: &str) -> String {
        format!(
            "[Unit]\n\
             Description=kenny agent ({agent_id} -> {server})\n\
             After=network-online.target\n\
             Wants=network-online.target\n\
             \n\
             [Service]\n\
             Type=simple\n\
             ExecStart={exec_path} run-service\n\
             Restart=on-failure\n\
             RestartSec=5\n\
             User=root\n\
             \n\
             [Install]\n\
             WantedBy=multi-user.target\n",
            agent_id = cfg.agent_id,
            server = cfg.server,
            exec_path = exec_path,
        )
    }

    /// The current process's effective uid, parsed from `/proc/self/status` (no libc
    /// dependency). The `Uid:` line is `real  effective  saved  fs`.
    fn effective_uid() -> anyhow::Result<u32> {
        let status =
            std::fs::read_to_string("/proc/self/status").context("reading /proc/self/status")?;
        for line in status.lines() {
            if let Some(rest) = line.strip_prefix("Uid:") {
                let mut fields = rest.split_whitespace();
                let _real = fields.next();
                if let Some(euid) = fields.next() {
                    return euid.parse::<u32>().context("parsing effective uid");
                }
            }
        }
        anyhow::bail!("could not determine effective uid from /proc/self/status");
    }

    /// Require root (euid 0); otherwise bail with a clear "run as root" message.
    pub(crate) fn require_root() -> anyhow::Result<()> {
        if effective_uid()? != 0 {
            anyhow::bail!("this command must be run as root (try: sudo kenny-agent ...)");
        }
        Ok(())
    }

    /// Run `systemctl <args>`, turning a missing binary or a down system bus (no systemd
    /// as PID 1) into a clear, non-panicking error.
    fn systemctl(args: &[&str]) -> anyhow::Result<()> {
        let output = std::process::Command::new("systemctl")
            .args(args)
            .output()
            .map_err(|e| {
                if e.kind() == std::io::ErrorKind::NotFound {
                    anyhow::anyhow!(
                        "`systemctl` not found: kenny-agent's Linux service lifecycle requires \
                         systemd"
                    )
                } else {
                    anyhow::anyhow!("failed to run `systemctl {}`: {e}", args.join(" "))
                }
            })?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            anyhow::bail!(
                "`systemctl {}` failed ({}): {} — systemd must be running as PID 1",
                args.join(" "),
                output.status,
                stderr.trim(),
            );
        }
        Ok(())
    }

    /// `install` — persist the config, render the unit, and enable it via systemd.
    pub fn install(args: InstallArgs) -> anyhow::Result<()> {
        require_root()?;

        let cfg = ServiceConfig {
            server: args.run.server.clone(),
            agent_id: args.run.agent_id.clone(),
            token: args.run.token.clone(),
            server_pubkey: args.run.server_pubkey.clone(),
            enroll_token: args.run.enroll_token.clone(),
            telemetry_interval_secs: args.run.telemetry_interval_secs,
        };

        // Ensure the FHS directories exist: config in /etc, state/key/control in
        // /var/lib, logs in /var/log. The latter two are best-effort so a locked-down
        // host can still install (paths fall back to temp_dir at runtime if absent).
        std::fs::create_dir_all(ETC_DIR).with_context(|| format!("creating {ETC_DIR}"))?;
        let _ = std::fs::create_dir_all(STATE_DIR);
        let _ = std::fs::create_dir_all(LOG_DIR);

        let cfg_path = std::path::Path::new(ETC_DIR).join(CONFIG_FILE);
        std::fs::write(&cfg_path, serde_json::to_vec_pretty(&cfg)?)
            .with_context(|| format!("writing {}", cfg_path.display()))?;
        info!(path = %cfg_path.display(), "wrote service config");

        let exec = std::env::current_exe()?;
        let exec_str = exec.to_string_lossy();
        let unit = render_unit(&cfg, &exec_str);
        let path = unit_path(&args.service_name);
        std::fs::write(&path, unit)
            .with_context(|| format!("writing systemd unit {}", path.display()))?;
        info!(path = %path.display(), "wrote systemd unit");

        systemctl(&["daemon-reload"])?;
        let unit_name = unit_file_name(&args.service_name);
        systemctl(&["enable", "--now", &unit_name])?;
        info!(unit = %unit_name, "service installed and started");
        Ok(())
    }

    /// `uninstall` — disable + stop the unit, remove the file, reload systemd. Best-effort.
    pub fn uninstall(args: UninstallArgs) -> anyhow::Result<()> {
        require_root()?;

        let unit_name = unit_file_name(&args.service_name);
        if let Err(e) = systemctl(&["disable", "--now", &unit_name]) {
            tracing::warn!(error = %e, "systemctl disable --now failed; continuing with removal");
        }

        let path = unit_path(&args.service_name);
        if path.exists() {
            std::fs::remove_file(&path).with_context(|| format!("removing {}", path.display()))?;
            info!(path = %path.display(), "removed systemd unit");
        }

        let _ = systemctl(&["daemon-reload"]);
        info!(unit = %unit_name, "service removed");
        Ok(())
    }

    /// `run-service` — read the persisted config and run the tunnel until `SIGTERM`.
    ///
    /// No SCM: systemd owns process lifecycle (auto-restart via `Restart=on-failure`).
    /// A `SIGTERM` (systemctl stop) drives a graceful shutdown through `run_until`.
    pub fn run_service(args: RunServiceArgs) -> anyhow::Result<()> {
        let config = resolve_run_config(&args)?;
        let runtime = tokio::runtime::Runtime::new()?;
        runtime.block_on(async move {
            let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);
            tokio::spawn(async move {
                use tokio::signal::unix::{signal, SignalKind};
                if let Ok(mut term) = signal(SignalKind::terminate()) {
                    term.recv().await;
                    let _ = shutdown_tx.send(true);
                }
            });
            crate::tunnel::run_until(config, shutdown_rx).await;
        });
        info!("service stopped cleanly");
        Ok(())
    }

    /// Merge explicit flags over the persisted `/etc/kenny` config (flags win).
    fn resolve_run_config(args: &RunServiceArgs) -> anyhow::Result<Config> {
        let mut server = None;
        let mut agent_id = None;
        let mut token = None;
        let mut server_pubkey = None;
        let mut enroll_token = None;
        let mut interval = None;

        let cfg_path = std::path::Path::new(ETC_DIR).join(CONFIG_FILE);
        if let Ok(bytes) = std::fs::read(&cfg_path) {
            if let Ok(persisted) = serde_json::from_slice::<ServiceConfig>(&bytes) {
                server = Some(persisted.server);
                agent_id = Some(persisted.agent_id);
                token = persisted.token;
                server_pubkey = persisted.server_pubkey;
                enroll_token = persisted.enroll_token;
                interval = Some(persisted.telemetry_interval_secs);
            }
        }

        if let Some(s) = &args.run.server {
            server = Some(s.clone());
        }
        if let Some(a) = &args.run.agent_id {
            agent_id = Some(a.clone());
        }
        if let Some(t) = &args.run.token {
            token = Some(t.clone());
        }
        if let Some(k) = &args.run.server_pubkey {
            server_pubkey = Some(k.clone());
        }
        if let Some(e) = &args.run.enroll_token {
            enroll_token = Some(e.clone());
        }
        if let Some(i) = args.run.telemetry_interval_secs {
            interval = Some(i);
        }

        Ok(Config {
            server: server.ok_or_else(|| anyhow::anyhow!("no server in config file or flags"))?,
            agent_id: agent_id
                .ok_or_else(|| anyhow::anyhow!("no agent-id in config file or flags"))?,
            token,
            server_pubkey,
            enroll_token,
            telemetry_interval_secs: interval.unwrap_or(900),
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn sample_cfg() -> ServiceConfig {
            ServiceConfig {
                server: "wss://kenny.example.com/agent/ws".to_string(),
                agent_id: "linux-box".to_string(),
                token: None,
                server_pubkey: Some("A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=".to_string()),
                enroll_token: None,
                telemetry_interval_secs: 900,
            }
        }

        #[test]
        fn render_unit_contains_required_directives() {
            let unit = render_unit(&sample_cfg(), "/opt/kenny/kenny-agent");
            assert!(
                unit.contains("ExecStart=/opt/kenny/kenny-agent run-service"),
                "unit must launch run-service from the exec path:\n{unit}"
            );
            assert!(
                unit.contains("Restart=on-failure"),
                "missing restart policy:\n{unit}"
            );
            assert!(
                unit.contains("RestartSec=5"),
                "missing restart delay:\n{unit}"
            );
            assert!(
                unit.contains("Type=simple"),
                "missing service type:\n{unit}"
            );
            assert!(unit.contains("User=root"), "missing User=root:\n{unit}");
            assert!(
                unit.contains("After=network-online.target"),
                "missing network ordering:\n{unit}"
            );
            assert!(
                unit.contains("WantedBy=multi-user.target"),
                "missing install target:\n{unit}"
            );
            // The agent identity is surfaced in the description for operators.
            assert!(
                unit.contains("linux-box"),
                "missing agent id in description:\n{unit}"
            );
        }

        #[test]
        fn unit_path_is_under_systemd_dir() {
            let p = unit_path("kenny-agent");
            assert_eq!(
                p,
                std::path::Path::new("/etc/systemd/system/kenny-agent.service")
            );
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use std::ffi::OsString;
    use std::path::PathBuf;
    use std::time::Duration;

    use tracing::{error, info};
    use windows_service::service::{
        ServiceAccess, ServiceErrorControl, ServiceExitCode, ServiceInfo, ServiceStartType,
        ServiceState, ServiceStatus, ServiceType,
    };
    use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};
    use windows_service::{define_windows_service, service::ServiceControl};

    use super::{ServiceConfig, CONFIG_FILE};
    use crate::config::{Config, InstallArgs, RunServiceArgs, UninstallArgs};

    /// Service type for a standalone process.
    const SERVICE_TYPE: ServiceType = ServiceType::OWN_PROCESS;

    /// Directory holding the running executable (config + staged binaries live here).
    fn exe_dir() -> anyhow::Result<PathBuf> {
        let exe = std::env::current_exe()?;
        Ok(exe
            .parent()
            .ok_or_else(|| anyhow::anyhow!("exe has no parent dir"))?
            .to_path_buf())
    }

    /// Path to the persisted service config next to the exe.
    fn config_path() -> anyhow::Result<PathBuf> {
        Ok(exe_dir()?.join(CONFIG_FILE))
    }

    /// `install` — register the auto-start service and persist its config.
    pub fn install(args: InstallArgs) -> anyhow::Result<()> {
        // Persist the connection config so `run-service` can read it.
        let cfg = ServiceConfig {
            server: args.run.server.clone(),
            agent_id: args.run.agent_id.clone(),
            token: args.run.token.clone(),
            server_pubkey: args.run.server_pubkey.clone(),
            enroll_token: args.run.enroll_token.clone(),
            telemetry_interval_secs: args.run.telemetry_interval_secs,
        };
        let path = config_path()?;
        std::fs::write(&path, serde_json::to_vec_pretty(&cfg)?)?;
        info!(path = %path.display(), "wrote service config");

        let manager = ServiceManager::local_computer(
            None::<&str>,
            ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE,
        )?;

        let exe = std::env::current_exe()?;
        // run-service reads config from disk; pass --service-name for stop reporting.
        let launch_args = vec![
            OsString::from("run-service"),
            OsString::from("--service-name"),
            OsString::from(&args.service_name),
        ];

        let info = ServiceInfo {
            name: OsString::from(&args.service_name),
            display_name: OsString::from("kenny agent"),
            service_type: SERVICE_TYPE,
            start_type: ServiceStartType::AutoStart,
            error_control: ServiceErrorControl::Normal,
            executable_path: exe.clone(),
            launch_arguments: launch_args,
            dependencies: vec![],
            account_name: None, // LocalSystem
            account_password: None,
        };

        let service =
            manager.create_service(&info, ServiceAccess::CHANGE_CONFIG | ServiceAccess::START)?;
        service.set_description("kenny outbound-tunnel remote-admin and telemetry agent")?;

        // Restart-on-failure recovery: restart after 5s for the first failures.
        use windows_service::service::ServiceAction;
        use windows_service::service::ServiceActionType;
        use windows_service::service::{ServiceFailureActions, ServiceFailureResetPeriod};
        let actions = ServiceFailureActions {
            reset_period: ServiceFailureResetPeriod::After(Duration::from_secs(86400)),
            reboot_msg: None,
            command: None,
            actions: Some(vec![
                ServiceAction {
                    action_type: ServiceActionType::Restart,
                    delay: Duration::from_secs(5),
                },
                ServiceAction {
                    action_type: ServiceActionType::Restart,
                    delay: Duration::from_secs(5),
                },
                ServiceAction {
                    action_type: ServiceActionType::Restart,
                    delay: Duration::from_secs(30),
                },
            ]),
        };
        // Need DACL access to set failure actions; reopen with the right rights.
        let svc2 = manager.open_service(
            &args.service_name,
            ServiceAccess::CHANGE_CONFIG | ServiceAccess::START | ServiceAccess::STOP,
        )?;
        svc2.update_failure_actions(actions)?;

        // Start it now.
        svc2.start::<&str>(&[])?;
        info!(service = %args.service_name, "service installed and started");

        // Set up the local remote-control kill switch: a shared control file the user's
        // tray can write and the LocalSystem service can read, plus a logon autostart for
        // the tray itself. Best-effort: the service + gating work even if this fails (the
        // tray can be started manually), so don't abort the install. See ADR-0011.
        if let Err(e) = setup_tray_kill_switch(&exe) {
            tracing::warn!(error = %e, "could not set up tray kill switch; configure it manually");
        }

        // Show the tray now, in the installing user's interactive session. The
        // HKLM\Run entry only fires at the *next* logon, so without this the icon
        // (and the screen-capture responder, ADR-0018) would be missing until then.
        // Best-effort: a single-instance guard in the tray prevents a duplicate icon.
        match std::process::Command::new(&exe).arg("tray").spawn() {
            Ok(_) => info!("started tray helper in the current session"),
            Err(e) => {
                tracing::warn!(error = %e, "could not start tray now; it will start at next logon")
            }
        }

        Ok(())
    }

    /// Registry key holding per-machine logon autostart entries.
    const RUN_KEY: &str = r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run";
    /// Value name for the tray autostart entry.
    const TRAY_RUN_VALUE: &str = "kenny-agent-tray";

    /// Prepare the shared control file's directory (writable by the interactive user,
    /// readable by the service) and register the tray to auto-start at logon.
    fn setup_tray_kill_switch(exe: &std::path::Path) -> anyhow::Result<()> {
        // The control file lives in a shared, cross-session location (ProgramData).
        // Create its directory and grant Authenticated Users *modify* so a standard
        // user's tray can flip the switch while the LocalSystem service reads it.
        if let Some(dir) = crate::control::control_path().parent() {
            std::fs::create_dir_all(dir)?;
            let status = std::process::Command::new("icacls")
                .arg(dir)
                .arg("/grant")
                // S-1-5-11 = Authenticated Users; (OI)(CI)M = inherit + modify.
                .arg("*S-1-5-11:(OI)(CI)M")
                .status()?;
            if !status.success() {
                anyhow::bail!("icacls grant on {} failed", dir.display());
            }
        }

        // Auto-start the tray helper in the interactive user session at logon.
        let command = format!("\"{}\" tray", exe.display());
        let status = std::process::Command::new("reg")
            .args(["add", RUN_KEY, "/v", TRAY_RUN_VALUE, "/t", "REG_SZ", "/d"])
            .arg(&command)
            .arg("/f")
            .status()?;
        if !status.success() {
            anyhow::bail!("registering tray autostart failed");
        }
        info!("registered tray autostart and control-file permissions");
        Ok(())
    }

    /// Remove the tray's logon autostart entry (best effort).
    fn remove_tray_autostart() {
        let _ = std::process::Command::new("reg")
            .args(["delete", RUN_KEY, "/v", TRAY_RUN_VALUE, "/f"])
            .status();
    }

    /// (Re)launch the tray helper into the active interactive session.
    ///
    /// The service runs in session 0; a plain `spawn()` child would land there too —
    /// invisible to the logged-in user. To put the tray on the real desktop we take the
    /// active console session's user token (`WTSGetActiveConsoleSessionId` +
    /// `WTSQueryUserToken`) and `CreateProcessAsUserW` on `winsta0\default`.
    ///
    /// The tray takes a single-instance mutex, so this is a no-op when one is already
    /// running and is therefore safe to call unconditionally on every service start. That
    /// is what brings the tray back after the user closes it (Task Manager) or it crashes:
    /// a service restart relaunches it, and the screen-capture path relaunches it on demand
    /// when the tray's pipe is missing (see [`crate::screencap_ipc::capture_via_tray`]).
    /// Best-effort — before anyone logs in there is no token, a normal non-fatal outcome
    /// that the logon autostart covers.
    ///
    /// An `Err` whose message mentions no active console session / user token means nobody
    /// is logged in; callers use that to distinguish "nobody there" from "tray crashed".
    pub(crate) fn launch_tray_in_active_session() -> anyhow::Result<()> {
        use anyhow::Context as _;
        use windows::core::PWSTR;
        use windows::Win32::Foundation::{CloseHandle, HANDLE};
        use windows::Win32::System::RemoteDesktop::{
            WTSGetActiveConsoleSessionId, WTSQueryUserToken,
        };
        use windows::Win32::System::Threading::{
            CreateProcessAsUserW, CREATE_NO_WINDOW, PROCESS_INFORMATION, STARTUPINFOW,
        };

        let exe = std::env::current_exe()?;

        // SAFETY: every call is passed valid, owned buffers / out-params; the user token
        // and the new process/thread handles are closed on all paths.
        unsafe {
            let session_id = WTSGetActiveConsoleSessionId();
            // 0xFFFFFFFF means no session is currently attached to the physical console.
            if session_id == u32::MAX {
                anyhow::bail!("no active console session");
            }

            let mut token = HANDLE::default();
            WTSQueryUserToken(session_id, &mut token)
                .context("WTSQueryUserToken (nobody logged in?)")?;

            // CreateProcessAsUserW may write into the command-line buffer, so it must be a
            // writable, NUL-terminated UTF-16 vector that outlives the call.
            let mut cmdline: Vec<u16> = format!("\"{}\" tray", exe.display())
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            // Target the interactive window station/desktop so the icon actually appears.
            let mut desktop: Vec<u16> = "winsta0\\default"
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();

            let si = STARTUPINFOW {
                cb: std::mem::size_of::<STARTUPINFOW>() as u32,
                lpDesktop: PWSTR(desktop.as_mut_ptr()),
                ..Default::default()
            };
            let mut pi = PROCESS_INFORMATION::default();

            let result = CreateProcessAsUserW(
                Some(token),
                None,
                Some(PWSTR(cmdline.as_mut_ptr())),
                None,
                None,
                false,
                CREATE_NO_WINDOW,
                None,
                None,
                &si,
                &mut pi,
            );

            let _ = CloseHandle(token);
            result.context("CreateProcessAsUserW")?;
            // We don't wait on the tray; release our handles to the new process/thread.
            let _ = CloseHandle(pi.hProcess);
            let _ = CloseHandle(pi.hThread);
        }
        Ok(())
    }

    /// `uninstall` — stop (best effort) and delete the service.
    pub fn uninstall(args: UninstallArgs) -> anyhow::Result<()> {
        let manager = ServiceManager::local_computer(None::<&str>, ServiceManagerAccess::CONNECT)?;
        let service = manager.open_service(
            &args.service_name,
            ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::DELETE,
        )?;

        // Best-effort stop, then wait briefly for it to actually stop.
        if let Ok(status) = service.query_status() {
            if status.current_state != ServiceState::Stopped {
                let _ = service.stop();
                for _ in 0..20 {
                    std::thread::sleep(Duration::from_millis(500));
                    if let Ok(s) = service.query_status() {
                        if s.current_state == ServiceState::Stopped {
                            break;
                        }
                    }
                }
            }
        }
        service.delete()?;
        // Stop auto-starting the tray. The control file is left in place so the user's
        // on/off choice survives a reinstall; remove it manually to reset to default-on.
        remove_tray_autostart();

        // Best-effort: remove the %ProgramFiles%\kenny install dir, but never the
        // directory we're executing from (a portable/in-place run must not delete itself).
        let dir = install_dir();
        match exe_dir() {
            Ok(cur) if cur.starts_with(&dir) => {
                info!(dir = %dir.display(), "running from install dir; leaving it in place");
            }
            _ => {
                if dir.exists() {
                    if let Err(e) = std::fs::remove_dir_all(&dir) {
                        tracing::warn!(error = %e, dir = %dir.display(), "could not remove install dir");
                    } else {
                        info!(dir = %dir.display(), "removed install dir");
                    }
                }
            }
        }

        info!(service = %args.service_name, "service removed");
        Ok(())
    }

    /// Install directory used by `setup`: `%ProgramFiles%\kenny`
    /// (fallback `C:\Program Files\kenny`).
    fn install_dir() -> PathBuf {
        let base = std::env::var_os("ProgramFiles")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\Program Files"));
        base.join("kenny")
    }

    // The SCM calls `ffi_service_main`; `define_windows_service` generates it and
    // forwards to `service_main`.
    define_windows_service!(ffi_service_main, service_main);

    /// `run-service` — dispatch to the SCM. This blocks until the service stops.
    pub fn run_service(args: RunServiceArgs) -> anyhow::Result<()> {
        // Stash the resolved config + service name where `service_main` can read it.
        let cfg = resolve_config(&args)?;
        SERVICE_STATE
            .set(ResolvedState {
                config: cfg,
                service_name: args.service_name.clone(),
            })
            .map_err(|_| anyhow::anyhow!("service state already initialized"))?;

        use windows_service::service_dispatcher;
        service_dispatcher::start(&args.service_name, ffi_service_main)?;
        Ok(())
    }

    /// Merge explicit flags over the persisted config file.
    fn resolve_config(args: &RunServiceArgs) -> anyhow::Result<Config> {
        // Start from the persisted file if present.
        let mut server = None;
        let mut agent_id = None;
        let mut token = None;
        let mut server_pubkey = None;
        let mut enroll_token = None;
        let mut interval = None;
        if let Ok(bytes) = std::fs::read(config_path()?) {
            if let Ok(persisted) = serde_json::from_slice::<ServiceConfig>(&bytes) {
                server = Some(persisted.server);
                agent_id = Some(persisted.agent_id);
                token = persisted.token;
                server_pubkey = persisted.server_pubkey;
                enroll_token = persisted.enroll_token;
                interval = Some(persisted.telemetry_interval_secs);
            }
        }
        // Explicit flags win.
        if let Some(s) = &args.run.server {
            server = Some(s.clone());
        }
        if let Some(a) = &args.run.agent_id {
            agent_id = Some(a.clone());
        }
        if let Some(t) = &args.run.token {
            token = Some(t.clone());
        }
        if let Some(k) = &args.run.server_pubkey {
            server_pubkey = Some(k.clone());
        }
        if let Some(e) = &args.run.enroll_token {
            enroll_token = Some(e.clone());
        }
        if let Some(i) = args.run.telemetry_interval_secs {
            interval = Some(i);
        }
        Ok(Config {
            server: server.ok_or_else(|| anyhow::anyhow!("no server in config file or flags"))?,
            agent_id: agent_id
                .ok_or_else(|| anyhow::anyhow!("no agent-id in config file or flags"))?,
            token,
            server_pubkey,
            enroll_token,
            telemetry_interval_secs: interval.unwrap_or(900),
        })
    }

    /// Config resolved before SCM dispatch, read inside `service_main`.
    struct ResolvedState {
        config: Config,
        service_name: String,
    }

    static SERVICE_STATE: std::sync::OnceLock<ResolvedState> = std::sync::OnceLock::new();

    /// The actual service body, called by the generated `ffi_service_main`.
    fn service_main(_args: Vec<OsString>) {
        if let Err(e) = run_service_inner() {
            error!(error = %e, "service exited with error");
        }
    }

    fn run_service_inner() -> anyhow::Result<()> {
        let state = SERVICE_STATE
            .get()
            .ok_or_else(|| anyhow::anyhow!("service state not initialized"))?;

        // Shutdown signal driven by the SCM control handler.
        let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

        let event_handler = move |control_event| -> ServiceControlHandlerResult {
            match control_event {
                ServiceControl::Stop | ServiceControl::Preshutdown => {
                    let _ = shutdown_tx.send(true);
                    ServiceControlHandlerResult::NoError
                }
                ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
                _ => ServiceControlHandlerResult::NotImplemented,
            }
        };

        let status_handle = service_control_handler::register(&state.service_name, event_handler)?;

        // Report Running.
        status_handle.set_service_status(ServiceStatus {
            service_type: SERVICE_TYPE,
            current_state: ServiceState::Running,
            controls_accepted: windows_service::service::ServiceControlAccept::STOP
                | windows_service::service::ServiceControlAccept::PRESHUTDOWN,
            exit_code: ServiceExitCode::Win32(0),
            checkpoint: 0,
            wait_hint: Duration::default(),
            process_id: None,
        })?;

        // (Re)launch the tray into the interactive session. A service start/restart is the
        // recovery path for a tray the user closed or that crashed; the tray's
        // single-instance guard makes this harmless when one is already running.
        if let Err(e) = launch_tray_in_active_session() {
            info!(error = %e, "did not launch tray on service start (likely nobody logged in)");
        }

        // Run the tunnel on a tokio runtime until the shutdown signal fires.
        let runtime = tokio::runtime::Runtime::new()?;
        let config = state.config.clone();
        runtime.block_on(async move {
            crate::tunnel::run_until(config, shutdown_rx).await;
        });

        // Report Stopped so the SCM (and the updater helper) can proceed.
        status_handle.set_service_status(ServiceStatus {
            service_type: SERVICE_TYPE,
            current_state: ServiceState::Stopped,
            controls_accepted: windows_service::service::ServiceControlAccept::empty(),
            exit_code: ServiceExitCode::Win32(0),
            checkpoint: 0,
            wait_hint: Duration::default(),
            process_id: None,
        })?;
        info!("service stopped cleanly");
        Ok(())
    }
}

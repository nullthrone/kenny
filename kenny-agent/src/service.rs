//! Windows service lifecycle: `install`, `uninstall`, and `run-service`.
//!
//! All real logic is `#[cfg(windows)]`. On other platforms these are stubs that
//! report `unsupported`, keeping Linux builds/tests green.
//!
//! `install` registers an auto-starting service (with restart-on-failure recovery)
//! that launches `kenny-agent run-service`, and persists the connection config to a
//! JSON file next to the exe so `run-service` can read it. `run-service` registers
//! the SCM control handler and runs the tunnel with a graceful stop signal.

#[cfg(windows)]
pub use windows_impl::{install, run_service, uninstall};

#[cfg(not(windows))]
mod stub {
    use crate::config::{InstallArgs, RunServiceArgs, UninstallArgs};

    /// `install` is Windows-only.
    pub fn install(_args: InstallArgs) -> anyhow::Result<()> {
        anyhow::bail!("`install` is only supported on Windows");
    }

    /// `uninstall` is Windows-only.
    pub fn uninstall(_args: UninstallArgs) -> anyhow::Result<()> {
        anyhow::bail!("`uninstall` is only supported on Windows");
    }

    /// `run-service` is Windows-only.
    pub fn run_service(_args: RunServiceArgs) -> anyhow::Result<()> {
        anyhow::bail!("`run-service` is only supported on Windows");
    }
}

#[cfg(not(windows))]
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
    /// a service restart relaunches it. Best-effort — before anyone logs in there is no
    /// token, a normal non-fatal outcome that the logon autostart covers.
    fn launch_tray_in_active_session() -> anyhow::Result<()> {
        use anyhow::Context as _;
        use windows::core::PWSTR;
        use windows::Win32::Foundation::{CloseHandle, FALSE, HANDLE};
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
                token,
                None,
                PWSTR(cmdline.as_mut_ptr()),
                None,
                None,
                FALSE,
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
        info!(service = %args.service_name, "service removed");
        Ok(())
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

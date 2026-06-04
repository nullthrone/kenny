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
    pub token: String,
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
            executable_path: exe,
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
        let mut interval = None;
        if let Ok(bytes) = std::fs::read(config_path()?) {
            if let Ok(persisted) = serde_json::from_slice::<ServiceConfig>(&bytes) {
                server = Some(persisted.server);
                agent_id = Some(persisted.agent_id);
                token = Some(persisted.token);
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
        if let Some(i) = args.run.telemetry_interval_secs {
            interval = Some(i);
        }
        Ok(Config {
            server: server.ok_or_else(|| anyhow::anyhow!("no server in config file or flags"))?,
            agent_id: agent_id
                .ok_or_else(|| anyhow::anyhow!("no agent-id in config file or flags"))?,
            token: token.ok_or_else(|| anyhow::anyhow!("no token in config file or flags"))?,
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

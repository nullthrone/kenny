//! kenny-agent entry point.
//!
//! Opens one outbound WebSocket to kenny-server, registers, then serves forwarded
//! tool requests and pushes telemetry snapshots. See ../docs/protocol.md for the
//! wire contract and ../docs/adr/ for architecture decisions.
//!
//! Subcommands:
//! * (none) / `run`  — foreground tunnel (reconnects forever). Default; the
//!   historical `--server/--agent-id/--token` invocation maps here unchanged.
//! * `install`       — register the Windows service (Windows only).
//! * `uninstall`     — remove the Windows service (Windows only).
//! * `run-service`   — SCM entry point with graceful stop (Windows only).
//! * `finish-update` — hidden updater helper that swaps the binary (Windows only).

mod config;
mod control;
mod dispatch;
mod handlers;
mod protocol;
mod service;
mod telemetry;
mod tray;
mod tunnel;
mod util;

use clap::Parser;
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

use config::{Cli, Command};
pub use protocol::PROTOCOL_VERSION;

/// Agent version, **led by the GitHub release tag** at build time (see `build.rs`);
/// falls back to the Cargo package version for dev/CI builds.
pub const BUILD_VERSION: &str = env!("KENNY_BUILD_VERSION");

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    // Declare per-monitor DPI awareness before any window/screen work so
    // `screen_capture` grabs the full native resolution on HiDPI displays
    // instead of a virtualized (scaled/cropped) view. Best-effort: harmless if
    // the awareness context is already set.
    set_dpi_awareness();

    let cli = Cli::parse();

    match cli.command {
        // Explicit `run` subcommand.
        Some(Command::Run(run)) => run_tunnel(run),

        // No subcommand: default to running the tunnel with the top-level flags.
        // This preserves `kenny-agent --server ... --agent-id ... --token ...`.
        None => match cli.run.into_run_args() {
            Ok(run) => run_tunnel(run),
            Err(msg) => {
                error!("{msg}");
                eprintln!("error: {msg}\n\nRun `kenny-agent --help` for usage.");
                std::process::exit(2);
            }
        },

        // Service management (Windows; stubs elsewhere).
        Some(Command::Install(args)) => {
            if let Err(e) = service::install(args) {
                error!(error = %e, "install failed");
                std::process::exit(1);
            }
        }
        Some(Command::Uninstall(args)) => {
            if let Err(e) = service::uninstall(args) {
                error!(error = %e, "uninstall failed");
                std::process::exit(1);
            }
        }
        Some(Command::RunService(args)) => {
            if let Err(e) = service::run_service(args) {
                error!(error = %e, "run-service failed");
                std::process::exit(1);
            }
        }

        // Tray helper (Windows; no-op stub elsewhere).
        Some(Command::Tray) => {
            if let Err(e) = tray::run() {
                error!(error = %e, "tray failed");
                std::process::exit(1);
            }
        }

        // Hidden updater helper.
        Some(Command::FinishUpdate(args)) => {
            #[cfg(windows)]
            {
                if let Err(e) = handlers::agent_update::run_finish_update(
                    &args.service,
                    &args.new,
                    &args.target,
                ) {
                    error!(error = %e, "finish-update failed");
                    std::process::exit(1);
                }
            }
            #[cfg(not(windows))]
            {
                let _ = args;
                error!("finish-update is only supported on Windows");
                std::process::exit(1);
            }
        }
    }
}

/// Declare per-monitor-v2 DPI awareness for the process (Windows only).
///
/// Without this, GDI screen captures on HiDPI monitors are scaled down to the
/// virtualized resolution. Failure is non-fatal (e.g. the context is already
/// set via manifest), so we only log it.
#[cfg(windows)]
fn set_dpi_awareness() {
    use windows::Win32::UI::HiDpi::{
        SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    };
    // SAFETY: no pointers involved; the call is self-contained.
    let result =
        unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };
    if let Err(e) = result {
        info!(error = %e, "SetProcessDpiAwarenessContext failed (likely already set)");
    }
}

/// No-op DPI awareness setup off Windows.
#[cfg(not(windows))]
fn set_dpi_awareness() {}

/// Run the foreground reconnecting tunnel (never returns under normal operation).
fn run_tunnel(config: config::Config) {
    info!(
        agent_id = %config.agent_id,
        server = %config.server,
        protocol = PROTOCOL_VERSION,
        version = BUILD_VERSION,
        "kenny-agent starting"
    );

    let runtime = match tokio::runtime::Runtime::new() {
        Ok(rt) => rt,
        Err(e) => {
            error!(error = %e, "failed to start tokio runtime");
            std::process::exit(1);
        }
    };
    // `run` reconnects forever and never returns.
    runtime.block_on(async move { tunnel::run(config).await });
}

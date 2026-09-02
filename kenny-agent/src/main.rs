//! kenny-agent entry point.
//!
//! Opens one outbound WebSocket to kenny-server, registers, then serves forwarded
//! tool requests and pushes telemetry snapshots. See ../docs/protocol.md for the
//! wire contract and ../docs/adr/ for architecture decisions.
//!
//! Subcommands:
//! * (none) / `run`  — foreground tunnel (reconnects forever). Default; the
//!   historical `--server/--agent-id/--token` invocation maps here unchanged.
//! * `setup`         — self-elevating bootstrap installer: elevate via UAC, copy the
//!   binary into %ProgramFiles%\kenny, and run `install` from there (Windows only).
//! * `install`       — register the Windows service (Windows only).
//! * `uninstall`     — remove the Windows service (Windows only).
//! * `run-service`   — SCM entry point with graceful stop (Windows only).
//! * `finish-update` — hidden updater helper that swaps the binary (Windows only).

mod coexist;
mod config;
mod control;
mod dispatch;
#[cfg(test)]
mod fuzz_tests;
mod handlers;
mod ipc;
mod keys;
mod log_forward;
mod policy;
mod protocol;
mod screencap_ipc;
mod service;
mod session_launch_ipc;
mod setup;
mod telemetry;
mod tray;
mod tunnel;
mod util;

use std::sync::OnceLock;

use clap::Parser;
use tracing::{error, info};
use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::EnvFilter;

use config::{Cli, Command};
use log_forward::ForwardLayer;
pub use protocol::PROTOCOL_VERSION;

/// Agent version, **led by the GitHub release tag** at build time (see `build.rs`);
/// falls back to the Cargo package version for dev/CI builds.
pub const BUILD_VERSION: &str = env!("KENNY_BUILD_VERSION");

/// Release channel this binary was built from (`stable`/`dev`, see `build.rs` and
/// ADR-0048). `stable` unless CI set `KENNY_AGENT_CHANNEL=dev` (the `release-dev.yml`
/// build) — every other build, including local `cargo build`, gets `stable`.
pub const BUILD_CHANNEL: &str = env!("KENNY_BUILD_CHANNEL");

/// Base file name of the rolling agent log. `tracing_appender`'s daily rotation
/// appends a `.YYYY-MM-DD` suffix, so on disk the files are
/// `kenny-agent.log.2026-06-06` etc. Shared with the tray so its "open logs"
/// menu item can locate the newest one.
pub const LOG_FILE_PREFIX: &str = "kenny-agent.log";

/// Keeps the non-blocking file-appender worker alive for the whole process.
/// Dropping the [`WorkerGuard`] flushes and stops the writer thread, so it must
/// outlive every log call.
static FILE_LOG_GUARD: OnceLock<WorkerGuard> = OnceLock::new();

fn main() {
    init_tracing();

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
        Some(Command::Setup(args)) => {
            if let Err(e) = setup::setup(args) {
                error!(error = %e, "setup failed");
                eprintln!("error: {e}");
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

/// Initialize the process tracing subscriber as a layered registry:
///
/// * a stderr `fmt` layer (foreground behavior, unchanged),
/// * a daily-rolling, non-blocking file layer (best-effort), and
/// * the [`ForwardLayer`] that ships records to the server.
///
/// The env filter (`RUST_LOG`, default `info`) governs all layers; the forward
/// layer additionally honors `KENNY_LOG_FORWARD_LEVEL`.
fn init_tracing() {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let stderr_layer = tracing_subscriber::fmt::layer().with_writer(std::io::stderr);

    // Best-effort daily file layer; if the log dir can't be created, skip it
    // rather than failing to start.
    let file_layer = match log_dir() {
        Some(dir) if std::fs::create_dir_all(&dir).is_ok() => {
            let appender = tracing_appender::rolling::daily(&dir, LOG_FILE_PREFIX);
            let (writer, guard) = tracing_appender::non_blocking(appender);
            // Stash the guard for the process lifetime.
            let _ = FILE_LOG_GUARD.set(guard);
            Some(
                tracing_subscriber::fmt::layer()
                    .with_ansi(false)
                    .with_writer(writer),
            )
        }
        _ => None,
    };

    tracing_subscriber::registry()
        .with(env_filter)
        .with(stderr_layer)
        .with(file_layer)
        .with(ForwardLayer)
        .init();
}

/// Directory for rolling agent log files.
///
/// On Windows: `%PROGRAMDATA%\kenny\logs` (falling back to `C:\ProgramData`).
/// Elsewhere: a portable temp-dir location so dev/CI builds work.
#[cfg(windows)]
pub fn log_dir() -> Option<std::path::PathBuf> {
    let base = std::env::var_os("PROGRAMDATA")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from(r"C:\ProgramData"));
    Some(base.join("kenny").join("logs"))
}

/// Log directory on Linux: `/var/log/kenny` when it exists and is writable (the
/// systemd install creates it), else a portable temp-dir path so dev/CI runs work.
///
/// The gate is "dir exists & writable", **not** "am I root": a root `cargo test` in a
/// sandbox without `/var/log/kenny` must still fall back to `temp_dir()`.
#[cfg(target_os = "linux")]
pub fn log_dir() -> Option<std::path::PathBuf> {
    let fhs = std::path::PathBuf::from("/var/log/kenny");
    if dir_is_writable(&fhs) {
        Some(fhs)
    } else {
        Some(std::env::temp_dir().join("kenny").join("logs"))
    }
}

/// Portable log directory used off Windows and Linux (macOS/BSD/etc).
#[cfg(all(not(windows), not(target_os = "linux")))]
pub fn log_dir() -> Option<std::path::PathBuf> {
    Some(std::env::temp_dir().join("kenny").join("logs"))
}

/// Whether `dir` exists and is writable by the current effective user, probed by
/// creating and removing a temporary file. Used to gate the FHS log path.
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
    // The server host for the `agent_update` allowlist is recorded inside
    // `tunnel::run_until`, which every entry point (foreground and service) funnels
    // through, so it does not need to be set here.
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

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;

    #[test]
    fn log_dir_falls_back_to_temp_without_fhs_dir() {
        // In CI/sandbox the FHS log dir does not exist, so `log_dir` must fall back to
        // the portable temp path regardless of whether the test runs as root.
        if !std::path::Path::new("/var/log/kenny").exists() {
            assert_eq!(
                log_dir(),
                Some(std::env::temp_dir().join("kenny").join("logs"))
            );
        }
    }

    #[test]
    fn absent_dir_is_never_writable() {
        assert!(!dir_is_writable(std::path::Path::new(
            "/var/log/kenny-definitely-not-present"
        )));
    }
}

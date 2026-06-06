//! Command-line / environment configuration (clap-derived).
//!
//! The agent supports subcommands (`run`, `install`, `uninstall`, `run-service`,
//! and the hidden `finish-update`), but the historical invocation with **no
//! subcommand** and the run flags at the top level MUST keep working and select the
//! `run` path. The integration test in `kenny-server` depends on
//! `kenny-agent --server <ws> --agent-id <id> --token <tok> [--telemetry-interval-secs N]`.
//!
//! We achieve "default to run when no subcommand is given" by making the run flags
//! global/top-level (a flattened [`RunArgs`]) and leaving the subcommand optional.

use clap::{Args, Parser, Subcommand};

/// The connection parameters needed to run the tunnel.
///
/// These are the flags that have always been accepted at the top level. They are
/// flattened into the top-level [`Cli`] so `kenny-agent --server ... --agent-id ...`
/// keeps working with no subcommand, and are also accepted by `run`/`run-service`.
#[derive(Debug, Clone, Args)]
pub struct RunArgs {
    /// WebSocket URL of kenny-server, e.g. `wss://kenny.example.com/agent/ws`.
    #[arg(long, env = "KENNY_SERVER")]
    pub server: String,

    /// Stable identifier for this agent (e.g. `example-pc`).
    #[arg(long = "agent-id", env = "KENNY_AGENT_ID")]
    pub agent_id: String,

    /// Per-agent API key presented in the `register` frame.
    #[arg(long, env = "KENNY_TOKEN")]
    pub token: String,

    /// Telemetry push interval in seconds (default 900 per ADR-0007).
    #[arg(
        long = "telemetry-interval-secs",
        env = "KENNY_TELEMETRY_INTERVAL_SECS",
        default_value_t = 900
    )]
    pub telemetry_interval_secs: u64,
}

/// Backwards-compatible alias: the tunnel runtime config is exactly [`RunArgs`].
pub type Config = RunArgs;

/// Default Windows service name used by `install`/`uninstall`/`run-service`.
pub const SERVICE_NAME: &str = "kenny-agent";

/// Top-level CLI. The run flags live here (top level) so the no-subcommand form
/// keeps working; `command` is optional and defaults to running the tunnel.
#[derive(Debug, Parser)]
#[command(name = "kenny-agent", version = env!("KENNY_BUILD_VERSION"), about)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Option<Command>,

    /// Run flags, accepted at the top level (no subcommand) for compatibility.
    /// All fields are optional at parse time; when the run path is selected (no
    /// subcommand) we validate the required ones via [`TopLevelRunArgs::into_run_args`].
    #[command(flatten)]
    pub run: TopLevelRunArgs,
}

/// Like [`RunArgs`] but every field is optional, so that subcommands which do **not**
/// need connection flags (e.g. `uninstall`) can be invoked without them. When the
/// run path is selected we validate that the required fields are present.
#[derive(Debug, Clone, Args)]
pub struct TopLevelRunArgs {
    #[arg(long, env = "KENNY_SERVER")]
    pub server: Option<String>,

    #[arg(long = "agent-id", env = "KENNY_AGENT_ID")]
    pub agent_id: Option<String>,

    #[arg(long, env = "KENNY_TOKEN")]
    pub token: Option<String>,

    #[arg(
        long = "telemetry-interval-secs",
        env = "KENNY_TELEMETRY_INTERVAL_SECS"
    )]
    pub telemetry_interval_secs: Option<u64>,
}

impl TopLevelRunArgs {
    /// Resolve the top-level (optional) flags into a concrete [`RunArgs`], erroring
    /// if a required connection flag is missing.
    pub fn into_run_args(self) -> Result<RunArgs, String> {
        Ok(RunArgs {
            server: self.server.ok_or("--server is required to run the agent")?,
            agent_id: self
                .agent_id
                .ok_or("--agent-id is required to run the agent")?,
            token: self.token.ok_or("--token is required to run the agent")?,
            telemetry_interval_secs: self.telemetry_interval_secs.unwrap_or(900),
        })
    }
}

/// Subcommands. All are optional; omitting the subcommand runs the tunnel.
#[derive(Debug, Subcommand)]
pub enum Command {
    /// Run the agent in the foreground (reconnecting tunnel). This is the default
    /// when no subcommand is given.
    Run(RunArgs),

    /// Install the agent as an auto-starting Windows service (Windows only).
    Install(InstallArgs),

    /// Remove the Windows service (Windows only).
    Uninstall(UninstallArgs),

    /// Entry point invoked by the Windows Service Control Manager (Windows only).
    #[command(name = "run-service")]
    RunService(RunServiceArgs),

    /// Run the system-tray helper that lets the person at the endpoint switch remote
    /// control on/off (Windows only; a no-op stub elsewhere). Runs in the interactive
    /// user session and is auto-started at logon by `install`.
    Tray,

    /// Hidden helper: wait for the service to stop, swap binaries, restart it.
    /// Spawned detached by the `agent_update` handler (Windows only).
    #[command(name = "finish-update", hide = true)]
    FinishUpdate(FinishUpdateArgs),
}

/// Args for `install`. Connection flags are required (written into the service
/// configuration); the service name is optional.
#[derive(Debug, Clone, Args)]
pub struct InstallArgs {
    #[command(flatten)]
    pub run: RunArgs,

    /// Windows service name to register.
    #[arg(long = "service-name", default_value = SERVICE_NAME)]
    pub service_name: String,
}

/// Args for `uninstall`.
#[derive(Debug, Clone, Args)]
pub struct UninstallArgs {
    /// Windows service name to remove.
    #[arg(long = "service-name", default_value = SERVICE_NAME)]
    pub service_name: String,
}

/// Args for `run-service`. Connection flags are optional here because `install`
/// persists them to a config file next to the exe; explicit flags override it.
#[derive(Debug, Clone, Args)]
pub struct RunServiceArgs {
    #[command(flatten)]
    pub run: TopLevelRunArgs,

    /// Windows service name (used for graceful-stop reporting).
    #[arg(long = "service-name", default_value = SERVICE_NAME)]
    pub service_name: String,
}

/// Args for the hidden `finish-update` helper.
#[derive(Debug, Clone, Args)]
pub struct FinishUpdateArgs {
    /// Windows service to stop, swap, and restart.
    #[arg(long)]
    pub service: String,

    /// Path to the freshly-staged `.new.exe`.
    #[arg(long)]
    pub new: String,

    /// Path to the running binary to replace.
    #[arg(long)]
    pub target: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn parses_required_args_and_default_interval() {
        // Historical no-subcommand invocation must still parse and select run.
        let cli = Cli::parse_from([
            "kenny-agent",
            "--server",
            "wss://example/agent/ws",
            "--agent-id",
            "example-pc",
            "--token",
            "secret",
        ]);
        assert!(cli.command.is_none(), "no subcommand => default run path");
        let run = cli.run.into_run_args().expect("required flags present");
        assert_eq!(run.server, "wss://example/agent/ws");
        assert_eq!(run.agent_id, "example-pc");
        assert_eq!(run.telemetry_interval_secs, 900);
    }

    #[test]
    fn no_subcommand_with_interval_selects_run() {
        let cli = Cli::parse_from([
            "kenny-agent",
            "--server",
            "ws://x/agent/ws",
            "--agent-id",
            "dev",
            "--token",
            "dev-token",
            "--telemetry-interval-secs",
            "2",
        ]);
        assert!(cli.command.is_none());
        let run = cli.run.into_run_args().unwrap();
        assert_eq!(run.telemetry_interval_secs, 2);
    }

    #[test]
    fn explicit_run_subcommand_parses() {
        let cli = Cli::parse_from([
            "kenny-agent",
            "run",
            "--server",
            "ws://x/agent/ws",
            "--agent-id",
            "dev",
            "--token",
            "dev-token",
        ]);
        match cli.command {
            Some(Command::Run(run)) => {
                assert_eq!(run.agent_id, "dev");
                assert_eq!(run.telemetry_interval_secs, 900);
            }
            other => panic!("expected run subcommand, got {other:?}"),
        }
    }

    #[test]
    fn uninstall_needs_no_connection_flags() {
        let cli = Cli::parse_from(["kenny-agent", "uninstall"]);
        match cli.command {
            Some(Command::Uninstall(a)) => assert_eq!(a.service_name, SERVICE_NAME),
            other => panic!("expected uninstall, got {other:?}"),
        }
    }
}

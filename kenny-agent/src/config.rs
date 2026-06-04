//! Command-line / environment configuration (clap-derived).

use clap::Parser;

/// kenny-agent: outbound-tunnel remote-admin and telemetry agent.
#[derive(Debug, Clone, Parser)]
#[command(name = "kenny-agent", version, about)]
pub struct Config {
    /// WebSocket URL of kenny-server, e.g. `wss://kenny.example.com/agent/ws`.
    #[arg(long, env = "KENNY_SERVER")]
    pub server: String,

    /// Stable identifier for this agent (e.g. `papa-pc`).
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

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[test]
    fn parses_required_args_and_default_interval() {
        let cfg = Config::parse_from([
            "kenny-agent",
            "--server",
            "wss://example/agent/ws",
            "--agent-id",
            "papa-pc",
            "--token",
            "secret",
        ]);
        assert_eq!(cfg.server, "wss://example/agent/ws");
        assert_eq!(cfg.agent_id, "papa-pc");
        assert_eq!(cfg.telemetry_interval_secs, 900);
    }
}

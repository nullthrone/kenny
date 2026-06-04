//! kenny-agent entry point.
//!
//! Opens one outbound WebSocket to kenny-server, registers, then serves forwarded
//! tool requests and pushes telemetry snapshots. See ../docs/protocol.md for the
//! wire contract and ../docs/adr/ for architecture decisions.

mod config;
mod dispatch;
mod handlers;
mod protocol;
mod telemetry;
mod tunnel;
mod util;

use clap::Parser;
use tracing::info;
use tracing_subscriber::EnvFilter;

pub use protocol::PROTOCOL_VERSION;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let config = config::Config::parse();
    info!(
        agent_id = %config.agent_id,
        server = %config.server,
        protocol = PROTOCOL_VERSION,
        version = env!("CARGO_PKG_VERSION"),
        "kenny-agent starting"
    );

    // `run` reconnects forever and never returns.
    tunnel::run(config).await
}

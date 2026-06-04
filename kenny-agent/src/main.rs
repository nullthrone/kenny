//! kenny-agent entry point.
//!
//! Opens one outbound WebSocket to kenny-server, registers, then serves forwarded
//! tool requests and pushes telemetry snapshots. See ../docs/protocol.md for the wire
//! contract and ../docs/adr/ for architecture decisions.

/// Wire-protocol version implemented by this binary (see docs/protocol.md § Versioning).
pub const PROTOCOL_VERSION: &str = "0.1";

fn main() {
    // Implemented by the agent-dev subagent (connect loop, dispatch, telemetry scheduler).
    println!("kenny-agent {} (protocol {PROTOCOL_VERSION})", env!("CARGO_PKG_VERSION"));
}

//! Telemetry push scheduler.
//!
//! Runs concurrently with the tunnel I/O loop: every `interval` it collects a full
//! snapshot and hands a [`Frame::Telemetry`] to the tunnel's outbound channel. One
//! snapshot is collected immediately on start so the server sees fresh data right
//! after register. See ADR-0007.

use std::time::Duration;

use tokio::sync::mpsc;
use tracing::{debug, warn};

use crate::protocol::Frame;

/// Drive the periodic telemetry push until the outbound channel closes.
///
/// `out` is the tunnel's sender for frames to write to the WebSocket.
pub async fn run(agent_id: String, interval: Duration, out: mpsc::Sender<Frame>) {
    let mut ticker = tokio::time::interval(interval);
    // Skip burst of missed ticks if collection runs long.
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        ticker.tick().await;
        // Collection runs real WMI/PowerShell/CIM on Windows and can take several
        // seconds. Run it off the async runtime via `spawn_blocking` so it never stalls
        // the tunnel's read loop, heartbeat replies, or in-flight tool responses (which
        // share this task's runtime). The first tick still fires immediately, so the
        // server still sees fresh data right after register.
        let collect_agent_id = agent_id.clone();
        let telemetry = match tokio::task::spawn_blocking(move || {
            crate::telemetry::collect(&collect_agent_id, &[])
        })
        .await
        {
            Ok(t) => t,
            Err(e) => {
                warn!(error = %e, "telemetry collection task failed; skipping this tick");
                continue;
            }
        };
        debug!(sections = telemetry.snapshot.len(), "pushing telemetry");
        if out.send(Frame::Telemetry(telemetry)).await.is_err() {
            warn!("telemetry channel closed; scheduler stopping");
            break;
        }
    }
}

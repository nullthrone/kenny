//! Telemetry push scheduler.
//!
//! Runs concurrently with the tunnel I/O loop: it collects a full snapshot, hands a
//! [`Frame::Telemetry`] to the tunnel's outbound channel, then waits before the next
//! push. One snapshot is collected immediately on start so the server sees fresh data
//! right after register. See ADR-0007.
//!
//! The wait between pushes is the configured `interval` normally, but stretches while a
//! protected game is running (anti-cheat coexistence, ADR-0035) so the process/port
//! enumeration in the snapshot backs off — see [`crate::coexist::telemetry_delay`].

use std::time::Duration;

use tokio::sync::mpsc;
use tracing::{debug, warn};

use crate::protocol::Frame;

/// Drive the periodic telemetry push until the outbound channel closes.
///
/// `out` is the tunnel's sender for frames to write to the WebSocket.
pub async fn run(agent_id: String, interval: Duration, out: mpsc::Sender<Frame>) {
    loop {
        // Collection runs real WMI/PowerShell/CIM on Windows and can take several
        // seconds. Run it off the async runtime via `spawn_blocking` so it never stalls
        // the tunnel's read loop, heartbeat replies, or in-flight tool responses (which
        // share this task's runtime). The first pass runs immediately, so the server
        // sees fresh data right after register.
        let collect_agent_id = agent_id.clone();
        match tokio::task::spawn_blocking(move || crate::telemetry::collect(&collect_agent_id, &[]))
            .await
        {
            Ok(telemetry) => {
                debug!(sections = telemetry.snapshot.len(), "pushing telemetry");
                if out.send(Frame::Telemetry(telemetry)).await.is_err() {
                    warn!("telemetry channel closed; scheduler stopping");
                    break;
                }
            }
            Err(e) => {
                warn!(error = %e, "telemetry collection task failed; skipping this tick");
            }
        }
        // Wait until the next push. Stretched while a protected game is active.
        tokio::time::sleep(crate::coexist::telemetry_delay(interval)).await;
    }
}

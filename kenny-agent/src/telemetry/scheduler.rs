//! Telemetry push scheduler.
//!
//! Runs concurrently with the tunnel I/O loop: it collects a full snapshot, hands a
//! [`Frame::Telemetry`] to the tunnel's outbound channel, then waits before the next
//! push. The first snapshot is collected as soon as `first_push_after` resolves — the
//! tunnel resolves it once the session's first `policy` frame is applied, or after a
//! short bound — so the server sees fresh data right after register, already gated by
//! `policy.collect` (ADR-0069). See ADR-0007.
//!
//! The wait between pushes is the configured `interval` normally, but stretches while a
//! protected game is running (anti-cheat coexistence, ADR-0035) so the process/port
//! enumeration in the snapshot backs off — see [`crate::coexist::telemetry_delay`].

use std::future::Future;
use std::time::Duration;

use tokio::sync::mpsc;
use tracing::{debug, warn};

use crate::protocol::Frame;

/// Drive the periodic telemetry push until the outbound channel closes.
///
/// `out` is the tunnel's sender for frames to write to the WebSocket. Nothing is
/// collected until `first_push_after` resolves.
pub async fn run(
    agent_id: String,
    interval: Duration,
    out: mpsc::Sender<Frame>,
    first_push_after: impl Future<Output = ()>,
) {
    first_push_after.await;
    loop {
        // Collection runs real WMI/PowerShell/CIM on Windows and can take several
        // seconds. Run it off the async runtime via `spawn_blocking` so it never stalls
        // the tunnel's read loop, heartbeat replies, or in-flight tool responses (which
        // share this task's runtime). The first pass runs as soon as `first_push_after`
        // resolves, so the server sees fresh data right after register.
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The first snapshot is not taken before `first_push_after` resolves — the property
    /// that lets the tunnel apply the session's `policy` frame before the first push.
    #[tokio::test]
    async fn first_push_waits_for_the_gate() {
        let (out, mut rx) = mpsc::channel(4);
        let (release, released) = tokio::sync::oneshot::channel::<()>();
        let scheduler = tokio::spawn(run(
            "test".to_string(),
            Duration::from_secs(3600),
            out,
            async move {
                let _ = released.await;
            },
        ));

        let early = tokio::time::timeout(Duration::from_millis(300), rx.recv()).await;
        assert!(
            early.is_err(),
            "a snapshot was pushed before the gate opened"
        );

        release.send(()).unwrap();
        let frame = tokio::time::timeout(Duration::from_secs(60), rx.recv())
            .await
            .expect("no snapshot after the gate opened")
            .expect("channel open");
        assert!(matches!(frame, Frame::Telemetry(_)));
        scheduler.abort();
    }
}

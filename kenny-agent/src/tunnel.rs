//! WebSocket tunnel client.
//!
//! Opens one outbound connection to `kenny-server`, sends `register`, then runs
//! three concurrent concerns over the single socket:
//!
//! * **read loop** — decode inbound frames; dispatch `request` → `response`, reply
//!   to `ping` with `pong`.
//! * **telemetry scheduler** — push `telemetry` frames on a timer.
//! * **heartbeat** — send periodic `ping` so the server's missed-interval logic
//!   keeps us online.
//!
//! All outbound frames funnel through an mpsc channel into a single writer task, so
//! dispatch, telemetry, and heartbeat never contend on the sink. On disconnect the
//! whole stack tears down and [`run`] reconnects with exponential backoff. See
//! ADR-0003 (self-built tunnel) and ADR-0004 (agent dials out).

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, error, info, warn};

use crate::config::Config;
use crate::dispatch;
use crate::protocol::{Frame, Register, RegisterMeta};
use crate::telemetry::scheduler;

/// Initial reconnect backoff.
const BACKOFF_MIN: Duration = Duration::from_secs(1);
/// Maximum reconnect backoff.
const BACKOFF_MAX: Duration = Duration::from_secs(60);
/// Heartbeat ping interval.
const HEARTBEAT: Duration = Duration::from_secs(30);
/// Bound on the outbound frame channel.
const OUTBOX_CAP: usize = 64;
/// Maximum log records drained into frames per wakeup.
const LOG_BATCH: usize = 64;

/// Connect-and-serve forever, reconnecting with exponential backoff. Never returns.
///
/// This is the foreground (`run`) path. For the Windows service path that must stop
/// gracefully on an SCM control event, use [`run_until`] with a shutdown signal.
pub async fn run(config: Config) -> ! {
    // A receiver that never fires: the foreground agent reconnects forever.
    let (_tx, never) = tokio::sync::watch::channel(false);
    run_until(config, never).await;
    // `run_until` only returns when `shutdown` fires, which `never` never does.
    unreachable!("foreground tunnel run returned without a shutdown signal")
}

/// Connect-and-serve with exponential backoff until `shutdown` flips to `true`.
///
/// The foreground [`run`] path passes a receiver that never fires (so it loops
/// forever); the Windows service passes a [`watch::Receiver`](tokio::sync::watch)
/// driven by the SCM stop handler so the loop ends and the process exits cleanly.
pub async fn run_until(config: Config, mut shutdown: tokio::sync::watch::Receiver<bool>) {
    // Honor a shutdown that was requested before we ever connected.
    if *shutdown.borrow() {
        return;
    }
    let mut backoff = BACKOFF_MIN;
    loop {
        // Use a separate watcher clone for the select arm so `serve_once` can hold
        // its own mutable borrow of `shutdown` concurrently.
        let mut watcher = shutdown.clone();
        tokio::select! {
            biased;
            _ = watcher.changed() => {
                if *watcher.borrow() {
                    info!("shutdown signalled; stopping tunnel");
                    return;
                }
            }
            outcome = serve_once(&config, &mut shutdown) => {
                match outcome {
                    Ok(()) => {
                        // A clean close may be a graceful shutdown; check before reconnecting.
                        if *shutdown.borrow() {
                            return;
                        }
                        info!("tunnel closed cleanly; reconnecting");
                        backoff = BACKOFF_MIN;
                    }
                    Err(e) => {
                        warn!(error = %e, backoff_secs = backoff.as_secs(), "tunnel error; backing off");
                    }
                }
            }
        }
        // Back off, but wake immediately if asked to shut down.
        let mut watcher = shutdown.clone();
        tokio::select! {
            biased;
            _ = watcher.changed() => {
                if *watcher.borrow() {
                    info!("shutdown signalled during backoff; stopping tunnel");
                    return;
                }
            }
            _ = tokio::time::sleep(backoff) => {}
        }
        backoff = (backoff * 2).min(BACKOFF_MAX);
    }
}

/// One full session: connect, register, serve until the socket drops or shutdown.
async fn serve_once(
    config: &Config,
    shutdown: &mut tokio::sync::watch::Receiver<bool>,
) -> anyhow::Result<()> {
    info!(server = %config.server, "connecting");
    let (ws, _resp) = connect_async(&config.server).await?;
    let (mut sink, mut stream) = ws.split();

    // Single outbound channel; one writer task owns the sink.
    let (tx, mut rx) = mpsc::channel::<Frame>(OUTBOX_CAP);

    // Register immediately.
    tx.send(Frame::Register(Register {
        agent_id: config.agent_id.clone(),
        token: config.token.clone(),
        meta: RegisterMeta {
            hostname: crate::util::hostname(),
            os: crate::util::os_family().to_string(),
            version: crate::BUILD_VERSION.to_string(),
        },
    }))
    .await
    .ok();

    // Writer task: serialize frames and push them onto the socket.
    let writer = tokio::spawn(async move {
        while let Some(frame) = rx.recv().await {
            let text = match serde_json::to_string(&frame) {
                Ok(t) => t,
                Err(e) => {
                    error!(error = %e, "failed to serialize outbound frame");
                    continue;
                }
            };
            if let Err(e) = sink.send(Message::Text(text)).await {
                warn!(error = %e, "write failed; closing session");
                break;
            }
        }
    });

    // Telemetry scheduler.
    let telemetry_tx = tx.clone();
    let agent_id = config.agent_id.clone();
    let interval = Duration::from_secs(config.telemetry_interval_secs);
    let telemetry = tokio::spawn(scheduler::run(agent_id, interval, telemetry_tx));

    // Heartbeat.
    let heartbeat_tx = tx.clone();
    let heartbeat = tokio::spawn(async move {
        let mut ticker = tokio::time::interval(HEARTBEAT);
        ticker.tick().await; // consume immediate tick
        loop {
            ticker.tick().await;
            if heartbeat_tx.send(Frame::Ping).await.is_err() {
                break;
            }
        }
    });

    // Log forwarding: drain buffered `tracing` records into `log` frames. Woken by
    // the forwarder's `Notify`. Uses `try_send` so a full outbound channel drops
    // the record rather than blocking the writer (telemetry/responses win). On a
    // closed channel the session is ending, so stop.
    let log_tx = tx.clone();
    let log_agent_id = config.agent_id.clone();
    let log_drain = tokio::spawn(async move {
        loop {
            crate::log_forward::notify().notified().await;
            for ev in crate::log_forward::drain_into(LOG_BATCH) {
                match log_tx.try_send(ev.into_frame(&log_agent_id)) {
                    Ok(()) => {}
                    Err(mpsc::error::TrySendError::Full(_)) => {
                        // Outbound is saturated; drop this record and move on.
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => return,
                }
            }
        }
    });

    // Read loop: dispatch requests, answer pings. Runs until the socket closes or a
    // shutdown is requested (service stop).
    let read_result = tokio::select! {
        biased;
        _ = shutdown.changed() => {
            if *shutdown.borrow() {
                info!("shutdown signalled; ending session");
            }
            Ok(())
        }
        r = read_loop(&mut stream, &tx) => r,
    };

    // Tear down the session's tasks.
    drop(tx);
    telemetry.abort();
    heartbeat.abort();
    log_drain.abort();
    let _ = writer.await;

    read_result
}

/// Process inbound messages until the stream ends.
async fn read_loop<S>(stream: &mut S, tx: &mpsc::Sender<Frame>) -> anyhow::Result<()>
where
    S: StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin,
{
    while let Some(msg) = stream.next().await {
        match msg? {
            Message::Text(text) => {
                handle_text(&text, tx).await;
            }
            Message::Binary(_) => {
                debug!("ignoring unexpected binary message");
            }
            Message::Ping(payload) => {
                // Reply at the WS-protocol level via a pong frame in our protocol;
                // tungstenite also auto-pongs control frames, but the wire contract
                // models ping/pong as JSON frames too.
                debug!(bytes = payload.len(), "ws ping");
            }
            Message::Pong(_) => {}
            Message::Close(_) => {
                info!("server closed the connection");
                break;
            }
            Message::Frame(_) => {}
        }
    }
    Ok(())
}

/// Decode one JSON text frame and act on it.
async fn handle_text(text: &str, tx: &mpsc::Sender<Frame>) {
    let frame: Frame = match serde_json::from_str(text) {
        Ok(f) => f,
        Err(e) => {
            warn!(error = %e, "dropping undecodable frame");
            return;
        }
    };
    match frame {
        Frame::Request(req) => {
            let response = dispatch::handle(req).await;
            if tx.send(Frame::Response(response)).await.is_err() {
                warn!("outbound channel closed while sending response");
            }
        }
        Frame::Ping => {
            let _ = tx.send(Frame::Pong).await;
        }
        Frame::Pong => {}
        // The agent never expects to receive these; ignore defensively.
        Frame::Register(_) | Frame::Response(_) | Frame::Telemetry(_) | Frame::Log(_) => {
            debug!("ignoring frame not addressed to the agent");
        }
    }
}

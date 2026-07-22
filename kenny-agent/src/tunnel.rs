//! WebSocket tunnel client.
//!
//! Opens one outbound connection to `kenny-server`, sends `register`, then runs
//! three concurrent concerns over the single socket:
//!
//! * **read loop** — decode inbound frames; reply to `ping` with `pong` and spawn
//!   each `request` onto its own task so a slow tool never stalls the socket (which
//!   would starve the server's WebSocket keepalive and get us disconnected).
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
use crate::keys::{self, AgentKey};
use crate::protocol::{Frame, Register, RegisterMeta, PROTOCOL_VERSION};
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
    // Record the server host for the always-on safety guard's `agent_update` allowlist.
    // Both the foreground (`run`) and the Windows service (`run-service`) paths funnel
    // through here, so the host can never be left unset by a forgetful entry point. The
    // service path previously never set it, so `agent_update` from the configured server
    // was wrongly refused as "host not allowlisted". `set_server_url` is idempotent.
    crate::policy::set_server_url(&config.server);

    // Honor a shutdown that was requested before we ever connected.
    if *shutdown.borrow() {
        return;
    }

    // Anti-cheat coexistence poller (ADR-0039): watch for a protected game's anti-cheat
    // process and flip the process-global "game active" flag. Spawned once here (not per
    // session) so its state survives tunnel reconnects; it exits when shutdown fires.
    spawn_coexist_poller(shutdown.clone());

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

/// Spawn the anti-cheat coexistence poller (ADR-0039).
///
/// On a timer it refreshes the process **name list** (the cheapest `sysinfo` refresh, so
/// it opens no handle against the game) and updates the global "protected game running"
/// flag consulted by the dispatch gate, the telemetry scheduler, and the process/port
/// collectors. A no-op when the feature is disabled. The refresh runs on the blocking
/// pool so it never stalls the async runtime, and the task exits when `shutdown` fires.
fn spawn_coexist_poller(mut shutdown: tokio::sync::watch::Receiver<bool>) {
    if !crate::coexist::enabled() {
        return;
    }
    let poll = crate::coexist::poll_interval();
    tokio::spawn(async move {
        loop {
            // Prime immediately on the first iteration so the flag is correct before the
            // first tool call arrives, then re-poll every `poll`.
            let _ = tokio::task::spawn_blocking(|| {
                let mut sys = sysinfo::System::new();
                crate::coexist::poll_once(&mut sys);
            })
            .await;
            tokio::select! {
                biased;
                _ = shutdown.changed() => {
                    if *shutdown.borrow() {
                        return;
                    }
                }
                _ = tokio::time::sleep(poll) => {}
            }
        }
    });
}

/// One full session: connect, register, serve until the socket drops or shutdown.
async fn serve_once(
    config: &Config,
    shutdown: &mut tokio::sync::watch::Receiver<bool>,
) -> anyhow::Result<()> {
    // The signature path (v0.8 mutual auth, ADR-0023) is selected whenever a pinned
    // server public key is configured. Otherwise fall back to the legacy token path for
    // the migration window.
    let signature_path = config.server_pubkey.is_some();

    // Load (or first-run generate) the agent's Ed25519 key, and — on first contact —
    // enroll its public key with the server. Both are no-ops on the legacy token path.
    let agent_key = if signature_path {
        let key = AgentKey::load_or_generate()?;
        maybe_enroll(config, &key)?;
        Some(key)
    } else {
        None
    };
    let pinned_server_key = match &config.server_pubkey {
        Some(b64) => Some(keys::parse_server_pubkey(b64)?),
        None => None,
    };

    info!(server = %config.server, signature_path, "connecting");
    let (ws, _resp) = connect_async(&config.server).await?;
    let (mut sink, mut stream) = ws.split();

    // Single outbound channel; one writer task owns the sink.
    let (tx, mut rx) = mpsc::channel::<Frame>(OUTBOX_CAP);

    // Build `register`. On the signature path we put `protocol`/`client_nonce` on the
    // wire (token omitted) so the server runs the challenge-response; on the legacy path
    // we send the bearer token and no nonce.
    let client_nonce_b64 = signature_path.then(keys::random_nonce_b64);
    tx.send(Frame::Register(Register {
        agent_id: config.agent_id.clone(),
        protocol: signature_path.then(|| PROTOCOL_VERSION.to_string()),
        client_nonce: client_nonce_b64.clone(),
        token: if signature_path {
            None
        } else {
            config.token.clone()
        },
        meta: RegisterMeta {
            hostname: crate::util::hostname(),
            os: crate::util::os_family().to_string(),
            version: crate::BUILD_VERSION.to_string(),
            arch: crate::util::arch().to_string(),
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

    // Mutual-auth handshake (signature path only). BEFORE spawning telemetry/heartbeat/
    // log/read-dispatch, block on the next frame: it MUST be `challenge`. Verify the
    // server's signature against the pinned key, then send `auth`. On ANY failure (wrong
    // frame, bad signature, decode/close) we abort the session WITHOUT sending `auth` and
    // WITHOUT dispatching any tool, so a spoofed/MITM server can never push a `request`.
    // This is the anti-spoofing guarantee (ADR-0023).
    if signature_path {
        // SAFETY of unwraps: on the signature path these were set together above.
        let server_key = pinned_server_key
            .as_ref()
            .expect("pinned server key present on signature path");
        let agent_key = agent_key
            .as_ref()
            .expect("agent key present on signature path");
        let client_nonce_b64 = client_nonce_b64
            .as_deref()
            .expect("client nonce present on signature path");

        let handshake = run_handshake(
            &mut stream,
            &tx,
            &config.agent_id,
            client_nonce_b64,
            server_key,
            agent_key,
        )
        .await;
        if let Err(e) = handshake {
            warn!(error = %e, "mutual-auth handshake failed; aborting session before dispatch");
            // Tear down the writer and bail; the backoff loop reconnects.
            drop(tx);
            let _ = writer.await;
            return Err(e);
        }
        info!("mutual-auth handshake complete; server identity verified");
    }

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

/// Run the v0.8 mutual-auth challenge-response over an already-connected stream.
///
/// `register` has already been sent through `tx`. Here we block on the next inbound
/// frame, which MUST be `challenge`; rebuild the transcript, verify `server_sig` against
/// the pinned key, and reply with `auth` (the agent's signature over the same
/// transcript). Returns `Ok(())` only when the server's identity is proven — the caller
/// gates ALL request dispatch on that. Any deviation is an error and no `auth` is sent.
async fn run_handshake<S>(
    stream: &mut S,
    tx: &mpsc::Sender<Frame>,
    agent_id: &str,
    client_nonce_b64: &str,
    server_key: &ed25519_dalek::VerifyingKey,
    agent_key: &AgentKey,
) -> anyhow::Result<()>
where
    S: StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin,
{
    use base64::engine::general_purpose::STANDARD;
    use base64::Engine as _;

    // Read frames until we get a text frame to interpret. A control ping/pong before the
    // challenge is tolerated; a Close or any non-challenge JSON frame aborts.
    let challenge = loop {
        let Some(msg) = stream.next().await else {
            anyhow::bail!("connection closed before challenge");
        };
        match msg? {
            Message::Text(text) => {
                let frame: Frame = serde_json::from_str(&text)
                    .map_err(|e| anyhow::anyhow!("undecodable handshake frame: {e}"))?;
                match frame {
                    Frame::Challenge(c) => break c,
                    other => anyhow::bail!(
                        "expected challenge as the first frame, got {}",
                        frame_kind(&other)
                    ),
                }
            }
            Message::Ping(_) | Message::Pong(_) => continue,
            Message::Close(_) => anyhow::bail!("server closed before challenge"),
            Message::Binary(_) | Message::Frame(_) => {
                anyhow::bail!("unexpected non-text frame during handshake")
            }
        }
    };

    // Rebuild the transcript from the raw nonce bytes and verify the server's signature.
    let client_nonce_raw = STANDARD
        .decode(client_nonce_b64)
        .map_err(|e| anyhow::anyhow!("our own client_nonce is not base64: {e}"))?;
    let server_nonce_raw = STANDARD
        .decode(challenge.server_nonce.trim())
        .map_err(|e| anyhow::anyhow!("server_nonce is not base64: {e}"))?;
    let transcript = keys::build_transcript(agent_id, &client_nonce_raw, &server_nonce_raw);

    keys::verify_server_sig(server_key, &transcript, &challenge.server_sig)?;

    // Server identity proven: sign the SAME transcript and send `auth`.
    let agent_sig = agent_key.sign(&transcript);
    tx.send(Frame::Auth(crate::protocol::Auth { agent_sig }))
        .await
        .map_err(|_| anyhow::anyhow!("outbound channel closed before sending auth"))?;
    Ok(())
}

/// Human-readable tag for an unexpected handshake frame (for log/error messages).
fn frame_kind(frame: &Frame) -> &'static str {
    match frame {
        Frame::Register(_) => "register",
        Frame::Challenge(_) => "challenge",
        Frame::Auth(_) => "auth",
        Frame::Request(_) => "request",
        Frame::Response(_) => "response",
        Frame::Telemetry(_) => "telemetry",
        Frame::Log(_) => "log",
        Frame::Policy(_) => "policy",
        Frame::Ping => "ping",
        Frame::Pong => "pong",
    }
}

/// Derive the server's HTTPS base URL from the configured WebSocket URL.
///
/// `wss://host/agent/ws` → `https://host`; `ws://host:port/...` → `http://host:port`.
/// Only the scheme + authority are kept; the enrollment path is appended by the caller.
pub(crate) fn http_base_from_ws(ws_url: &str) -> anyhow::Result<String> {
    let (scheme, rest) = if let Some(r) = ws_url.strip_prefix("wss://") {
        ("https", r)
    } else if let Some(r) = ws_url.strip_prefix("ws://") {
        ("http", r)
    } else if ws_url.starts_with("https://") || ws_url.starts_with("http://") {
        // Already an HTTP(S) URL; take authority only.
        let (s, r) = ws_url.split_once("://").unwrap();
        (s, r)
    } else {
        anyhow::bail!("server URL must be ws(s):// or http(s)://: {ws_url}");
    };
    // Authority is everything up to the first '/'.
    let authority = rest.split('/').next().unwrap_or(rest);
    if authority.is_empty() {
        anyhow::bail!("server URL has no host: {ws_url}");
    }
    Ok(format!("{scheme}://{authority}"))
}

/// One-time enrollment (ADR-0023): if no agent key existed before this run and an enroll
/// token is configured, POST the freshly generated public key to the server so it can pin
/// the agent's identity. Thereafter only signatures authenticate.
///
/// We treat a present-and-non-empty key file as "already enrolled" and skip. Enrollment
/// runs over HTTPS using the same synchronous HTTP client (`ureq`) as the self-updater.
fn maybe_enroll(config: &Config, agent_key: &AgentKey) -> anyhow::Result<()> {
    let Some(enroll_token) = config.enroll_token.as_deref() else {
        // No enroll token: assume the key is already registered (or operator enrolls
        // out-of-band). Nothing to do.
        return Ok(());
    };
    // If a key file already existed before this process generated one, the agent has
    // (most likely) enrolled previously. `load_or_generate` persists on first run, so by
    // here the file always exists; we instead use a sentinel marker file to record that
    // enrollment succeeded, and skip when it is present.
    let marker = keys::key_path().with_extension("enrolled");
    if marker.exists() {
        return Ok(());
    }

    let base = http_base_from_ws(&config.server)?;
    let url = format!(
        "{base}/api/agents/{}/enroll",
        urlencode_segment(&config.agent_id)
    );
    let public_key = agent_key.public_key_b64();

    info!(url = %url, "enrolling agent public key (first run)");
    // ureq is synchronous; run it on a blocking section. We are already on a tokio
    // worker, but enrollment is a one-shot at startup so a brief block is acceptable.
    let body = serde_json::json!({ "public_key": public_key });
    let resp = ureq::post(&url)
        .set("Authorization", &format!("Bearer {enroll_token}"))
        .send_json(body);
    match resp {
        Ok(_) => {
            // Record success so we never re-enroll (the token is single-use server-side).
            if let Err(e) = std::fs::write(&marker, b"ok") {
                warn!(error = %e, "could not write enrollment marker; may retry enroll next start");
            }
            info!("agent enrolled successfully");
            Ok(())
        }
        Err(ureq::Error::Status(code, _)) => {
            // A 409/already-enrolled is benign: the public key is on file. Treat any 2xx
            // as success above; here, mark conflict as done so we don't loop.
            if code == 409 {
                let _ = std::fs::write(&marker, b"conflict");
                info!(code, "agent already enrolled; continuing");
                Ok(())
            } else {
                anyhow::bail!("enrollment failed with HTTP {code}")
            }
        }
        Err(e) => Err(anyhow::anyhow!("enrollment request failed: {e}")),
    }
}

/// Minimal percent-encoding for a single path segment (agent ids are typically already
/// URL-safe, but be defensive about spaces and reserved characters).
fn urlencode_segment(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
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
            // Dispatch on its own task so a long-running tool (e.g. a deep
            // `powershell_exec` scan) never blocks the read loop. Blocking here
            // would stop us polling the socket, so tungstenite could not answer
            // the server's WebSocket keepalive pings — the server then drops the
            // connection mid-command. Responses are correlated by `id`, so
            // concurrent in-flight tools are fine.
            let tx = tx.clone();
            tokio::spawn(async move {
                let response = dispatch::handle(req).await;
                if tx.send(Frame::Response(response)).await.is_err() {
                    warn!("outbound channel closed while sending response");
                }
            });
        }
        Frame::Policy(p) => {
            // Operator's append-only deny rules (ADR-0021): additive to the built-ins,
            // which they can never weaken or remove. The agent never sends a Policy frame.
            info!(count = p.rules.len(), "applied operator policy rules");
            crate::policy::set_operator_rules(p.rules);
        }
        Frame::Ping => {
            let _ = tx.send(Frame::Pong).await;
        }
        Frame::Pong => {}
        // The agent never expects to receive these in the post-handshake read loop;
        // stray `challenge`/`auth` after auth are defensively ignored (the handshake
        // already completed before this loop started).
        Frame::Register(_)
        | Frame::Challenge(_)
        | Frame::Auth(_)
        | Frame::Response(_)
        | Frame::Telemetry(_)
        | Frame::Log(_) => {
            debug!("ignoring frame not addressed to the agent");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::STANDARD;
    use base64::Engine as _;
    use ed25519_dalek::{Signer, SigningKey};
    use tokio::net::TcpListener;

    /// Throwaway seeds (matching the golden vectors' material). Real keys are generated
    /// from the OS RNG; these fixed seeds keep the mock-server handshake deterministic.
    const SERVER_SEED: [u8; 32] = [
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
        25, 26, 27, 28, 29, 30, 31,
    ];
    const AGENT_SEED: [u8; 32] = [
        32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54,
        55, 56, 57, 58, 59, 60, 61, 62, 63,
    ];
    /// A key the agent does NOT pin — used to forge an invalid challenge in the anti-spoof test.
    const WRONG_SEED: [u8; 32] = [9u8; 32];

    /// Build a `Config` for the signature path against `ws_url`, with the agent key file
    /// preloaded from `AGENT_SEED` and the server pin set to `SERVER_SEED`'s public key.
    fn signature_config(ws_url: &str, key_path: &std::path::Path) -> Config {
        // Preload the agent's persisted seed so `load_or_generate` reuses it.
        std::fs::write(key_path, AGENT_SEED).unwrap();
        std::env::set_var(crate::keys::KEY_FILE_ENV, key_path);
        let server_pub = SigningKey::from_bytes(&SERVER_SEED).verifying_key();
        Config {
            server: ws_url.to_string(),
            agent_id: "example-pc".to_string(),
            token: None,
            server_pubkey: Some(STANDARD.encode(server_pub.to_bytes())),
            enroll_token: None,
            telemetry_interval_secs: 3600,
        }
    }

    /// Read the next JSON `Frame` from a server-side WS stream, skipping control frames.
    async fn next_frame<S>(stream: &mut S) -> Option<Frame>
    where
        S: StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin,
    {
        while let Some(msg) = stream.next().await {
            match msg.ok()? {
                Message::Text(t) => return serde_json::from_str(&t).ok(),
                Message::Close(_) => return None,
                _ => continue,
            }
        }
        None
    }

    /// Spawn a one-connection mock server on an ephemeral port. Returns the `ws://` URL
    /// and a oneshot that resolves once the server task finishes with its observations.
    async fn bind_mock() -> (String, TcpListener) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        (format!("ws://{addr}/agent/ws"), listener)
    }

    /// POSITIVE: a correctly-signed challenge ⇒ the agent replies with a valid `auth`
    /// (verifiable under its public key) and then dispatches a subsequent `request`.
    // The guard serializes access to the process-global KENNY_* env vars across tests.
    // `#[tokio::test]` is single-threaded, so holding it across awaits cannot deadlock.
    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn handshake_positive_auths_then_dispatches() {
        let _g = crate::control::TEST_ENV_LOCK.lock().unwrap();
        let key_path = std::env::temp_dir().join("kenny-tunnel-pos.key");
        let _ = std::fs::remove_file(&key_path);
        let (url, listener) = bind_mock().await;
        let config = signature_config(&url, &key_path);

        // Mock server: accept, read register, send a correctly-signed challenge, expect a
        // valid auth, then send a request and expect a response.
        let server = tokio::spawn(async move {
            let (sock, _) = listener.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_async(sock).await.unwrap();

            // 1. register
            let reg = match next_frame(&mut ws).await {
                Some(Frame::Register(r)) => r,
                other => panic!("expected register, got {other:?}"),
            };
            let client_nonce = reg.client_nonce.expect("signature path sends client_nonce");
            let client_nonce_raw = STANDARD.decode(&client_nonce).unwrap();

            // 2. challenge, signed by the SERVER key the agent pins.
            let server_signing = SigningKey::from_bytes(&SERVER_SEED);
            let server_nonce_raw = [0xABu8; 32];
            let transcript =
                keys::build_transcript(&reg.agent_id, &client_nonce_raw, &server_nonce_raw);
            let server_sig = STANDARD.encode(server_signing.sign(&transcript).to_bytes());
            let challenge = Frame::Challenge(crate::protocol::Challenge {
                server_nonce: STANDARD.encode(server_nonce_raw),
                server_sig,
            });
            ws.send(Message::Text(serde_json::to_string(&challenge).unwrap()))
                .await
                .unwrap();

            // 3. auth — must verify under the AGENT public key over the same transcript.
            let auth = match next_frame(&mut ws).await {
                Some(Frame::Auth(a)) => a,
                other => panic!("expected auth, got {other:?}"),
            };
            let agent_pub = SigningKey::from_bytes(&AGENT_SEED).verifying_key();
            keys::verify_server_sig(&agent_pub, &transcript, &auth.agent_sig)
                .expect("agent_sig must verify under the agent public key");

            // 4. Now that auth succeeded, send a request and expect a response.
            let req = Frame::Request(crate::protocol::Request {
                id: "req-1".to_string(),
                tool: "__test_probe__".to_string(),
                args: serde_json::json!({}),
            });
            ws.send(Message::Text(serde_json::to_string(&req).unwrap()))
                .await
                .unwrap();
            // Drain frames until we see the response to our request (telemetry/heartbeat
            // may interleave).
            loop {
                match next_frame(&mut ws).await {
                    Some(Frame::Response(r)) if r.id == "req-1" => return true,
                    Some(_) => continue,
                    None => return false,
                }
            }
        });

        let (_tx, never) = tokio::sync::watch::channel(false);
        let mut shutdown = never;
        let agent = tokio::spawn(async move {
            // One session is enough; serve_once returns when the mock closes.
            let _ = serve_once(&config, &mut shutdown).await;
        });

        let dispatched = tokio::time::timeout(Duration::from_secs(15), server)
            .await
            .expect("server task timed out")
            .unwrap();
        assert!(
            dispatched,
            "agent must dispatch the request and answer after a valid handshake"
        );
        agent.abort();
        std::env::remove_var(crate::keys::KEY_FILE_ENV);
        let _ = std::fs::remove_file(&key_path);
    }

    /// NEGATIVE / anti-spoof (the critical test): a challenge signed with the WRONG key
    /// ⇒ the agent sends NO `auth` and NEVER dispatches the request that follows. We
    /// assert the server only ever sees `register` (no `auth`, no `response`).
    #[allow(clippy::await_holding_lock)]
    #[tokio::test]
    async fn handshake_negative_wrong_key_never_auths_or_dispatches() {
        let _g = crate::control::TEST_ENV_LOCK.lock().unwrap();
        let key_path = std::env::temp_dir().join("kenny-tunnel-neg.key");
        let _ = std::fs::remove_file(&key_path);
        let (url, listener) = bind_mock().await;
        let config = signature_config(&url, &key_path);

        let server = tokio::spawn(async move {
            let (sock, _) = listener.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_async(sock).await.unwrap();

            let reg = match next_frame(&mut ws).await {
                Some(Frame::Register(r)) => r,
                other => panic!("expected register, got {other:?}"),
            };
            let client_nonce_raw = STANDARD
                .decode(reg.client_nonce.as_deref().unwrap())
                .unwrap();

            // Forge a challenge signed with a key the agent does NOT pin.
            let wrong_signing = SigningKey::from_bytes(&WRONG_SEED);
            let server_nonce_raw = [0xCDu8; 32];
            let transcript =
                keys::build_transcript(&reg.agent_id, &client_nonce_raw, &server_nonce_raw);
            let bad_sig = STANDARD.encode(wrong_signing.sign(&transcript).to_bytes());
            let challenge = Frame::Challenge(crate::protocol::Challenge {
                server_nonce: STANDARD.encode(server_nonce_raw),
                server_sig: bad_sig,
            });
            ws.send(Message::Text(serde_json::to_string(&challenge).unwrap()))
                .await
                .unwrap();

            // Immediately push a request, as a hostile/MITM server would, to try to drive
            // a tool handler before auth.
            let req = Frame::Request(crate::protocol::Request {
                id: "evil-1".to_string(),
                tool: "__test_probe__".to_string(),
                args: serde_json::json!({}),
            });
            ws.send(Message::Text(serde_json::to_string(&req).unwrap()))
                .await
                .unwrap();

            // The agent must abort: it sends NO auth and NO response. We expect the stream
            // to close (or stay silent) — assert we never observe auth/response.
            loop {
                match next_frame(&mut ws).await {
                    Some(Frame::Auth(_)) => return Err("agent sent auth after a bad challenge"),
                    Some(Frame::Response(_)) => {
                        return Err("agent dispatched a request before auth")
                    }
                    Some(_) => continue,
                    None => return Ok(()), // stream closed without auth/response: correct.
                }
            }
        });

        let (_tx, never) = tokio::sync::watch::channel(false);
        let mut shutdown = never;
        let agent = tokio::spawn(async move {
            let _ = serve_once(&config, &mut shutdown).await;
        });

        let outcome = tokio::time::timeout(Duration::from_secs(15), server)
            .await
            .expect("server task timed out")
            .unwrap();
        assert!(outcome.is_ok(), "{}", outcome.unwrap_err());
        agent.abort();
        std::env::remove_var(crate::keys::KEY_FILE_ENV);
        let _ = std::fs::remove_file(&key_path);
    }

    /// REGRESSION: `run_until` must record the configured server host into the
    /// `agent_update` allowlist. Both the foreground (`run`) and the Windows service
    /// (`run-service`) paths funnel through `run_until`; the service path previously
    /// never recorded the host, so `agent_update` from the configured server was wrongly
    /// refused as "host not allowlisted". We pre-fire the shutdown so `run_until` returns
    /// immediately after recording the host — no network needed.
    #[tokio::test]
    async fn run_until_allowlists_server_host_for_agent_update() {
        // A distinctive host so this never collides with the GitHub/`evil.example.com`
        // assertions in the policy unit tests (the global `SERVER_HOST` is set-once).
        let host = "update-allowlist-regression.example";
        let config = Config {
            server: format!("wss://{host}/agent/ws"),
            agent_id: "example-pc".to_string(),
            token: Some("legacy-token".to_string()),
            server_pubkey: None,
            enroll_token: None,
            telemetry_interval_secs: 3600,
        };

        // Shutdown already requested ⇒ `run_until` records the host, then returns at once.
        let (_tx, shutdown) = tokio::sync::watch::channel(true);
        run_until(config, shutdown).await;

        // The safety guard now permits an `agent_update` served by the configured host.
        crate::policy::check(
            "agent_update",
            &serde_json::json!({
                "version": "1.2.3",
                "url": format!("https://{host}/kenny-agent.exe"),
                "sha256": "ab",
            }),
        )
        .expect("agent_update from the configured server host must be allowlisted");
    }
}

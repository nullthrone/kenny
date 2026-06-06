# 0019. Non-blocking request dispatch in the agent tunnel

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

The agent runs one WebSocket session with a single read loop that, for each inbound
`request` frame, awaited the tool handler inline and only then sent the `response`.
While a handler ran, the read loop did not poll the socket.

The server runs behind uvicorn, which enables WebSocket keepalive by default
(`ws_ping_interval=20`, `ws_ping_timeout=20`): it sends a WS-protocol ping every
~20 s and closes the connection if no pong arrives within ~20 s. `tokio-tungstenite`
only answers those pings while the stream is being polled. So any tool that ran
longer than the keepalive timeout (e.g. a `powershell_exec` that recursively scans a
whole disk) starved the keepalive, and the server dropped the agent mid-command —
observed as a reproducible disconnect. The agent's own application-level
`ping`/`pong` heartbeat runs on a separate task and does not help, because the drop
happens one layer down, on the WS protocol keepalive.

## Considered Options

- **A. Spawn each request on its own task** so the read loop keeps polling the socket
  and tungstenite keeps answering keepalive pings.
- **B. Disable or lengthen the server's WS keepalive** (`ws_ping_interval` /
  `ws_ping_timeout`) so slow tools fit inside the window.
- **C. Stream/heartbeat from inside the handler** so a long tool periodically yields
  back to the read loop.

## Decision Outcome

Chosen option: "A", because it fixes the root cause (the read loop must never block)
with a small, local change, keeps the wire contract untouched, and lets independent
tools run concurrently. Responses are correlated by request `id`, so concurrency is
safe. Option B only widens the window — a long enough tool still dies, and it weakens
liveness detection for genuinely stuck agents. Option C pushes keepalive concerns
into every handler.

### Consequences

- Good, because long-running tools no longer disconnect the agent; the server's
  keepalive and per-call `timeout_s` continue to work as intended.
- Good, because multiple in-flight requests now execute concurrently.
- Bad, because there is no longer an implicit one-at-a-time bound on tool execution;
  if that ever becomes a problem, add an explicit concurrency limit (e.g. a
  semaphore) rather than reverting to a blocking read loop.

## More Information

Implemented in `kenny-agent/src/tunnel.rs` (`handle_text`). Related: ADR-0003
(self-built tunnel), ADR-0004 (agent dials out).

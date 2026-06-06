# 0017. Observability: structured logging and a persistent event store

- Status: accepted
- Date: 2026-06-05

## Context and Problem Statement

kenny has an observability gap. Both components emit useful events but nothing is
central or persistent:

- The **server** uses Python `logging` with no central configuration, so it defaults
  to `WARNING` on stderr without timestamps, and only a handful of call sites log at
  all. The tool-call audit (`CallLog`) is an in-memory `deque(200)` lost on restart.
- The **agent** logs via `tracing` to stderr only. When it runs as a Windows service
  (ADR-0013, LocalSystem, no attached terminal) all of that output is discarded, so
  there is no record of reconnects, tool dispatch, or errors on the endpoint.

We want operator-visible events — from both the server and every agent — to be
configurable, durable, and viewable fleet-wide in the dashboard.

## Considered Options

- **Forward agent logs over the existing tunnel + one persistent event store** on the
  server, plus a local rotating file on the agent and central server log config.
- **Local files only** on each side, aggregated out-of-band (e.g. ship logs with a
  separate agent/collector).
- **An external log stack** (e.g. Loki/ELK) the server and agents push to.

## Decision Outcome

Chosen option: forward agent logs over the existing tunnel and persist everything in
SQLite, because it reuses the contract-first wire and the low-ops SQLite storage kenny
already runs (ADR-0007), and keeps a family-scale fleet dependency-light. Concretely:

- A new additive **`log` frame** (agent → server), one event per frame, for events at
  or above `KENNY_LOG_FORWARD_LEVEL` (default `info`). The agent also writes a fuller
  record (`>= debug`/`RUST_LOG`) to a local **rotating file** so the deep record
  survives offline. Forwarding is best-effort: a **process-global bounded ring buffer**
  decouples the `tracing` layer from the per-session tunnel channel, so events produced
  while disconnected accumulate (oldest dropped under pressure) and flush on reconnect.
- A single **`events` table** in SQLite holds server log records, forwarded agent log
  events, and the tool-call audit, discriminated by `kind` (`log` | `audit`) and
  `source` (`server` | `agent`), with the same ~30-day retention as snapshots.
- The server gains a central `configure_logging()` (level via `KENNY_LOG_LEVEL`,
  timestamped format, uvicorn loggers wired) and a `logging.Handler` that bridges the
  sync logging API to async SQLite via a bounded `asyncio.Queue` drained by a
  background task — no synchronous SQLite writes on the event loop.
- The dashboard gains a fleet-wide events/logs panel backed by a new `/api/events`.

### Consequences

- Good, because endpoint events (reconnects, tool dispatch, errors) are no longer lost
  in service mode, and the operator sees server + agent events in one place.
- Good, because retention and storage reuse the existing SQLite/30-day pattern; no new
  infrastructure to run.
- Bad, because the agent now carries a small always-on forwarding path and the server a
  small write path; both are bounded/drop-on-pressure to protect the hot paths.
- Neutral, because the `tracing` forwarding layer must filter its own and the tunnel
  writer's targets to avoid a feedback loop — an explicit, tested constraint.

## More Information

Frame shape: `docs/protocol.md` § `log` and `docs/fixtures/log.json`
(`PROTOCOL_VERSION` 0.4). Builds on ADR-0007 (push model + SQLite) and is motivated by
ADR-0013 (Windows service — service-mode stderr is discarded).

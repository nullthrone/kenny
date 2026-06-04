---
name: kenny-telemetry
description: How kenny telemetry works end-to-end — agent collectors, the push scheduler, server storage, health rules, and the fleet dashboard. Use when adding or changing a telemetry section or the dashboard.
---

# kenny telemetry

Telemetry is a **pushed** snapshot: the agent collects sections on a timer and sends one
`telemetry` frame; the server stores it and evaluates fleet health. On-demand refresh is
the `telemetry.collect` tool. See `docs/adr/0007-telemetry-push-model-and-sqlite-storage.md`.

## The pieces

- **Agent collector** (`kenny-agent/src/telemetry/collectors/<section>.rs`): produces one
  section payload — required `status` + `summary`, plus raw fields defined in the contract.
  Gate Windows-only data behind `#[cfg(windows)]`; provide a portable fallback (often
  `sysinfo`) or a `status:"ok", summary:"n/a on this platform"` stub.
- **Scheduler** (`kenny-agent/src/telemetry/scheduler.rs`): runs all collectors every
  interval (default 900 s) and pushes the snapshot.
- **Store** (`kenny-server/kenny_server/store.py`): SQLite, latest snapshot + ~30 days of
  history per agent, with a retention job.
- **Health rules** (`kenny-server/kenny_server/health_rules.py`): the authoritative
  thresholds (e.g. disk > 90% ⇒ crit, Defender scan > 14 days ⇒ warn). Aggregates section
  statuses into an overall per-agent health.
- **Dashboard** (`kenny-server/kenny_server/webui/`): fleet grid of agent tiles (overall
  traffic-light), drill-down to one agent's sections + history trends, "refresh now".

## Sections

Mandatory + four bundles (hardware health, security & crypto, update & stability,
operations & daily) — the authoritative list is in `docs/protocol.md` § Telemetry sections.
Use `/add-collector <section>` to add one across all layers.

## Done when

The section round-trips as a fixture, a collector emits it, a health-rule test covers its
thresholds, and it renders in the drill-down view.

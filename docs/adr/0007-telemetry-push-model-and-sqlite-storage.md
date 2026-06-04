# 0007. Telemetry push model and SQLite storage

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

kenny should continuously surface the health of every PC in the fleet (disk, Defender,
Windows Update, network, and more) in a dashboard with drill-down, not only answer
on-demand tool calls.

## Considered Options

- Agent **pushes** snapshots on a timer; server stores and evaluates
- Server **polls** each agent on a timer via `request`/`response`
- On-demand only (no background collection)

## Decision Outcome

Chosen option: "Agent pushes on a timer (default 900 s)" over the existing tunnel,
plus an on-demand `telemetry.collect` tool for "refresh now". The server persists
snapshots in **SQLite** (latest + ~30 days history) and evaluates health thresholds
server-side in `health_rules.py`. Each section carries `status`/`summary` so fleet
aggregation needs no per-section domain logic.

### Consequences

- Good, because offline/intermittent agents still deliver their last-known state, and
  trends over time are available for the drill-down view.
- Good, because thresholds live in one server-side place and can change without
  redeploying agents.
- Bad, because the server owns retention and a small write path; SQLite is the
  deliberate low-ops choice for a family-scale fleet (revisit if fleet grows large).

## More Information

Snapshot shape and section list: `docs/protocol.md` § Telemetry sections and
`docs/fixtures/telemetry_snapshot.json`.

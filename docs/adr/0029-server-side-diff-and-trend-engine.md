# 0029. Server-side snapshot diff and trend engine

- Status: accepted
- Date: 2026-07-02

## Context and Problem Statement

kenny stores ~30 days of telemetry snapshots per host (ADR-0007) but never compares
them: nobody notices that an autostart entry appeared overnight, that a service was
flipped to disabled, or that a disk has been filling at 2 % a day and will be full in
two weeks. In a family setting, "what changed on this PC since yesterday?" and "what
will break soon?" are exactly the questions the operator has — and all the data to
answer them is already in the store.

The structural question is *where* cross-snapshot analysis lives. Collectors are
deliberately stateless functions (ADR-0007, reaffirmed in ADR-0026: rolling-window
accumulation belongs server-side), so the agent cannot answer "what's new" without
breaking that model.

## Considered Options

- **Agent-side diffing** (agent remembers the previous inventory and reports deltas).
  Rejected: violates the stateless-collector rule, loses state on agent restart, and
  puts trend logic where it cannot be updated without redeploying agents.
- **Store diffs/forecasts as their own tables at insert time.** Rejected: derived data
  that can always be recomputed from snapshots would need migrations and backfills;
  computing on read (and once per new snapshot in the alert loop) is cheap at family
  scale.
- **Pure server-side compute modules over the existing snapshot history.** Chosen.

## Decision Outcome

Two pure, I/O-free modules (patterned after `fleet_stats.py`):

- **`diffs.py`** compares two snapshots via a data-driven per-section table
  (`SPECS`, extensible like `health_rules.RULES`): each entry names the section's list
  field, the identity key fields, and which fields count as "changed". Initially
  covers `autostart`, `services`, `peripherals`; entries for the ADR-0030 inventory
  sections (`installed_software`, `browser_extensions`, `listening_ports`,
  `scheduled_tasks`, `local_accounts`) are already present and activate automatically
  when those sections ship. Sections absent from either snapshot are skipped, so a
  collector rollout never floods the diff with "added" rows.
- **`trends.py`** fits ordinary least squares over `TelemetryStore.daily_latest()`
  (one point per UTC day): a per-volume "days until full" disk forecast and a battery
  health drift. Forecasts are deliberately shy — at least 5 daily points, a rising
  slope and r² ≥ 0.5, else `None` — so noise never produces a scary made-up number.

**Surfacing:** two read-only endpoints (`GET /api/agent/{id}/changes?days=N`,
`GET /api/agent/{id}/trends`) behind the existing operator auth; a "changes &
forecast" panel in the agent drill-down; a "Disks filling <30d" Overview KPI; and a
diff step in the alert loop (ADR-0028) that runs once per *new* snapshot (persisted
cursor scope `change:_cursor`), batches inventory changes into one notification per
host, treats `local_accounts` changes as high priority, and raises a forecast alert
when a volume is under 14 days from full (24 h cooldown).

**Threshold placement:** `health_rules.py` stays authoritative for *per-snapshot*
thresholds, by design evaluated against one snapshot. Cross-snapshot judgments
(forecast horizons, diff notification rules) cannot live there; they are colocated in
`trends.py`/`alerting.py` as an explicit, bounded exception to the "thresholds only in
`health_rules.py`" rule — still server-side only, never in the agent.

### Consequences

- Good, because "new since yesterday" surfaces automatically — new software, autostart
  entries, USB devices, admin-group changes — the highest-signal security question in a
  family fleet, with zero agent changes.
- Good, because disk-full events become predictable ("~19 days") instead of reactive
  (the 80 % warn threshold firing when it is already tight).
- Good, because both modules are pure functions over stored data: trivially testable,
  recomputable, no migrations, no new storage.
- Bad, because consecutive-snapshot diffing in the alert loop misses intermediate
  states between two pushes (a program installed and removed within one 900 s interval
  never appears). Accepted: the dashboard's day-baseline diff has the same granularity
  as its data, and kenny is not a forensic tool.
- Bad, because there are now two homes for server-side judgment (per-snapshot rules in
  `health_rules.py`, cross-snapshot rules in `trends.py`/`alerting.py`). Mitigated by
  keeping the exception explicit and small.

## More Information

- Related: ADR-0007 (stateless collectors, snapshot store), ADR-0026 (server-side
  accumulation precedent), ADR-0028 (notification channel these findings ride on),
  ADR-0030 (inventory sections that widen the diff surface).
- Code: `kenny-server/kenny_server/diffs.py`, `kenny-server/kenny_server/trends.py`,
  `kenny-server/kenny_server/alerting.py` (`_change_notifications`,
  `_forecast_alert`), `kenny-server/kenny_server/fleet_stats.py` (KPI),
  `kenny-server/kenny_server/webui/__init__.py` (endpoints),
  `kenny-server/kenny_server/webui/index.html` (panel).

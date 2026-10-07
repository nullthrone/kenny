# 0070. Long-lived per-device hardware history

- Status: proposed
- Boundary moved: **the storage/observability model**. History is no longer only the
  ~30-day window of raw snapshots; a compact per-device daily metrics table is kept for up
  to two years, so wear, error-counter and fan trends that unfold over months can be seen.
- Amends: [ADR-0007](0007-telemetry-push-model-and-sqlite-storage.md)
- Date: 2026-10-07

## Context and Problem Statement

The signals that precede a hardware failure move slowly: an SSD's `percentage_used`
climbs a few points a quarter, a fan's RPM at a given duty sinks over months, a corrected
error rate rises over weeks. ADR-0007 keeps raw snapshots for about 30 days, which is too
short to fit a wear-out curve, to baseline a fan before it degrades, or to tell a first
error from a standing one.

Each snapshot is about 90 KB, so keeping snapshots longer costs roughly 2 GB per host per
year at the default push interval, for data of which a few dozen numbers per device per day
are ever read.

## Considered Options

- **Keep raw snapshots longer** (raise `KENNY_TELEMETRY_RETENTION_DAYS`). Rejected: the
  blob size makes a year of history the dominant cost of the database and of every backup.
- **Roll up on insert**, in `TelemetryStore.insert` or the tunnel's `after_insert` hook.
  Rejected: the store has no insert hook, the tunnel's single `after_insert` slot is taken,
  and a hook there misses snapshots that arrive through the dashboard's collect-now path.
- **A daily rollup into a compact table, run by the alert loop.** Chosen.

## Decision Outcome

Chosen option: "a daily rollup run by the alert loop", because the loop already owns the
24-hourly maintenance pass, sees every stored snapshot whichever path inserted it, and runs
before snapshot pruning.

- **Table.** `hw_metrics(agent_id, device_key, metric, day, value)` with
  `PRIMARY KEY (agent_id, device_key, metric, day)` and `WITHOUT ROWID`, in the telemetry
  database file. `day` is a UTC `YYYY-MM-DD`; `value` is a real. `hw_rollup_state` holds the
  last fully rolled-up day per agent.
- **Rollup.** `AlertEngine._maybe_prune` rolls up first, then prunes snapshots. Per agent
  it reads each day after `last_day` up to and including today, reduces that day's
  snapshots with the pure `hardware_metrics.extract`, upserts the rows in one
  `BEGIN IMMEDIATE` transaction under `write_lock()` (ADR-0051), and sets `last_day` to
  yesterday. Today's rows are provisional and are rewritten by the next run. The first run
  backfills whatever snapshots exist. The rollup is idempotent.
- **Identity.** A device is keyed by its serial (disks), its UUID, bus id or PCI id (GPUs),
  or its fan key; hardware-error counts are keyed by component (`host:<component>`). A
  replaced device therefore starts a new series, and a counter that decreases is a reset,
  not a recovery.
- **Retention.** `KENNY_HW_HISTORY_RETENTION_DAYS` (default 730, minimum 30) bounds the
  table; it is pruned by the same periodic sweep as the other stores. Removing a host
  deletes its rows.
- **Backups.** The table lives in the same file as the telemetry, so the scheduled
  `VACUUM INTO` backup of ADR-0039 covers it.
- **Consumers.** `trends.py` derives wear-out, spare-capacity, counter, PCIe-width, fan
  and error-rate forecasts from it; the alert loop raises `hardware_forecast`, and the
  trends API serves the series to the dashboard.

### Consequences

- Good, because two years of per-device history costs kilobytes per host, not gigabytes.
- Good, because the metric set is closed and named in one module, so a consumer never
  parses old snapshots.
- Bad, because the rollup lags: a day is final only after the next run, and the loop runs
  it at most every 24 h. Snapshot retention must stay above one day.
- Bad, because a metric added later has no history before its first rollup.

## More Information

- Code: `kenny-server/kenny_server/store.py` (`HardwareHistoryStore`),
  `hardware_metrics.py`, `trends.py`, `alerting.py`.
- Related: [ADR-0039](0039-server-database-backup-and-restore.md),
  [ADR-0051](0051-process-wide-sqlite-write-serialization.md),
  [ADR-0058](0058-time-aware-findings-and-incident-posture-split.md),
  [ADR-0067](0067-alert-delivery-is-routed-by-actionability.md).

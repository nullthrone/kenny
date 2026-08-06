# 0027. Push alerting via ntfy/webhook and a weekly digest

- Status: accepted
- Date: 2026-07-02

## Context and Problem Statement

kenny evaluates every telemetry snapshot with authoritative server-side health rules
(ADR-0007) and surfaces warn/crit on the dashboard — but nothing ever reaches the
operator unless they open that dashboard. A dead disk, a disabled Defender, or a machine
that silently stopped pushing telemetry goes unnoticed for days in a family setting,
where nobody watches a fleet dashboard routinely. kenny needs a push channel: notify the
operator when something *changes* for the worse (or recovers), and once a week summarize
the fleet even when nothing is on fire.

## Considered Options

- **Agent-side alerting** (agent decides and notifies directly). Rejected: thresholds
  must stay server-side (ADR-0007) so they can change without redeploying agents, and an
  offline agent obviously cannot report its own absence.
- **A new wire frame for alerts.** Rejected: alerting needs no agent cooperation at all —
  everything derives from data the server already has (snapshots, push timestamps).
  ADR-0017 set the precedent of preferring existing plumbing over new frames.
- **E-mail (SMTP) as the channel.** Rejected for now: SMTP setup is the classic
  self-hosting pain point (relay, auth, spam folders). ntfy delivers a phone push with a
  single HTTP POST and no account; a generic webhook covers everything else.
- **Server-side pull-evaluation loop + ntfy/webhook notifiers.** Chosen.

## Decision Outcome

A background evaluation loop on the server (`alerting.py`, started from the app lifespan
like the webfilter refresh loop) re-runs `health_rules.evaluate_snapshot()` over every
known agent's latest snapshot on a short interval (default 60 s) and notifies on
**transitions only**: ok→warn, ok→crit, warn→crit (escalation), warn/crit→ok (recovery),
plus offline detection. No new frames, no protocol change, no agent involvement;
thresholds remain exclusively in `health_rules.py`.

**Offline detection is push-derived**: an agent is offline when its newest snapshot is
older than `KENNY_ALERT_OFFLINE_AFTER_SECS` (default 2700 s = three missed 900 s push
intervals, matching the protocol's staleness rule of thumb) and the in-memory registry
holds no live connection. Health evaluation is skipped for offline agents so stale
snapshots cannot flap.

**Flap suppression** is stateful and persisted (`alert_state` table, one row per
`(agent_id, scope)` where scope is `offline`, `overall`, `section:<name>`, later
`change:<section>`/`digest`): a per-scope cooldown (default 1 h) bounds a flapping
section to one alert plus one recovery per window, escalations to crit always fire, and
a recovery is only sent when the degraded episode itself was notified. Persisting the
state means a server restart does not re-fire alerts for already-notified conditions.

**Delivery** goes through a minimal notifier abstraction (`notify.py`) with exactly two
channels, both env-configured and both a single HTTP POST: **ntfy** (topic URL +
optional token; title/priority/tags as headers — works out of the box with the ntfy
phone apps) and a **generic JSON webhook** (operator-provided URL; kenny defines the
payload, the receiver adapts). Delivery is strictly best-effort: send errors are logged
and swallowed, a dead notification target never stalls or kills the loop, and with no
channel configured the loop still runs and records history.

**Alert history reuses the existing `events` table** (`kind='alert'`, `source='server'`,
ADR-0017's store): the Activity view shows alerts with no new UI plumbing, and the
weekly digest reads them back. A **weekly digest** (rendered plain text over existing
data: fleet health, alert/change counts, forecasts, pending updates) is scheduled inside
the same loop and sent on the same channels at low priority.

### Consequences

- Good, because warn/crit conditions and dead agents reach the operator's phone within
  ~a minute, without anyone watching the dashboard.
- Good, because it is entirely server-side: no protocol bump, no agent redeploy, and
  thresholds stay in one place (`health_rules.py`).
- Good, because transition+cooldown semantics with persisted state make the channel
  quiet by construction — no reminder spam, no restart re-fires, crit always cuts
  through.
- Bad, because offline alerts fire for family PCs that are simply switched off; the
  operator tunes `KENNY_ALERT_OFFLINE_AFTER_SECS` (or disables via
  `KENNY_ALERT_INTERVAL_SECS=0`) if that is noise for their fleet.
- Bad, because notifications leave the operator's infrastructure when public ntfy.sh is
  used — the topic name is effectively the secret. Self-hosting ntfy or using the
  webhook keeps delivery private; alert bodies deliberately carry only host names,
  section names and rule reasons.

## More Information

- Config: `KENNY_NTFY_URL`, `KENNY_NTFY_TOKEN`, `KENNY_WEBHOOK_URL`,
  `KENNY_ALERT_INTERVAL_SECS`, `KENNY_ALERT_COOLDOWN_SECS`,
  `KENNY_ALERT_OFFLINE_AFTER_SECS` (see `kenny-server/.env.example`).
- Related: ADR-0007 (server-side thresholds, push model), ADR-0017 (events table),
  the diff/trend engine (`diffs.py`/`trends.py`) feeding change notifications here.
- Code: `kenny-server/kenny_server/notify.py`, `kenny-server/kenny_server/alerting.py`,
  `kenny-server/kenny_server/store.py` (`AlertStateStore`, `EventStore.insert_alert`),
  `kenny-server/kenny_server/main.py` (lifespan wiring).

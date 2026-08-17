# Alerting, digests & forecasts

kenny evaluates every telemetry snapshot with authoritative server-side health rules and
surfaces warn/crit on the dashboard — but in a family setting nobody watches a fleet
dashboard routinely. This page covers the **push channel** that reaches the operator's
phone when something changes for the worse (or recovers), the **change and forecast**
findings that ride the same channel, and the **weekly digest**. It is entirely
server-side: no protocol bump, no agent involvement, thresholds stay in `health_rules.py`
(see [ADR-0027](adr/0027-push-alerting-ntfy-webhook-and-weekly-digest.md) and
`diffs.py` / `trends.py`).

## Push alerting

A background loop on the server re-runs the health rules over **every known agent's**
latest snapshot on a short interval (default 60 s) and notifies on **transitions only** —
not on every push:

- **Escalations to `crit` always fire** (`ok→crit`, `warn→crit`).
- **`warn` transitions respect a per-scope cooldown** (default 1 h) so a flapping section
  is bounded to one alert plus one recovery per window — no reminder spam.
- **A recovery is only announced if the degrading episode was itself announced.** An
  improvement that nobody was told about stays silent, and `crit→warn` updates state
  quietly.

The persisted flap-suppression state means a server restart never re-fires alerts for
conditions that were already notified.

!!! note "Reliability alarm suppression also dampens this loop"
    An operator-suppressed reliability event pattern (ADR-0041, issue #166) is excluded from
    the `reliability` section's severity scoring wherever that scoring runs — including this
    push-alert loop and the weekly digest below, not just the dashboard. Muting a known-noisy
    Windows quirk (e.g. a `CryptSvc` pattern rotating hundreds of times a day) stops it from
    triggering push notifications too. See [Alarm suppression](telemetry.md#alarm-suppression).

!!! note "Offline detection is push-derived"
    An agent counts as **offline** when its newest snapshot is older than the offline
    threshold (default 2700 s = three missed 15-min pushes) **and** the in-memory registry
    holds no live tunnel connection. Health evaluation is skipped for offline agents, so a
    stale snapshot can't flap. Offline PCs that are simply switched off will alert — tune
    `KENNY_ALERT_OFFLINE_AFTER_SECS` or disable the loop entirely with
    `KENNY_ALERT_INTERVAL_SECS=0` if that is noise for your fleet.

Every emitted alert is also written to the **events table** (`kind='alert'`) as an audit
trail and as the weekly digest's input, so it shows up in the dashboard's **[Log](dashboard.md#log)**
page with no extra UI plumbing.

<figure markdown>
![Emitted alerts and server/agent events in the Log page.](assets/screenshots/log.png)
<figcaption>Emitted alerts and server/agent events in the Log page, filterable by the ALERTS and EVENTS chips.</figcaption>
</figure>

## An alert can open a ticket

A genuine alert (not a recovery, not the digest) can also open a [ticket](itsm.md) — the
same ITSM record a Discord conversation or a dashboard action produces, so a
Defender-disabled or a disk-forecast notification arrives with somewhere to work it rather
than just a push you have to remember. This runs **after** delivery and is strictly
best-effort: a failure to open the ticket is logged and swallowed, never lets a failing
side effect make an alert late or lost — alerting must not become less reliable by gaining
one.

An alert-origin ticket has **no requester** — it belongs to the fleet, not a person — so it
is operator-only in the [Inbox](dashboard.md#inbox): a scoped `user` never sees it. It
starts life pinned to the alerting agent, at `high` priority for a `high`/`urgent`
notification and `normal` otherwise, with the alert's own message as its opening summary.

### Which events open a ticket is configurable

By default, every genuine alert — a health escalation, an agent going offline, a disk-fill
forecast — opens a ticket, and a recovery, an inventory change, and the weekly digest never
do. An operator can narrow or widen that per fleet or per host from the **Auto-ticket
rules** section of [Admin](dashboard.md#auto-ticket-rules), or via the `ticket_rule_*`
MCP tools. Each rule names an event type (`health` / `offline` / `disk_forecast` /
`change`), an optional section and host, and a decision: `open_all` (always), `open_crit`
(only when the subject is `crit`) or `never`.

Two practical cases this solves:

- **A family PC that is simply switched off overnight** re-opens an offline ticket every
  cooldown window. A `never` rule on `offline` (fleet-wide or for just that host) stops the
  tickets without silencing the offline *alert* itself — delivery and the events-table audit
  trail are unaffected.
- **Inventory changes never open a ticket by default**, even though a new local administrator
  account is exactly the kind of thing worth a ticket. An `open_all` rule on `change` with
  section `local_accounts` promotes it.

Recoveries and the weekly digest can never open a ticket, no matter what rule is written — the
rule engine only ever narrows or widens *genuine alerts*, and running the empty rule table
through the same decision path reproduces this section's coded default exactly.

## Change notifications

A diff step in the same loop compares **consecutive snapshots** (once per *new* snapshot,
tracked by a persisted cursor — never per evaluation tick, and never re-diffed after a
restart) and reports what appeared, disappeared or changed in the inventory sections:

| Section | Diffed on |
|---------|-----------|
| `autostart` | entry added/removed, command changed |
| `services` | service added/removed, start type changed |
| `peripherals` | device added/removed |
| `installed_software` | app added/removed, version changed |
| `browser_extensions` | extension added/removed |
| `listening_ports` | port added/removed |
| `scheduled_tasks` | task added/removed, action changed |
| `local_accounts` | account added/removed, admin/enabled changed |

Changes are batched into **one notification per host**. `local_accounts` changes (a new
account, or one flipped to admin/enabled) **escalate to high priority** — the
highest-signal security question in a family fleet. **Sections absent from either
snapshot are skipped**, so rolling out a new collector never floods the diff with "added"
rows for a whole section.

## Forecasts

`trends.py` fits ordinary least squares over the per-day history (one representative
snapshot per UTC day). Forecasts are deliberately shy — they need at least 5 daily
points, a genuinely rising slope and a decent fit (r² ≥ 0.5), else they return nothing
rather than a scary made-up number:

- **Disk-fill forecast** — *days until full* per volume. Under **~14 days** raises an
  alert (re-firing at most every 24 h); under **~30 days** shows as a Today KPI and in
  the weekly digest.
- **Battery drift** — health change as **percent per 30 days**; a meaningful decline
  appears in the digest.

These same computations feed the per-host **Forecast** panel at the top of
[the host page](dashboard.md#the-host-page), which synthesizes them (with the inventory
diff) into a short prose outlook.

## Weekly digest

A plain-text weekly summary is scheduled inside the same loop and sent on the same
channels at low priority. It renders — entirely from data already in the stores — the
fleet health mix, degraded hosts, 7-day alert / change / crit counts, disk-fill
forecasts, battery drift, pending reboots / failed updates / OS EOL, and 7-day screen
time.

- Scheduled by default **Monday 08:00** (`KENNY_DIGEST_DAY` / `KENNY_DIGEST_HOUR`); the
  last-sent time is persisted so a restart never double-sends, and the first digest
  arrives at the next scheduled slot rather than on install.
- Only sent if `KENNY_DIGEST_ENABLED` is on **and** at least one notifier is configured.
- Preview it without sending via the operator-only endpoint **`GET /api/digest/preview`**.

## Notification channels

Delivery goes through three best-effort channels, **all off unless configured** (a single
HTTP POST each):

| Channel | Configure with | Payload |
|---------|----------------|---------|
| **ntfy** | `KENNY_NTFY_URL` (+ optional `KENNY_NTFY_TOKEN` bearer) | POST body to an ntfy topic; title/priority/tags as headers — works out of the box with the ntfy phone apps |
| **Generic webhook** | `KENNY_WEBHOOK_URL` | JSON POST (`kind`, `title`, `body`, `priority`, `tags`, `agent_id`, `event_type`, `sections`, `at`) |
| **Discord** | `KENNY_DISCORD_WEBHOOK_URL` | JSON POST of a Discord embed — title, body as the description, priority as the embed colour, and `kind` / `agent_id` as fields |

Delivery is strictly best-effort: send errors are logged and swallowed, a dead target
never stalls or kills the loop. **With no channel configured, evaluation still runs and
records alert history — it just pushes nothing.**

## Configuration

Alerting environment variables (see [`setup.md`](setup.md) for the full list):

| Variable | Default | Purpose |
|----------|---------|---------|
| `KENNY_ALERT_INTERVAL_SECS` | `60` | Evaluation interval; `0` disables the loop |
| `KENNY_ALERT_COOLDOWN_SECS` | `3600` | Per-scope flap suppression window |
| `KENNY_ALERT_OFFLINE_AFTER_SECS` | `2700` | Offline after this (three missed 15-min pushes) |
| `KENNY_DIGEST_ENABLED` | `1` | Send the weekly digest |
| `KENNY_DIGEST_DAY` | `mon` | Digest day of week |
| `KENNY_DIGEST_HOUR` | `8` | Digest hour (0–23) |
| `KENNY_NTFY_URL` | *(empty)* | ntfy topic URL; empty = channel off |
| `KENNY_NTFY_TOKEN` | *(empty)* | Optional ntfy bearer token |
| `KENNY_WEBHOOK_URL` | *(empty)* | Generic JSON webhook URL; empty = channel off |
| `KENNY_DISCORD_WEBHOOK_URL` | *(empty)* | Discord webhook URL; empty = channel off |

## See also

- [`setup.md`](setup.md) — hosting, TLS, and the full environment-variable list
- [`dashboard.md`](dashboard.md) — the Today KPIs and the per-host Forecast panel
- [`telemetry.md`](telemetry.md) — the sections and health rules these alerts evaluate
- [`itsm.md`](itsm.md) — tickets, the Discord bot, and what an alert-opened ticket looks like
- [ADR-0027](adr/0027-push-alerting-ntfy-webhook-and-weekly-digest.md) — push alerting & weekly digest
- `kenny-server/kenny_server/ticket_rules.py` — the auto-ticket rule model, and why an
  empty rule table reproduces the coded default

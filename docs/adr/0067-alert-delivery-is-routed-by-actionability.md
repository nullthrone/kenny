# 0067. Alert delivery is routed by actionability

- Status: proposed
- Boundary moved: **the observability model (alert delivery)**. What reaches a person is no
  longer "every notified transition, pushed now". Each notification carries a route
  (`push` / `daily` / `record`), independent of whether it opens a ticket, and a pushed
  alert is a channel message that is edited to show its current state.
- Date: 2026-09-25

## Context and Problem Statement

ADR-0027 pushes every notified transition on every channel, bounded only by a per-scope
cooldown. On a family fleet with Discord as the channel, most of what arrived asked for no
action:

- a PC switched off and back on (offline plus back online),
- software version bumps, USB devices and service start types (inventory changes),
- warn findings such as a pending reboot or a stale Defender scan, pushed one by one,
- a separate recovery message for every episode, including those the operator ended
  themselves by suppressing a pattern,
- a disk forecast repeated every 24 h.

Warn and recovery shared a colour, and crit and offline shared another. The cooldown made it
worse: a change blocked by it was dropped, so a new autostart entry could be lost behind a
batch of updates. A push channel that is mostly noise trains its reader to ignore it, which
defeats the point of ADR-0027.

## Considered Options

- **Tune thresholds and cooldowns only.** Rejected. It leaves one channel for two jobs:
  interrupting and informing. A longer cooldown also drops more of the signal.
- **A severity floor per channel** (e.g. Discord gets only crit). Rejected. Warn findings,
  missing agents and forecasts would reach no one: ADR-0027 exists because nobody opens
  the dashboard unprompted.
- **Route each notification by whether it needs action now, batch what can wait into one
  daily message, and edit pushed messages on recovery.** Chosen.

## Decision Outcome

**Routes.** `push` goes to the human channels now; `daily` is collected into one daily
summary; `record` stays in the events table and the ticket queue.

- Push: a crit (with its pass's warn lines as context), an inventory change on an
  operator-editable allowlist, and the recovery of a pushed episode.
- Daily: a warn, a disk forecast (once per episode), and a *missing* host, meaning one silent
  for days.
- Record: every other change, and the recovery of an episode that was never pushed or that
  the operator's own write caused.

**Channels.**

- Human channels (ntfy, Discord) receive `push` only. The generic webhook is a machine
  integration point: it receives every route, labelled.
- Being *offline* notifies no one. It only pauses health evaluation.
- The routing is orthogonal to `ticket_rules`: a warn still opens its ticket.

**The daily summary** is derived from `alert_state`, not from a queue. It lists findings
routed daily since the last summary that are still open, and sends nothing when nothing is
new.

**Editing in place.** On a channel that can edit (Discord), a pushed alert's message id is
kept and the recovery rewrites that message: lines struck through, then green once every
finding has resolved. There is no second message, and an edit does not notify anyone in
Discord.

### Consequences

- Good, because a push means "act now" again, and the channel shows the current state
  instead of a log of state changes.
- Good, because nothing stops being recorded or ticketed; only interruption moved.
- Good, because an allowlisted change bypasses the cooldown, so a security-relevant addition
  is never dropped.
- Bad, because a warn now reaches the operator up to a day later. A warn that must not
  wait has to be expressed as crit in `health_rules.py`.
- Bad, because an allowlisted entry that flaps pushes on every snapshot. This is accepted,
  because the flapping is itself a signal.
- Bad, because edit-in-place adds persisted channel state (`notification_messages`). A
  replaced webhook URL leaves stale ids, whose edits fail and are dropped.

## More Information

- Amends [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md): transitions and
  cooldowns still decide *whether* something is notified; this record decides *who* hears it.
  Channels stay live settings per [ADR-0054](0054-alert-channels-as-live-settings.md).
- Config: `KENNY_ALERT_DAILY_HOUR`, `KENNY_ALERT_MISSING_AFTER_DAYS`,
  `KENNY_ALERT_CHANGE_PUSH`.
- Code: `kenny-server/kenny_server/alerting.py` (routes, missing host, daily summary),
  `kenny-server/kenny_server/notify.py` (Discord rendering and edit),
  `kenny-server/kenny_server/store.py` (`AlertStateStore`).

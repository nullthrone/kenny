# 0059. The inbox is the ticket queue

- Status: proposed
- Boundary moved: **what may enter the operator's work queue** — only an entity with a
  lifecycle, never a derived value; and **the observability storage model** — an emitted
  alert now carries a durable reference to the ticket it belongs to.
- Touches: [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md),
  [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md),
  [ADR-0049](0049-ticket-blocked-on-axis.md)
- Date: 2026-09-11

## Context and Problem Statement

`GET /api/inbox` merged three row kinds into one list: tickets, flagged health sections,
and held approvals. They are not the same kind of thing.

A **ticket** has a lifecycle — it can be claimed, worked, blocked, resolved, and a person
can be finished with it. A **flagged section** has none: it is a verdict recomputed from
the newest snapshot on every request, with no identity, no history and nothing to resolve.
A **held approval** has none either, because it is not a separate thing at all — it is a
state of the ticket that holds it (`blocked_on='approval'`, one open gate per ticket,
enforced by a partial unique index), and that ticket was already in the same group.

Two failures followed from the mixture, and neither is cosmetic:

**The queue showed one finding twice.** `DEFAULT_DECISION["health"]` is `open_all`, so a
crit/warn transition already opens a ticket. The section row beside it was usually the
same finding again — once as a live state, once as the case opened from it.

**A queue row led out of the queue.** A section has no object in the inbox, so its link
went to `#/fleet/{host}?section=…`: a different destination, a different breadcrumb, and
no way back to the group and scroll position the reader came from. That is not a sloppy
route; it is what a row with nowhere of its own to go must do.

ADR-0046 already settled that the case is the entity and everything else is a binding to
it. The inbox had not been rebuilt on that.

A third gap was invisible rather than annoying: nothing recorded *which* alerts a ticket
was about. The connection existed only implicitly through the dedup key, and a recurrence
was written to the trail as prose ("the same condition alerted again: …"). A reader could
not ask what fired, how often, or whether it is still true.

## Considered Options

- **Keep the merge, fix the link.** Make the section row open its detail inside the inbox.
- **Make everything a ticket, including standing findings.** Gap-fill: any crit/warn
  section without an open ticket gets one.
- **Only entities with a lifecycle enter the queue.** Derived state is read where it is
  derived; a finding reaches the queue only through the rule that opens a ticket.

## Decision Outcome

Chosen option: **"only entities with a lifecycle enter the queue"**, because the
duplication and the surprising jump are both symptoms of one cause — a row that is not a
thing — and only this option removes the cause rather than decorating it. Giving a
recomputed verdict a detail view inside the queue would make it look like an object
without making it one; the next question ("why can't I close it?") has no answer.

**No gap-filling.** A standing crit finding whose ticket was cancelled does not come back
as a ticket. The operator who cancelled it decided something, and a queue that immediately
re-raises what was just dismissed teaches people to ignore it. Standing findings are read
on Fleet and Today, which is where they are derived.

**An approval is shown, not offered, in the queue.** A row says its ticket waits for an
approval; the decision is made where the frozen call it would run is shown beside it.
Deciding from a list, beside a title, is deciding without the evidence — and ADR-0045's
rule that a tier is never permission to skip a confirmation is worth little if the
confirmation is shown without what it confirms. What this fixes is the *form* a decision
must take, not which screen takes it: which surface carries the card is a UI question and
is answered in the code, not here.

**An emitted alert records its ticket.** `events` gains a nullable `ticket_id`, written
after the ticket decision and inside alerting's existing swallow, so the link can never
make a notification late or lost (ADR-0027). A column and not a join table: the relation
is one-to-many with the alert on the many side, `events` has exactly one writer, and
pruning drops the link together with the row instead of leaving an orphan.

**A ticket's subject is read from its dedup key, not stored again.** The key already names
the sections; a second column would be a second place for the same fact to be wrong.

### Consequences

- Good, because the queue's counts are now exactly `TicketStore.counts()`. The header
  badge and the NEEDS YOU chip are one number by construction — `docs/dashboard.md` had
  claimed that while the merge made it false.
- Good, because every row opens its own ticket. No row in the queue leads to another
  screen.
- Good, because an alert-opened ticket can answer "what fired, how often, and is it still
  true" without leaving the page.
- Bad, because a crit finding that is not ticketed — a `never` rule, or a cancelled
  ticket — is no longer visible in the queue. It is on Fleet and Today, one destination
  away. This is the accepted cost of the membership rule and the reason it is recorded.
- Bad / accepted, because the alert history is bounded by event retention (~30 days,
  `EventStore.prune`) while tickets are not. An old ticket's opening alert can be gone;
  the panel renders that as an empty history, never as an error.
- Neutral: the row badge became the ticket's priority. The origin moved into the row's
  secondary line, because the `ALERT` badge was what used to tell a reader that kenny
  opened the case rather than a person.
- Deliberately not done: a drawer or split pane. Opening a ticket is a screen change and
  that was never the complaint; landing on a *different* screen was.
- Explicitly out of scope: Today (`#/today`) stays a state mirror and keeps its section
  links. It is the lage overview, not a work queue, and the membership rule is about queues.

## More Information

Builds on [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md) (the case is the
entity) and [ADR-0049](0049-ticket-blocked-on-axis.md) (the five groups and the blocked-on
axis this queue renders). Leaves [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md)'s
transitions-only rule untouched: nothing here makes the alert loop react to standing state.
No wire-contract impact — this sits entirely above the agent tunnel, as
[ADR-0050](0050-the-ticket-is-its-own-chat-surface.md) recorded for the ticket surface;
`docs/protocol.md` and `docs/fixtures/` are unchanged and `PROTOCOL_VERSION` does not move.

Code: `kenny-server/kenny_server/webui/inbox.py`, `alert_subject.py`, `ticket_alerts.py`,
`store.py` (`EventStore`), `alerting.py` (`_dispatch`, `_forecast_alert`), `main.py`
(`alert_dedup_key`, `open_alert_ticket`), `ticketstore.py` (`_migrate_dedup_keys`),
`kenny-web/src/views/Inbox.tsx`, `kenny-web/src/views/inbox/InboxRow.tsx`,
`kenny-web/src/views/ticket/LinkedAlerts.tsx`.

Tests: `tests/test_inbox_api.py`, `test_alert_subject.py`, `test_ticket_alerts.py`,
`test_main_wiring.py`, `test_migration.py`, `test_alerting.py`,
`kenny-web/src/views/ticket/LinkedAlerts.test.tsx`,
`kenny-web/src/views/inbox/InboxRow.test.tsx`.

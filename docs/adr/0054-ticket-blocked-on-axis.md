# 0054. The ticket lifecycle splits into two axes: state and blocked-on

- Status: proposed
- Date: 2026-08-04
- Amends: [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md)

## Context and Problem Statement

ADR-0050 made the ticket a first-class entity with a state machine, and the state machine
that shipped had nine states: `new`, `triage`, `in_progress`, `awaiting_user`,
`awaiting_approval`, `awaiting_agent`, `resolved`, `closed`, `cancelled`. In practice most
of that enum was decorative. From the dashboard alone a ticket could only ever go
`new → resolved → closed` (plus Reopen); `triage`, `awaiting_user`, `awaiting_approval` and
`awaiting_agent` were written exclusively by the Discord loop, and **`cancelled` had no
dashboard affordance at all** — `_ACTORS` granted the requester a right to cancel their own
ticket from any live state, but `POST /tickets/{tid}/transition` floored at `operator` and
carried no ownership check, so that right had no HTTP route. A server with no Discord
configured could not reach six of the nine states from any UI, while the state filter
dropdown offered all nine regardless.

Two further defects sat underneath the reachability problem, and both point at the same
root cause. First, a blocked state had no clock and no consequence: `awaiting_user` could
sit forever with nobody nudged, and `awaiting_agent` meant "waiting on an operator to pick
this up" (`docs/itsm.md`) with no field recording an operator and no action to pick it up.
Second, `triage` existed for roughly one Python statement — `discord_service.open_ticket`
wrote it and overwrote it with `in_progress` on the very next line — while being the *only*
exit from `new`.

All three defects trace to one modelling error: the enum conflated **where a ticket is in
its life** with **who the ball is with**. `awaiting_user`/`awaiting_approval`/
`awaiting_agent` are not lifecycle phases; they are one phase (being worked) plus a reason
it is stalled. Splitting the two into separate axes is what makes every remaining state
reachable and driveable, gives a stall a subject and a clock, and replaces three
independent stall mechanisms with one.

## Considered Options

- **Leave the nine states and only fix reachability** (route affordances from
  `can_transition()`, add a dashboard button for `cancelled`). Rejected: it fixes the
  symptom, not the cause — `awaiting_agent` still has no assignee, `awaiting_user` still
  has no clock, and `triage` still exists for one statement. The dashboard would also still
  need three near-identical stall mechanisms instead of one.
- **Two axes: `state ∈ {new, in_progress, resolved, closed, cancelled}` plus
  `blocked_on ∈ {"", user, approval, operator}`, meaningful only while
  `state == "in_progress"`.** Chosen.
- **Keep a flat enum but widen it further** (e.g. add `blocked_on_user`,
  `blocked_on_operator_stale` as explicit states to give staleness its own name). Rejected:
  this makes the reachability and consequence problems worse, not better — more states to
  wire dashboard affordances for, and the state/reason conflation compounds instead of
  resolving.

## Decision Outcome

Chosen option: **the two-axis split**, because it is the only one of the three that removes
the modelling error rather than working around it. `_ALLOWED` shrinks from 24 edges over
nine states to 7 edges over five:

```
new         -> in_progress, resolved, cancelled
in_progress -> resolved, cancelled
resolved    -> closed, in_progress
closed      -> ()          # terminal
cancelled   -> ()          # terminal
```

Every one of those edges now has a dashboard button, computed from
`TicketService.can_transition()` and served in the API payload as `allowed_transitions` —
the same field the dashboard renders its buttons from, so an unreachable option can no
longer be offered (`kenny_server/webui/tickets.py`, `kenny_server/webui/index.html`).
`POST /tickets/{tid}/transition` floors at `user` instead of `operator`, with the existing
per-ticket ownership check every other handler already carries — this is what finally gives
a requester's cancel right an HTTP route.

Blocking is **not a transition**: `TicketService.block()`/`unblock()` are their own
chokepoint methods with their own actor tables (`_BLOCK_SETTERS`/`_UNBLOCK_CLEARERS`),
mirroring `transition()`'s discipline that `TicketStore.set_state`/`set_blocked` are never
called from anywhere else. Only `system`/`operator` may set a block — nobody blocks their
own ticket by asking a question of themselves — but the *requester* may clear their own
`user` block (answering kenny's question), while only an operator may clear a block that
was escalated to `operator` by the stall sweep: `system` may raise that escalation but must
not be able to quietly undo it. One hand-authored guard sits outside both tables: `system`
may not drive `in_progress -> cancelled` while `blocked_on == "approval"` — cancelling out
from under an open approval gate is a human decision (the requester withdrawing, or an
operator), never a side effect of a lifecycle move.

The stall sweep (`TicketService.nudge_stalled`, run from the existing `ticket_sweep_loop`
alongside `expire_due`/`auto_close_resolved`) gives a block a clock without adding a second
one: a ticket blocked on `user` or `operator` past `KENNY_TICKET_STALL_NUDGE_SECS`
(default 2 days) gets one reminder via a registered `StallNotifier` — the same
constructor-time-registration pattern `GateResumer` already established, so `tickets.py`
never has to import Discord or model code to know something has to be told. Past
`KENNY_TICKET_STALL_GIVEUP_SECS` (default 7 days) a `user` block that is still unanswered
re-blocks as `operator` — a human needs to pick it up, since the person it was waiting on
did not answer. **`approval` is deliberately excluded from both passes**: it already has
the gate TTL (`KENNY_TICKET_APPROVAL_TTL_SECS`/`expire_due`), and a second clock on the
same wait would double-nudge or double-escalate the same block.

`TicketService.assign()` adds an `assignee_user_id` column and a claim action, closing the
consequence gap `awaiting_agent` had: a ticket needing an operator now has somewhere for
"which operator" to live and a button to set it, independent of the lifecycle state.

The dashboard stops holding a second copy of the vocabulary. `GET /api/tickets/vocabulary`
serves `STATES`/`BLOCKED_REASONS`/`PRIORITIES`/`KNOWN_CATEGORIES` from the live module —
the same pattern `ticket_rules.py`'s `/api/ticket-rules/vocabulary` already established for
auto-ticket rules (ADR-0053) — and `kenny_server/webui/index.html` fetches it once instead
of hardcoding `TICKET_STATES`. `GET /api/tickets/summary` buckets the fleet's tickets into
*needs you / waiting / working / new / done* so the list groups by who the ball is with
instead of by a nine-value dropdown nobody could act on uniformly.

### Migration

There is no migration framework beyond `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`
(the same idiom `UpdateStore._migrate` uses for its `channel` column, ADR-0052).
`TicketStore._migrate` adds `blocked_on`/`blocked_since`/`blocked_ref`/`blocked_nudged_at`/
`assignee_user_id` and folds every legacy row: `triage -> in_progress`,
`awaiting_user -> in_progress + blocked_on="user"`, `awaiting_approval -> in_progress +
blocked_on="approval"`, `awaiting_agent -> in_progress + blocked_on="operator"`. The
backfill is idempotent by content — a folded row no longer matches its old `state` string,
so a second `connect()` touches nothing — not by a "migration already ran" flag.

**The historical `ticket_events` trail is not rewritten.** A pre-migration row's
`from_state`/`to_state` keeps its original nine-state-era value. ADR-0050 makes the trail
the authority and states outright that "where they disagree, the trail is the authority" —
back-dating old rows to read as if the new model had always been in force is exactly the
kind of rewrite that stance forbids. The timeline renderer already treats these columns as
opaque strings, so a legacy row keeps displaying correctly; it simply says `awaiting_approval`
instead of `in_progress` for events that predate this ADR.

### Consequences

- Good, because every dashboard-offered move is now a move the service will actually
  accept — `allowed_transitions`/`allowed_blocks`/`can_unblock` are computed by the same
  `can_transition`/`can_block`/`can_unblock` the API enforces, not a second approximation.
- Good, because `cancelled` is reachable from a server with no Discord configured for the
  first time, and a requester's pre-existing cancel right finally has an HTTP route.
- Good, because a stalled ticket now has one clock and one escalation path instead of none,
  and a ticket needing an operator has somewhere for "which operator" to live.
- Good, because the dashboard's vocabulary can no longer silently drift from the service's
  — the exact failure mode `TICKET_STATES`/`TICKET_TERMINAL` had become.
- Bad, because this is a breaking change to the ticket data model: every caller of
  `transition()` into a retired state (`triage`, `awaiting_*`) now gets `unknown_state`
  (400) instead of an old behavior, and every consumer of `ticket.state` that special-cased
  one of the retired strings had to be found and updated (`discord_service.py`,
  `webui/index.html`, `scripts/screenshots/seed.py`).
- Bad, because a pre-migration ticket's trail and its current `state`/`blocked_on` now use
  different vocabularies for the same underlying wait — a reader has to know the migration
  happened to reconcile `awaiting_approval` in the timeline with `in_progress` +
  `blocked_on="approval"` on the ticket itself.
- Neutral: this ADR does not change who may do what, only how it is represented —
  `_ACTORS`/`_BLOCK_SETTERS`/`_UNBLOCK_CLEARERS` preserve every authorization decision the
  nine-state table made (a requester still cannot resolve their own ticket or grant their
  own approval; `system` still cannot leave a ticket's own gate unresolved by force).

## More Information

- Amends [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md): the lifecycle it
  introduced is restructured, not reversed — the ticket is still the entity, the trail is
  still the authority, and durable gates still work exactly as that record describes.
- Answers the deferral in [ADR-0053](0053-operator-configurable-auto-ticket-rules.md)'s
  Consequences: "deduplicating against an already-open ticket ... is a real idea,
  deliberately deferred to its own ADR because it changes ticket semantics, not alerting
  policy" — this is that ADR, though it addresses reachability and staleness rather than
  deduplication specifically; per-`(agent_id, section)` deduplication remains open.
- Patterned on [ADR-0045](0045-reliability-alarm-suppression.md) and
  [ADR-0052](0052-second-release-channel-dev-prereleases.md) for the migration idiom
  (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`, deviation-only additive columns).
- Code: `kenny-server/kenny_server/tickets.py` (`STATES`, `BLOCKED_REASONS`, `_ALLOWED`,
  `_ACTORS`, `_BLOCK_SETTERS`, `_UNBLOCK_CLEARERS`, `TicketService.block`/`unblock`/
  `assign`/`nudge_stalled`), `kenny-server/kenny_server/ticketstore.py` (`_migrate`,
  `set_blocked`, `set_assignee`, `mark_nudged`, `counts`), `kenny-server/kenny_server/discord_service.py`
  (`on_hold`, `_run_turn`, `resume`, `notify_stalled`), `kenny-server/kenny_server/webui/tickets.py`
  (`/api/tickets/vocabulary`, `/api/tickets/summary`, `/block`, `/unblock`, `/assign`),
  `kenny-server/kenny_server/config.py` (`KENNY_TICKET_STALL_NUDGE_SECS`,
  `KENNY_TICKET_STALL_GIVEUP_SECS`).

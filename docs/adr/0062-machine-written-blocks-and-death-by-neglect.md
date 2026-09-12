# 0062. A block is machine-written, and a ticket nobody works dies of neglect

- Status: accepted
- Boundary moved: the ticket lifecycle model — who may put a ticket into a waiting state,
  and whether a ticket can reach a terminal state with no actor at all.
- Date: 2026-09-12
- Amends: [ADR-0049](0049-ticket-blocked-on-axis.md)

## Context and Problem Statement

ADR-0049 split the lifecycle into `state` and `blocked_on` and gave every remaining edge a
dashboard button computed from `can_transition()`/`can_block()`. Making each value
*reachable* was the right fix for an enum most of which was decorative. Making each value
reachable **by hand** was not, and three defects follow from it.

**A hand-set block has no referent.** `POST /tickets/{tid}/block` never passed a `ref`, and
`_check_block` never wanted one. Setting `blocked_on="approval"` from the console therefore
produced a ticket waiting on an approval that does not exist. Three surfaces then asserted
it — the header chip ("AWAITING APPROVAL"), the inbox row ("waiting for approval") and the
timeline ("parked this ticket on an approval") — while the banner that would carry the
decision rendered nothing, because there was no pending row to render. Nor could it ever
end: `expire_due` iterates approval rows and finds none, and `nudge_stalled` excludes
`approval` by design, so the ticket sat in *needs you* until a human clicked Unblock. The
real gate path (`ticket_assistant.open_approval`) creates the row first and blocks with
`ref=approval.id`; it was the only producer of a coherent `approval` block all along.

**Claiming and retargeting model a workplace kenny does not have.** `assignee_user_id` was
introduced by ADR-0049 to close a "consequence gap" left by retiring `awaiting_agent` — the
reasoning was that a ticket needing an operator ought to have somewhere for *which*
operator to live. That is an argument from model symmetry, not from need. In a household
installation there is no handover of responsibility between operators: two of them may work
the same ticket at once, and `ticket_events` already records who did what and when. The
column was read only by the model briefing; the console never displayed it, so the only way
to learn a ticket's assignee was the wording of its Claim button. Retargeting has the same
shape: a ticket is about the machine the problem is on, and `webui/tickets.py` already
called that target "an authorization control" while offering a button that moved it.

**A ticket nobody works never ends.** The stall ladder ADR-0049 built runs nudge → escalate
to `operator` → nothing. An escalated ticket sits in *needs you* indefinitely, and a `new`
alert-origin ticket in a deployment without triage never leaves `new` at all.

## Considered Options

- **Hide the affordances in the console only.** Rejected: the routes stay open, and the
  console starts suppressing options the server advertises — the second copy of the
  vocabulary ADR-0049 set out to abolish.
- **Require a `ref` on a hand-set block.** Fixes the phantom without answering who would
  legitimately type one. A person asserting a wait they did not create has no `ref` to give.
- **Narrow the actor tables so blocks are machine-written, delete claim and retarget, and
  add a fourth rung to the stall ladder.** Chosen.
- **A new `abandoned` state** rather than `cancelled` + `resolved_by`. Rejected: exactly the
  enum inflation ADR-0049 was written against.

## Decision Outcome

**Blocks are written where the wait begins.** `_BLOCK_SETTERS` is `{"system"}` for all three
reasons, so a block always carries the `ref` of the thing being waited for.
`_UNBLOCK_CLEARERS["approval"]` becomes `{"system"}` to match: a gate is settled by a
decision or by its TTL, both of which resolve the held call, and a person declaring the wait
over would strand a pending approval behind an unblocked ticket — the phantom inverted. The
requester may still clear their own `user` block and an operator may clear `user` or a
`operator` escalation, because the answer often arrives out of band.

**`TicketService.assign`/`reassign` and `TicketStore.set_assignee`/`set_agent_id` are
deleted**, along with their routes. `tickets.assignee_user_id` stays as a column with no
writer and no reader: rows written before this record carry a value, and `ticket_events`
keeps the `assign`/`handoff` rows that explain them. Retiring a kind does not rewrite a
trail (ADR-0046), so both still render.

**`TicketService.abandon_stale` cancels a `new`/`in_progress` ticket no person has touched
for `KENNY_TICKET_ABANDON_SECS` (default 14 days), with `resolved_by="inactivity"`.** It
runs as the fourth pass of `sweep()`, after `nudge_stalled`, so a ticket the escalation pass
has just handed to a human is not dropped in the same tick. `blocked_on="approval"` is
excluded: `transition()` denies an open gate on the way into any terminal state, and ending
a ticket here would make that denial a side effect of ageing — the thing the hand-authored
guard in `_check_transition` refuses for `system`. Cancelled rather than resolved because
nothing was solved, and because `closed` is reachable only through `resolved`, so resolving
first would both claim a resolution that never happened and route around that same guard,
which covers `cancelled` alone.

**The clock is a new column, `tickets.last_human_at`, not `updated_at`.** This is the part
that is not optional. `updated_at` is wrong in both directions: a note, a tool call and a
message go through `_insert_event`, which never touches the tickets row, so a ticket an
operator is actively annotating looks untouched — while `set_blocked` does bump it,
including from this module's own escalation pass and from the assistant's gate handling, so
a ticket only machines have touched looks freshly handled. `last_human_at` is written in
exactly one place, `_insert_event`, when the actor matches `operator:`/`user:` — every human
action already leaves a trail row, so the clock needs no second write site and no new route
can bypass it. It is seeded to `created_at` at creation (empty would sort before every
stamp, making a new ticket read as infinitely idle) and backfilled from the trail at
migration, falling back to `created_at`.

**`PATCH /api/tickets/{tid}` now writes a `kind="patch"` trail row naming the actor and the
fields changed.** It wrote nothing before: a title or priority could be rewritten with no
record of by whom. Closing that audit gap is what makes `_insert_event` a complete account
of human activity, so one change settles both.

### Consequences

- Good, because the state three surfaces described as a pending decision that did not exist
  can no longer be created — by the console, by `curl`, or by a future route.
- Good, because a ticket that has been forgotten now ends, and says in `resolved_by` that
  nobody decided it, so *dropped* stays distinguishable from *withdrawn*.
- Good, because the ticket page falls to one primary action plus, at most, one contextual
  button and one quiet exit, without any option being hidden from a server that still offers
  it — `allowed_blocks` is genuinely empty now.
- Good, because field edits are auditable for the first time.
- Bad, because this narrows the actor tables ADR-0049's "Neutral" consequence promised to
  preserve. That promise was kept for a model in which a human describing a wait was
  coherent; the `approval` phantom is the proof that it was not.
- Bad, because a requester whose ticket has no bound Discord thread is not told it was
  dropped — alert-origin tickets have nobody to tell, but a dashboard-origin one only learns
  from the inbox.
- Bad, because `last_human_at` is a second clock on a row that already has `updated_at`, and
  a reader has to know which question each answers.
- Neutral: `blocked_ref` still has no reader. With hand-set blocks gone it is now always the
  real gate's id, which makes linking the ticket to its approval possible; that is a
  separate change.

## More Information

- Amends [ADR-0049](0049-ticket-blocked-on-axis.md): the two axes and the five states stand
  exactly as recorded. What changes is who may move the second axis, and that the ladder it
  started now has a last rung.
- Related: [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md) (the trail is the
  authority and is never rewritten — why the retired columns and event kinds stay),
  [ADR-0059](0059-the-inbox-is-the-ticket-queue.md) (the buckets `blocked_on` feeds),
  [ADR-0032](0032-runtime-settings-in-the-dashboard.md) (`KENNY_TICKET_ABANDON_SECS` is a
  live setting by virtue of being in the catalog).
- Code: `kenny-server/kenny_server/tickets.py` (`_BLOCK_SETTERS`, `_UNBLOCK_CLEARERS`,
  `abandon_stale`, `sweep`, `update`), `kenny-server/kenny_server/ticketstore.py`
  (`last_human_at`, `_is_human_actor`, `_migrate`, `list(human_before=…)`),
  `kenny-server/kenny_server/config.py`, `kenny-web/src/views/ticket/TicketActions.tsx`.

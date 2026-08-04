# 0055. The ticket is its own chat surface

- Status: proposed
- Date: 2026-08-04
- Amends: [ADR-0049](0049-tiered-tool-classification.md), [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md)
- Touches: [ADR-0048](0048-delegated-identity-from-a-chat-platform.md), [ADR-0054](0054-ticket-blocked-on-axis.md)

> Numbered 0055, not 0054: this record and
> [ADR-0054](0054-ticket-blocked-on-axis.md) (the lifecycle's two-axis split) were developed
> concurrently and both first claimed 0054. That record landed first, so `on_hold`/the turn
> cap/`resume` below are written against its `blocked_on` axis (`block()`/`unblock()`), not
> the nine-state model this record's own extraction started from — the extraction moved the
> code, ADR-0054 changed what it says.

## Context and Problem Statement

A ticket's assistant loop — the session that gates and drives kenny's tool calls against
the one PC a ticket is pinned to — has only ever had one door: a Discord thread.
`DiscordService` bundled the transport (gateway, threads, slash commands, identity mapping)
and the loop itself (session rebuild, the four-control gate, turn-driving) into one 2000+
line module. In the dashboard an operator could read a ticket, note it, reassign it, resolve
it — but not talk to kenny about it. A household with no Discord bot configured had no
ticket assistant at all, on any surface.

Two pre-existing gaps ride along with this and are worth fixing at the same time:

1. **Kenny's own replies leave no durable wording anywhere.** A `message` trail row for a
   reply has only ever carried a bare summary; the actual text lived solely in the
   resume-only transcript that ADR-0050 already prunes 30 days after close. Reload the
   ticket a month later and what kenny said is gone, even though the ticket itself is not.
2. **A lifecycle move made from the dashboard is invisible to Discord.** Resolve, cancel,
   close, and the auto-close sweep changed the ticket's state without telling its thread —
   the requester never found out their ticket was done unless they happened to check the
   dashboard.

Giving the ticket detail view its own chat means answering, again, three questions this
codebase already settled once for Discord: what may a non-operator's message make kenny do
autonomously (ADR-0049), whose identity and controls make that defensible (ADR-0048), and
what does a ticket keep a record of (ADR-0050)? A second transport does not get to reopen
those on its own terms — it either qualifies under what is already there, or the difference
has to be recorded honestly.

## Considered Options

- **Build a second, independent tool loop for the dashboard** — its own session shape, its
  own gate, its own trail-writing. Rejected outright: two implementations of "what is
  read-only, what is a standard change, what needs consent or approval" are guaranteed to
  drift from each other, which is exactly the class of divergence `kenny-server/CLAUDE.md`
  requires a joined test for. Building a second one is building the drift, not preventing it.
- **Keep the assistant Discord-only; let the dashboard show a read-only mirror of the
  Discord transcript.** Rejected: it does nothing for a household with no Discord configured,
  and it does not touch either of the two gaps above — a mirror of an ungated conversation
  is not a record of one.
- **Extract the transport-agnostic half of the existing loop into its own module, behind a
  narrow surface protocol, and make Discord its first implementation rather than its only
  caller.** Chosen.

## Decision Outcome

Chosen option: **the ticket-bound assistant loop moves out of `discord_service.py` into a
new `ticket_assistant.py`, and `DiscordService` becomes the first of (now) two callers of
it.** `toolloop.drive_events` — the actual agentic tool-use loop — is untouched by this
record; nothing new was built there. What moved is `TicketSession`, `TicketPolicy`, session
rebuild, and turn-driving: a transport split, not a new engine. A `TicketSurface` protocol
(`deliver_reply`, `announce_gate`, `on_transition`) is the seam a transport implements to
have a turn's output mirrored to it; the dashboard's chat route is not one of these — it is
a second, direct caller of `TicketAssistant.run_turn` that forwards the loop's own events as
Server-Sent Events, exactly the vocabulary the existing copilot chat already renders.

### Amends ADR-0050: the trail now carries verbatim text — for curated work, not for a private conversation

Every dashboard-typed message, and every one of kenny's own replies regardless of which
surface(s) it went out on, now writes its wording into the trail
(`TicketAssistant.append_message(..., verbatim=True)`). A Discord-origin message from a
family member is unchanged: the trail row for it still carries only a summary, exactly as
before this record.

This is not a reversion to ADR-0050's rejected "thread-as-record" option. ADR-0050 already
drew the distinction this amendment leans on: the trail and the paraphrase are deliberately
*neither* a transcript — a machine-readable record of what happened, plus a generated
prose explanation, and "the operator needs to know what kenny did and why, not everything
that was said." That reasoning was never an objection to storing wording as such; it was an
objection to storing somebody's private platform conversation verbatim for no operational
need. A dashboard message is not that: it is curated work — the same rationale
[ADR-0027](0027-persistent-chat-history.md) already accepted when it let the copilot's chat
history grow unbounded, because that history is the operator's own deliberate use of the
tool, not a conversation they merely happened to be a party to. Kenny's own reply carries
the same reasoning a step further: it is never a private family conversation regardless of
which door it left through, so it is recorded in full every time, even when it answered a
Discord-origin message. What stays exactly as opaque to kenny's storage as it always was is
the *other* half of a Discord exchange — a family member's own words are still a summary
line and nothing more.

Named honestly, the consequence this carries: the trail was already unpruned by design, and
it now holds more per ticket than it used to. There is still no prune knob for it — there
never was one — but the volume a long-lived, chat-heavy ticket accumulates is larger than it
would have been before this record. `_MAX_TRAIL_TEXT_CHARS` (20,000, in `ticket_assistant.py`)
bounds one row's contribution; it does not bound the trail as a whole.

### Amends ADR-0049: a second surface now holds the ticket's tiered, autonomous gate

ADR-0049 separated a tool's tier (what kind of change it is) from a surface's policy (what
it does about each tier), and recorded that the dashboard holds *both* change tiers — every
state-changing tool there stops for a confirm, unchanged by that record. That statement was
true of the copilot; it is not what the ticket detail view now does. A message sent from a
ticket's own chat runs under `TicketPolicy.gate`, the same gate Discord has always used:
`read_only` runs immediately, `standard_change` runs autonomously with a trail row recording
that it did, `normal_change` holds for an operator, and a privacy-touching call holds for the
ticket's own requester's consent, consent always resolved before approval.

This is not the copilot's binary "both tiers always hold" gate reachable from a second URL —
it is a distinct, second surface that qualifies for tiered autonomy the same way Discord did,
because it carries the same controls: the ticket's target host is frozen at creation, its
tool set is cut by a capability profile, and the tier gate plus consent sit in front of every
call. The ticket detail view earns this not by being *the dashboard*, but by being *a ticket*
— the object ADR-0048's four controls were written for, reachable now from a second
transport rather than only from Discord.

### Touches ADR-0048: same controls, no delegation on this path

ADR-0048's four controls were framed for a platform kenny does not own, where the danger is
a second, parallel authorization system creeping in beside `webui/authz.py`. Three of the
four apply to the dashboard surface completely unchanged: the target host is fixed at
ticket creation and nothing in the conversation can move it (`TicketPolicy.resolve_target`
discards a model-supplied `agent_id`/`id` exactly as it always has); the capability profile
narrows the reachable tool set the same way, checked at both the schema and the dispatch
side; and the tier gate plus consent are the identical `TicketPolicy.gate` a Discord-driven
turn goes through.

The fourth control — delegated identity, and the "no parallel authorization" concern it
exists to police — does not arise on this path at all. It is not weakened or exempted; it
is simply not a question a dashboard caller raises. The caller is already authenticated by
the ordinary dashboard auth machinery (`OperatorAuthMiddleware`, resolving a `Principal`
from a session cookie or a PAT) before the ticket route is ever reached — there is no
external platform's identity assertion being trusted here, and therefore nothing for a
second authorization system to sit beside. The dashboard's own auth *is* the one
authorization system, on this path exactly as on every other `/api/*` route. Recording this
plainly matters so that a later reader does not mistake the fourth control's absence here for
a loosening of ADR-0048 — it is the same gate, reachable from a transport that never needed
the control in the first place.

### New in this record: the acting principal, not always the requester

Discord only ever had one possible speaker per actionable message: the ticket's own
requester. The dashboard breaks that assumption — an operator working someone else's ticket,
or the requester themselves, may be the one typing. `TicketAssistant.session_for(ticket,
actor=principal)` therefore builds a turn's authorization context from the *acting*
principal's own current role, capability profile, and host scope. The ticket's frozen
`role_snapshot`/`profile_snapshot` only narrow further when the actor **is** the requester:
that snapshot is the requester's own frozen context, and it must neither narrow nor widen a
third party who happens to be driving this turn instead. For Discord this changes nothing —
the acting principal there is always the requester on every actionable message — but it is
new, deliberate behavior for the dashboard, not a side effect of the extraction.

The turn cap (`KENNY_DISCORD_MAX_TURNS_PER_TICKET`) and Discord's per-account rate limit both
exist to bound *autonomous* work — the volume of turns a ticket runs with nobody watching.
An operator+-driven turn, from either surface, is exempt from both: the cap exists to block
a ticket on `"operator"` once it has run too long unattended
([ADR-0054](0054-ticket-blocked-on-axis.md)'s blocked-on axis), and an operator is by
definition the human that block was already waiting for. A scoped `user` chatting from the
dashboard is capped and rate-limited exactly like a Discord requester — the env vars keep
their historical, Discord-flavored names, but the cap they enforce is ticket-wide now,
across whichever surface drove the turn.

### A gap closed in passing: lifecycle moves now reach the thread

`TicketService.transition()` and `auto_close_resolved()` call a best-effort
`TransitionNotifier` (`tickets.set_transition_notifier`) after a state change commits;
`DiscordService.on_transition` uses it to post a short message and archive the thread on
`resolved`/`closed`/`cancelled`. This closes gap #2 from the Context above: a dashboard
Resolve/Cancel/Close, and the auto-close sweeper, used to leave a ticket's Discord thread
silent. It is a bug fix riding along with this feature, not new scope — `tickets.py` itself
stays transport-blind, knowing only that *something* wants to be told.

### Wire-contract impact

None. This surface sits entirely above the agent tunnel: `docs/protocol.md` and
`docs/fixtures/` are untouched, and there is no `PROTOCOL_VERSION` bump — the same posture
[ADR-0029](0029-push-alerting-ntfy-webhook-and-weekly-digest.md) and
[ADR-0048](0048-delegated-identity-from-a-chat-platform.md) record for their own additions.

### Consequences

- Good, because a household with no Discord bot configured now has a working ticket
  assistant at all — the assistant is constructed as soon as an Anthropic client is
  available, no longer gated on a Discord token.
- Good, because the extraction removes duplication risk rather than adding it: there is one
  gate, one session shape, one trail writer, and Discord and the dashboard are both callers
  of it, not two independent implementations that could silently diverge.
- Good, because kenny's own words, and an operator's own typed work on a ticket, are finally
  part of the durable record instead of living only in a 30-day-pruned transcript.
- Good, because a resolved/closed/cancelled ticket's Discord thread is no longer silently
  out of date — the requester who opened it over Discord finds out it is done, from Discord.
- Bad, because the trail — already unpruned by design — now grows with conversation volume,
  and there is still no knob to bound that growth; a long-lived, chat-heavy ticket accumulates
  more storage than it would have before this record.
- Bad, because "why can't I do this from the ticket chat?" now has one more axis to check
  than before: whose turn it is (operator vs. requester) changes which host-scope/profile
  applies, on top of the tier gate and consent that already applied.
- Neutral, because none of ADR-0048's, ADR-0049's, or ADR-0050's underlying guarantees moved
  — this record extends where they apply, it does not relax what they require.

## More Information

- Builds on [ADR-0009](0009-server-hosted-claude-chat.md) (the tool-use loop and its
  confirm-gate) and [ADR-0042](0042-explicit-per-call-agent-targeting.md) (the frozen-target
  guarantee this record does not touch: a ticket's `agent_id` remains unmovable by anything
  said in either surface's chat).
- Amends [ADR-0049](0049-tiered-tool-classification.md) (a second surface now qualifies for
  tiered autonomy) and [ADR-0050](0050-ticket-as-entity-chat-thread-as-binding.md) (the
  trail's `message` kind may now carry verbatim text for curated work).
- Touches [ADR-0048](0048-delegated-identity-from-a-chat-platform.md): three of its four
  controls apply unchanged, the fourth (delegated identity) does not arise on an
  already-authenticated transport.
- Implementation: `kenny-server/kenny_server/ticket_assistant.py` (the extracted loop —
  `TicketSession`, `TicketPolicy`, `TicketAssistant.session_for`/`run_turn`/`resume`/
  `append_message`, the `TicketSurface` protocol); `kenny-server/kenny_server/discord_service.py`
  (`DiscordService.deliver_reply`/`announce_gate`/`on_transition`, the Discord
  `TicketSurface` implementation); `kenny-server/kenny_server/tickets.py`
  (`TransitionNotifier`, `TicketService.set_transition_notifier`, called from `transition()`
  and `auto_close_resolved()`); `kenny-server/kenny_server/webui/tickets.py`
  (`api_ticket_chat_stream`, `build_ticket_routes`'s `POST /api/tickets/{tid}/chat/stream`);
  `kenny-server/kenny_server/webui/index.html` (`ticketComposerHtml`, `ticketOpenGate`,
  `handleTicketChatEvent`, `startTicketChatTurn`, `ticketEventHtml`'s `message` branch).
- Tests: `kenny-server/tests/test_ticket_assistant.py` (session built from the acting
  principal, not always the requester; operator-turn cap exemption); the chat-route
  coverage and the Discord-surface regression tests in `test_tickets_api.py` and
  `test_discord_service.py`; the transition-notifier seam test in `test_tickets.py`.

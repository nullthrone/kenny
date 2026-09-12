# 0063. The copilot proposes a ticket; it does not open one

- Status: proposed
- Boundary moved: **the agent/session model** — what a dashboard copilot turn may
  originate. Until now such a turn produced prose, a read of the fleet, or a gated
  change to a machine. It may now originate a *ticket*, and it gains the first tool
  whose effect is an affordance in the operator's browser rather than data for the
  model or a change on a host.
- Amends: [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md),
  [ADR-0009](0009-server-hosted-claude-chat.md)
- Touches: [ADR-0045](0045-tiered-tool-classification.md),
  [ADR-0050](0050-the-ticket-is-its-own-chat-surface.md),
  [ADR-0061](0061-the-ticket-keeps-a-record-not-a-transcript.md)
- Date: 2026-09-12

## Context and Problem Statement

An operator investigates in the Ask kenny drawer: a few read-only checks against one
host, and a conclusion — the update service is disabled, that is why updates fail. To
turn that into a ticket they leave the conversation, open the inbox, press NEW TICKET
and retype from memory what was just established. The conclusion the conversation
reached is discarded and reconstructed by hand, badly.

Getting it into a ticket instead raises two questions this codebase has answered
before, and one it has not.

**Who writes the sentence?** [ADR-0061](0061-the-ticket-keeps-a-record-not-a-transcript.md)
faced the same shape a week ago for a ticket's own turn summary and rejected a
second, server-side model call that re-reads the transcript to guess what mattered:
"the one participant that already knows is the one being summarised." That reasoning
holds here unchanged. The turn that ran the checks is the turn that should write the
draft.

**Who opens the ticket?** Less obvious, and the reason this record exists. Once the
copilot can write a draft, giving it `ticket_create` is one line away, and
`ticket_resolve` is one line after that.

## Considered Options

- **A deterministic "make a ticket" button in the drawer, prefilled by a second model
  call over the transcript.** Rejected on ADR-0061's grounds, which were about this
  exact trade: a summariser that has to re-derive what mattered, when a participant
  already knows.
- **A state-changing `ticket_create`, opened through the copilot's confirm gate.** The
  gate would show the operator the exact arguments and nothing would run unapproved —
  genuinely safe, and idiomatic to this surface. Rejected on what the gate *is*: an
  approve/deny on frozen arguments. A ticket's title and description are prose someone
  has to be able to fix, and "deny, re-prompt, deny again" is not editing.
- **The copilot gets the whole ticket lifecycle: create, start, block, resolve.**
  Rejected, at length, below.
- **Two read-only tools — the copilot proposes a ticket and looks for one that already
  exists — and the operator opens it through the route that already opens tickets.**
  Chosen.

## Decision Outcome

Chosen option: **`ticket_draft` and `ticket_find`, both `READ_ONLY`, and no write path
at all.** `ticket_draft` validates a title, a summary and a host and hands them back;
the dashboard renders them as the form the inbox's NEW TICKET modal already is, filled
in. The operator corrects the wording and submits, through `POST /api/tickets` — the
one route that has ever opened a ticket, with the authorization it already has.
`ticket_find` lists open tickets so a problem already on the queue is pointed at
rather than filed twice.

### Amends ADR-0046: a fourth origin, and the first one that is a proposal

ADR-0046 settled that a case arises equally from a chat platform, the dashboard, or an
alert, and that "nothing in its lifecycle knows what opened it." A copilot-drafted
ticket is a fourth origin on exactly those terms: `origin="copilot"`, a genesis row
reading "opened from an Ask kenny conversation", and no lifecycle behaviour of its
own. It is deliberately *not* an origin the unprompted investigation runs for
([ADR-0056](0056-unprompted-ticket-triage.md) reads `origin == "alert"` and nothing
else): triage exists because nobody looked at the alert, and here somebody just did.

What is new is that the origin is a *proposal*. The other three mint a ticket outright.
This one produces a form, and the ticket exists only if a person submits it.

### Why the lifecycle stays where it is

The copilot gets one verb, and it is not `create`. Three separate reasons, each
sufficient:

1. **"Who may move this ticket, and when" already has an implementation.** It is
   `TicketService.transition`'s actor rules, `webui/authz.py`'s ownership guards, and
   [ADR-0062](0062-machine-written-blocks-and-death-by-neglect.md)'s rule on who may
   write a block. A second set of verbs on the copilot would be a second answer to the
   same question, sitting beside that one rather than inside it — the drift ADR-0050
   refused when it refused a second tool loop.
2. **The copilot has no ticket.** [ADR-0050](0050-the-ticket-is-its-own-chat-surface.md)
   gave the ticket its own chat surface precisely so a conversation about a case runs
   under that case's frozen host, capability profile and tier gate. A copilot session
   is fleet-wide and bound to nothing; "resolve the ticket we were discussing" is a
   reference the model resolves, not a target the server froze.
3. **The work belongs on the other surface anyway.** A drafted ticket lands in the
   inbox with a chat of its own. Driving it from the drawer would mean two
   conversations about one case, and only one of them would be on its record.

### Touches ADR-0045: read-only, and why that is not a loophole

Both tools are `READ_ONLY`, so neither stops at the dashboard's confirm gate, and
ADR-0045's "the dashboard holds both change tiers" is untouched — there is no change
tier here to hold. `ticket_draft` is read-only because it *creates nothing*: its whole
output is text handed back to the browser. Putting a confirmation in front of it would
ask the same operator twice for the same ticket, once on a dialog and once on the form
that actually opens it, and the first of those two would be a confirmation for an
action that has no effect if refused. This is the same reasoning ADR-0061 recorded for
`ticket_summary`, applied to a tool that does even less.

The tier is doing no work here that withholding does not already do. Both names are in
`ticket_assistant.EXCLUDED_TOOLS`, so no ticket-bound turn and no unprompted
investigation can reach either — a ticket must not file tickets, and `ticket_find`
lists across requesters, which on a surface a host-scoped household member can reach
would be the one place they could read what everybody else has open.

### What the ticket records, and who wrote it

The drafted title and summary are the model's sentences as the operator left them —
curated work, the same standing ADR-0050 gave a dashboard-typed message. Beside them
the route writes one `note` row naming the read-only calls that actually ran in the
conversation: `diag_services` on pc-kid, `fs_disk_usage`, and so on.

That row is composed **on the server, from the chat session itself**, never by the
model and never from the request body. The browser sends only `chat_session_id`; the
server folds that session's transcript, counts a call only when its result came back
without an error, and drops the change-tier ones — what a chat session *changed* is the
audit log's question, not this ticket's. Evidence beside a conclusion is ADR-0060's
rule; deriving it rather than accepting it is what keeps the trail the authority
ADR-0046 made it.

### Wire-contract impact

None. Ticketing does not exist on the agent wire — `docs/protocol.md` and
`docs/fixtures/` are untouched, there is no `PROTOCOL_VERSION` bump, and neither tool
is registered on the MCP surface: an MCP client has no drawer for a form to appear in.

### Consequences

- Good, because the conclusion a conversation reached survives into the ticket without
  anybody retyping it, and without a second model call to re-derive it.
- Good, because the ticket is still opened by the person whose name goes on it,
  carrying the wording they left in the fields.
- Good, because there stays exactly one path that creates a ticket, so no authorization
  question about creation is answered twice.
- Good, because the drawer and the inbox ask the same questions: the fields are one
  shared component, so a ticket's shape cannot depend on where it was opened from.
- Bad, because a drafted ticket's title and summary are a model's prose, and an
  operator who submits the form unread files whatever it wrote. The form is the
  mitigation and it is only as good as the reading.
- Bad, because "why can I not do that from the drawer?" is now a real question with a
  two-part answer: the copilot may propose a ticket and may not move one.
- Bad, because `chat_session_id` is caller-supplied, so an operator can attach another
  conversation's evidence to a ticket. They are operator+, the note is attributed to
  them, and the alternative — a redeemable draft token with a store of its own — buys
  little against a caller who could type the same claim into the description.
- Neutral, because a draft is live only: reopening a conversation from history does not
  re-offer the form, since re-offering it invites a second ticket for a case the first
  one already covers.

## More Information

- Amends [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md) (a fourth origin)
  and [ADR-0009](0009-server-hosted-claude-chat.md) (a copilot tool result that
  addresses the surface, not only the model).
- Follows [ADR-0061](0061-the-ticket-keeps-a-record-not-a-transcript.md)'s reasoning on
  who writes a conclusion, and [ADR-0060](0060-the-ticket-shows-findings-the-trail-stays-the-audit.md)'s
  on carrying evidence beside one.
- Implementation: `kenny-server/kenny_server/copilot_tickets.py` (both handlers and the
  evidence derivation); `kenny-server/kenny_server/toolloop.py` (the two `SERVER_TOOLS`
  schemas); `kenny-server/kenny_server/ticket_assistant.py` (`EXCLUDED_TOOLS`);
  `kenny-server/kenny_server/chat.py` (`_surface_events`, the system prompt);
  `kenny-server/kenny_server/webui/tickets.py` (`chat_session_id` and the evidence row);
  `kenny-web/src/components/TicketDraftForm/` (the fields both surfaces share);
  `kenny-web/src/components/AskKennyDrawer/TicketDraftCard.tsx`.
- Tests: `kenny-server/tests/test_copilot_tickets.py` (the offered/withheld seam in one
  test, a draft writing no ticket, the evidence derivation);
  `kenny-server/tests/test_tickets_api.py` (the note the route writes, and the three
  ways it writes none); `kenny-web/src/components/AskKennyDrawer/TicketDraftCard.test.tsx`
  (the operator's edits are what gets filed).

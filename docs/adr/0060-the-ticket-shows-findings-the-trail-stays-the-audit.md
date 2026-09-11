# 0060. The ticket shows findings; the trail stays the audit

- Status: accepted
- Boundary moved: the observability/record model — the trail remains the complete audit
  but is no longer what a ticket *shows*; a deterministic projection now sits between the
  trail and every surface that reads it.
- Amends: [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md),
  [ADR-0050](0050-the-ticket-is-its-own-chat-surface.md)
- Touches: [ADR-0056](0056-unprompted-ticket-triage.md),
  [ADR-0045](0045-tiered-tool-classification.md)
- Date: 2026-09-11

## Context and Problem Statement

ADR-0046 settled what a ticket records — a machine-readable trail of every message, tool
call with its arguments, gate and state change — and said, in the same breath, that this
trail is what the detail view shows. The second half was an assumption, not a decision,
and a year of the trail growing has shown what it costs.

A real ticket (#76, an alert about failing Windows updates) renders as: an opening state
row, an "opened from an alert" note, a "work started" row, six `tool_call` rows each with
its raw argument JSON, a gate row carrying the verdict payload, the verdict itself, a
second `tool_call` row carrying the *same* verdict payload again, a "waiting for a reply"
block row and a "stall reminder sent" note. Three findings' worth of meaning — the disk is
full, kenny looked, kenny has no more to do without a person — arrive as fifteen rows, with
the one conclusion printed three times.

Two separate confusions produce that. First, **the audit and the history are different
documents.** Tool arguments are in the trail because ADR-0046 needed "why did this run?"
answerable on a surface that acts with nobody present; that is a query, and a query's
answer does not have to be the page's prose. Second, **the machinery's own bookkeeping
looks like events.** `work started`, `waiting for a reply`, `stall reminder sent` and the
verdict tool's two `tool_call` rows are shadows of things the page already states — the
status chip, the gate card, the finding — not additional things that happened.

A third, smaller version of the same problem sat under the timeline: two permanent
composers, "Ask kenny about this ticket" and "Add a note", asking the reader to choose
between them before knowing which they wanted.

The question: can the ticket be made readable without weakening what the audit — and what
`triage.may_resolve` — can see?

## Considered Options

- **Write less to the trail.** Stop recording tool arguments, drop the bookkeeping rows at
  the source. Rejected outright: `triage.may_resolve` (ADR-0056) decides whether an
  unprompted verdict may resolve a ticket by looking for a *successful read-only
  `tool_call` row*, and ADR-0046 put arguments there precisely so an unattended call can be
  explained afterwards. Making the record thinner to make the page shorter trades an
  irreversible thing for a reversible one.
- **A severity or audience column on `ticket_events`.** Rejected: it freezes "is this
  noise?" at write time, where the answer is not yet known, and it is a migration every
  time the answer changes. The question belongs to the reader, not the writer.
- **Filter in the browser.** Cheapest, and rejected: Discord and the ticket briefing need
  the same sentences, and a rule that lives in `eventFormat.ts` is a rule the server cannot
  reach. This codebase's own standing objection to two implementations of one judgement
  (ADR-0050's rejected "second tool loop") applies unchanged.
- **Chosen: a deterministic projection on the server, and a second tab for the trail
  itself.**

## Decision Outcome

Chosen option: **`ticket_timeline.project()` maps the trail onto presented entries, and the
ticket detail view reads that by default with the raw trail one tab away.** The projection
is a pure function with no I/O and no model in it; `GET /api/tickets/{tid}/timeline` serves
it, `GET /api/tickets/{tid}/events` is untouched and still serves the trail.

Six presented kinds, and each answers a question a reader actually has: `message` (what a
person or kenny said), `finding` (a triage verdict), `activity` (what kenny looked at),
`action` (what kenny changed, and whether anyone was asked), `lifecycle` (a move somebody
decided) and `problem` (what failed, and therefore what is missing).

### Nothing is lost silently, and that is testable

Every entry carries `source_event_ids`: the trail rows it stands for. Every row the
projection does not present is matched by a *named* rule in `DROP_RULES`. So the property
"this view invented nothing and lost nothing" is not a claim in a comment — it is an
assertion over a realistic trail (`tests/test_ticket_timeline.py`), and a row that is
neither presented nor explicitly dropped fails it. Tidying a view is exactly the kind of
change that loses things quietly; this is the guard against that, and it is the reason the
projection returns provenance rather than plain strings.

### The prose is kenny's voice and can only ever be true

The condenser writes ordinary sentences — "I looked at the agent's health, the event log
(System, Setup, Application) and disk usage." — in the same byline and the same register as
the model's own replies, with no marker distinguishing them.

That is deliberate, and it does not weaken ADR-0056's load-bearing separation between fact
and judgement, because the separation is enforced in what the generator *may say* rather
than in how it is labelled: it states only what the trail records as having happened. It
never rates, concludes, reassures or characterises. A sentence that can only be true is
safe to leave unmarked beside a sentence that might not be; a marker would be protecting
the reader from a risk that has been removed rather than hidden. Judgement stays exactly
where ADR-0056 put it — in a verdict, framed, with its evidence beside it.

Two consequences follow and are accepted. The tool-phrase tables are hand-maintained, which
ADR-0026 and ADR-0041 both rejected for event patterns and service names; the difference is
that those spaces are open and per-fleet while the tool set is closed, lives in this
repository, and is held complete by a test against `tool_classes.TOOL_CLASSES`. And a tool
whose phrase is missing reads as its own bare name — which is why that test exists rather
than a fallback nobody would notice.

### An autonomous change stays visible; a read-only look does not

Read-only calls condense into one sentence per run. A `standard_change` does not: it ran
without anyone deciding (ADR-0045's tiered gate, which ADR-0050 extended to this surface),
and it says so — "I flushed the DNS cache without being asked — that is a routine change."
The autonomy is read from the gate row the trail already writes, not inferred from the
tier, so a standard change that held for someone's consent first is not misdescribed as
unasked. This is the one class of event where nobody was consulted, and it is therefore the
last thing that may be tidied away.

### Amends ADR-0046: the trail is the audit, not the ticket's history

ADR-0046's "the record is deliberately two things, and neither is a transcript" stands
unchanged, and so does everything it requires be written. What changes is one sentence's
worth of consequence: the trail is no longer what the detail view renders. It is complete,
unpruned, one tab away, and still the authority where the two disagree — the projection
reads it and cannot write to it.

### Amends ADR-0050: the ticket's chat surface moves into the drawer

ADR-0050's ticket-bound assistant is untouched in every respect that record cares about:
`POST /api/tickets/{tid}/chat/stream`, `TicketPolicy.gate`, the frozen target, the
capability profile, `session_for(actor=…)`, and the verbatim trail writing all stay exactly
as they are. What moves is where the reader types. The Ask kenny drawer gains a second
context: on a ticket route it is that ticket's chat, named in its scope chip, with no
conversation history of its own because the ticket's timeline is its history.

The ticket page binds the target (`chatStore.openForTicket`) rather than the drawer
fetching it, and a ticket route the drawer is *not* bound to renders as "not ready" and
never as fleet chat — the failure that would otherwise be silent is a turn running on the
copilot's endpoint under the copilot's gate while the reader believes they are talking
about the ticket.

**The gate does not move with it.** A ticket's gate is durable, may wait for a different
operator than whoever has the drawer open, and is answered beside the frozen call it would
run. Offering a second CONFIRM inside the drawer would be offering the decision without
what it decides, which is ADR-0045's own objection to deciding from a list. The drawer says
where the decision is and closes.

### Wire-contract impact

None. This sits entirely above the agent tunnel: `docs/protocol.md` and `docs/fixtures/`
are unchanged and `PROTOCOL_VERSION` does not move — the same posture ADR-0050 and ADR-0059
record for their own additions.

### Consequences

- Good, because the ticket answers what happened in the number of lines the answer takes.
  #76 reads as one activity sentence, one finding, and the moves people made.
- Good, because the duplication is gone structurally rather than cosmetically: the verdict
  tool is `SURFACE_ONLY_TOOLS`, so its two `tool_call` rows are not rendered at all, and no
  deduplication heuristic has to keep working.
- Good, because "why did this run?" is one tab away and unchanged, and the resolve gate
  reads the trail directly — there is a test that a read-only call condensed out of sight
  still licenses a closing verdict.
- Good, because one composer and one chat surface mean the reader no longer chooses between
  two boxes before knowing which they wanted.
- Bad, because there is now a second place a reader has to know about. A finding whose
  evidence somebody doubts is one click further away than it was, and somebody who does not
  know the audit tab exists will believe the ticket is all there is.
- Bad, because the prose tables are hand-maintained and will be wrong for a while whenever
  a tool is added in a hurry — visibly wrong, because the completeness test fails, but wrong
  in the commit that adds the tool rather than in the one that adds the phrase.
- Bad / accepted, because `DROP_RULES` encodes today's reading of which rows are
  bookkeeping. A row that becomes meaningful later stays hidden until somebody notices; the
  audit tab is the mitigation, not a cure.
- Neutral, because nothing about what is recorded, who may read a ticket, or what may run
  on a host has moved. Both tabs answer to the same ownership check, and the filtering is
  presentation — never access control.

## More Information

- Amends [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md) (the trail is the
  audit, not the rendering) and [ADR-0050](0050-the-ticket-is-its-own-chat-surface.md) (the
  ticket's chat keeps its endpoint and its gate, and changes its mount point).
- Touches [ADR-0056](0056-unprompted-ticket-triage.md) (why factual-only prose in kenny's
  voice leaves the fact/judgement separation intact, and why the verdict keeps its evidence
  beside it) and [ADR-0045](0045-tiered-tool-classification.md) (an autonomously authorised
  change is the one thing the view may not condense).
- Continues [ADR-0059](0059-the-inbox-is-the-ticket-queue.md)'s line of reasoning one screen
  further in: a queue admits only things with a lifecycle, and a ticket shows only things a
  person would call an outcome.
- Code: `kenny-server/kenny_server/ticket_timeline.py`,
  `kenny-server/kenny_server/webui/tickets.py` (`api_ticket_timeline`),
  `kenny-web/src/views/ticket/Timeline.tsx`, `AuditTrail.tsx`, `entryFormat.ts`,
  `NoteComposer.tsx`, `kenny-web/src/views/InboxTicket.tsx`,
  `kenny-web/src/chat/chatStore.ts`, `scope.ts`, `ticketTurn.ts`,
  `kenny-web/src/components/AskKennyDrawer/AskKennyDrawer.tsx`.
- Tests: `kenny-server/tests/test_ticket_timeline.py` (completeness, prose coverage, one
  verdict one entry, `may_resolve` unaffected), `test_tickets_api.py` (same ownership rule
  on both readings), `kenny-web/src/views/ticket/Timeline.test.tsx`, `AuditTrail.test.tsx`,
  `entryFormat.test.ts`, `NoteComposer.test.tsx`,
  `kenny-web/src/components/AskKennyDrawer/AskKennyDrawer.test.tsx`.

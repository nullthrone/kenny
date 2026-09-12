# 0061. The ticket keeps a record, not a transcript

- Status: proposed
- Boundary moved: **the observability/record model** — [ADR-0060](0060-the-ticket-shows-findings-the-trail-stays-the-audit.md)
  put a strictly deterministic projection between the trail and every surface that reads
  it, with no model in that path. One presented line now comes from the model: a summary
  it writes for the ticket at the end of a turn. Judgement enters what a ticket *shows*,
  under a named rule and beside prose that is still deterministic.
- Amends: [ADR-0060](0060-the-ticket-shows-findings-the-trail-stays-the-audit.md),
  [ADR-0050](0050-the-ticket-is-its-own-chat-surface.md)
- Date: 2026-09-12

## Context and Problem Statement

ADR-0050 made every reply of kenny's durable: a ticket-bound turn writes its wording into
the trail, whichever surface it went out on. ADR-0060 then split what a ticket *shows*
from what it *stores* — a deterministic projection composes the presented timeline, and
the trail stays the audit behind it. A reply, being stored verbatim, was presented
verbatim.

The result reads as a second, worse copy of a conversation that happened somewhere else.
A ticket's analysis tab carries every answer kenny gave in the drawer or in the Discord
thread, at full length, interleaved with the deterministic lines describing what it
actually did. The reader who opens a ticket to find out where it stands has to re-read a
dialogue to extract three facts from it — and the dialogue is already legible on the
surface it was had on, live, where the person having it is looking.

What the ticket needs is the outcome: what was found, what changed, what is now true.
Nothing in the trail carries that. A tool call records that `powershell_exec` ran and
succeeded; only the conversation says the adapter turned out to be unplugged. The
deterministic projection cannot compose that sentence, and it should not try — inferring
a conclusion from a call's arguments is exactly the invention ADR-0060 forbids.

## Considered Options

- **Truncate the reply to its first paragraph, with an expander.** Deterministic and
  cheap, and it answers nothing: the first paragraph of a reply is written for the person
  in the conversation, not for the ticket, and half a dialogue is still a dialogue.
- **Drop kenny's prose from the presented timeline and show only the deterministic
  lines.** Honest, and it loses the only thing that ever said what the work meant. A
  ticket whose whole record is "I looked at the event log" answers nothing either.
- **Summarise the turn with a second model call, server-side, after the turn ends.**
  Keeps the presented line out of the conversation's own control, at the cost of a second
  round-trip per turn and a summariser that has to re-read the transcript to guess what
  mattered — the one participant that already knows is the one being summarised.
- **Let the turn write its own record through a tool, and drop its prose from the
  timeline.** Chosen.

## Decision Outcome

Chosen option: **a surface-only tool, `ticket_summary`, which a ticket-bound turn calls
once when it found something out, changed something, or reached a conclusion.** Its one
argument is one or two plain sentences; the handler writes a `note` trail row marked
`turn_summary`, and the projection presents that row as kenny's own line. Kenny's replies
are dropped from the presented timeline under a named rule (`assistant_prose`) — they stay
in the trail, and the audit tab renders them verbatim, so nothing is lost and one thing
moves.

**Judgement was already on this page; what changes is how much of it.** ADR-0056's triage
verdict is a model's conclusion rendered as a finding, and ADR-0060 accommodated it by
carrying its evidence beside it. This is the same admission for an ordinary turn, with the
same discipline: the summary is presented as a *message from kenny*, never as a status
line, so a reader can always tell the sentence somebody could have written from the
sentence that can only ever be true.

**Bounded because a model wrote it.** One row per call, 600 characters, refused outright
when empty — an empty summary would read as a turn that found nothing, which is a claim
the model did not make. The tool is `READ_ONLY`: it touches no machine and moves no
ticket, so putting a confirmation in front of it would park the ticket on a gate for the
privilege of saying what already happened. An unprompted investigation does not get it at
all (`TRIAGE_TOOLS`); it has a verdict, and two ways to write one conclusion are two
records of it.

### Consequences

- Good, because the ticket reads as a case file: what was found, what was done, what is
  waiting — with the conversation one click away on the surface it happened on.
- Good, because the correction case finally has somewhere to live. A turn that overturns
  an earlier finding says so in the record, instead of leaving the ticket showing both
  conclusions as equally current prose.
- Bad, because a turn that does not call the tool leaves the ticket with only its
  deterministic lines. The prompt asks for the call; nothing enforces it, and nothing
  should — a server that synthesised the summary when the model declined to write one
  would be inventing the very sentence this record admits it cannot compose.
- Bad, because the presented timeline is no longer uniformly trustworthy in the way
  ADR-0060 could claim. One line in it can now be wrong in the way a model is wrong. The
  trail behind it is unchanged, and is still what `triage.may_resolve` reads.

## More Information

- [ADR-0060](0060-the-ticket-shows-findings-the-trail-stays-the-audit.md) — the projection
  this amends, and the completeness test (`tests/test_ticket_timeline.py`) that still
  requires every dropped row to match a named rule.
- [ADR-0056](0056-unprompted-ticket-triage.md) — the first model-authored entry on a
  ticket, and the evidence-beside-verdict rule this follows.
- [ADR-0046](0046-ticket-as-entity-chat-thread-as-binding.md) — the trail as the audit,
  which is where a reply now lives alone.

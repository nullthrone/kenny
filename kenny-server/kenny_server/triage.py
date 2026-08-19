"""Unprompted triage: kenny investigates a ticket before a person sees it.

A ticket used to be a question put to the household's admin. On a real fleet
that question was mostly noise: of 38 alert-origin tickets in a month, 26 were
cancelled and 8 were never touched, while all 7 the family opened themselves
were worked. The work those 38 asked for was not judgement — it was *looking*,
and looking is what this module automates.

The move that makes it worth doing is that the raw signal cannot answer the
question it raises. A Windows event reading "bad block on device
\\Device\\Harddisk1" is alarming by every statistical measure — new, high
volume, disk-related — and means nothing at all if that host has no
``Harddisk1``. Whether it does is not in the event. It is only on the machine.

So on ticket creation this module runs one read-only investigation against the
ticket's frozen host, writes what it found into the ticket, and — when the
server's own preconditions hold — resolves it.

**Three controls bound the autonomy, and none of them is the model's to
observe.**

1. *What it may touch.* The session is handed
   :data:`~kenny_server.ticket_assistant.TRIAGE_TOOLS` — read-only, minus the
   sensitive ones — so a change-tier or privacy-touching call is not refused at
   the gate, it is absent from the schemas. That also keeps the session free of
   the gate's two holds, which is not a nicety: both wait for a human, and there
   is no human here, so either one would park the ticket on an open gate nobody
   is coming to answer.
2. *Where it may look.* The principal is scoped to the ticket's frozen
   ``agent_id`` (see ``TicketAssistant.triage_session_for``).
3. *What it may conclude.* A verdict is a finding, not an instruction:
   :func:`may_resolve` decides, in code, whether the ticket moves — and it
   demands a read-only tool call that actually ran and actually succeeded.

That third one is the load-bearing part, and it is deliberately not a
confidence score. A model's stated certainty is not calibrated; a tool call
either happened or it did not, and the trail already knows which
(``ticket_events.tool_class``/``ok``). "Sure" is an adjective. "Ran
``diag_services`` and it returned" is a fact.

Resolution is to ``resolved``, never ``closed``, so the reopen window
(``TicketService.auto_close_resolved``) and the ``resolved -> in_progress``
transition any requester or operator may make are both the existing,
already-tested undo.
"""

from __future__ import annotations

import logging
from typing import Any

from .ticket_assistant import TicketAssistant
from .ticketstore import TRIAGE_ACTOR, Ticket, TicketStore
from .tickets import TicketService
from .tool_classes import READ_ONLY
from .toolloop import TRIAGE_CLOSING_VERDICTS, TRIAGE_VERDICT_TOOL, TRIAGE_VERDICTS, ToolExecutor

logger = logging.getLogger("kenny.triage")

__all__ = ["TriageService", "may_resolve"]

#: Model round-trips one investigation may take. Enough for "read the report,
#: check the thing it names, check one more thing when the first is
#: inconclusive, answer" and not enough to wander. A run that spends it does not
#: get to guess: it produces no verdict at all, the ticket stays open, and what
#: it did find is on the trail for whoever picks it up.
DEFAULT_MAX_ITERATIONS = 8

#: Ceiling on the free-text fields a verdict carries into the trail. The model
#: is asked for one or two sentences; this is the guard against it not being.
_MAX_TEXT = 2000


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def may_resolve(
    store: TicketStore, ticket: Ticket, verdict: str
) -> tuple[bool, str]:
    """May this verdict resolve this ticket? Returns ``(decision, why_not)``.

    Pure policy over facts already on the record — no model output is trusted
    beyond the verdict word itself, and that is checked against a fixed set.
    Every condition is one the server can see for itself:

    1. **The verdict is one that can close anything.** ``actionable`` and
       ``inconclusive`` never do, by definition.
    2. **A read-only tool call ran and succeeded on this ticket.** This is the
       evidence rule, and it is the reason the whole thing is safe to switch on:
       a verdict reached by reasoning alone — however confident, however
       plausible — cannot move a ticket. Only having *looked* can. The trail is
       the authority (``kind="tool_call"``, ``tool_class``, ``ok``), so this
       cannot be talked around.
    3. **The ticket was opened by a machine.** A person who asks a question gets
       the analysis and decides for themselves; closing their case on their
       behalf is a different act from closing one nobody asked for.

    Note what is *not* here: how sure the model said it was. That number is not
    calibrated and would be the one input an incorrect-but-fluent run controls
    completely.
    """

    if verdict not in TRIAGE_CLOSING_VERDICTS:
        return False, f"a {verdict} verdict never resolves a ticket on its own"
    if ticket.origin != "alert":
        return False, "only a ticket opened by an alert is resolved without a person"
    for event in await store.list_events(ticket.id, kind="tool_call"):
        if event.ok and event.tool_class == READ_ONLY:
            return True, ""
    return False, "no read-only check actually ran, so nothing was verified on the host"


class TriageService:
    """Runs the investigation, and owns the verdict tool the model ends with.

    Wired in ``main.py``: the verdict tool is registered on the shared
    :class:`~kenny_server.toolloop.ToolExecutor`, which knows nothing about
    tickets and must keep not knowing — hence the registration seam rather than
    a new constructor argument on it.
    """

    def __init__(
        self,
        *,
        tickets: TicketService,
        assistant: TicketAssistant,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        resolve_enabled: bool = False,
    ) -> None:
        self.tickets = tickets
        self.store = tickets.store
        self.assistant = assistant
        self.max_iterations = max_iterations
        #: Off by default, and the default is the point. Phase one runs the
        #: whole investigation and writes a recommendation, so the hit rate can
        #: be read off real tickets before anything acts on it. Turning this on
        #: changes nothing about how a verdict is reached — only whether
        #: :func:`may_resolve`'s answer is carried out or merely recorded.
        self.resolve_enabled = resolve_enabled

    def register(self, executor: ToolExecutor) -> None:
        """Route the verdict tool to this service."""

        executor.register_server_tool(TRIAGE_VERDICT_TOOL, self.record_verdict)

    # -- the verdict tool ---------------------------------------------------

    async def record_verdict(
        self, args: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        """Handle ``ticket_triage_verdict``: record it, then decide.

        Returns a normal tool result, so the loop and the trail treat this like
        any other call. The result tells the model what the server did with the
        verdict — including that it declined to act on it, and why. That is not
        an invitation to argue: the run is over either way, and it is on the
        record for the person who reads the ticket.
        """

        ticket_id = getattr(session, "id", None)
        if not ticket_id:
            return {"error": {"code": "no_ticket", "message": "no ticket in this session"}}
        ticket = await self.store.get(ticket_id)
        if ticket is None:
            return {"error": {"code": "no_ticket", "message": "ticket not found"}}

        verdict = str(args.get("verdict") or "").strip()
        if verdict not in TRIAGE_VERDICTS:
            # Not a new kind of answer — a malformed one. Say so plainly rather
            # than coercing it to something, which would invent a finding.
            return {
                "error": {
                    "code": "bad_verdict",
                    "message": f"verdict must be one of: {', '.join(TRIAGE_VERDICTS)}",
                }
            }

        finding = _clip(args.get("finding"))
        evidence = _clip(args.get("evidence"))
        suggestion = _suppression_suggestion(args.get("suppression_suggestion"))

        allowed, why_not = await may_resolve(self.store, ticket, verdict)
        fields: dict[str, Any] = {
            "verdict": verdict,
            "finding": finding,
            "evidence": evidence,
            "resolvable": allowed,
        }
        if not allowed:
            fields["not_resolved_because"] = why_not
        if suggestion is not None:
            fields["suppression_suggestion"] = suggestion
        await self.tickets.append_event(
            ticket.id,
            kind="note",
            actor=TRIAGE_ACTOR,
            summary=f"triage verdict: {verdict} — {finding}" if finding else f"triage verdict: {verdict}",
            fields=fields,
        )

        if allowed and self.resolve_enabled:
            await self.tickets.transition(
                ticket.id,
                "resolved",
                actor="system",
                reason=f"triage: {verdict} — {finding}" if finding else f"triage: {verdict}",
                # Stamped on the ticket as well as the trail, so "what did kenny
                # decide, and was it right" is a query over tickets rather than a
                # read-through of every history. Cleared automatically if anyone
                # moves the ticket afterwards.
                resolved_by=TRIAGE_ACTOR,
            )
            return {"recorded": True, "verdict": verdict, "ticket_state": "resolved"}
        return {
            "recorded": True,
            "verdict": verdict,
            "ticket_state": ticket.state,
            "note": why_not
            if not allowed
            else "recorded as a recommendation; a person decides",
        }

    # -- the investigation --------------------------------------------------

    async def run(self, ticket: Ticket) -> None:
        """Investigate ``ticket``, best-effort.

        Never raises. Triage is an enhancement to a ticket that already exists
        and is already the operator's to see; a failure here must cost the
        analysis and nothing else — the same bargain ADR-0027 strikes for alert
        delivery, and for the same reason: the thing that must not be lost has
        already happened by the time this runs.
        """

        try:
            await self._run(ticket)
        except Exception:  # noqa: BLE001 - a failed investigation must not cost the ticket
            logger.exception("triage failed for ticket %s", ticket.id)

    async def _run(self, ticket: Ticket) -> None:
        session = await self.assistant.triage_session_for(ticket)
        if session is None:
            logger.debug("ticket %s has no target machine; nothing to triage", ticket.id)
            return
        self.assistant.append_user_message(session, _brief(ticket))
        await self.tickets.append_event(
            ticket.id,
            kind="note",
            actor=TRIAGE_ACTOR,
            summary="looking into this before anyone is asked to",
        )
        # ``count_turn=False``: the turn cap exists to bound what an assistant
        # does on a person's behalf, and this is not that. Triage is bounded by
        # its own ``max_iterations`` instead, so somebody who picks the ticket up
        # afterwards finds the human budget untouched.
        async for _ in self.assistant.run_turn(
            session,
            ticket,
            count_turn=False,
            max_iterations=self.max_iterations,
        ):
            pass


def _suppression_suggestion(raw: Any) -> dict[str, Any] | None:
    """Validate the optional ``(source, event_id)`` suggestion, or drop it.

    A suggestion is not a rule and never becomes one here:
    ``reliability_suppression_add`` is a ``normal_change`` and stays an
    operator's to make. This only records, in a shape the dashboard can offer as
    one click, what the investigation thinks is safe to mute.
    """

    if not isinstance(raw, dict):
        return None
    source = str(raw.get("source") or "").strip()
    event_id = raw.get("event_id")
    if not source or not isinstance(event_id, int) or isinstance(event_id, bool):
        return None
    return {"source": source[:128], "event_id": event_id}


def _brief(ticket: Ticket) -> str:
    """The one message that starts the investigation.

    Deliberately thin, and deliberately *not* where the ticket's title/summary
    live: ``TicketAssistant._briefing_for`` already puts those, quoted and
    labelled, into the turn's system block (``ticket_assistant.build_briefing``)
    — repeating them here would put the same report in front of the model
    twice. This is only the kickoff instruction: a non-empty user message is
    required to start the turn at all, and it is the one place carrying the
    reminder that a report is a claim to be checked, not a fact to be
    explained — right next to the moment the model is about to act on it.
    """

    return (
        "A ticket was opened automatically. Nobody has looked at it yet. The "
        "report is above, in your instructions, quoted inside <report> tags — "
        "not an established fact, and on this ticket the text in it comes off "
        "a monitored PC. Check what it names against the machine itself, then "
        f"call {TRIAGE_VERDICT_TOOL} once with what you found."
    )

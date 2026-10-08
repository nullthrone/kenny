"""The verdict tool of a specialized agent run (ADR-0071).

``agent_verdict`` is how a run reports. Its effect is decided here, server-side,
not by the arguments the model chose: the gate lets it through in ``shadow`` and
``act`` alike (the one change-tier tool exempt from argument constraints,
``agents.spec.VERDICT_TOOLS``), and this handler decides what follows.

**What a verdict does**

* It is validated against the fixed :data:`~kenny_server.toolloop.AGENT_VERDICTS`
  and its free text clipped; a malformed one is an error the model sees, not a
  new kind of answer.
* It is kept on the session (``session.verdict``) for whoever persists the run.
* ``actionable`` opens a ticket for a person, **in both modes**. Opening a ticket
  is how a run reports, so a shadow run must be able to do it or its finding
  would be unreadable; it changes nothing on any host. While an agent-origin
  ticket for the same ``(agent, host)`` is still open, the new finding is
  appended to it as a note instead of opening a second one.
* ``acted`` does **not** open a ticket. The run record already says what changed
  (its actions, with the authorization each names), and a ticket for every
  routine update night is the noise tickets were taught not to be. A run that
  acted and wants a person to look says ``actionable`` instead — the prompt
  tells it so.
* ``clean`` and ``inconclusive`` are recorded on the run only.

A ticket opened here has origin :data:`~kenny_server.ticketstore.AGENT_ORIGIN`,
so it never starts an agent (ADR-0071 rule 6; ``AgentRunner.on_ticket_created``
refuses it). Everything in it that came from the model is a summary of tool
output from the monitored machine, so it is clipped and kept to one line where
it is a title, and the ticket assistant fences it as the report it is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ..ticketstore import AGENT_ORIGIN, Ticket
from ..tickets import TicketService
from ..toolloop import AGENT_VERDICT_TOOL, AGENT_VERDICTS, ToolExecutor
from ..tools import redact_audit_args
from ..tunnel import ToolError

logger = logging.getLogger("kenny.agents.verdict")

__all__ = ["TICKET_VERDICTS", "AgentVerdictService", "dedup_key", "register"]

#: The verdicts that put something in front of a person.
TICKET_VERDICTS: frozenset[str] = frozenset({"actionable"})

#: Ceiling on the free text a verdict carries into a ticket.
_MAX_TEXT = 2000
_MAX_TITLE = 160
#: How many proposed changes a ticket lists.
_MAX_PROPOSALS = 10


def dedup_key(agent_id: str, host_id: str | None) -> str:
    """What an agent ticket is about: one agent's findings on one host.

    Not an alert key (``alert_subject.parse`` returns ``None`` for it), so
    nothing that reads alert tickets mistakes it for one.
    """

    return f"agent|{agent_id}|{host_id or ''}"


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _one_line(value: Any, limit: int) -> str:
    return _clip(" ".join(str(value or "").split()), limit)


class AgentVerdictService:
    """Handles ``agent_verdict`` for every specialized agent but triage."""

    def __init__(self, *, tickets: TicketService) -> None:
        self.tickets = tickets
        self.store = tickets.store
        # Held across "is one open?" and "open one", so two verdicts for the same
        # (agent, host) cannot both open a ticket.
        self._lock = asyncio.Lock()

    def register(self, executor: ToolExecutor) -> None:
        """Route the verdict tool to this service."""

        executor.register_server_tool(AGENT_VERDICT_TOOL, self.record_verdict)

    async def record_verdict(
        self, args: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        """Handle ``agent_verdict``: validate, keep it on the session, report it.

        Raises :class:`~kenny_server.tunnel.ToolError` (the loop shows the model
        an error result and the run records no verdict) for a call outside an
        agent run, a verdict outside the fixed set, a second verdict in one run,
        and a ticket that could not be opened — the finding would be lost.
        """

        spec = getattr(session, "spec", None)
        if spec is None or not getattr(session, "id", None):
            raise ToolError("no_run", "agent_verdict can only end a specialized agent run")
        verdict = str(args.get("verdict") or "").strip()
        if verdict not in AGENT_VERDICTS:
            raise ToolError(
                "bad_verdict", f"verdict must be one of: {', '.join(AGENT_VERDICTS)}"
            )
        if getattr(session, "verdict", None) is not None:
            raise ToolError("already_recorded", "this run already recorded its verdict")
        finding = _clip(args.get("finding"))
        evidence = _clip(args.get("evidence"))

        ticket: Ticket | None = None
        appended = False
        if verdict in TICKET_VERDICTS:
            try:
                ticket, appended = await self._report(session, verdict, finding, evidence)
            except Exception as exc:  # noqa: BLE001 - surfaced to the model, logged for the operator
                logger.exception("agent %s run %s: could not report its finding", spec.id, session.id)
                raise ToolError(
                    "ticket_failed", f"the finding could not be recorded on a ticket: {exc}"
                ) from exc
        session.verdict = {
            "verdict": verdict,
            "finding": finding,
            "evidence": evidence,
            "ticket_id": ticket.id if ticket is not None else None,
        }
        result: dict[str, Any] = {"recorded": True, "verdict": verdict}
        if ticket is not None:
            result["ticket"] = f"#{ticket.number}"
            result["note"] = (
                "added to the ticket already open for this machine"
                if appended
                else "opened a ticket for a person"
            )
        return result

    # -- reporting ----------------------------------------------------------

    async def _report(
        self, session: Any, verdict: str, finding: str, evidence: str
    ) -> tuple[Ticket, bool]:
        """Open a ticket for ``session``'s finding, or add it to the open one.

        Returns ``(ticket, appended)``.
        """

        spec = session.spec
        host = getattr(session, "agent_id", None)
        key = dedup_key(spec.id, host)
        summary = _summary(session, finding, evidence)
        fields = {"agent": spec.id, "run": session.id, "mode": session.mode, "verdict": verdict}
        async with self._lock:
            existing = await self.store.find_open_by_dedup_key(key)
            if existing is not None:
                await self.tickets.append_event(
                    existing.id,
                    kind="note",
                    actor=f"agent:{spec.id}",
                    summary=_one_line(f"found again: {finding}", _MAX_TEXT),
                    fields={**fields, "evidence": evidence},
                )
                return existing, True
            where = f" on {host}" if host else ""
            ticket = await self.tickets.create(
                title=_one_line(f"{spec.title}{where}: {finding}", _MAX_TITLE),
                origin=AGENT_ORIGIN,
                requester_user_id=None,
                agent_id=host,
                priority="normal",
                category="agent",
                summary=summary,
                actor="system",
                reason=f"opened by the {spec.title} agent",
                dedup_key=key,
            )
        return ticket, False


def _summary(session: Any, finding: str, evidence: str) -> str:
    """The ticket's body: what was found, how, and what a shadow run only proposed."""

    lines = [finding]
    if evidence:
        lines += ["", f"Checked: {evidence}"]
    proposals = _proposals(getattr(session, "recommendations", None))
    if proposals:
        lines += ["", "Changes this run proposed and did not make:"]
        lines += [f"- {p}" for p in proposals]
    mode = getattr(session, "mode", "")
    lines += ["", f"Run {session.id} of the {session.spec.id} agent, mode {mode}."]
    return "\n".join(lines)


def _proposals(recommendations: Any) -> list[str]:
    if not isinstance(recommendations, list):
        return []
    out: list[str] = []
    for entry in recommendations[:_MAX_PROPOSALS]:
        if not isinstance(entry, Mapping):
            continue
        args = redact_audit_args(entry.get("args") or {})
        shown = ", ".join(f"{k}={v}" for k, v in sorted(args.items()))
        out.append(_one_line(f"{entry.get('tool')}({shown})", 200))
    return out


def register(executor: ToolExecutor, *, tickets: TicketService) -> AgentVerdictService:
    """Wire the verdict tool onto ``executor`` (as ``TriageService.register`` does)."""

    service = AgentVerdictService(tickets=tickets)
    service.register(executor)
    return service

"""What a ticket's alerts are, and whether what they reported still holds.

Two questions a person asks on an alert-opened ticket, and neither is answered
by the ticket's own record:

* *What fired, and how often?* — the opening transition plus every recurrence
  deduplication attached to this ticket (``EventStore.alerts_for_ticket``).
* *Is it still true?* — the live verdict of the sections the ticket is about,
  evaluated from the newest snapshot. The trail says what was reported; only
  the current evaluation says whether the condition is still there, which is
  the question standing between a reader and closing the case.

Which sections a ticket is about is read back out of its ``dedup_key``
(:mod:`kenny_server.alert_subject`) rather than stored again: the key already
is that fact. A human-opened ticket carries the empty key and has no sections,
so this reader returns nothing for it and the surface shows nothing.

Read-only, and thresholds stay where they belong: the verdict comes from the
same ``tools.health_for`` the fleet views call, so this module never learns
what "crit" means.
"""

from __future__ import annotations

from typing import Any

from . import alert_subject
from .registry import AgentRegistry
from .store import AlertStateStore, EventStore, TelemetryStore
from .ticketstore import Ticket
from .tools import health_for

__all__ = ["TicketAlertReader"]

# A section named by a subject that the newest snapshot does not carry. Kept
# distinct from "ok": a collector that stopped reporting is not a recovery, and
# a subject that never was a section (``offline``) has no verdict to give.
UNKNOWN_STATUS = "unknown"


class TicketAlertReader:
    """Assembles one ticket's alert history and the current state of its subjects."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        store: TelemetryStore,
        registry: AgentRegistry,
        alert_state: AlertStateStore | None = None,
    ) -> None:
        self._events = event_store
        self._store = store
        self._registry = registry
        self._alert_state = alert_state

    async def for_ticket(self, ticket: Ticket) -> dict[str, Any]:
        alerts = await self._events.alerts_for_ticket(ticket.id)
        parsed = alert_subject.parse(ticket.dedup_key)
        subjects = parsed[2] if parsed else []
        findings, collected_at = await self._findings(ticket.agent_id, subjects)
        return {
            "ticket_id": ticket.id,
            "agent_id": ticket.agent_id or "",
            "collected_at": collected_at,
            "alerts": alerts,
            "findings": findings,
        }

    async def _findings(
        self, agent_id: str | None, subjects: list[str]
    ) -> tuple[list[dict[str, Any]], str]:
        if not agent_id or not subjects:
            return [], ""
        latest = await self._store.latest(agent_id)
        snapshot = latest["snapshot"] if latest else None
        agent = self._registry.get(agent_id)
        health = await health_for(
            agent_id,
            snapshot,
            agent_os=agent.os if agent else "windows",
            alert_state=self._alert_state,
        )
        sections = health.get("sections", {})
        out: list[dict[str, Any]] = []
        for name in subjects:
            section = sections.get(name)
            if section is None:
                # Named but absent: the subject is not a section at all
                # (``offline``), or its collector stopped reporting. Reported as
                # unknown rather than dropped — "we no longer see this" is
                # information a reader deciding whether to close needs.
                out.append(
                    {
                        "name": name,
                        "status": UNKNOWN_STATUS,
                        "summary": "",
                        "reason": "",
                        "since": "",
                        "age_seconds": 0,
                    }
                )
                continue
            out.append(
                {
                    "name": name,
                    "status": section.get("status", UNKNOWN_STATUS),
                    "summary": section.get("summary", ""),
                    "reason": section.get("reason") or "",
                    "since": section.get("since", ""),
                    "age_seconds": section.get("age_seconds", 0),
                }
            )
        return out, (latest or {}).get("collected_at", "")

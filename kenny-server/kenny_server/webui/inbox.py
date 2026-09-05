"""`GET /api/inbox` -- the merged ticket/approval/flagged-section inbox.

Deliberately its own module, not a fifth thing bolted onto
:mod:`kenny_server.webui.tickets` (another surface is actively changing that
file) or reshaped inside :mod:`kenny_server.webui`: this route only reads.
Lifecycle rules and approval authorization stay exactly where they already
live --

* :class:`~kenny_server.ticketstore.TicketStore` owns the ``needs_you`` /
  ``waiting`` / ``working`` / ``new`` / ``done`` bucket rule (see its
  ``counts()`` docstring) -- this module reuses that rule to fetch the *rows*
  behind each bucket's count, it does not restate the rule.
* :class:`~kenny_server.tickets.TicketService` owns who may decide an
  approval (``decide_approval``) -- this module only surfaces enough of a
  held gate (``InboxGate``) for the console to call the existing
  ``POST /api/approvals/{aid}`` route; it never decides anything itself.

The merge extends the ticket bucket vocabulary to two more sources, both
folded into ``needs_you`` (the only bucket a flagged section or a held
approval can belong to -- neither has a ``waiting``/``working``/``new``/
``done`` state of its own):

* flagged (crit/warn) telemetry sections, reusing
  :func:`kenny_server.webui._overview` (the same per-host summary
  ``/api/fleet`` already builds) rather than re-deriving health;
* held approvals (:meth:`TicketStore.list_open_approvals`), kept **at least
  as strict as** ``GET /api/approvals`` itself (operator-only) even though
  ``/api/inbox`` is reachable by a scoped ``user`` for the ticket/section
  slices.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..registry import AgentRegistry
from ..store import TelemetryStore
from ..ticketstore import Ticket, TicketApproval, TicketStore
from ..tickets import TicketService
from . import _known_ids, _overview, section_target
from .authz import guard, principal_of, visible_ids

__all__ = ["build_inbox_routes"]

# The five groups TicketStore.counts() already buckets tickets into (see its
# docstring for the rule). This module extends the vocabulary; it does not
# add a new group.
_GROUPS = ("needs_you", "waiting", "working", "new", "done")

# A household fleet's open-ticket count is small (TicketStore.counts()'s own
# reasoning for its full-scan-in-Python approach) -- large enough to not
# truncate a real inbox, small enough to stay a single cheap query.
_TICKET_FETCH_LIMIT = 500


def _age_seconds(iso: str | None, *, now: datetime) -> int:
    if not iso:
        return 0
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0, int((now - ts).total_seconds()))


def _ticket_item(ticket: Ticket, *, now: datetime) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "kind": "alert" if ticket.origin == "alert" else "ticket",
        # Ticket.blocked_on is already '' | 'user' | 'approval' | 'operator' --
        # exactly InboxItem.waits_on's vocabulary for a ticket row, no mapping.
        "waits_on": ticket.blocked_on or "",
        "severity": None,
        "title": ticket.title,
        # The display ref (#42) belongs here, in text -- never in `target` below.
        # A ticket kenny resolved by itself says so in the row, not only on its
        # own page: the DONE list is where the hit rate gets read, and that is
        # only possible if kenny's decisions are distinguishable from a
        # person's without opening each one. Appended to `meta` rather than
        # given its own field — the row already has a place for "what else is
        # true about this ticket".
        "meta": (
            f"#{ticket.number} · {ticket.priority}"
            + (" · resolved by kenny" if ticket.resolved_by == "triage" else "")
        ),
        "host": ticket.agent_id,
        "age_seconds": _age_seconds(ticket.blocked_since or ticket.updated_at, now=now),
        "gate": None,
        # A ticket has two ids: `id` (uuid, what every /api/tickets/{tid} route
        # resolves) and `number` (the display ref, routable only through
        # TicketStore.get_by_number(), which no HTTP route calls). `target` must
        # carry `id` or the console's #/inbox/ticket/{id} link 404s.
        "target": f"#/inbox/ticket/{ticket.id}",
    }


def _approval_item(approval: TicketApproval, *, now: datetime) -> dict[str, Any]:
    return {
        "id": approval.id,
        "kind": "approval",
        "waits_on": "approval",
        "severity": None,
        "title": f"{approval.tool} needs approval",
        "meta": approval.tool_class,
        "host": approval.agent_id,
        "age_seconds": _age_seconds(approval.requested_at, now=now),
        "gate": {
            "approval_id": approval.id,
            "ticket_id": approval.ticket_id,
            "tool": approval.tool,
            "args": approval.args,
            "agent_id": approval.agent_id or "",
            "tool_class": approval.tool_class,
            "held_since": approval.requested_at,
        },
        # The gate's own ticket, always -- `approval.ticket_id` is the row's
        # foreign key and is the same uuid `id` #/inbox/ticket/{id} resolves.
        # Deriving it from a separately fetched Ticket meant a row whose ticket
        # failed to load rendered a link to "", which navigates nowhere; a
        # ticket that really is gone should say so on the ticket page.
        "target": f"#/inbox/ticket/{approval.ticket_id}",
    }


def _section_item(
    agent_id: str,
    section: dict[str, Any],
    severity: str,
    *,
    collected_at: str | None,
    now: datetime,
) -> dict[str, Any]:
    name = section["name"]
    return {
        "id": f"section:{agent_id}:{name}",
        "kind": "section",
        "waits_on": "attention",
        "severity": severity,
        "title": section.get("reason") or section.get("summary") or name,
        "meta": name.replace("_", " "),
        "host": agent_id,
        "age_seconds": _age_seconds(collected_at, now=now),
        "gate": None,
        # The flagged section itself, not the machine it sits on -- see
        # `section_target`.
        "target": section_target(agent_id, name),
    }


def build_inbox_routes(
    *,
    tickets: TicketService,
    ticket_store: TicketStore,
    registry: AgentRegistry,
    telemetry_store: TelemetryStore,
) -> list[Route]:
    """Build the ``/api/inbox`` route.

    ``tickets``/``ticket_store`` are the ticket lifecycle service and its
    backing store (approve/deny decisions stay a separate call,
    ``POST /api/approvals/{aid}``, per the module docstring); ``registry``/
    ``telemetry_store`` are the same fleet collaborators ``/api/fleet`` reads,
    needed here only to reuse :func:`kenny_server.webui._overview` for the
    flagged-section slice.
    """

    async def _bucket_tickets(requester_user_id: int | None) -> dict[str, list[Ticket]]:
        async def fetch(**kw: Any) -> list[Ticket]:
            return await ticket_store.list(
                requester_user_id=requester_user_id, limit=_TICKET_FETCH_LIMIT, **kw
            )

        new_all = await fetch(state="new")
        # Mirrors TicketStore.counts()'s bucket rule (see its docstring) for the
        # one split that rule needs and `TicketStore.list()` cannot express
        # directly (no "requester IS NULL" filter): a `new` ticket with no
        # requester is alert-origin, and alert-origin tickets are operator-only.
        new_needs_you = [t for t in new_all if t.requester_user_id is None]
        new_only = [t for t in new_all if t.requester_user_id is not None]
        needs_you_blocked = await fetch(blocked_on_in=("operator", "approval"))
        return {
            "needs_you": needs_you_blocked + new_needs_you,
            "waiting": await fetch(blocked_on="user"),
            "working": await fetch(state="in_progress", blocked_on=""),
            "new": new_only,
            "done": await fetch(states=("resolved", "closed", "cancelled")),
        }

    async def _flagged_section_items(request: Request, *, now: datetime) -> list[dict[str, Any]]:
        principal = principal_of(request)
        ids = await _known_ids(registry, telemetry_store)
        if principal is not None:
            ids = visible_ids(principal, ids)
        crit_items: list[dict[str, Any]] = []
        warn_items: list[dict[str, Any]] = []
        for agent_id in ids:
            overview = await _overview(agent_id, registry, telemetry_store)
            collected_at = overview["collected_at"]
            for section in overview["crit_sections"]:
                crit_items.append(
                    _section_item(agent_id, section, "crit", collected_at=collected_at, now=now)
                )
            for section in overview["warn_sections"]:
                warn_items.append(
                    _section_item(agent_id, section, "warn", collected_at=collected_at, now=now)
                )
        return crit_items + warn_items

    async def _approval_items(request: Request, *, now: datetime) -> list[dict[str, Any]]:
        principal = principal_of(request)
        # See module docstring: at least as strict as GET /api/approvals.
        if principal is not None and not principal.at_least("operator"):
            return []
        items: list[dict[str, Any]] = []
        for approval in await ticket_store.list_open_approvals():
            items.append(_approval_item(approval, now=now))
        return items

    async def api_inbox(request: Request) -> JSONResponse:
        principal = principal_of(request)
        group = request.query_params.get("group", "needs_you")
        if group not in _GROUPS:
            return JSONResponse(
                {"error": f"group must be one of {', '.join(_GROUPS)}"}, status_code=400
            )
        now = datetime.now(timezone.utc)
        requester_user_id = (
            None if principal is None or principal.at_least("operator") else principal.user_id
        )

        buckets = await _bucket_tickets(requester_user_id)
        section_items = await _flagged_section_items(request, now=now)
        approval_items = await _approval_items(request, now=now)

        counts = await ticket_store.counts(requester_user_id=requester_user_id)
        counts["needs_you"] += len(section_items) + len(approval_items)

        if group == "needs_you":
            # Same ranking `/api/today` uses for its capped list: crit sections,
            # warn sections, held approvals, then tickets -- here the full set,
            # not capped, since this is the actual inbox list.
            items = (
                section_items
                + approval_items
                + [_ticket_item(t, now=now) for t in buckets["needs_you"]]
            )
        else:
            items = [_ticket_item(t, now=now) for t in buckets[group]]

        return JSONResponse({"group": group, "counts": counts, "items": items})

    return [Route("/api/inbox", guard(api_inbox, min_role="user"))]

"""``GET /api/inbox`` -- the ticket queue, grouped by who the ball is with.

Every row is a ticket. That is the membership rule (ADR-0059): the queue
carries only things with a lifecycle -- something that can be claimed, worked,
blocked and closed, and that a person can be finished with. A flagged health
section is none of those; it is a verdict recomputed from the newest snapshot
on every request, so it is read where it is derived (Fleet, Today) and reaches
this queue only when a rule turns it into a ticket (``ticket_rules.py``). A
held approval is not a second thing either: it is a state of the ticket that
holds it (``blocked_on='approval'``), and that ticket is already in
``needs_you``.

Deliberately its own module, not a fifth thing bolted onto
:mod:`kenny_server.webui.tickets` (another surface is actively changing that
file) or reshaped inside :mod:`kenny_server.webui`: this route only reads.
Lifecycle rules stay where they live --
:class:`~kenny_server.ticketstore.TicketStore` owns the ``needs_you`` /
``waiting`` / ``working`` / ``new`` / ``done`` bucket rule (see its
``counts()`` docstring); this module reuses that rule to fetch the *rows*
behind each bucket's count, it does not restate it.

Because the counts are now exactly ``TicketStore.counts()``, the header badge
(which reads ``/api/tickets/summary``) and this route's ``needs_you`` are the
same number by construction rather than by coincidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..ticketstore import Ticket, TicketStore
from .authz import guard, principal_of

__all__ = ["build_inbox_routes"]

# The five groups TicketStore.counts() buckets tickets into (see its docstring
# for the rule).
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


def _meta(ticket: Ticket) -> str:
    """The row's secondary line: the display ref, then what else is true of it.

    ``origin`` lives here because the row's badge is the priority now, and a
    reader still needs to tell a case kenny opened from one a person did. The
    display ref (#42) belongs in text and never in ``target``.
    """

    parts = [f"#{ticket.number}", ticket.origin]
    if ticket.blocked_on == "approval":
        parts.append("waiting for approval")
    elif ticket.blocked_on == "user":
        parts.append("waiting for an answer")
    elif ticket.blocked_on == "operator":
        parts.append("waiting for an operator")
    # A ticket kenny resolved by itself says so in the row, not only on its own
    # page: the DONE list is where the hit rate gets read, and that is only
    # possible if kenny's decisions are distinguishable from a person's without
    # opening each one.
    if ticket.resolved_by == "triage":
        parts.append("resolved by kenny")
    return " · ".join(parts)


def _ticket_item(ticket: Ticket, *, now: datetime) -> dict[str, Any]:
    return {
        "id": ticket.id,
        # Ticket.blocked_on is already '' | 'user' | 'approval' | 'operator' --
        # exactly InboxItem.waits_on's vocabulary, no mapping.
        "waits_on": ticket.blocked_on or "",
        "priority": ticket.priority,
        "title": ticket.title,
        "meta": _meta(ticket),
        "host": ticket.agent_id,
        "age_seconds": _age_seconds(ticket.blocked_since or ticket.updated_at, now=now),
        # A ticket has two ids: `id` (uuid, what every /api/tickets/{tid} route
        # resolves) and `number` (the display ref, routable only through
        # TicketStore.get_by_number(), which no HTTP route calls). `target` must
        # carry `id` or the console's #/inbox/ticket/{id} link 404s.
        "target": f"#/inbox/ticket/{ticket.id}",
    }


def build_inbox_routes(*, ticket_store: TicketStore) -> list[Route]:
    """Build the ``/api/inbox`` route over the ticket store alone."""

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
        # Deduplicated by id: TicketService.block() only ever blocks an
        # `in_progress` ticket, so the two halves cannot overlap through the
        # service -- but the rows-must-equal-the-count test is only honest if a
        # direct store write cannot make it double-count.
        seen = {t.id for t in needs_you_blocked}
        return {
            "needs_you": needs_you_blocked + [t for t in new_needs_you if t.id not in seen],
            "waiting": await fetch(blocked_on="user"),
            "working": await fetch(state="in_progress", blocked_on=""),
            "new": new_only,
            "done": await fetch(states=("resolved", "closed", "cancelled")),
        }

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
        counts = await ticket_store.counts(requester_user_id=requester_user_id)
        items = [_ticket_item(t, now=now) for t in buckets[group]]

        return JSONResponse({"group": group, "counts": counts, "items": items})

    return [Route("/api/inbox", guard(api_inbox, min_role="user"))]

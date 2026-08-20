"""Ticket, approval, Discord-identity, tool-class and auto-ticket-rule routes
for the dashboard API.

Everything here is a thin HTTP skin over :class:`~kenny_server.tickets.TicketService`
and :class:`~kenny_server.ticketstore.TicketStore` — the lifecycle rules (legal
transitions, who may drive them, redaction) live there, not here. This module's
own job is role/scope enforcement plus translating lifecycle exceptions into the
same ``{"error": ..., "detail": ...}`` JSON shape the rest of the dashboard API
uses (see :class:`~kenny_server.webui.authz.Forbidden`).

**Ownership.** :func:`~kenny_server.webui.authz.guard` only ever checks *role*
and, optionally, *host* scope (``host_param``) — it has no notion of ticket
ownership. So every per-ticket handler that a scoped ``user`` may reach calls
:func:`_owned_or_operator` itself: an operator+ principal passes unconditionally,
a ``user`` principal only if they are the ticket's ``requester_user_id``. An
alert-origin ticket (``requester_user_id is None``) therefore has no owner and
is operator-only, matching the listing rule below.

**Listing.** ``GET /api/tickets`` never takes an ownership/requester filter from
the caller for a scoped ``user`` — it is always narrowed to
``requester_user_id=principal.user_id`` server-side, so a `user` can never widen
their own view by request parameter.

**Discord is optional, and tickets do not depend on it.** ``identities``,
``user_store`` and ``discord`` are keyword-only collaborators defaulting to
``None``: a server with no Discord configuration still serves every ticket and
approval route, and the routes that genuinely need a Discord collaborator answer
``503`` instead. The two collaborators are separate on purpose — the identity
mapping is a plain SQLite store that exists whether or not a bot is connected,
while the guild member list and the connection status can only come from a live
gateway (:class:`~kenny_server.discord_service.DiscordService`).

**Self-service unbind (``/api/me/discord``).** ADR-0044 keeps *enrollment*
operator-only — a chat platform's identity assertion carries no proof of
possession, so kenny will not mint a binding from self-service. Seeing and
*removing* your own binding is different: it only takes privilege away, so it
needs no operator step. These two routes are floored at plain ``user`` and
resolve their target from ``principal.user_id`` alone, never from anything
the caller sends, so they answer with the same clean "not linked" shape
whether the store holds nothing for that account or isn't configured at all.

**Auto-ticket rules** (``ticket_rules.py``) live here too, as a thin CRUD skin over
:class:`~kenny_server.ticket_rules.TicketRuleList` — the mirror
``AlertEngine._dispatch`` consults to decide which alerts open a ticket. See
that module for the matching algorithm.
"""

from __future__ import annotations

import json
import logging
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import tool_classes
from ..auth import Principal
from ..discord_identity import DiscordIdentityStore, IdentityConflict
from ..ticket_rules import DECISIONS, EVENT_TYPES, KNOWN_SECTIONS, TicketRuleList
from ..ticketstore import Ticket, TicketApproval, TicketStore
from ..tickets import (
    BLOCKED_REASONS,
    KNOWN_CATEGORIES,
    PRIORITIES,
    STATES as TICKET_STATES,
    TicketError,
    TicketService,
    TransitionError,
)
from ..userstore import UserStore
from .authz import Forbidden, guard, require_host, require_user

if TYPE_CHECKING:  # pragma: no cover - import cycle-free typing only
    from ..discord_service import DiscordService
    from ..ticket_assistant import TicketAssistant

# SSE response headers: disable proxy/browser buffering so events flush live —
# the same headers ``webui/__init__.py``'s streaming routes use.
_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse(event: dict[str, Any]) -> bytes:
    """Encode one ``drive_events`` event as a Server-Sent Events ``data:`` frame.

    A small, deliberate duplicate of ``webui/__init__.py``'s helper of the same
    name: this module documents itself as a thin, standalone skin, and a
    one-line encoder is cheaper to duplicate than to couple two otherwise
    independent route modules over.
    """

    return f"data: {json.dumps(event, default=str)}\n\n".encode()

logger = logging.getLogger("kenny.webui.tickets")

# -- small local helpers (mirrors webui/users.py's shape) ----------------------


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - malformed/empty body
        return {}
    return data if isinstance(data, dict) else {}


_STATUS_ERROR_NAMES = {
    400: "invalid",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    503: "unavailable",
}


def _err(detail: str, status: int = 400) -> JSONResponse:
    """One error shape for the whole module: ``{"error": ..., "detail": ...}``.

    The ``error`` name is derived from the status, so a 404/409/503 raised here
    reads the same way as one translated from a :class:`TicketError`.
    """

    return JSONResponse(
        {"error": _STATUS_ERROR_NAMES.get(status, "error"), "detail": detail},
        status_code=status,
    )


def _ticket_error_response(exc: TicketError) -> JSONResponse:
    """Render a lifecycle exception as the dashboard API's standard error shape."""

    if isinstance(exc, TransitionError):
        return JSONResponse(exc.as_dict(), status_code=exc.status_code)
    error = _STATUS_ERROR_NAMES.get(exc.status_code, "error")
    return JSONResponse({"error": error, "detail": str(exc)}, status_code=exc.status_code)


def _catches_ticket_errors(handler: Callable) -> Callable:
    """Translate :class:`Forbidden`/:class:`TicketError` into a JSON response.

    ``guard()`` only catches the ``Forbidden`` it itself raises (the min-role
    check); ownership checks and lifecycle errors happen *inside* the handler,
    so they need their own translation layer. Composed as
    ``guard(_catches_ticket_errors(handler), ...)``.
    """

    @wraps(handler)
    async def wrapped(request: Request) -> JSONResponse:
        try:
            return await handler(request)
        except Forbidden as exc:
            return exc.response
        except TicketError as exc:
            return _ticket_error_response(exc)

    return wrapped


def _actor(principal: Principal) -> str:
    """Render a principal as a ``TicketService`` actor string.

    Only the two documented forms are produced: ``"operator:<uid>"`` for
    operator+ callers (superuser collapses into "operator" here — both may
    drive anything an operator may, per ``tickets.py``'s ``_ROLE_PREFIXES``) and
    ``"user:<uid>"`` for the scoped role. A ``None`` user id (the legacy shared
    env-token superuser has no user row) drops the suffix rather than rendering
    a literal ``"operator:None"``.
    """

    role = "operator" if principal.at_least("operator") else "user"
    return f"{role}:{principal.user_id}" if principal.user_id is not None else role


def _owned_or_operator(principal: Principal, ticket: Ticket) -> None:
    """Raise :class:`Forbidden` unless ``principal`` may see ``ticket``.

    Operator+ always passes. A scoped ``user`` passes only if they are the
    ticket's requester; an alert-origin ticket (``requester_user_id is None``)
    has no owner and so is never visible to a `user`.
    """

    if principal.at_least("operator"):
        return
    if ticket.requester_user_id is None or ticket.requester_user_id != principal.user_id:
        raise Forbidden(403, "not your ticket")


def _affordances(tickets: TicketService, ticket: Ticket, principal: Principal) -> dict[str, Any]:
    """A ticket's ``as_dict()`` plus what *this* principal may legally do to it.

    Computed from :meth:`TicketService.can_transition`/``can_block``/
    ``can_unblock`` rather than duplicated here, so the dashboard's buttons can
    only ever offer a move the service would actually accept — the point of
    this whole change. Kept out of ``Ticket.as_dict()`` itself: that dataclass
    has no principal and should not grow one.
    """

    actor = _actor(principal)
    payload = ticket.as_dict()
    payload["allowed_transitions"] = sorted(
        s for s in TICKET_STATES if tickets.can_transition(ticket, s, actor)
    )
    payload["allowed_blocks"] = sorted(
        r for r in BLOCKED_REASONS if tickets.can_block(ticket, r, actor)
    )
    payload["can_unblock"] = tickets.can_unblock(ticket, actor)
    return payload


def _validate_priority(value: str) -> str | None:
    """Return an error message if ``value`` is outside the closed vocabulary."""

    if value not in PRIORITIES:
        return f"priority must be one of {', '.join(PRIORITIES)}"
    return None


def _warn_if_unknown_category(category: Any) -> None:
    """Log (never reject) a category outside the advertised vocabulary.

    Matches ``ticket_rules.KNOWN_SECTIONS``' discipline: the vocabulary is
    advertised for the dashboard's dropdown, not enforced, because nothing
    downstream branches on a ticket's category the way it does on priority.
    """

    if category and category not in KNOWN_CATEGORIES:
        logger.info("ticket category %r is outside the advertised vocabulary", category)


def build_ticket_routes(
    *,
    tickets: TicketService,
    store: TicketStore,
    identities: DiscordIdentityStore | None = None,
    user_store: UserStore | None = None,
    discord: DiscordService | None = None,
    ticket_rules: TicketRuleList | None = None,
    assistant: TicketAssistant | None = None,
) -> list[Route]:
    """Ticket/approval/Discord/tool-class routes. See module docstring.

    ``assistant`` is optional exactly like ``discord``: a server with no usable
    Anthropic client has none, and only the one route that genuinely needs it
    (``/chat/stream``) answers ``503`` without it — every other ticket route
    works whether or not an assistant is configured.
    """

    # -- tickets -------------------------------------------------------------

    async def api_tickets_list(request: Request) -> JSONResponse:
        principal = require_user(request)
        q = request.query_params
        try:
            limit = int(q.get("limit", "50"))
        except ValueError:
            limit = 50
        state = q.get("state")
        states_param = q.get("states")
        states = [s for s in states_param.split(",") if s] if states_param else None
        if not principal.at_least("operator"):
            # A scoped `user` only ever sees their own tickets — never a
            # request-supplied requester/agent filter. Alert-origin tickets
            # (requester_user_id is None) are excluded by construction.
            rows = await store.list(
                state=state,
                states=states,
                requester_user_id=principal.user_id,
                limit=limit,
            )
        else:
            agent_id = q.get("agent_id")
            requester_param = q.get("requester_user_id")
            requester_user_id = int(requester_param) if requester_param else None
            rows = await store.list(
                state=state,
                states=states,
                requester_user_id=requester_user_id,
                agent_id=agent_id,
                limit=limit,
            )
        return JSONResponse(
            {
                "tickets": [
                    {**_affordances(tickets, t, principal), "assistant_available": assistant is not None}
                    for t in rows
                ]
            }
        )

    async def api_tickets_vocabulary(_request: Request) -> JSONResponse:
        """The vocabulary the dashboard's ticket UI actually accepts.

        Derived from ``tickets.py``'s live constants so the dashboard never
        hardcodes a second copy that can drift from what the service enforces
        — the exact failure mode this endpoint exists to close off (see
        ADR amending ADR-0046).
        """

        return JSONResponse(
            {
                "states": sorted(TICKET_STATES),
                "blocked_reasons": sorted(BLOCKED_REASONS),
                "priorities": list(PRIORITIES),
                "categories": sorted(KNOWN_CATEGORIES),
            }
        )

    async def api_tickets_summary(request: Request) -> JSONResponse:
        """Bucket counts for the dashboard's grouped ticket list.

        Narrowed to one requester's tickets for a scoped `user`, mirroring
        ``api_tickets_list``'s own scoping rule.
        """

        principal = require_user(request)
        requester_user_id = None if principal.at_least("operator") else principal.user_id
        counts = await store.counts(requester_user_id=requester_user_id)
        return JSONResponse(counts)

    async def api_tickets_create(request: Request) -> JSONResponse:
        """Open a ticket, optionally starting work on it in the same call.

        ``start_immediately`` is a convenience over the two calls the dashboard
        would otherwise make, **not** a way around the ``new -> in_progress``
        gate. It goes through :meth:`TicketService.transition` as the caller,
        so ``tickets.py``'s ``_ACTORS`` rule still decides: system/operator may
        start work, a requester may not ("opening a ticket does not entitle its
        author to drive its lifecycle"). Issuing it as ``actor="system"`` on a
        requester's behalf would have made the flag a bypass of exactly that
        rule, so it does not.

        A refused start is reported, never forced and never fatal: the ticket
        was created and that write is durable, so the response is still 201 with
        the ticket, plus ``started`` and — when it did not start — ``start_error``
        carrying the lifecycle's own reason. Failing the whole call would lose a
        ticket the caller successfully opened.
        """

        principal = require_user(request)
        body = await _body(request)
        title = str(body.get("title", "")).strip()
        if not title:
            return _err("title is required")
        priority = str(body.get("priority", "normal"))
        priority_error = _validate_priority(priority)
        if priority_error:
            return _err(priority_error)
        category = body.get("category")
        _warn_if_unknown_category(category)
        if principal.at_least("operator"):
            requester = body.get("requester_user_id")
            requester_user_id = int(requester) if requester is not None else None
        else:
            # A scoped `user` may only ever open a ticket on their own behalf.
            requester_user_id = principal.user_id
        agent_id = body.get("agent_id")
        if agent_id:
            # A ticket's `agent_id` is frozen at creation as the routing target
            # every later tool call is checked against (see `TicketService.create`),
            # so a scoped `user` naming a host outside their scope here would let
            # them aim work at a machine they cannot otherwise even see. Same
            # check `guard(..., host_param=...)` runs for a path parameter; here
            # the host is in the body, so it is checked by hand. A no-op for
            # operator+, which is never host-scoped.
            require_host(principal, str(agent_id))
        ticket = await tickets.create(
            title=title,
            origin=str(body.get("origin", "dashboard")),
            requester_user_id=requester_user_id,
            agent_id=body.get("agent_id"),
            priority=priority,
            category=category,
            summary=str(body.get("summary", "")),
            actor=_actor(principal),
        )
        started = False
        start_error: str | None = None
        if bool(body.get("start_immediately")):
            try:
                ticket = await tickets.transition(
                    ticket.id,
                    "in_progress",
                    actor=_actor(principal),
                    reason="started on creation",
                )
                started = True
            except TicketError as exc:
                start_error = str(exc)
                logger.info(
                    "ticket %s created but not started: %s", ticket.id, start_error
                )
        payload = _affordances(tickets, ticket, principal)
        payload["started"] = started
        payload["start_error"] = start_error
        return JSONResponse(payload, status_code=201)

    async def api_ticket_get(request: Request) -> JSONResponse:
        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        return JSONResponse(
            {
                **_affordances(tickets, ticket, principal),
                # The dashboard's composer/mirror-checkbox affordances key off
                # these: whether a chat turn can be driven at all, and whether
                # there is a Discord thread to optionally mirror one into.
                "assistant_available": assistant is not None,
                "discord_thread": (await store.get_channel(ticket.id)) is not None,
            }
        )

    async def api_ticket_patch(request: Request) -> JSONResponse:
        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        body = await _body(request)
        priority = body.get("priority")
        if priority is not None:
            priority_error = _validate_priority(str(priority))
            if priority_error:
                return _err(priority_error)
        category = body.get("category")
        if category is not None:
            _warn_if_unknown_category(category)
        updated = await tickets.update(
            ticket.id,
            title=body.get("title"),
            summary=body.get("summary"),
            resolution=body.get("resolution"),
            priority=priority,
            category=category,
        )
        return JSONResponse(_affordances(tickets, updated, principal))

    async def api_ticket_reassign(request: Request) -> JSONResponse:
        principal = require_user(request)
        body = await _body(request)
        agent_id = str(body.get("agent_id") or "").strip()
        if not agent_id:
            # A ticket's frozen target is an authorization control, not a field
            # an empty body may clear: a target-less ticket is the one shape in
            # which a host argument has nothing to be pinned to. Unassigning is
            # deliberate work, not the default of a missing key.
            return _err("agent_id is required")
        updated = await tickets.reassign(
            request.path_params["tid"], agent_id, actor=_actor(principal)
        )
        return JSONResponse(_affordances(tickets, updated, principal))

    async def api_ticket_assign(request: Request) -> JSONResponse:
        """Claim (or unclaim) which operator owns working this ticket.

        The dashboard's "Claim" button sends ``{"assignee_user_id":
        <principal.user_id>}``; a ``null`` unclaims. Distinct from
        ``reassign``, which retargets the *host* a ticket is about.
        """

        principal = require_user(request)
        body = await _body(request)
        has_key = "assignee_user_id" in body
        if not has_key:
            return _err("assignee_user_id is required (null to unclaim)")
        raw = body.get("assignee_user_id")
        assignee_user_id = int(raw) if raw is not None else None
        updated = await tickets.assign(
            request.path_params["tid"], assignee_user_id, actor=_actor(principal)
        )
        return JSONResponse(_affordances(tickets, updated, principal))

    async def api_ticket_events(request: Request) -> JSONResponse:
        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        q = request.query_params
        try:
            limit = int(q.get("limit", "500"))
        except ValueError:
            limit = 500
        events = await tickets.events(ticket.id, limit=limit)
        return JSONResponse({"events": [e.as_dict() for e in events]})

    async def api_ticket_note(request: Request) -> JSONResponse:
        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        # Redundant while the route floor is ``operator``, and deliberately so:
        # every other per-ticket handler carries this check, and the one that
        # does not is the one that breaks silently if the floor ever moves.
        _owned_or_operator(principal, ticket)
        body = await _body(request)
        await tickets.append_event(
            ticket.id,
            kind="note",
            actor=_actor(principal),
            summary=str(body.get("summary", "")),
        )
        return JSONResponse({"ok": True}, status_code=201)

    async def api_ticket_chat_stream(request: Request) -> Response:
        """Drive one turn of the ticket assistant, forwarding events as SSE.

        Mirrors ``webui/__init__.py``'s ``/api/chat/stream``: every check that
        can fail is done *before* the first byte, as a plain JSON error
        response, so the caller never has to distinguish a 4xx/5xx from a
        stream that merely emitted an in-band ``error`` event. Once streaming
        starts the status is fixed at 200 and ``run_turn`` itself is the only
        thing that can still fail, in-band.

        The event vocabulary is exactly ``toolloop.drive_events``'s:
        ``text_delta``, ``tool_result``, ``pending``, ``denied``, ``done``,
        ``error`` — the same one ``handleChatEvent`` already renders for the
        copilot, so the dashboard's ticket chat needs no new client-side event
        handling, only a new place to point ``streamSSE`` at.
        """

        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        body = await _body(request)
        message = str(body.get("message", "")).strip()
        if not message:
            return _err("message is required")
        mirror_to_discord = bool(body.get("mirror_to_discord", False))

        if assistant is None:
            return _err("the AI assistant is not configured", 503)
        if ticket.agent_id is None:
            return _err("this ticket has no target machine", 400)
        if ticket.state in ("closed", "cancelled"):
            return _err("this ticket is closed", 409)
        # The partial unique index (``idx_ticket_approvals_open``) enforces
        # this too, but checking here turns "a gate is already open" into a
        # clean pre-stream 409 instead of a mid-turn failure the model has to
        # be told about — the same reasoning ``_handle_thread_message`` uses.
        if await store.get_open_approval(ticket.id) is not None:
            return _err(
                "a request is already waiting for a decision on this ticket", 409
            )
        if mirror_to_discord and (
            discord is None or (await store.get_channel(ticket.id)) is None
        ):
            return _err("this ticket has no Discord thread to mirror to", 400)

        session = await assistant.session_for(ticket, actor=principal)
        if session is None:
            # Shouldn't normally happen given the checks above (the caller is
            # already an authenticated, enabled account with a live ticket) —
            # handled anyway, since ``session_for``'s contract is to return
            # ``None`` on failure rather than raise.
            return _err("could not build a session for this ticket", 400)

        # actionable=True: ``_owned_or_operator`` above already admitted only
        # the ticket's own requester or an operator+, exactly who _SYSTEM_PROMPT
        # describes as actionable — the same reasoning the trail row below
        # has always used for this route.
        assistant.append_inbound(
            session,
            author_id=_actor(principal),
            kenny_user=principal.username,
            role=principal.role,
            actionable=True,
            content=message,
        )
        await assistant.append_message(
            ticket,
            actor=_actor(principal),
            text=message,
            actionable=True,
            surface="dashboard",
            verbatim=True,
        )

        surfaces = (discord,) if mirror_to_discord and discord is not None else ()

        async def gen() -> Any:
            try:
                async for event in assistant.run_turn(session, ticket, surfaces=surfaces):
                    yield _sse(event)
            except Exception as exc:  # noqa: BLE001 - surface to the UI in-band
                logger.exception("ticket %s: chat stream failed", ticket.id)
                yield _sse({"type": "error", "error": str(exc)})

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers=_STREAM_HEADERS
        )

    async def api_ticket_close(request: Request) -> JSONResponse:
        principal = require_user(request)
        body = await _body(request)
        updated = await tickets.transition(
            request.path_params["tid"],
            "closed",
            actor=_actor(principal),
            reason=str(body.get("reason", "")),
        )
        return JSONResponse(_affordances(tickets, updated, principal))

    async def api_ticket_transition(request: Request) -> JSONResponse:
        """Lifecycle moves: start work, resolve, reopen, cancel.

        One generic route rather than one per verb: ``transition()`` already
        enforces legality (``_check_transition``) and actor authority, so this
        covers every edge without duplicating that logic per action.
        ``and_close`` (only meaningful when ``to == "resolved"``) chains straight
        into ``closed`` in the same call, mirroring what the Discord `/close`
        path already does for a requester — without removing the
        separate, later "Close ticket" action, since the ``resolved`` dwell
        window (and the sweeper's auto-close) is the intended undo window.

        Floor is ``user``, not ``operator``: a requester's own right to cancel
        their ticket (``_ACTORS`` in ``tickets.py``) needs an HTTP route, and
        ``transition()``'s own actor/ownership checks are the real gate — this
        handler only adds the ownership check every other per-ticket handler
        already carries (``_owned_or_operator``), so a `user` cannot even name
        someone else's ticket id.
        """

        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        body = await _body(request)
        to_state = str(body.get("to") or "").strip()
        if not to_state:
            return _err("to is required")
        reason = str(body.get("reason", ""))
        updated = await tickets.transition(
            ticket.id, to_state, actor=_actor(principal), reason=reason
        )
        if to_state == "resolved" and body.get("and_close"):
            updated = await tickets.transition(
                ticket.id,
                "closed",
                actor=_actor(principal),
                reason=reason or "resolved and closed together",
            )
        return JSONResponse(_affordances(tickets, updated, principal))

    async def api_ticket_block(request: Request) -> JSONResponse:
        """Operator-driven: mark a ticket blocked on a reason (see ``BLOCKED_REASONS``).

        No ``requester`` role ever appears in ``_BLOCK_SETTERS`` (see
        ``tickets.py``) — a scoped `user` can never legally set a block, so this
        route's floor is ``operator`` rather than repeating an ownership check
        that would never let a `user` through anyway.
        """

        principal = require_user(request)
        body = await _body(request)
        blocked_on = str(body.get("blocked_on") or "").strip()
        if not blocked_on:
            return _err("blocked_on is required")
        updated = await tickets.block(
            request.path_params["tid"],
            blocked_on,
            actor=_actor(principal),
            reason=str(body.get("reason", "")),
        )
        return JSONResponse(_affordances(tickets, updated, principal))

    async def api_ticket_unblock(request: Request) -> JSONResponse:
        """Clear whatever a ticket is blocked on.

        Floor is ``user``: a requester may clear their own ticket's ``user``
        block (answering kenny's question from the dashboard instead of
        Discord) — ``unblock()``'s own ``_check_unblock`` still refuses an
        ``approval``/``operator`` block to anyone but an operator.
        """

        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        body = await _body(request)
        updated = await tickets.unblock(
            ticket.id, actor=_actor(principal), reason=str(body.get("reason", ""))
        )
        return JSONResponse(_affordances(tickets, updated, principal))

    # -- approvals -------------------------------------------------------------

    async def api_approvals_list(request: Request) -> JSONResponse:
        """Pending approval gates.

        Operator+ sees every open gate, unmodified. A scoped ``user`` sees only
        the gates on tickets *they themselves* opened: ``decide_approval``
        already lets a requester decide a ``user_consent`` gate on their own
        ticket ("Approving is enforced here", ``tickets.py``), but until now
        this listing floored at ``operator`` so that requester had no way to
        even find the gate they are the only person allowed to act on. See
        ``api_approval_decide`` for the matching (and separately gated) widening
        of who may *decide* one.
        """

        principal = require_user(request)
        ticket_id = request.query_params.get("ticket_id")
        if ticket_id is not None and not principal.at_least("operator"):
            _owned_or_operator(principal, await tickets.get(ticket_id))
        approvals = await store.list_open_approvals(ticket_id=ticket_id)
        if not principal.at_least("operator"):
            owned: list[TicketApproval] = []
            for approval in approvals:
                ticket = await tickets.get(approval.ticket_id)
                if ticket.requester_user_id == principal.user_id:
                    owned.append(approval)
            approvals = owned
        return JSONResponse({"approvals": [a.as_dict() for a in approvals]})

    async def api_approval_decide(request: Request) -> JSONResponse:
        """Decide a held call and then let the ticket act on the decision.

        Deciding is only half of it: the frozen call runs when the ticket is
        *resumed*, so a dashboard decision that stopped at the row would leave
        an operator reading "approved" while the ticket sat blocked on
        ``approval`` and nothing ever executed. The resume goes through
        :meth:`TicketAssistant.resume` directly — not gated on
        ``discord is not None``, since a dashboard-only ticket (no Discord
        configured at all) must resume too. When Discord *is* configured it is
        still passed as a surface, exactly as the Discord button's own resume
        path always has: a ticket with no Discord thread bound just makes that
        surface a no-op (``deliver_reply``/``announce_gate`` check the binding
        themselves).

        The decision is durable before the resume starts, so a resume failure is
        logged and reported (``resumed``/``resume_status`` — the latter is
        :data:`~kenny_server.ticket_assistant.ResumeStatus`'s real value, not a
        hardcoded success), never raised: failing the request would tell the
        caller their decision did not happen when it did. If a
        Discord approval card produced this gate, it is also resolved
        (made non-clickable) here — best-effort, independent of ``resumed`` —
        so a card decided from the dashboard does not sit in Discord looking
        like it is still awaiting a click.
        """

        principal = require_user(request)
        body = await _body(request)
        approve = body.get("approve")
        if not isinstance(approve, bool):
            return _err("approve must be a boolean")
        if not principal.at_least("operator"):
            # ``TicketService.decide_approval`` enforces who may *approve* a
            # gate (only an operator for ``operator_approval``, only the
            # ticket's own requester for ``user_consent``), but it explicitly
            # leaves *denial* open to any actor ("Denying stays open to every
            # actor" — the sweeper's expiry and a requester's own withdrawal
            # both rely on that). Left alone, that would let a scoped `user`
            # deny anyone's pending gate over this route, not just their own —
            # a real widening, not the neutral floor-lower it looks like. So
            # this route adds the same pre-check ``handle_component`` already
            # runs before ever reaching the service for the Discord button
            # (``discord_service.py``): a `user` may reach only a
            # ``user_consent`` gate on a ticket they themselves requested;
            # everything else (another user's consent gate, any
            # ``operator_approval``) is refused here, before either
            # `approve=True` or `approve=False` gets near the service.
            approval = await store.get_approval(request.path_params["aid"])
            if approval is not None:
                if approval.kind != "user_consent":
                    raise Forbidden(403, "only an operator can decide this step")
                ticket = await tickets.get(approval.ticket_id)
                _owned_or_operator(principal, ticket)
            # else: no pre-check to run — decide_approval itself raises
            # ApprovalNotFoundError, translated to the usual 404 below.
        decided = await tickets.decide_approval(
            request.path_params["aid"],
            approve=approve,
            decided_by=principal.user_id,
            decided_via="dashboard",
            actor=_actor(principal),
        )
        # Not a ``ResumeStatus`` member — the assistant is a deployment-time
        # optional, distinct from every reason ``resume()`` itself can give up.
        resume_status = "not_configured"
        if assistant is not None:
            surfaces = (discord,) if discord is not None else ()
            try:
                resume_status = await assistant.resume(
                    decided.ticket_id,
                    approval=decided,
                    decided_by=principal,
                    surfaces=surfaces,
                )
            except Exception:  # noqa: BLE001 - the decision already happened
                logger.exception(
                    "ticket %s: resuming after a dashboard decision on %s failed",
                    decided.ticket_id,
                    decided.id,
                )
        if discord is not None and decided.discord_channel_id and decided.discord_message_id:
            try:
                await discord.gateway.resolve_card(
                    channel_id=decided.discord_channel_id,
                    message_id=decided.discord_message_id,
                    outcome="approved" if approve else "denied",
                    decided_by=str(principal.user_id),
                )
            except Exception:  # noqa: BLE001 - the decision already happened
                logger.exception(
                    "ticket %s: resolving the Discord approval card for %s failed",
                    decided.ticket_id,
                    decided.id,
                )
        return JSONResponse(
            {
                **decided.as_dict(),
                "resumed": resume_status == "resumed",
                "resume_status": resume_status,
            }
        )

    # -- Discord (self-service: see/remove the caller's own binding) -----------
    #
    # ADR-0044 requires an operator to *create* a Discord binding — a chat
    # platform's identity assertion carries no proof of possession, so kenny
    # will not mint one from self-service. That is unchanged here. But
    # unbinding only takes *away* the privilege a binding carries, never
    # grants any, so it needs no operator step: these two routes let a plain
    # `user` see and remove their own binding, nothing else. Same 503-when-
    # unconfigured posture as the rest of this section (``_need_identities``),
    # so an unconfigured server still serves every other route.

    async def api_me_discord_get(request: Request) -> JSONResponse:
        """The caller's own Discord binding(s), or a clean "not linked" answer.

        Only *active* (non-disabled) bindings are shown — a disabled row was
        already revoked by an operator and, per ``resolve()``, carries no
        privilege, so it is not this account's business to see or clear.
        Never a 404: an unlinked account and an unconfigured server both read
        as ``{"linked": false, "bindings": []}``, not an error.

        Only what the store actually holds is returned — ``discord_user_id``
        (the snowflake), ``guild_id``, ``linked_at``, ``linked_via``. No
        display name is invented: ``display_hint`` lives on the mutable,
        unverified ``/link`` claim, never on the identity itself (see
        ADR-0044), so the caller only ever learns the raw Discord id.
        """

        principal = require_user(request)
        rows = (
            []
            if identities is None or principal.user_id is None
            else await identities.list_identities(
                user_id=principal.user_id, include_disabled=False
            )
        )
        return JSONResponse(
            {
                "linked": bool(rows),
                "bindings": [
                    {
                        "discord_user_id": r.discord_user_id,
                        "guild_id": r.guild_id,
                        "linked_at": r.linked_at,
                        "linked_via": r.linked_via,
                    }
                    for r in rows
                ],
                "note": (
                    "kenny only knows the Discord account id (snowflake); "
                    "it never stores a display name"
                ),
            }
        )

    async def api_me_discord_delete(request: Request) -> JSONResponse:
        """Remove every one of the caller's own (active) Discord bindings.

        The target is always ``principal.user_id`` — resolved from the
        authenticated session and nothing else. This handler does not read a
        path parameter, a query string, or the request body to decide *whose*
        binding to remove, so nothing a caller sends can point it at another
        account (see the cross-user test in ``tests/test_tickets_api.py``).

        A user may hold a binding per guild (the ``(user_id, guild_id)``
        unique index). This route takes no guild argument and removes all of
        the caller's bindings in one call — self-service unbind is total, not
        per-guild, since a binding is the same privilege grant in whichever
        guild it lives in and the caller has no legitimate reason to keep one
        while dropping another. A disabled (operator-revoked) row is left
        alone; it already carries no privilege.

        Always ``200``, even when nothing was removed — an idempotent "make
        sure I'm unbound" call, not a lookup that can 404.
        """

        principal = require_user(request)
        removed = (
            0
            if identities is None or principal.user_id is None
            else await identities.unlink_user(principal.user_id)
        )
        if removed:
            logger.info(
                "discord: user %s removed their own Discord binding (%d guild(s))",
                principal.user_id,
                removed,
            )
        return JSONResponse({"ok": True, "removed": removed})

    # -- Discord (superuser-managed identities; operator-visible status) -------

    async def api_discord_status(_request: Request) -> JSONResponse:
        """Gateway diagnostics, or a flat "not configured" answer.

        Deliberately never ``503``: "is Discord set up at all?" is exactly the
        question this route exists to answer, so an unconfigured server answers
        it with ``configured: false`` rather than an error.
        """

        if discord is None:
            return JSONResponse({"connected": False, "configured": False})
        return JSONResponse({"configured": True, **discord.diagnostics()})

    def _need_identities() -> JSONResponse | None:
        if identities is None:
            return _err("the Discord identity store is not configured", 503)
        return None

    def _need_gateway() -> JSONResponse | None:
        if discord is None:
            return _err("the Discord gateway is not configured", 503)
        return None

    async def api_discord_identities_list(request: Request) -> JSONResponse:
        missing = _need_identities()
        if missing is not None:
            return missing
        assert identities is not None
        rows = await identities.list_identities(guild_id=request.query_params.get("guild_id"))
        return JSONResponse({"identities": [r.as_dict() for r in rows]})

    def _guild_for(named: str | None) -> str | JSONResponse:
        """Resolve the guild a request applies to, or explain why it cannot.

        A guild is never guessed: the caller may name one (and only one on the
        allowlist), otherwise the single configured guild is used, and an
        ambiguous or unconfigured allowlist is an error rather than a default.
        ``guild_id`` is half of the identity table's key, so picking the wrong
        one would silently mint a binding that resolves nowhere.
        """

        wanted = (named or "").strip()
        guilds = sorted(discord.guild_ids) if discord is not None else []
        if wanted:
            if guilds and wanted not in guilds:
                return _err(f"guild {wanted} is not in the allowlist", 403)
            return wanted
        if len(guilds) == 1:
            return guilds[0]
        if not guilds:
            return _err("no Discord guild is configured; pass guild_id", 400)
        return _err(f"several guilds are configured ({', '.join(guilds)}); pass guild_id")

    async def api_discord_identity_create(request: Request) -> JSONResponse:
        """Enrollment path B: an operator links a guild member outright."""

        missing = _need_identities()
        if missing is not None:
            return missing
        assert identities is not None
        principal = require_user(request, "superuser")
        body = await _body(request)
        discord_user_id = str(body.get("discord_user_id", "")).strip()
        if not discord_user_id:
            return _err("discord_user_id is required")
        raw_user_id = body.get("user_id")
        if raw_user_id is None:
            return _err("user_id is required")
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return _err("user_id must be an integer")
        if user_store is not None and await user_store.get_user(user_id) is None:
            return _err("user not found", 404)
        guild = _guild_for(str(body.get("guild_id") or ""))
        if isinstance(guild, JSONResponse):
            return guild
        try:
            identity = await identities.link(
                discord_user_id=discord_user_id,
                user_id=user_id,
                guild_id=guild,
                linked_via="member_list",
                linked_by=principal.user_id,
            )
        except IdentityConflict as exc:
            return _err(str(exc), exc.status_code)
        return JSONResponse(identity.as_dict(), status_code=201)

    async def api_discord_identity_delete(request: Request) -> JSONResponse:
        missing = _need_identities()
        if missing is not None:
            return missing
        assert identities is not None
        removed = await identities.unlink(request.path_params["did"])
        if not removed:
            return _err("identity not found", 404)
        return JSONResponse({"ok": True})

    async def api_discord_members(request: Request) -> JSONResponse:
        """The guild member picker's source. Needs a live gateway, not the store."""

        missing = _need_gateway()
        if missing is not None:
            return missing
        assert discord is not None
        guild = _guild_for(request.query_params.get("guild_id"))
        if isinstance(guild, JSONResponse):
            return guild
        members = await discord.gateway.list_guild_members(guild_id=guild)
        return JSONResponse(
            {
                "guild_id": guild,
                "members": [
                    {"user_id": m.user_id, "display_hint": m.display_hint} for m in members
                ],
            }
        )

    async def api_discord_claims_list(request: Request) -> JSONResponse:
        missing = _need_identities()
        if missing is not None:
            return missing
        assert identities is not None
        claims = await identities.list_pending_claims(
            guild_id=request.query_params.get("guild_id")
        )
        return JSONResponse({"claims": [c.as_dict() for c in claims]})

    async def api_discord_claim_confirm(request: Request) -> JSONResponse:
        """Enrollment path A: an operator confirms a ``/link`` claim code.

        The claim carries the snowflake and the guild; the operator supplies the
        kenny account it maps to. A code that is unknown, expired or already
        consumed changes nothing and reads as ``404``.
        """

        missing = _need_identities()
        if missing is not None:
            return missing
        assert identities is not None
        principal = require_user(request, "superuser")
        body = await _body(request)
        raw_user_id = body.get("user_id")
        if raw_user_id is None:
            return _err("user_id is required")
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return _err("user_id must be an integer")
        if user_store is not None and await user_store.get_user(user_id) is None:
            return _err("user not found", 404)
        try:
            identity = await identities.consume_claim(
                request.path_params["code"], user_id=user_id, linked_by=principal.user_id
            )
        except IdentityConflict as exc:
            return _err(str(exc), exc.status_code)
        if identity is None:
            return _err("no such claim, or it expired or was already used", 404)
        return JSONResponse(identity.as_dict())

    # -- capability profiles ----------------------------------------------------

    async def api_tool_classes(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "profiles": {
                    name: sorted(tools) for name, tools in tool_classes.PROFILES.items()
                },
                "classes": dict(tool_classes.TOOL_CLASSES),
            }
        )

    async def api_user_profile_put(request: Request) -> JSONResponse:
        if user_store is None:
            return _err("user store is not configured", 503)
        uid = int(request.path_params["uid"])
        if await user_store.get_user(uid) is None:
            return _err("user not found", 404)
        body = await _body(request)
        profile = body.get("capability_profile")
        if profile is not None and not isinstance(profile, str):
            return _err("capability_profile must be a string or null")
        try:
            await user_store.set_capability_profile(uid, profile)
        except ValueError as exc:
            return _err(str(exc))
        return JSONResponse(
            {"id": uid, "capability_profile": await user_store.get_capability_profile(uid)}
        )

    # -- auto-ticket rules (ticket_rules.py) -------------------------------------
    #
    # Which alerts open a ticket is operator policy, decided by AlertEngine
    # through ``ticket_rules.decide`` -- this is the CRUD skin over the same
    # mirror the engine consults. Operator+ only, on every route including the
    # read: an alert-origin ticket is itself operator-only by
    # ``_owned_or_operator`` above, so a scoped `user` has no legitimate use for
    # the rules that decide when one gets minted, and showing them would leak
    # fleet host names for no benefit.

    async def api_ticket_rules_vocabulary(_request: Request) -> JSONResponse:
        """The vocabulary the add form / API validation actually accepts.

        Derived from ``ticket_rules.py``'s live registries so the dashboard
        never hardcodes a second copy that can drift from what the engine
        emits or the store validates.
        """

        return JSONResponse(
            {
                "event_types": list(EVENT_TYPES),
                "decisions": list(DECISIONS),
                "sections": {k: sorted(v) for k, v in KNOWN_SECTIONS.items()},
            }
        )

    async def api_ticket_rules_list(_request: Request) -> JSONResponse:
        if ticket_rules is None:
            return _err("ticket rules are not configured", 503)
        return JSONResponse({"rules": ticket_rules.rules()})

    async def api_ticket_rules_add(request: Request) -> JSONResponse:
        if ticket_rules is None:
            return _err("ticket rules are not configured", 503)
        principal = require_user(request, "operator")
        body = await _body(request)
        try:
            rules, warnings = await ticket_rules.add(
                event_type=str(body.get("event_type") or ""),
                decision=str(body.get("decision") or ""),
                section=str(body.get("section") or ""),
                agent_id=str(body.get("agent_id") or ""),
                note=str(body.get("note") or ""),
                created_by=getattr(principal, "username", "") or "",
            )
        except ValueError as exc:
            return _err(str(exc))
        return JSONResponse({"rules": rules, "warnings": warnings}, status_code=201)

    async def api_ticket_rules_remove(request: Request) -> JSONResponse:
        if ticket_rules is None:
            return _err("ticket rules are not configured", 503)
        removed, rules = await ticket_rules.remove(request.path_params["rule_id"])
        return JSONResponse({"ok": True, "removed": removed, "rules": rules})

    g = lambda handler, **kw: guard(_catches_ticket_errors(handler), **kw)  # noqa: E731

    return [
        Route("/api/tickets/vocabulary", g(api_tickets_vocabulary, min_role="user")),
        Route("/api/tickets/summary", g(api_tickets_summary, min_role="user")),
        Route("/api/tickets", g(api_tickets_list, min_role="user")),
        Route("/api/tickets", g(api_tickets_create, min_role="user"), methods=["POST"]),
        Route("/api/tickets/{tid}", g(api_ticket_get, min_role="user")),
        Route(
            "/api/tickets/{tid}", g(api_ticket_patch, min_role="user"), methods=["PATCH"]
        ),
        Route(
            "/api/tickets/{tid}/reassign",
            g(api_ticket_reassign, min_role="operator"),
            methods=["POST"],
        ),
        Route(
            "/api/tickets/{tid}/assign",
            g(api_ticket_assign, min_role="operator"),
            methods=["POST"],
        ),
        Route("/api/tickets/{tid}/events", g(api_ticket_events, min_role="user")),
        Route(
            "/api/tickets/{tid}/note",
            g(api_ticket_note, min_role="operator"),
            methods=["POST"],
        ),
        Route(
            "/api/tickets/{tid}/chat/stream",
            g(api_ticket_chat_stream, min_role="user"),
            methods=["POST"],
        ),
        Route(
            "/api/tickets/{tid}/close",
            g(api_ticket_close, min_role="user"),
            methods=["POST"],
        ),
        Route(
            "/api/tickets/{tid}/transition",
            g(api_ticket_transition, min_role="user"),
            methods=["POST"],
        ),
        Route(
            "/api/tickets/{tid}/block",
            g(api_ticket_block, min_role="operator"),
            methods=["POST"],
        ),
        Route(
            "/api/tickets/{tid}/unblock",
            g(api_ticket_unblock, min_role="user"),
            methods=["POST"],
        ),
        Route("/api/approvals", g(api_approvals_list, min_role="user")),
        Route(
            "/api/approvals/{aid}",
            g(api_approval_decide, min_role="user"),
            methods=["POST"],
        ),
        Route("/api/me/discord", g(api_me_discord_get, min_role="user")),
        Route(
            "/api/me/discord",
            g(api_me_discord_delete, min_role="user"),
            methods=["DELETE"],
        ),
        Route("/api/discord/status", g(api_discord_status, min_role="operator")),
        Route(
            "/api/discord/identities", g(api_discord_identities_list, min_role="superuser")
        ),
        Route(
            "/api/discord/identities",
            g(api_discord_identity_create, min_role="superuser"),
            methods=["POST"],
        ),
        Route(
            "/api/discord/identities/{did}",
            g(api_discord_identity_delete, min_role="superuser"),
            methods=["DELETE"],
        ),
        Route("/api/discord/members", g(api_discord_members, min_role="superuser")),
        Route("/api/discord/claims", g(api_discord_claims_list, min_role="superuser")),
        Route(
            "/api/discord/claims/{code}",
            g(api_discord_claim_confirm, min_role="superuser"),
            methods=["POST"],
        ),
        Route(
            "/api/users/{uid}/profile",
            g(api_user_profile_put, min_role="superuser"),
            methods=["PUT"],
        ),
        Route("/api/tool-classes", g(api_tool_classes, min_role="operator")),
        Route(
            "/api/ticket-rules/vocabulary",
            g(api_ticket_rules_vocabulary, min_role="operator"),
        ),
        Route("/api/ticket-rules", g(api_ticket_rules_list, min_role="operator")),
        Route(
            "/api/ticket-rules", g(api_ticket_rules_add, min_role="operator"), methods=["POST"]
        ),
        Route(
            "/api/ticket-rules/{rule_id}",
            g(api_ticket_rules_remove, min_role="operator"),
            methods=["DELETE"],
        ),
    ]


__all__ = ["build_ticket_routes"]

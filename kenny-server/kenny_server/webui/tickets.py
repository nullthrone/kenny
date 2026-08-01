"""Ticket, approval, Discord-identity and tool-class routes for the dashboard API.

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

**Discord/collaborators are optional.** ``identities``, ``user_store`` and
``gateway_status`` are accepted as keyword-only collaborators defaulting to
``None`` so this module imports and the routes register cleanly before the
integration step (in ``main.py``) wires the concrete Discord service. Until
then, any route that needs one of them returns ``503``. The identity/member/
claim handlers are intentionally duck-typed (``list()``/``create()``/etc. on
whatever ``identities`` turns out to be) rather than importing a concrete type
from ``discord_identity``/``discord_service`` — those modules are owned and
under active development elsewhere.
"""

from __future__ import annotations

import inspect
from dataclasses import asdict, is_dataclass
from functools import wraps
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import tool_classes
from ..auth import Principal
from ..ticketstore import Ticket, TicketStore
from ..tickets import TicketError, TicketService, TransitionError
from .authz import Forbidden, guard, require_user

# -- small local helpers (mirrors webui/users.py's shape) ----------------------


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - malformed/empty body
        return {}
    return data if isinstance(data, dict) else {}


def _err(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": "invalid", "detail": detail}, status_code=status)


_STATUS_ERROR_NAMES = {400: "invalid", 403: "forbidden", 404: "not_found", 409: "conflict"}


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


def _as_dict(obj: Any) -> dict[str, Any]:
    """Best-effort ``dict`` view of a collaborator-supplied row of unknown shape."""

    if isinstance(obj, dict):
        return obj
    as_dict = getattr(obj, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    if is_dataclass(obj):
        return asdict(obj)
    return vars(obj)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def build_ticket_routes(
    *,
    tickets: TicketService,
    store: TicketStore,
    identities: Any = None,
    user_store: Any = None,
    gateway_status: Any = None,
) -> list[Route]:
    """Ticket/approval/Discord/tool-class routes. See module docstring."""

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
        return JSONResponse({"tickets": [t.as_dict() for t in rows]})

    async def api_tickets_create(request: Request) -> JSONResponse:
        principal = require_user(request)
        body = await _body(request)
        title = str(body.get("title", "")).strip()
        if not title:
            return _err("title is required")
        if principal.at_least("operator"):
            requester = body.get("requester_user_id")
            requester_user_id = int(requester) if requester is not None else None
        else:
            # A scoped `user` may only ever open a ticket on their own behalf.
            requester_user_id = principal.user_id
        ticket = await tickets.create(
            title=title,
            origin=str(body.get("origin", "dashboard")),
            requester_user_id=requester_user_id,
            agent_id=body.get("agent_id"),
            priority=str(body.get("priority", "normal")),
            category=body.get("category"),
            summary=str(body.get("summary", "")),
            actor=_actor(principal),
        )
        return JSONResponse(ticket.as_dict(), status_code=201)

    async def api_ticket_get(request: Request) -> JSONResponse:
        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        return JSONResponse(ticket.as_dict())

    async def api_ticket_patch(request: Request) -> JSONResponse:
        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
        _owned_or_operator(principal, ticket)
        body = await _body(request)
        updated = await tickets.update(
            ticket.id,
            title=body.get("title"),
            summary=body.get("summary"),
            resolution=body.get("resolution"),
            priority=body.get("priority"),
            category=body.get("category"),
        )
        return JSONResponse(updated.as_dict())

    async def api_ticket_reassign(request: Request) -> JSONResponse:
        principal = require_user(request)
        body = await _body(request)
        updated = await tickets.reassign(
            request.path_params["tid"], body.get("agent_id"), actor=_actor(principal)
        )
        return JSONResponse(updated.as_dict())

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
        body = await _body(request)
        await tickets.append_event(
            ticket.id,
            kind="note",
            actor=_actor(principal),
            summary=str(body.get("summary", "")),
        )
        return JSONResponse({"ok": True}, status_code=201)

    async def api_ticket_close(request: Request) -> JSONResponse:
        principal = require_user(request)
        body = await _body(request)
        updated = await tickets.transition(
            request.path_params["tid"],
            "closed",
            actor=_actor(principal),
            reason=str(body.get("reason", "")),
        )
        return JSONResponse(updated.as_dict())

    # -- approvals -------------------------------------------------------------

    async def api_approvals_list(request: Request) -> JSONResponse:
        q = request.query_params
        approvals = await store.list_open_approvals(ticket_id=q.get("ticket_id"))
        return JSONResponse({"approvals": [a.as_dict() for a in approvals]})

    async def api_approval_decide(request: Request) -> JSONResponse:
        principal = require_user(request)
        body = await _body(request)
        approve = body.get("approve")
        if not isinstance(approve, bool):
            return _err("approve must be a boolean")
        decided = await tickets.decide_approval(
            request.path_params["aid"],
            approve=approve,
            decided_by=principal.user_id,
            decided_via="dashboard",
            actor=_actor(principal),
        )
        return JSONResponse(decided.as_dict())

    # -- Discord (superuser-managed identities; operator-visible status) -------

    async def api_discord_status(_request: Request) -> JSONResponse:
        if gateway_status is None:
            return JSONResponse({"connected": False, "configured": False})
        result = await _maybe_await(
            gateway_status() if callable(gateway_status) else gateway_status
        )
        if not isinstance(result, dict):
            result = _as_dict(result)
        return JSONResponse(result)

    def _identities_required() -> JSONResponse | None:
        if identities is None:
            return _err("discord identity service is not configured", 503)
        return None

    async def api_discord_identities_list(_request: Request) -> JSONResponse:
        missing = _identities_required()
        if missing is not None:
            return missing
        rows = await _maybe_await(identities.list())
        return JSONResponse({"identities": [_as_dict(r) for r in rows]})

    async def api_discord_identity_create(request: Request) -> JSONResponse:
        missing = _identities_required()
        if missing is not None:
            return missing
        body = await _body(request)
        created = await _maybe_await(identities.create(**body))
        return JSONResponse(_as_dict(created), status_code=201)

    async def api_discord_identity_delete(request: Request) -> JSONResponse:
        missing = _identities_required()
        if missing is not None:
            return missing
        ok = await _maybe_await(identities.delete(request.path_params["did"]))
        return JSONResponse({"ok": bool(ok)})

    async def api_discord_members(_request: Request) -> JSONResponse:
        missing = _identities_required()
        if missing is not None:
            return missing
        rows = await _maybe_await(identities.list_members())
        return JSONResponse({"members": [_as_dict(r) for r in rows]})

    async def api_discord_claim(request: Request) -> JSONResponse:
        missing = _identities_required()
        if missing is not None:
            return missing
        body = await _body(request)
        result = await _maybe_await(identities.claim(request.path_params["code"], **body))
        return JSONResponse(_as_dict(result))

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

    g = lambda handler, **kw: guard(_catches_ticket_errors(handler), **kw)  # noqa: E731

    return [
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
        Route("/api/tickets/{tid}/events", g(api_ticket_events, min_role="user")),
        Route(
            "/api/tickets/{tid}/note",
            g(api_ticket_note, min_role="operator"),
            methods=["POST"],
        ),
        Route(
            "/api/tickets/{tid}/close",
            g(api_ticket_close, min_role="user"),
            methods=["POST"],
        ),
        Route("/api/approvals", g(api_approvals_list, min_role="operator")),
        Route(
            "/api/approvals/{aid}",
            g(api_approval_decide, min_role="operator"),
            methods=["POST"],
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
        Route(
            "/api/discord/claims/{code}",
            g(api_discord_claim, min_role="superuser"),
            methods=["POST"],
        ),
        Route(
            "/api/users/{uid}/profile",
            g(api_user_profile_put, min_role="superuser"),
            methods=["PUT"],
        ),
        Route("/api/tool-classes", g(api_tool_classes, min_role="operator")),
    ]


__all__ = ["build_ticket_routes"]

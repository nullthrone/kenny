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

**Discord is optional, and tickets do not depend on it.** ``identities``,
``user_store`` and ``discord`` are keyword-only collaborators defaulting to
``None``: a server with no Discord configuration still serves every ticket and
approval route, and the routes that genuinely need a Discord collaborator answer
``503`` instead. The two collaborators are separate on purpose — the identity
mapping is a plain SQLite store that exists whether or not a bot is connected,
while the guild member list and the connection status can only come from a live
gateway (:class:`~kenny_server.discord_service.DiscordService`).
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import tool_classes
from ..auth import Principal
from ..discord_identity import DiscordIdentityStore, IdentityConflict
from ..ticketstore import Ticket, TicketStore
from ..tickets import TicketError, TicketService, TransitionError
from ..userstore import UserStore
from .authz import Forbidden, guard, require_user

if TYPE_CHECKING:  # pragma: no cover - import cycle-free typing only
    from ..discord_service import DiscordService

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


def build_ticket_routes(
    *,
    tickets: TicketService,
    store: TicketStore,
    identities: DiscordIdentityStore | None = None,
    user_store: UserStore | None = None,
    discord: DiscordService | None = None,
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

    async def api_ticket_transition(request: Request) -> JSONResponse:
        """Operator-driven lifecycle moves: resolve, reopen, cancel.

        One generic route rather than one per verb: ``transition()`` already
        enforces legality (``_check_transition``) and actor authority, so this
        covers resolve/reopen/cancel without duplicating that logic per action.
        ``and_close`` (only meaningful when ``to == "resolved"``) chains straight
        into ``closed`` in the same call, mirroring what the Discord `/kenny
        close` path already does for a requester — without removing the
        separate, later "Close ticket" action, since the ``resolved`` dwell
        window (and the sweeper's auto-close) is the intended undo window.
        """

        principal = require_user(request)
        ticket = await tickets.get(request.path_params["tid"])
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
        return JSONResponse(updated.as_dict())

    # -- approvals -------------------------------------------------------------

    async def api_approvals_list(request: Request) -> JSONResponse:
        q = request.query_params
        approvals = await store.list_open_approvals(ticket_id=q.get("ticket_id"))
        return JSONResponse({"approvals": [a.as_dict() for a in approvals]})

    async def api_approval_decide(request: Request) -> JSONResponse:
        """Decide a held call and then let the ticket act on the decision.

        Deciding is only half of it: the frozen call runs when the ticket is
        *resumed*, so a dashboard decision that stopped at the row would leave
        an operator reading "approved" while the ticket sat in
        ``awaiting_approval`` and nothing ever executed. The resume goes through
        the same :meth:`DiscordService.resume` the Discord button uses.

        The decision is durable before the resume starts, so a resume failure is
        logged and reported (``resumed``), never raised: failing the request
        would tell the caller their decision did not happen when it did.
        """

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
        resumed = False
        if discord is not None:
            try:
                await discord.resume(decided.ticket_id, approval=decided)
                resumed = True
            except Exception:  # noqa: BLE001 - the decision already happened
                logger.exception(
                    "ticket %s: resuming after a dashboard decision on %s failed",
                    decided.ticket_id,
                    decided.id,
                )
        return JSONResponse({**decided.as_dict(), "resumed": resumed})

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
        """Enrollment path A: an operator confirms a ``/kenny link`` claim code.

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
        Route(
            "/api/tickets/{tid}/transition",
            g(api_ticket_transition, min_role="operator"),
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
    ]


__all__ = ["build_ticket_routes"]

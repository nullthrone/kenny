"""Ticket lifecycle: the one place a ticket's state may change.

:class:`TicketService` is the chokepoint. **Nothing else in the codebase may
ever change a ticket's state**: :meth:`kenny_server.ticketstore.TicketStore.set_state`
is the low-level primitive and :meth:`TicketService.transition` is its only
sanctioned caller. Everything a state change has to be true of — the legal
successor states (:data:`_ALLOWED`), who is allowed to drive each one
(:data:`_ACTORS`), and the audit row that must accompany it — lives here and
only here.

This module is transport-agnostic and model-agnostic by design: it must not
import chat, tool-loop, tool-class, Discord or Anthropic code. ``tool_class``
is just a string column to it.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .ticketstore import Ticket, TicketApproval, TicketEvent, TicketStore, to_iso

__all__ = [
    "DEFAULT_APPROVAL_TTL_SECS",
    "DEFAULT_AUTOCLOSE_SECS",
    "DEFAULT_SWEEP_INTERVAL_SECS",
    "REDACTED",
    "STATES",
    "ApprovalConflictError",
    "ApprovalNotFoundError",
    "TicketError",
    "TicketNotFoundError",
    "TicketService",
    "TransitionError",
    "redact_args",
    "ticket_sweep_loop",
]

logger = logging.getLogger("kenny.tickets")

# -- lifecycle -----------------------------------------------------------------

STATES: frozenset[str] = frozenset(
    {
        "new",
        "triage",
        "in_progress",
        "awaiting_user",
        "awaiting_approval",
        "awaiting_agent",
        "resolved",
        "closed",
        "cancelled",
    }
)

# Legal successor states. ``closed`` and ``cancelled`` map to the empty set:
# they are terminal, and a ticket that reached them can only be read.
_ALLOWED: dict[str, frozenset[str]] = {
    "new": frozenset({"triage", "cancelled"}),
    "triage": frozenset(
        {"in_progress", "awaiting_user", "awaiting_approval", "awaiting_agent", "cancelled"}
    ),
    "in_progress": frozenset(
        {"awaiting_user", "awaiting_approval", "awaiting_agent", "resolved", "cancelled"}
    ),
    "awaiting_user": frozenset({"in_progress", "resolved", "cancelled"}),
    "awaiting_approval": frozenset({"in_progress", "cancelled"}),
    "awaiting_agent": frozenset({"in_progress", "cancelled"}),
    # Reopening is only possible while the ticket is still ``resolved``; the
    # sweeper closes it once the window has passed, and ``closed`` is terminal.
    "resolved": frozenset({"closed", "in_progress"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}

ROLES: frozenset[str] = frozenset({"system", "requester", "operator"})

# Who may drive each transition. ``operator`` covers operator and superuser;
# ``requester`` additionally has to *own* the ticket (see ``_authorize``).
#
# Two rules shape the table: leaving ``awaiting_approval`` for ``in_progress``
# is the approval-driven transition and is operator-only — a requester must not
# be able to release their own gate; and a requester may cancel their ticket
# from any live state, close it once resolved, and answer a question
# (``awaiting_user -> in_progress``).
_ACTORS: dict[tuple[str, str], frozenset[str]] = {
    ("new", "triage"): frozenset({"system", "operator"}),
    ("new", "cancelled"): frozenset({"system", "requester", "operator"}),
    ("triage", "in_progress"): frozenset({"system", "operator"}),
    ("triage", "awaiting_user"): frozenset({"system", "operator"}),
    ("triage", "awaiting_approval"): frozenset({"system", "operator"}),
    ("triage", "awaiting_agent"): frozenset({"system", "operator"}),
    ("triage", "cancelled"): frozenset({"system", "requester", "operator"}),
    ("in_progress", "awaiting_user"): frozenset({"system", "operator"}),
    ("in_progress", "awaiting_approval"): frozenset({"system", "operator"}),
    ("in_progress", "awaiting_agent"): frozenset({"system", "operator"}),
    ("in_progress", "resolved"): frozenset({"system", "operator"}),
    ("in_progress", "cancelled"): frozenset({"system", "requester", "operator"}),
    ("awaiting_user", "in_progress"): frozenset({"system", "requester", "operator"}),
    ("awaiting_user", "resolved"): frozenset({"system", "operator"}),
    ("awaiting_user", "cancelled"): frozenset({"system", "requester", "operator"}),
    ("awaiting_approval", "in_progress"): frozenset({"operator"}),
    ("awaiting_approval", "cancelled"): frozenset({"requester", "operator"}),
    ("awaiting_agent", "in_progress"): frozenset({"system", "operator"}),
    ("awaiting_agent", "cancelled"): frozenset({"system", "requester", "operator"}),
    ("resolved", "closed"): frozenset({"system", "requester", "operator"}),
    ("resolved", "in_progress"): frozenset({"requester", "operator"}),
}

# Actor-string prefixes to lifecycle roles. Operator and superuser accounts are
# one role here: both may drive anything an operator may.
_ROLE_PREFIXES: dict[str, str] = {
    "system": "system",
    "user": "requester",
    "requester": "requester",
    "operator": "operator",
    "superuser": "operator",
}

# Trail kinds callers may append. ``state`` and ``handoff`` are written by
# ``transition``/``reassign`` themselves and are refused here, so the trail
# cannot claim a state change that never happened.
EVENT_KINDS: frozenset[str] = frozenset(
    {"note", "tool_call", "approval", "consent", "message", "error"}
)

APPROVAL_KINDS: frozenset[str] = frozenset({"operator_approval", "user_consent"})

DEFAULT_APPROVAL_TTL_SECS = 3600
DEFAULT_AUTOCLOSE_SECS = 3 * 24 * 3600
DEFAULT_SWEEP_INTERVAL_SECS = 300

# Settings keys the sweeper re-reads each pass through the injected getter. An
# unknown key yields None from the getter, which falls back to the defaults
# above — this module never reads the environment or the settings catalog.
SWEEP_INTERVAL_SETTING = "KENNY_TICKET_SWEEP_INTERVAL_SECS"
AUTOCLOSE_SETTING = "KENNY_TICKET_AUTOCLOSE_SECS"

# -- redaction -----------------------------------------------------------------

REDACTED = "***"
_SECRET_KEY_HINTS = ("password", "token", "secret", "key")


def _is_secret_key(key: Any) -> bool:
    name = str(key).lower()
    return any(hint in name for hint in _SECRET_KEY_HINTS)


def redact_args(value: Any) -> Any:
    """Return ``value`` with secret-looking dict keys replaced by ``"***"``.

    Redaction is by key *name* — any key whose lowercased name contains
    ``password``, ``token``, ``secret`` or ``key`` — and recurses through nested
    dicts and lists. Tool arguments reach the trail verbatim otherwise, and at
    least one capability tool (``account_create``) takes a plaintext password.
    """

    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_secret_key(k) else redact_args(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_args(v) for v in value]
    return value


# -- errors --------------------------------------------------------------------


class TicketError(Exception):
    """Base class for lifecycle errors. ``status_code`` maps to HTTP."""

    status_code = 400


class TicketNotFoundError(TicketError):
    """No such ticket."""

    status_code = 404

    def __init__(self, ticket_id: str) -> None:
        super().__init__(f"ticket {ticket_id} not found")
        self.ticket_id = ticket_id


class ApprovalNotFoundError(TicketError):
    """No such approval."""

    status_code = 404

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"approval {approval_id} not found")
        self.approval_id = approval_id


class ApprovalConflictError(TicketError):
    """A gate is already open for this ticket, or was already decided."""

    status_code = 409

    def __init__(self, message: str, *, ticket_id: str | None = None) -> None:
        super().__init__(message)
        self.ticket_id = ticket_id


class TransitionError(TicketError):
    """A state change was refused.

    ``code`` is ``illegal_transition`` (409), ``unknown_state`` (400) or
    ``forbidden_actor`` (403); ``status_code`` carries the matching HTTP status
    so an API layer does not have to re-derive it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        ticket_id: str,
        from_state: str | None,
        to_state: str,
        actor: str,
        role: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.ticket_id = ticket_id
        self.from_state = from_state
        self.to_state = to_state
        self.actor = actor
        self.role = role
        self.status_code = {
            "forbidden_actor": 403,
            "illegal_transition": 409,
            "unknown_state": 400,
        }.get(code, 400)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "detail": str(self),
            "ticket_id": self.ticket_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "actor": self.actor,
        }


def parse_actor(actor: str) -> tuple[str, int | None]:
    """Split an actor string into ``(role, user_id)``.

    ``"system"`` -> ``("system", None)``, ``"user:12"`` -> ``("requester", 12)``,
    ``"operator:3"`` -> ``("operator", 3)``. An unknown prefix yields role
    ``""``, which is in no entry of :data:`_ACTORS` and therefore never
    authorized.
    """

    text = (actor or "").strip()
    prefix, _, rest = text.partition(":")
    role = _ROLE_PREFIXES.get(prefix.lower(), "")
    user_id: int | None = None
    if rest:
        try:
            user_id = int(rest)
        except ValueError:
            user_id = None
    return role, user_id


# -- service -------------------------------------------------------------------


class TicketService:
    """Lifecycle operations over a :class:`~kenny_server.ticketstore.TicketStore`.

    Holds no transport and no model: creating, transitioning, annotating and
    gating a ticket are all expressible without knowing where the ticket came
    from. The clock is injected (``now``) so the sweeper is testable without
    sleeping.
    """

    def __init__(
        self,
        store: TicketStore,
        *,
        now: Callable[[], datetime] | None = None,
        approval_ttl_secs: int = DEFAULT_APPROVAL_TTL_SECS,
        autoclose_secs: int = DEFAULT_AUTOCLOSE_SECS,
    ) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.approval_ttl_secs = approval_ttl_secs
        self.autoclose_secs = autoclose_secs

    def now(self) -> datetime:
        """Current time through the injected clock."""

        return self._now()

    # -- creation ----------------------------------------------------------

    async def create(
        self,
        *,
        title: str,
        origin: str,
        requester_user_id: int | None = None,
        agent_id: str | None = None,
        role_snapshot: str | None = None,
        profile_snapshot: str | None = None,
        priority: str = "normal",
        category: str | None = None,
        summary: str = "",
        actor: str = "system",
        reason: str = "",
        id: str | None = None,
    ) -> Ticket:
        """Mint a ticket in state ``new`` and record its genesis event.

        ``agent_id`` is frozen here: it is the routing target every later tool
        call is checked against, and only :meth:`reassign` may change it.
        ``role_snapshot``/``profile_snapshot`` freeze the requester's
        authorization at creation time so a later account change cannot
        retroactively widen what an in-flight ticket was allowed to do.
        """

        stamp = to_iso(self.now())
        ticket = await self.store.create(
            id=id,
            title=title,
            origin=origin,
            state="new",
            priority=priority,
            category=category,
            requester_user_id=requester_user_id,
            agent_id=agent_id,
            role_snapshot=role_snapshot,
            profile_snapshot=profile_snapshot,
            summary=summary,
            now=stamp,
        )
        await self.store.append_event(
            ticket_id=ticket.id,
            kind="state",
            actor=actor,
            from_state=None,
            to_state="new",
            summary=reason or "ticket created",
            fields={"origin": origin, "agent_id": agent_id},
            now=stamp,
        )
        return ticket

    async def get(self, ticket_id: str) -> Ticket:
        """Return a ticket or raise :class:`TicketNotFoundError`."""

        ticket = await self.store.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

    # -- the chokepoint ----------------------------------------------------

    async def transition(
        self, ticket_id: str, to_state: str, *, actor: str, reason: str = ""
    ) -> Ticket:
        """Move a ticket to ``to_state`` on behalf of ``actor``.

        The only sanctioned caller of ``TicketStore.set_state``. Rejects an
        illegal transition (409) and an unauthorized actor (403) with a
        :class:`TransitionError`, and records a ``kind='state'`` event in the
        same transaction as the change.

        There is deliberately **no** ``agent_id`` parameter: retargeting a
        ticket at another host is a separate, operator-only :meth:`reassign`.
        The frozen routing target is a security control, so it must not be
        changeable as a side effect of a routine state change.
        """

        ticket = await self.get(ticket_id)
        self._check_transition(ticket, to_state, actor)
        updated = await self.store.set_state(
            ticket_id, to_state, actor=actor, reason=reason, now=to_iso(self.now())
        )
        if updated is None:  # pragma: no cover - existence checked above
            raise TicketNotFoundError(ticket_id)
        return updated

    def can_transition(self, ticket: Ticket, to_state: str, actor: str) -> bool:
        """True if :meth:`transition` would be accepted (for UI affordances)."""

        try:
            self._check_transition(ticket, to_state, actor)
        except TransitionError:
            return False
        return True

    def _check_transition(self, ticket: Ticket, to_state: str, actor: str) -> None:
        if to_state not in STATES:
            raise TransitionError(
                f"unknown state {to_state!r}",
                code="unknown_state",
                ticket_id=ticket.id,
                from_state=ticket.state,
                to_state=to_state,
                actor=actor,
            )
        allowed = _ALLOWED.get(ticket.state, frozenset())
        if to_state not in allowed:
            detail = (
                f"{ticket.state} is terminal"
                if not allowed
                else f"{ticket.state} -> {to_state} is not a legal transition"
            )
            raise TransitionError(
                detail,
                code="illegal_transition",
                ticket_id=ticket.id,
                from_state=ticket.state,
                to_state=to_state,
                actor=actor,
            )
        role, user_id = parse_actor(actor)
        drivers = _ACTORS.get((ticket.state, to_state), frozenset())
        if role not in drivers:
            raise TransitionError(
                f"{actor} may not drive {ticket.state} -> {to_state}",
                code="forbidden_actor",
                ticket_id=ticket.id,
                from_state=ticket.state,
                to_state=to_state,
                actor=actor,
                role=role or None,
            )
        if role == "requester" and (
            ticket.requester_user_id is None or ticket.requester_user_id != user_id
        ):
            raise TransitionError(
                f"{actor} does not own ticket {ticket.id}",
                code="forbidden_actor",
                ticket_id=ticket.id,
                from_state=ticket.state,
                to_state=to_state,
                actor=actor,
                role=role,
            )

    async def reassign(self, ticket_id: str, agent_id: str | None, *, actor: str) -> Ticket:
        """Retarget a ticket at another host. Operator-only.

        Kept apart from :meth:`transition` on purpose — see that docstring.
        Writes a ``kind='handoff'`` event in the same transaction.
        """

        ticket = await self.get(ticket_id)
        role, _ = parse_actor(actor)
        if role != "operator":
            raise TransitionError(
                f"{actor} may not reassign a ticket",
                code="forbidden_actor",
                ticket_id=ticket_id,
                from_state=ticket.state,
                to_state=ticket.state,
                actor=actor,
                role=role or None,
            )
        updated = await self.store.set_agent_id(
            ticket_id,
            agent_id,
            actor=actor,
            reason=f"reassigned to {agent_id or 'unassigned'}",
            now=to_iso(self.now()),
        )
        if updated is None:  # pragma: no cover - existence checked above
            raise TicketNotFoundError(ticket_id)
        return updated

    # -- annotation --------------------------------------------------------

    async def update(
        self,
        ticket_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        resolution: str | None = None,
        priority: str | None = None,
        category: str | None = None,
    ) -> Ticket:
        """Patch a ticket's editable fields. Never touches ``state``."""

        await self.get(ticket_id)
        updated = await self.store.update(
            ticket_id,
            title=title,
            summary=summary,
            resolution=resolution,
            priority=priority,
            category=category,
            now=to_iso(self.now()),
        )
        if updated is None:  # pragma: no cover - existence checked above
            raise TicketNotFoundError(ticket_id)
        return updated

    async def append_event(
        self,
        ticket_id: str,
        *,
        kind: str,
        actor: str,
        summary: str = "",
        tool: str | None = None,
        tool_class: str | None = None,
        ok: bool | None = None,
        args: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Append one trail entry.

        ``kind`` is one of :data:`EVENT_KINDS`; ``state`` and ``handoff`` rows
        belong to :meth:`transition`/:meth:`reassign` and are refused here.
        ``args`` (a ``tool_call``'s arguments) is redacted by key name before it
        is persisted — see :func:`redact_args`.
        """

        if kind not in EVENT_KINDS:
            raise ValueError(
                f"kind {kind!r} must be one of {sorted(EVENT_KINDS)}; "
                "state/handoff events are written by transition()/reassign()"
            )
        payload = dict(fields) if fields else {}
        if args is not None:
            payload["args"] = redact_args(args)
        await self.store.append_event(
            ticket_id=ticket_id,
            kind=kind,
            actor=actor,
            tool=tool,
            tool_class=tool_class,
            ok=ok,
            summary=summary,
            fields=payload or None,
            now=to_iso(self.now()),
        )

    async def events(self, ticket_id: str, *, limit: int = 500) -> list[TicketEvent]:
        """Return a ticket's trail, oldest first."""

        return await self.store.list_events(ticket_id, limit=limit)

    # -- gates -------------------------------------------------------------

    async def open_approval(
        self,
        ticket_id: str,
        *,
        tool_use_id: str,
        tool: str,
        tool_class: str,
        args: dict[str, Any],
        kind: str = "operator_approval",
        agent_id: str | None = None,
        ttl_secs: int | None = None,
        actor: str = "system",
    ) -> TicketApproval:
        """Open the ticket's one gate and record it on the trail.

        At most one gate may be open per ticket; the second attempt hits the
        partial unique index and is surfaced as :class:`ApprovalConflictError`.
        """

        if kind not in APPROVAL_KINDS:
            raise ValueError(f"kind {kind!r} must be one of {sorted(APPROVAL_KINDS)}")
        await self.get(ticket_id)
        now = self.now()
        ttl = self.approval_ttl_secs if ttl_secs is None else ttl_secs
        expires_at = to_iso(now + timedelta(seconds=ttl)) if ttl and ttl > 0 else None
        try:
            approval = await self.store.create_approval(
                ticket_id=ticket_id,
                tool_use_id=tool_use_id,
                tool=tool,
                tool_class=tool_class,
                args=args,
                kind=kind,
                agent_id=agent_id,
                expires_at=expires_at,
                now=to_iso(now),
            )
        except sqlite3.IntegrityError as exc:
            raise ApprovalConflictError(
                f"ticket {ticket_id} already has an open approval", ticket_id=ticket_id
            ) from exc
        await self.append_event(
            ticket_id,
            kind="approval",
            actor=actor,
            summary=f"{kind} requested for {tool}",
            tool=tool,
            tool_class=tool_class,
            args=args,
            fields={"approval_id": approval.id, "expires_at": expires_at},
        )
        return approval

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approve: bool,
        decided_by: int | None = None,
        decided_via: str | None = None,
        actor: str | None = None,
    ) -> TicketApproval:
        """Close a pending gate and record the decision.

        Deciding does not itself move the ticket: resuming from
        ``awaiting_approval`` is an operator-only transition, so a decision by
        anyone else can never restart the work on its own.
        """

        existing = await self.store.get_approval(approval_id)
        if existing is None:
            raise ApprovalNotFoundError(approval_id)
        if existing.status != "pending":
            raise ApprovalConflictError(
                f"approval {approval_id} was already {existing.status}",
                ticket_id=existing.ticket_id,
            )
        status = "approved" if approve else "denied"
        decided = await self.store.decide_approval(
            approval_id,
            status=status,
            decided_by=decided_by,
            decided_via=decided_via,
            now=to_iso(self.now()),
        )
        if decided is None:
            raise ApprovalConflictError(
                f"approval {approval_id} was already decided", ticket_id=existing.ticket_id
            )
        await self.append_event(
            existing.ticket_id,
            kind="approval",
            actor=actor or (f"operator:{decided_by}" if decided_by is not None else "system"),
            ok=approve,
            summary=f"{existing.kind} {status} for {existing.tool}",
            tool=existing.tool,
            tool_class=existing.tool_class,
            fields={"approval_id": approval_id, "decided_via": decided_via},
        )
        return decided

    async def expire_due(self, now: datetime | None = None) -> list[TicketApproval]:
        """Expire every pending gate whose ``expires_at`` has passed.

        Expiry closes the gate and records it, but does not move the ticket: the
        ticket stays in ``awaiting_approval`` until an operator picks it up,
        because leaving that state is an operator-only decision.
        """

        at = now or self.now()
        expired: list[TicketApproval] = []
        for approval in await self.store.list_open_approvals(due_at=at):
            row = await self.store.expire_approval(approval.id, now=at)
            if row is None:  # pragma: no cover - decided in between
                continue
            expired.append(row)
            await self.store.append_event(
                ticket_id=row.ticket_id,
                kind="approval",
                actor="system",
                ok=False,
                summary=f"{row.kind} expired for {row.tool}",
                tool=row.tool,
                tool_class=row.tool_class,
                fields={"approval_id": row.id, "expired_at": to_iso(at)},
                now=to_iso(at),
            )
        return expired

    # -- housekeeping ------------------------------------------------------

    async def auto_close_resolved(
        self, now: datetime | None = None, *, after_secs: int | None = None
    ) -> list[Ticket]:
        """Close ``resolved`` tickets untouched for longer than the reopen window."""

        at = now or self.now()
        window = self.autoclose_secs if after_secs is None else after_secs
        if window <= 0:
            return []
        cutoff = to_iso(at - timedelta(seconds=window))
        closed: list[Ticket] = []
        for ticket in await self.store.list(
            state="resolved", updated_before=cutoff, limit=200
        ):
            updated = await self.store.set_state(
                ticket.id,
                "closed",
                actor="system",
                reason="auto-closed after the reopen window",
                now=to_iso(at),
            )
            if updated is not None:
                closed.append(updated)
        return closed

    async def sweep(
        self, now: datetime | None = None, *, autoclose_secs: int | None = None
    ) -> None:
        """One housekeeping pass: expire due gates, auto-close stale resolutions."""

        at = now or self.now()
        await self.expire_due(at)
        await self.auto_close_resolved(at, after_secs=autoclose_secs)


def _as_int(value: Any, fallback: int) -> int:
    """Coerce a settings value to a positive int, falling back on anything else."""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


async def ticket_sweep_loop(
    service: TicketService,
    settings_getter: Callable[[str], Any],
    interval_secs: int = DEFAULT_SWEEP_INTERVAL_SECS,
    initial_delay: float = 30.0,
) -> None:
    """Periodically expire overdue approvals and auto-close resolved tickets.

    Same shape as ``main._backup_loop``: an initial delay, the cadence re-read
    from the injected getter each pass so a dashboard change retimes the loop,
    and a ``try``/``except`` inside the loop so one bad pass never kills the
    task. Cancellation propagates untouched.
    """

    await asyncio.sleep(initial_delay)
    while True:
        interval = interval_secs
        try:
            interval = _as_int(settings_getter(SWEEP_INTERVAL_SETTING), interval_secs)
            autoclose = _as_int(
                settings_getter(AUTOCLOSE_SETTING), service.autoclose_secs
            )
            await service.sweep(autoclose_secs=autoclose)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("ticket sweep pass failed")
        await asyncio.sleep(interval)

"""Ticket lifecycle: the one place a ticket's state (or blocked-on reason) may change.

:class:`TicketService` is the chokepoint. **Nothing else in the codebase may
ever change a ticket's state or its blocked-on reason**:
:meth:`kenny_server.ticketstore.TicketStore.set_state` and
:meth:`~kenny_server.ticketstore.TicketStore.set_blocked` are the low-level
primitives, and :meth:`TicketService.transition`/:meth:`TicketService.block`/
:meth:`TicketService.unblock` are their only sanctioned callers. Everything a
change has to be true of — the legal successor states (:data:`_ALLOWED`), who
is allowed to drive each one (:data:`_ACTORS`), who may set or clear each
blocked-on reason (:data:`_BLOCK_SETTERS`/:data:`_UNBLOCK_CLEARERS`), and the
audit row that must accompany it — lives here and only here.

The lifecycle has two axes, deliberately kept apart:

* **state** — where the ticket is in its life: ``new``, ``in_progress``,
  ``resolved``, ``closed``, ``cancelled``.
* **blocked_on** — who the ball is with, meaningful only while
  ``state == "in_progress"``: ``""`` (nobody), ``"user"`` (the requester),
  ``"approval"`` (an operator's sign-off gate), ``"operator"`` (a human needs
  to pick the ticket up). Blocking is *not* a state transition — a blocked
  ticket is still ``in_progress`` — so it has its own chokepoint methods
  rather than living in :data:`_ALLOWED`.

This module is transport-agnostic and model-agnostic by design: it must not
import chat, tool-loop, tool-class, Discord or Anthropic code. ``tool_class``
is just a string column to it.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .ticketstore import Ticket, TicketApproval, TicketEvent, TicketStore, to_iso

__all__ = [
    "BLOCKED_REASONS",
    "DEFAULT_APPROVAL_TTL_SECS",
    "DEFAULT_AUTOCLOSE_SECS",
    "DEFAULT_STALL_GIVEUP_SECS",
    "DEFAULT_STALL_NUDGE_SECS",
    "DEFAULT_SWEEP_INTERVAL_SECS",
    "GateResumer",
    "KNOWN_CATEGORIES",
    "PRIORITIES",
    "REDACTED",
    "STATES",
    "StallNotifier",
    "TransitionNotifier",
    "ApprovalConflictError",
    "ApprovalNotFoundError",
    "BlockError",
    "TicketError",
    "TicketNotFoundError",
    "TicketService",
    "TransitionError",
    "redact_args",
    "ticket_sweep_loop",
]

logger = logging.getLogger("kenny.tickets")

# -- lifecycle -----------------------------------------------------------------

STATES: frozenset[str] = frozenset({"new", "in_progress", "resolved", "closed", "cancelled"})

# Legal successor states. ``closed`` and ``cancelled`` map to the empty set:
# they are terminal, and a ticket that reached them can only be read.
#
# ``resolved`` is a legal successor of every live state, not just the ones
# where work actually happened: it means "nothing is left to do", which is
# equally true of a ticket nobody has touched yet, one blocked on a reply, or
# one sitting on a gate nobody needs answered anymore. ``closed`` stays
# reachable only from ``resolved`` — it is always the second half of
# "resolve, then close", never a direct exit, so it keeps the property that
# every closed ticket passed through the reopen window first.
_ALLOWED: dict[str, frozenset[str]] = {
    "new": frozenset({"in_progress", "resolved", "cancelled"}),
    "in_progress": frozenset({"resolved", "cancelled"}),
    # Reopening is only possible while the ticket is still ``resolved``; the
    # sweeper closes it once the window has passed, and ``closed`` is terminal.
    "resolved": frozenset({"closed", "in_progress"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}

ROLES: frozenset[str] = frozenset({"system", "requester", "operator"})

# Who may drive each transition. ``operator`` covers operator and superuser;
# ``requester`` additionally has to *own* the ticket (see ``_check_transition``).
#
# A requester may cancel their ticket from any live state and close/reopen it
# once resolved; ``system`` may not reopen (only a human decides a resolved
# ticket needs more work). A requester may never *start* work (``new ->
# in_progress`` is system/operator only) — opening a ticket does not entitle
# its author to drive its lifecycle, only to answer it or withdraw it.
_ACTORS: dict[tuple[str, str], frozenset[str]] = {
    ("new", "in_progress"): frozenset({"system", "operator"}),
    ("new", "resolved"): frozenset({"system", "operator"}),
    ("new", "cancelled"): frozenset({"system", "requester", "operator"}),
    ("in_progress", "resolved"): frozenset({"system", "operator"}),
    ("in_progress", "cancelled"): frozenset({"system", "requester", "operator"}),
    ("resolved", "closed"): frozenset({"system", "requester", "operator"}),
    ("resolved", "in_progress"): frozenset({"requester", "operator"}),
}

# States a transition into which must first deny any gate the ticket still has
# open — see ``transition``'s docstring for why that is a denial, not a resume.
_CLOSING_STATES: frozenset[str] = frozenset({"resolved", "closed", "cancelled"})

# Actor-string prefixes to lifecycle roles. Operator and superuser accounts are
# one role here: both may drive anything an operator may.
_ROLE_PREFIXES: dict[str, str] = {
    "system": "system",
    "user": "requester",
    "requester": "requester",
    "operator": "operator",
    "superuser": "operator",
}

# -- the blocked-on axis --------------------------------------------------------

BLOCKED_REASONS: frozenset[str] = frozenset({"user", "approval", "operator"})

# Who may set a blocked-on reason. Always system or operator: nobody blocks
# their own ticket by asking a question of themselves.
_BLOCK_SETTERS: dict[str, frozenset[str]] = {
    "user": frozenset({"system", "operator"}),
    "approval": frozenset({"system", "operator"}),
    "operator": frozenset({"system", "operator"}),
}

# Who may clear a blocked-on reason. The requester may answer their own
# ``user`` block (that is the whole point of the block) but never an
# ``approval`` or ``operator`` one — those are not theirs to resolve. Once a
# ``user`` block has been escalated to ``operator`` by the stall sweep
# (:meth:`TicketService.nudge_stalled`), only a human operator may clear it —
# ``system`` deliberately cannot un-escalate what it just escalated.
_UNBLOCK_CLEARERS: dict[str, frozenset[str]] = {
    "user": frozenset({"system", "operator", "requester"}),
    "approval": frozenset({"system", "operator"}),
    "operator": frozenset({"operator"}),
}

# Closed vocabulary: rejected outright if a caller sends anything else.
PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")

# Advertised, not enforced — the same discipline ``ticket_rules.KNOWN_SECTIONS``
# uses (a caller may set any category; this only drives the UI's dropdown and a
# soft warning on the API for anything unlisted). A closed list would block a
# legitimate ad-hoc category, and unlike priority nothing downstream branches
# on this value.
# ``web_filter`` is the parental-controls bypass request: a child asks for a
# site the filter blocks. It is a ticket like any other — the existing approval
# gate is the decision, and acting on an approved one is the operator's ordinary
# ``webfilter_set(add_domain, action="allow")`` + ``webfilter_push``. There is
# deliberately no second pending-request table with its own lifecycle.
KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {
        "alert",
        "account",
        "network",
        "software",
        "hardware",
        "performance",
        "web_filter",
        "other",
    }
)

# Trail kinds callers may append. ``state``, ``handoff``, ``block`` and
# ``assign`` are written by transition()/reassign()/block()/unblock()/assign()
# themselves and are refused here, so the trail cannot claim a change that
# never happened.
EVENT_KINDS: frozenset[str] = frozenset(
    {"note", "tool_call", "approval", "consent", "message", "error"}
)

APPROVAL_KINDS: frozenset[str] = frozenset({"operator_approval", "user_consent"})

#: Called with a gate this module just closed on its own (expiry). Whoever
#: drives the assistant for a ticket registers one; this module only knows that
#: something has to be told, never what a model or a chat platform is.
GateResumer = Callable[[TicketApproval], Awaitable[None]]

#: Called with a ticket this module just created, to investigate it before a
#: person is asked to. Registered by whoever can actually drive an assistant
#: turn (``main.py`` wires ``triage.TriageService.run``); this module only knows
#: that a new ticket is worth looking into, never how looking is done.
TriageRunner = Callable[["Ticket"], Awaitable[None]]

#: Called with ``(ticket, to_state)`` right after a transition (or an
#: auto-close) commits. Same shape of seam as :data:`GateResumer`: this module
#: stays transport- and model-blind (see the module docstring), so it only
#: knows that *someone* wants to hear about a state change, never who Discord
#: or a dashboard surface is. Registered by whoever drives the ticket
#: assistant — see ``ticket_assistant.TicketAssistant``.
TransitionNotifier = Callable[[Ticket, str], Awaitable[None]]

#: Called when the stall sweep sends a reminder for a ticket blocked on
#: ``"user"`` or ``"operator"`` for longer than the nudge window. Same shape
#: and rationale as :data:`GateResumer` — this module knows something has to
#: be told, never what a chat platform is.
StallNotifier = Callable[[Ticket, str], Awaitable[None]]

DEFAULT_APPROVAL_TTL_SECS = 3600
DEFAULT_AUTOCLOSE_SECS = 3 * 24 * 3600
DEFAULT_SWEEP_INTERVAL_SECS = 300
DEFAULT_STALL_NUDGE_SECS = 2 * 24 * 3600
DEFAULT_STALL_GIVEUP_SECS = 7 * 24 * 3600

# Settings keys the sweeper re-reads each pass through the injected getter. An
# unknown key yields None from the getter, which falls back to the defaults
# above — this module never reads the environment or the settings catalog.
SWEEP_INTERVAL_SETTING = "KENNY_TICKET_SWEEP_INTERVAL_SECS"
AUTOCLOSE_SETTING = "KENNY_TICKET_AUTOCLOSE_SECS"
STALL_NUDGE_SETTING = "KENNY_TICKET_STALL_NUDGE_SECS"
STALL_GIVEUP_SETTING = "KENNY_TICKET_STALL_GIVEUP_SECS"

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


class ApprovalForbiddenError(TicketError):
    """The actor may not decide this gate the way they asked to.

    Raised when anyone below operator tries to *approve*. Denial is deliberately
    open to every actor — the sweeper denies on expiry.
    """

    status_code = 403

    def __init__(self, message: str, *, actor: str, role: str) -> None:
        super().__init__(message)
        self.actor = actor
        self.role = role


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


class BlockError(TicketError):
    """A block/unblock was refused.

    ``code`` is ``illegal_block`` (409 — the ticket is not ``in_progress``, or
    there is nothing to unblock), ``unknown_reason`` (400) or
    ``forbidden_actor`` (403). Deliberately not a :class:`TransitionError`: a
    block changes ``blocked_on``, not ``state``, and conflating the two would
    let a caller reason about a block as if it were a state.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        ticket_id: str,
        blocked_on: str,
        actor: str,
        role: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.ticket_id = ticket_id
        self.blocked_on = blocked_on
        self.actor = actor
        self.role = role
        self.status_code = {
            "forbidden_actor": 403,
            "illegal_block": 409,
            "unknown_reason": 400,
        }.get(code, 400)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "detail": str(self),
            "ticket_id": self.ticket_id,
            "blocked_on": self.blocked_on,
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

    Holds no transport and no model: creating, transitioning, blocking and
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
        stall_nudge_secs: int = DEFAULT_STALL_NUDGE_SECS,
        stall_giveup_secs: int = DEFAULT_STALL_GIVEUP_SECS,
    ) -> None:
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.approval_ttl_secs = approval_ttl_secs
        self.autoclose_secs = autoclose_secs
        self.stall_nudge_secs = stall_nudge_secs
        self.stall_giveup_secs = stall_giveup_secs
        self._gate_resumer: GateResumer | None = None
        self._transition_notifier: TransitionNotifier | None = None
        self._stall_notifier: StallNotifier | None = None
        self._triage: TriageRunner | None = None
        self._triage_tasks: set[asyncio.Task[None]] = set()

    def set_triage(self, runner: "TriageRunner | None") -> None:
        """Register who investigates a newly created ticket (see :data:`TriageRunner`).

        Unset — the default, and what every test that does not ask for it gets —
        means tickets are created exactly as they were before triage existed.
        """

        self._triage = runner

    def _schedule_triage(self, ticket: Ticket) -> None:
        """Start the investigation without making the creator wait for it.

        An investigation talks to a model and to a PC; it takes seconds to tens
        of seconds. :meth:`create` is called from an alert loop and from a
        request handler, and neither may block on that — a slow or wedged
        triage must not delay, let alone fail, the creation of the ticket it is
        about.

        The task reference is held (``_triage_tasks``) because a bare
        ``create_task`` may be garbage-collected mid-flight, and discarded on
        completion so the set cannot grow without bound. Exceptions are the
        runner's own to handle — ``TriageService.run`` never raises — and the
        done-callback re-reads the result only to keep the loop from logging an
        unretrieved exception if some other runner ever does.
        """

        if self._triage is None:
            return
        task = asyncio.create_task(self._triage(ticket))
        self._triage_tasks.add(task)

        def _done(finished: asyncio.Task[None]) -> None:
            self._triage_tasks.discard(finished)
            if not finished.cancelled():
                exc = finished.exception()
                if exc is not None:
                    logger.exception("triage task for ticket %s failed", ticket.id, exc_info=exc)

        task.add_done_callback(_done)

    def set_gate_resumer(self, resumer: GateResumer | None) -> None:
        """Register who answers a gate this service closes by itself.

        Only :meth:`expire_due` uses it: every other decision arrives from a
        surface, which resumes its own ticket. An expiry has no such caller, so
        without a resumer the ticket would stay blocked on ``approval`` — a
        block only ``system``/``operator`` may clear — for good.
        """

        self._gate_resumer = resumer

    def set_transition_notifier(self, fn: TransitionNotifier | None) -> None:
        """Register who gets told about a state change, once it has committed.

        Called from :meth:`transition` and from :meth:`auto_close_resolved`
        (the sweeper's own ``resolved -> closed`` move) — the two places a
        ticket's state actually changes outside a gate resume. See
        :data:`TransitionNotifier`.
        """

        self._transition_notifier = fn

    async def _notify_transition(self, ticket: Ticket, to_state: str) -> None:
        """Best-effort fan-out to the registered notifier, mirroring ``expire_due``'s
        handling of :data:`GateResumer`: never let a notification failure look
        like the transition itself failed, since it already committed."""

        if self._transition_notifier is None:
            return
        try:
            await self._transition_notifier(ticket, to_state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the transition already committed
            logger.exception(
                "ticket %s: notifying the transition to %s failed", ticket.id, to_state
            )

    def set_stall_notifier(self, notifier: StallNotifier | None) -> None:
        """Register who is told when :meth:`nudge_stalled` sends a reminder."""

        self._stall_notifier = notifier

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
        dedup_key: str = "",
    ) -> Ticket:
        """Mint a ticket in state ``new`` and record its genesis event.

        ``agent_id`` is frozen here: it is the routing target every later tool
        call is checked against, and only :meth:`reassign` may change it.
        ``role_snapshot``/``profile_snapshot`` freeze the requester's
        authorization at creation time so a later account change cannot
        retroactively widen what an in-flight ticket was allowed to do.

        ``dedup_key`` names *what this ticket is about* so a caller can ask
        :meth:`~kenny_server.ticketstore.TicketStore.find_open_by_dedup_key`
        whether one is already open for the same thing before minting another.
        This method does not deduplicate on its own — a caller that wants that
        must look first, because "already open, so say nothing" and "already
        open, so note the recurrence there" are the caller's decision, not this
        one's. Empty (the default) opts out entirely.
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
            dedup_key=dedup_key,
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
        # Last, and only once the ticket is durably on the record: the
        # investigation writes into this ticket, so it cannot start before the
        # ticket exists, and it must not be able to prevent it from existing.
        self._schedule_triage(ticket)
        return ticket

    async def get(self, ticket_id: str) -> Ticket:
        """Return a ticket or raise :class:`TicketNotFoundError`."""

        ticket = await self.store.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

    # -- the chokepoint (state) ---------------------------------------------

    async def transition(
        self,
        ticket_id: str,
        to_state: str,
        *,
        actor: str,
        reason: str = "",
        resolved_by: str = "",
    ) -> Ticket:
        """Move a ticket to ``to_state`` on behalf of ``actor``.

        The only sanctioned caller of ``TicketStore.set_state``. Rejects an
        illegal transition (409) and an unauthorized actor (403) with a
        :class:`TransitionError`, and records a ``kind='state'`` event in the
        same transaction as the change. Leaving ``in_progress`` for anywhere
        else clears ``blocked_on`` at the store layer — "resolved but still
        blocked" is not representable.

        There is deliberately **no** ``agent_id`` parameter: retargeting a
        ticket at another host is a separate, operator-only :meth:`reassign`.
        The frozen routing target is a security control, so it must not be
        changeable as a side effect of a routine state change.

        Leaving the ticket with its one open gate still ``pending`` — now
        possible straight from a ``blocked_on="approval"`` ticket to
        ``resolved`` or ``cancelled`` — denies that gate first, via
        :meth:`decide_approval`, before the state itself moves. That
        deliberately does **not** run the registered :data:`GateResumer`: the
        resumer's job is to let a denial or expiry push the ticket back to
        unblocked ``in_progress`` so the assistant can keep going, which is
        exactly what this transition is ending. A held call must not execute,
        and a settled ticket must not be nudged back to ``in_progress``, just
        because someone later decides the gate.

        ``resolved_by`` names a non-human mover of the ticket (today only
        ``"triage"``) and is stamped on the ticket itself, not just the trail.
        It defaults to empty and is rewritten on every transition, so it always
        describes how the ticket reached the state it is in now — see
        :meth:`~kenny_server.ticketstore.TicketStore.set_state`.
        """

        ticket = await self.get(ticket_id)
        self._check_transition(ticket, to_state, actor)
        if to_state in _CLOSING_STATES:
            open_gate = await self.store.get_open_approval(ticket_id)
            if open_gate is not None:
                await self.decide_approval(
                    open_gate.id,
                    approve=False,
                    actor="system",
                    decided_via=f"ticket moved to {to_state}",
                )
        updated = await self.store.set_state(
            ticket_id,
            to_state,
            actor=actor,
            reason=reason,
            resolved_by=resolved_by,
            now=to_iso(self.now()),
        )
        if updated is None:  # pragma: no cover - existence checked above
            raise TicketNotFoundError(ticket_id)
        await self._notify_transition(updated, to_state)
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
        # A ticket whose own gate is waiting on an operator's sign-off cannot
        # be cancelled out from under that gate by the system — only a human
        # (the requester withdrawing, or an operator) may do that. Denying the
        # gate is a decision only ``decide_approval`` makes; ``system`` ending
        # the ticket first would let a held call's fate be decided as a side
        # effect of a lifecycle move instead of its own explicit denial.
        if to_state == "cancelled" and role == "system" and ticket.blocked_on == "approval":
            raise TransitionError(
                f"{actor} may not cancel a ticket awaiting approval",
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

    async def assign(
        self, ticket_id: str, assignee_user_id: int | None, *, actor: str
    ) -> Ticket:
        """Set (or clear) which operator owns working this ticket. Operator-only.

        This is the "claim" action: an operator assigning the ticket to
        themselves. Distinct from :meth:`reassign`, which retargets the *host*
        a ticket is about — this assigns the *person* working it. Writes a
        ``kind='assign'`` event in the same transaction as the column change.
        """

        ticket = await self.get(ticket_id)
        role, _ = parse_actor(actor)
        if role != "operator":
            raise TransitionError(
                f"{actor} may not assign a ticket",
                code="forbidden_actor",
                ticket_id=ticket_id,
                from_state=ticket.state,
                to_state=ticket.state,
                actor=actor,
                role=role or None,
            )
        updated = await self.store.set_assignee(
            ticket_id,
            assignee_user_id,
            actor=actor,
            reason=(
                f"assigned to {assignee_user_id}" if assignee_user_id is not None else "unassigned"
            ),
            now=to_iso(self.now()),
        )
        if updated is None:  # pragma: no cover - existence checked above
            raise TicketNotFoundError(ticket_id)
        return updated

    # -- the chokepoint (blocked-on) -----------------------------------------

    async def block(
        self, ticket_id: str, blocked_on: str, *, actor: str, reason: str = "", ref: str = ""
    ) -> Ticket:
        """Mark the ticket blocked on ``blocked_on`` (see :data:`BLOCKED_REASONS`).

        Only legal while the ticket is ``in_progress`` — blocking is a
        sub-state of "being worked", not a lifecycle move of its own. Calling
        this on an already-blocked ticket re-blocks it (resets
        ``blocked_since`` and clears any prior nudge stamp): this is how
        :meth:`nudge_stalled` escalates a stale ``user`` block to ``operator``.
        ``ref`` is an opaque pointer to what is being waited on (an approval
        id for ``"approval"``) — display-only, never interpreted here.
        """

        ticket = await self.get(ticket_id)
        self._check_block(ticket, blocked_on, actor)
        updated = await self.store.set_blocked(
            ticket_id, blocked_on, actor=actor, ref=ref, reason=reason, now=to_iso(self.now())
        )
        if updated is None:  # pragma: no cover - existence checked above
            raise TicketNotFoundError(ticket_id)
        return updated

    async def unblock(self, ticket_id: str, *, actor: str, reason: str = "") -> Ticket:
        """Clear whatever the ticket is blocked on. See :meth:`block`."""

        ticket = await self.get(ticket_id)
        self._check_unblock(ticket, actor)
        updated = await self.store.set_blocked(
            ticket_id, "", actor=actor, ref="", reason=reason, now=to_iso(self.now())
        )
        if updated is None:  # pragma: no cover - existence checked above
            raise TicketNotFoundError(ticket_id)
        return updated

    def can_block(self, ticket: Ticket, blocked_on: str, actor: str) -> bool:
        """True if :meth:`block` would be accepted (for UI affordances)."""

        try:
            self._check_block(ticket, blocked_on, actor)
        except BlockError:
            return False
        return True

    def can_unblock(self, ticket: Ticket, actor: str) -> bool:
        """True if :meth:`unblock` would be accepted (for UI affordances)."""

        try:
            self._check_unblock(ticket, actor)
        except BlockError:
            return False
        return True

    def _check_block(self, ticket: Ticket, blocked_on: str, actor: str) -> None:
        if ticket.state != "in_progress":
            raise BlockError(
                f"cannot block a ticket in state {ticket.state!r}",
                code="illegal_block",
                ticket_id=ticket.id,
                blocked_on=blocked_on,
                actor=actor,
            )
        if blocked_on not in BLOCKED_REASONS:
            raise BlockError(
                f"unknown block reason {blocked_on!r}",
                code="unknown_reason",
                ticket_id=ticket.id,
                blocked_on=blocked_on,
                actor=actor,
            )
        role, _ = parse_actor(actor)
        if role not in _BLOCK_SETTERS.get(blocked_on, frozenset()):
            raise BlockError(
                f"{actor} may not block a ticket on {blocked_on!r}",
                code="forbidden_actor",
                ticket_id=ticket.id,
                blocked_on=blocked_on,
                actor=actor,
                role=role or None,
            )

    def _check_unblock(self, ticket: Ticket, actor: str) -> None:
        if ticket.state != "in_progress" or not ticket.blocked_on:
            raise BlockError(
                "ticket is not blocked",
                code="illegal_block",
                ticket_id=ticket.id,
                blocked_on=ticket.blocked_on,
                actor=actor,
            )
        role, user_id = parse_actor(actor)
        if role not in _UNBLOCK_CLEARERS.get(ticket.blocked_on, frozenset()):
            raise BlockError(
                f"{actor} may not clear a {ticket.blocked_on!r} block",
                code="forbidden_actor",
                ticket_id=ticket.id,
                blocked_on=ticket.blocked_on,
                actor=actor,
                role=role or None,
            )
        if role == "requester" and (
            ticket.requester_user_id is None or ticket.requester_user_id != user_id
        ):
            raise BlockError(
                f"{actor} does not own ticket {ticket.id}",
                code="forbidden_actor",
                ticket_id=ticket.id,
                blocked_on=ticket.blocked_on,
                actor=actor,
                role=role,
            )

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
        """Patch a ticket's editable fields. Never touches ``state``/``blocked_on``."""

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

        ``kind`` is one of :data:`EVENT_KINDS`; ``state``, ``handoff``,
        ``block`` and ``assign`` rows belong to their own chokepoint methods
        and are refused here. ``args`` (a ``tool_call``'s arguments) is
        redacted by key name before it is persisted — see :func:`redact_args`.
        """

        if kind not in EVENT_KINDS:
            raise ValueError(
                f"kind {kind!r} must be one of {sorted(EVENT_KINDS)}; "
                "state/handoff/block/assign events are written by their own methods"
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

        **Approving is enforced here**, because a held call runs only by virtue
        of its row reading ``approved`` — this method, not the lifecycle
        transition, is the boundary. Who may approve depends on what the gate is
        for, and the two answers are deliberately different:

        * ``operator_approval`` asks whether the fleet should be changed, so only
          an operator may grant it. The requester must never release their own.
        * ``user_consent`` asks whether someone's screen, files or browsing may
          be looked at. Only the person it belongs to can answer that — an
          operator granting it for them would defeat the point of the gate.

        Denying stays open to every actor: expiry is a denial and the sweeper
        has to issue it, and a requester may always withdraw.

        Deciding does not itself move the ticket or clear its block; resuming
        (unblocking) is a separate call.
        """

        # One derivation, used for both the guard and the trail entry: a caller
        # that names a deciding user is acting as that operator, and a caller
        # that names nobody is the system.
        deciding_actor = actor or (
            f"operator:{decided_by}" if decided_by is not None else "system"
        )
        role, actor_user_id = parse_actor(deciding_actor)

        existing = await self.store.get_approval(approval_id)
        if existing is None:
            raise ApprovalNotFoundError(approval_id)

        if approve:
            if existing.kind == "user_consent":
                ticket = await self.get(existing.ticket_id)
                owner = ticket.requester_user_id
                if owner is None or role != "requester" or actor_user_id != owner:
                    raise ApprovalForbiddenError(
                        "consent can only be granted by the person it concerns",
                        actor=deciding_actor,
                        role=role,
                    )
            elif role != "operator":
                raise ApprovalForbiddenError(
                    "approving a held call requires an operator",
                    actor=deciding_actor,
                    role=role,
                )
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
            # The trail names what was actually answered: a privacy consent reads
            # as consent, not as an approval it never was.
            kind="consent" if existing.kind == "user_consent" else "approval",
            actor=deciding_actor,
            ok=approve,
            summary=f"{existing.kind} {status} for {existing.tool}",
            tool=existing.tool,
            tool_class=existing.tool_class,
            fields={"approval_id": approval_id, "decided_via": decided_via},
        )
        return decided

    async def expire_due(self, now: datetime | None = None) -> list[TicketApproval]:
        """Expire every pending gate whose ``expires_at`` has passed.

        An expired gate counts as a denial, and a denial has to reach the
        assistant: the held call needs its refusal ``tool_result`` and the
        ticket needs to leave its ``blocked_on="approval"`` state. This method
        closes and records the row and then hands it to the registered
        :data:`GateResumer` (see :meth:`set_gate_resumer`), which is the only
        part of this that knows what a model is. A resumer that fails is logged
        — the expiry itself is already durable and must not be rolled back.
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
            if self._gate_resumer is not None:
                try:
                    await self._gate_resumer(row)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one stuck ticket must not stop the sweep
                    logger.exception(
                        "ticket %s: resuming the expired gate %s failed",
                        row.ticket_id,
                        row.id,
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
            updated = await self.transition(
                ticket.id, "closed", actor="system", reason="auto-closed after the reopen window"
            )
            closed.append(updated)
        return closed

    async def nudge_stalled(
        self,
        now: datetime | None = None,
        *,
        nudge_secs: int | None = None,
        giveup_secs: int | None = None,
    ) -> list[Ticket]:
        """Remind about, and eventually escalate, tickets stuck on a block.

        Two independent passes, both scoped to ``blocked_on in ("user",
        "operator")`` — ``"approval"`` already has the gate TTL
        (:meth:`expire_due`) and must not be nudged again by a second clock:

        * **Nudge** (default :data:`DEFAULT_STALL_NUDGE_SECS`): every ticket
          blocked longer than the window and not yet nudged gets one reminder
          via the registered :data:`StallNotifier`, and is marked nudged so it
          is not reminded again next pass. A window of ``0`` disables this pass.
        * **Give up** (default :data:`DEFAULT_STALL_GIVEUP_SECS`, always
          longer than the nudge window): a ``user`` block still unanswered
          after the longer window is re-blocked as ``operator`` — a human
          needs to pick it up, since the person it was waiting on has not
          answered. This never applies to ``operator`` blocks: there is
          nowhere further to escalate an operator's own queue. A window of
          ``0`` disables this pass.

        Neither pass resolves or cancels a ticket on its own — only reminds
        and re-routes who is being waited on.
        """

        at = now or self.now()
        nudge_window = self.stall_nudge_secs if nudge_secs is None else nudge_secs
        giveup_window = self.stall_giveup_secs if giveup_secs is None else giveup_secs
        touched: list[Ticket] = []

        if nudge_window > 0:
            nudge_cutoff = to_iso(at - timedelta(seconds=nudge_window))
            for ticket in await self.store.list(
                blocked_on_in=("user", "operator"),
                blocked_before=nudge_cutoff,
                nudged=False,
                limit=200,
            ):
                updated = await self.store.mark_nudged(ticket.id, now=to_iso(at))
                if updated is None:  # pragma: no cover - deleted mid-sweep
                    continue
                touched.append(updated)
                if self._stall_notifier is not None:
                    try:
                        await self._stall_notifier(updated, updated.blocked_on)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - one stuck ticket must not stop the sweep
                        logger.exception(
                            "ticket %s: stall notifier failed", updated.id
                        )

        if giveup_window > 0:
            giveup_cutoff = to_iso(at - timedelta(seconds=giveup_window))
            for ticket in await self.store.list(
                blocked_on="user", blocked_before=giveup_cutoff, limit=200
            ):
                updated = await self.block(
                    ticket.id,
                    "operator",
                    actor="system",
                    reason=f"escalated: no reply from the requester in over {giveup_window}s",
                    ref=ticket.blocked_ref,
                )
                touched.append(updated)

        return touched

    async def sweep(
        self,
        now: datetime | None = None,
        *,
        autoclose_secs: int | None = None,
        stall_nudge_secs: int | None = None,
        stall_giveup_secs: int | None = None,
    ) -> None:
        """One housekeeping pass: expire due gates, nudge/escalate stalls, auto-close."""

        at = now or self.now()
        await self.expire_due(at)
        await self.nudge_stalled(at, nudge_secs=stall_nudge_secs, giveup_secs=stall_giveup_secs)
        await self.auto_close_resolved(at, after_secs=autoclose_secs)


def _as_int(value: Any, fallback: int) -> int:
    """Coerce a settings value to a non-negative int, falling back on anything else.

    Unlike the interval settings this backs, ``0`` is a meaningful value here
    (the stall passes' "disabled" sentinel), so this deliberately does not
    reject it the way an interval's "must be positive" coercion would.
    """

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def _as_positive_int(value: Any, fallback: int) -> int:
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
    """Periodically expire overdue approvals, nudge/escalate stalls, auto-close.

    Same shape as ``main._backup_loop``: an initial delay, the cadence re-read
    from the injected getter each pass so a dashboard change retimes the loop,
    and a ``try``/``except`` inside the loop so one bad pass never kills the
    task. Cancellation propagates untouched.
    """

    await asyncio.sleep(initial_delay)
    while True:
        interval = interval_secs
        try:
            interval = _as_positive_int(settings_getter(SWEEP_INTERVAL_SETTING), interval_secs)
            autoclose = _as_positive_int(
                settings_getter(AUTOCLOSE_SETTING), service.autoclose_secs
            )
            stall_nudge = _as_int(settings_getter(STALL_NUDGE_SETTING), service.stall_nudge_secs)
            stall_giveup = _as_int(
                settings_getter(STALL_GIVEUP_SETTING), service.stall_giveup_secs
            )
            await service.sweep(
                autoclose_secs=autoclose,
                stall_nudge_secs=stall_nudge,
                stall_giveup_secs=stall_giveup,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("ticket sweep pass failed")
        await asyncio.sleep(interval)

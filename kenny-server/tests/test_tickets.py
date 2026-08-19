"""``TicketService`` lifecycle, blocked-on axis, authorization, redaction and
sweeper tests."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.ticketstore import TicketStore, to_iso
from kenny_server.tickets import (
    _ACTORS,
    _ALLOWED,
    _BLOCK_SETTERS,
    _UNBLOCK_CLEARERS,
    BLOCKED_REASONS,
    STATES,
    ApprovalConflictError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
    BlockError,
    TicketNotFoundError,
    TicketService,
    TransitionError,
    parse_actor,
    redact_args,
    ticket_sweep_loop,
)

START = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

REQUESTER_ID = 12
_ACTOR_STRINGS = {
    "system": "system",
    "requester": f"user:{REQUESTER_ID}",
    "operator": "operator:3",
}


class Clock:
    """An injectable clock: no test ever has to sleep."""

    def __init__(self, start: datetime = START) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


@pytest.fixture
async def service(tmp_path):
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    svc = TicketService(store, now=Clock())
    try:
        yield svc
    finally:
        await store.close()


async def _new_ticket(svc: TicketService, **kwargs):
    params = {
        "title": "Printer offline",
        "origin": "discord",
        "requester_user_id": REQUESTER_ID,
        "agent_id": "pc-lena",
    }
    params.update(kwargs)
    return await svc.create(**params)


async def _ticket_in(svc: TicketService, state: str, *, blocked_on: str = ""):
    """A ticket forced into ``state`` (and, optionally, ``blocked_on``) through
    the store (test scaffolding only) -- bypassing ``TicketService``'s own
    legality checks the way the real chokepoint methods never would.
    """

    ticket = await _new_ticket(svc)
    if state != "new":
        forced = await svc.store.set_state(
            ticket.id, state, actor="system", reason="test setup", now=to_iso(svc.now())
        )
        assert forced is not None
        ticket = forced
    if blocked_on:
        forced = await svc.store.set_blocked(
            ticket.id, blocked_on, actor="system", ref="test-ref", now=to_iso(svc.now())
        )
        assert forced is not None
        ticket = forced
    return ticket


# -- creation ------------------------------------------------------------------


async def test_create_mints_new_and_records_genesis(service: TicketService) -> None:
    ticket = await _new_ticket(
        service, role_snapshot="user", profile_snapshot="family", priority="high"
    )
    assert ticket.state == "new"
    assert ticket.blocked_on == ""
    assert ticket.assignee_user_id is None
    assert ticket.number == 1
    assert ticket.agent_id == "pc-lena"
    assert ticket.role_snapshot == "user"
    assert ticket.profile_snapshot == "family"

    (event,) = await service.events(ticket.id)
    assert event.kind == "state"
    assert event.from_state is None
    assert event.to_state == "new"
    assert event.actor == "system"


async def test_get_unknown_ticket_raises_404(service: TicketService) -> None:
    with pytest.raises(TicketNotFoundError) as exc:
        await service.get("nope")
    assert exc.value.status_code == 404


# -- the transition table ------------------------------------------------------


def test_transition_table_covers_every_state() -> None:
    assert set(_ALLOWED) == STATES
    for from_state, targets in _ALLOWED.items():
        assert targets <= STATES
        for to_state in targets:
            # Every legal edge has an explicit actor rule -- no implicit "anyone".
            assert (from_state, to_state) in _ACTORS
            assert _ACTORS[(from_state, to_state)] <= {"system", "requester", "operator"}
    # No stray actor rule for an edge that is not legal.
    for from_state, to_state in _ACTORS:
        assert to_state in _ALLOWED[from_state]


def test_block_table_covers_every_reason() -> None:
    assert set(_BLOCK_SETTERS) == BLOCKED_REASONS
    assert set(_UNBLOCK_CLEARERS) == BLOCKED_REASONS
    for setters in _BLOCK_SETTERS.values():
        assert setters <= {"system", "requester", "operator"}
        # Nobody blocks their own ticket by asking a question of themselves.
        assert "requester" not in setters
    for clearers in _UNBLOCK_CLEARERS.values():
        assert clearers <= {"system", "requester", "operator"}


@pytest.mark.parametrize(
    ("from_state", "to_state", "role"),
    [
        (f, t, role)
        for f, targets in sorted(_ALLOWED.items())
        for t in sorted(targets)
        for role in sorted(_ACTORS[(f, t)])
    ],
)
async def test_every_legal_transition_succeeds(
    service: TicketService, from_state: str, to_state: str, role: str
) -> None:
    ticket = await _ticket_in(service, from_state)
    moved = await service.transition(
        ticket.id, to_state, actor=_ACTOR_STRINGS[role], reason="because"
    )
    assert moved.state == to_state

    (event,) = [e for e in await service.events(ticket.id) if e.to_state == to_state]
    assert event.kind == "state"
    assert event.from_state == from_state
    assert event.actor == _ACTOR_STRINGS[role]
    assert event.summary == "because"


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        ("new", "closed"),
        ("in_progress", "new"),
        ("in_progress", "in_progress"),
        ("resolved", "cancelled"),
        ("resolved", "new"),
        ("closed", "new"),
    ],
)
async def test_illegal_transitions_are_rejected(
    service: TicketService, from_state: str, to_state: str
) -> None:
    ticket = await _ticket_in(service, from_state)
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, to_state, actor="operator:3")
    assert exc.value.code == "illegal_transition"
    assert exc.value.status_code == 409
    assert exc.value.from_state == from_state
    assert exc.value.to_state == to_state
    assert (await service.get(ticket.id)).state == from_state


@pytest.mark.parametrize("terminal", ["closed", "cancelled"])
@pytest.mark.parametrize("target", sorted(STATES))
async def test_terminal_states_go_nowhere(
    service: TicketService, terminal: str, target: str
) -> None:
    ticket = await _ticket_in(service, terminal)
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, target, actor="operator:3")
    assert exc.value.code == "illegal_transition"
    assert "terminal" in str(exc.value)


async def test_unknown_state_is_rejected(service: TicketService) -> None:
    ticket = await _ticket_in(service, "new")
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, "escalated", actor="operator:3")
    assert exc.value.code == "unknown_state"
    assert exc.value.status_code == 400


async def test_retired_states_are_rejected(service: TicketService) -> None:
    """``triage``/``awaiting_*`` are folded into ``in_progress`` + blocked_on.

    A caller that still sends the old name (a stale client, a bad migration)
    must get the same ``unknown_state`` a made-up string would -- not be
    silently accepted as something that no longer means anything.
    """

    ticket = await _ticket_in(service, "new")
    for retired in ("triage", "awaiting_user", "awaiting_approval", "awaiting_agent"):
        with pytest.raises(TransitionError) as exc:
            await service.transition(ticket.id, retired, actor="operator:3")
        assert exc.value.code == "unknown_state"


async def test_transition_on_unknown_ticket_raises(service: TicketService) -> None:
    with pytest.raises(TicketNotFoundError):
        await service.transition("nope", "in_progress", actor="system")


# -- the actor table -----------------------------------------------------------


def test_parse_actor_maps_prefixes_to_roles() -> None:
    assert parse_actor("system") == ("system", None)
    assert parse_actor("user:12") == ("requester", 12)
    assert parse_actor("operator:3") == ("operator", 3)
    # A superuser drives everything an operator drives.
    assert parse_actor("superuser:1") == ("operator", 1)
    # Anything unrecognized gets a role that is in no rule.
    assert parse_actor("bot:9")[0] == ""
    assert parse_actor("")[0] == ""


async def test_only_an_operator_may_approve_a_gate(service: TicketService) -> None:
    """Approving is the boundary — not clearing the ``approval`` block.

    The system has to be able to unblock the ticket, or an expired gate would
    park it forever. What it must never do is mark the held call approved.
    """

    ticket = await _ticket_in(service, "in_progress", blocked_on="approval")
    approval = await service.open_approval(
        ticket.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
    )

    for actor in ("system", f"user:{REQUESTER_ID}"):
        with pytest.raises(ApprovalForbiddenError) as exc:
            await service.decide_approval(approval.id, approve=True, actor=actor)
        assert exc.value.status_code == 403
    assert (await service.store.get_approval(approval.id)).status == "pending"

    # Denial is open to everyone: the sweeper denies on expiry.
    denied = await service.decide_approval(approval.id, approve=False, actor="system")
    assert denied.status == "denied"

    # ...and the system may then unblock the ticket to report the refusal.
    moved = await service.unblock(ticket.id, actor="system")
    assert moved.blocked_on == ""
    assert moved.state == "in_progress"


async def test_only_the_affected_person_may_grant_consent(service: TicketService) -> None:
    """A consent gate answers a privacy question, so it is not the operator's to grant.

    The mirror image of the approval rule: an operator deciding whether someone's
    screen may be captured would defeat the gate's entire purpose.
    """

    ticket = await _ticket_in(service, "in_progress", blocked_on="user")
    consent = await service.open_approval(
        ticket.id,
        tool_use_id="tu2",
        tool="screen_capture",
        tool_class="read_only",
        args={},
        kind="user_consent",
    )

    for actor in ("operator:7", "system", "user:999"):
        with pytest.raises(ApprovalForbiddenError) as exc:
            await service.decide_approval(consent.id, approve=True, actor=actor)
        assert exc.value.status_code == 403
    assert (await service.store.get_approval(consent.id)).status == "pending"

    granted = await service.decide_approval(
        consent.id, approve=True, actor=f"user:{REQUESTER_ID}", decided_by=REQUESTER_ID
    )
    assert granted.status == "approved"

    # The trail calls it what it was, not an approval.
    trail = await service.events(ticket.id)
    assert trail[-1].kind == "consent"
    assert trail[-1].actor == f"user:{REQUESTER_ID}"


async def test_requester_may_not_touch_another_persons_ticket(
    service: TicketService,
) -> None:
    ticket = await _ticket_in(service, "in_progress")
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, "cancelled", actor="user:99")
    assert exc.value.code == "forbidden_actor"
    assert exc.value.status_code == 403

    # An alert-origin ticket has no requester at all: nobody owns it.
    orphan = await service.create(title="disk full", origin="alert")
    await service.store.set_state(orphan.id, "in_progress", actor="system")
    with pytest.raises(TransitionError):
        await service.transition(orphan.id, "cancelled", actor=f"user:{REQUESTER_ID}")


async def test_unknown_actor_prefix_is_never_authorized(service: TicketService) -> None:
    ticket = await _ticket_in(service, "new")
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, "in_progress", actor="discord-bot:1")
    assert exc.value.code == "forbidden_actor"


async def test_can_transition_mirrors_transition(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress", blocked_on="approval")
    assert service.can_transition(ticket, "resolved", "operator:3") is True
    # Resolving is not cancelling: a requester may withdraw their own ticket
    # from here, but only an operator (or the system) may call it resolved.
    assert service.can_transition(ticket, "resolved", f"user:{REQUESTER_ID}") is False
    assert service.can_transition(ticket, "cancelled", f"user:{REQUESTER_ID}") is True
    # ...but not the system, while the ticket's own gate is still open.
    assert service.can_transition(ticket, "cancelled", "system") is False


async def test_system_may_not_cancel_a_ticket_awaiting_approval(
    service: TicketService,
) -> None:
    """The one hand-authored guard outside the transition table.

    Cancelling out from under an open approval gate is a human decision (the
    requester withdrawing, or an operator) -- never something ``system`` does
    as a side effect of a lifecycle move. A ``user``/``operator`` block does
    not carry the same restriction: only ``approval`` does, because only it
    has a human sign-off already in flight.
    """

    ticket = await _ticket_in(service, "in_progress", blocked_on="approval")
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, "cancelled", actor="system")
    assert exc.value.code == "forbidden_actor"
    assert (await service.get(ticket.id)).state == "in_progress"

    # A requester or operator may still cancel it -- and doing so denies the
    # open gate, covered by test_leaving_a_blocked_ticket_denies_the_open_gate.
    moved = await service.transition(ticket.id, "cancelled", actor="operator:3")
    assert moved.state == "cancelled"

    for blocked_on in ("user", "operator"):
        other = await _ticket_in(service, "in_progress", blocked_on=blocked_on)
        moved = await service.transition(other.id, "cancelled", actor="system")
        assert moved.state == "cancelled"


# -- resolving from any live state ----------------------------------------------


@pytest.mark.parametrize("from_state", ["new", "in_progress"])
async def test_resolve_is_legal_from_every_live_state(
    service: TicketService, from_state: str
) -> None:
    ticket = await _ticket_in(service, from_state)
    moved = await service.transition(ticket.id, "resolved", actor="operator:3", reason="fixed")
    assert moved.state == "resolved"


@pytest.mark.parametrize("from_state", ["new", "in_progress"])
async def test_requester_may_not_resolve_their_own_ticket(
    service: TicketService, from_state: str
) -> None:
    ticket = await _ticket_in(service, from_state)
    with pytest.raises(TransitionError) as exc:
        await service.transition(
            ticket.id, "resolved", actor=f"user:{REQUESTER_ID}", reason="I fixed it myself"
        )
    assert exc.value.code == "forbidden_actor"
    assert (await service.get(ticket.id)).state == from_state


@pytest.mark.parametrize("to_state", ["resolved", "cancelled"])
async def test_leaving_a_blocked_ticket_denies_the_open_gate_without_resuming(
    service: TicketService, to_state: str
) -> None:
    """Resolving or cancelling a ticket blocked on ``approval`` must settle its gate.

    The gate is denied so an operator's later decision can never execute a
    held call for a case that is already closed out -- but the registered
    ``GateResumer`` must not run, or the denial would immediately unblock the
    ticket, undoing the very transition under test.
    """

    resumed: list[str] = []

    async def resumer(approval):
        resumed.append(approval.id)

    service.set_gate_resumer(resumer)
    ticket = await _ticket_in(service, "in_progress", blocked_on="approval")
    approval = await service.open_approval(
        ticket.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
    )

    # A requester may cancel their own ticket even while it awaits approval;
    # only ``system`` is barred from that specific edge (see the guard test).
    actor = "operator:3" if to_state == "resolved" else f"user:{REQUESTER_ID}"
    moved = await service.transition(ticket.id, to_state, actor=actor, reason="done")
    assert moved.state == to_state
    assert moved.blocked_on == ""

    decided = await service.store.get_approval(approval.id)
    assert decided.status == "denied"
    assert resumed == []

    trail = [e for e in await service.events(ticket.id) if e.kind == "approval"]
    assert trail[-1].ok is False
    assert trail[-1].actor == "system"

    # The gate is already decided; approving it now must not reopen the case.
    with pytest.raises(ApprovalConflictError):
        await service.decide_approval(approval.id, approve=True, actor="operator:3")
    assert (await service.get(ticket.id)).state == to_state


# -- the transition notifier ----------------------------------------------------


async def test_transition_notifier_fires_after_a_successful_transition(
    service: TicketService,
) -> None:
    seen: list[tuple[str, str]] = []

    async def notifier(ticket, to_state):
        seen.append((ticket.id, to_state))

    service.set_transition_notifier(notifier)
    ticket = await _new_ticket(service)

    moved = await service.transition(ticket.id, "in_progress", actor="system")

    assert seen == [(ticket.id, "in_progress")]
    assert moved.state == "in_progress"


async def test_transition_notifier_fires_on_auto_close_resolved(tmp_path) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, autoclose_secs=3600)
    seen: list[tuple[str, str]] = []

    async def notifier(ticket, to_state):
        seen.append((ticket.id, to_state))

    svc.set_transition_notifier(notifier)
    try:
        stale = await _ticket_in(svc, "resolved")
        clock.advance(3601)

        closed = await svc.auto_close_resolved()

        assert [t.id for t in closed] == [stale.id]
        assert seen == [(stale.id, "closed")]
    finally:
        await store.close()


async def test_a_raising_transition_notifier_does_not_block_the_transition(
    service: TicketService,
) -> None:
    """The transition already committed; a broken notifier must not undo that."""

    async def broken(ticket, to_state):
        raise RuntimeError("boom")

    service.set_transition_notifier(broken)
    ticket = await _new_ticket(service)

    moved = await service.transition(ticket.id, "in_progress", actor="system")

    assert moved.state == "in_progress"
    assert (await service.get(ticket.id)).state == "in_progress"

# -- the blocked-on axis ---------------------------------------------------------


async def test_block_requires_in_progress(service: TicketService) -> None:
    ticket = await _ticket_in(service, "new")
    with pytest.raises(BlockError) as exc:
        await service.block(ticket.id, "user", actor="operator:3")
    assert exc.value.code == "illegal_block"
    assert exc.value.status_code == 409


async def test_block_rejects_unknown_reason(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress")
    with pytest.raises(BlockError) as exc:
        await service.block(ticket.id, "vibes", actor="operator:3")
    assert exc.value.code == "unknown_reason"
    assert exc.value.status_code == 400


async def test_requester_may_not_block_a_ticket(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress")
    with pytest.raises(BlockError) as exc:
        await service.block(ticket.id, "user", actor=f"user:{REQUESTER_ID}")
    assert exc.value.code == "forbidden_actor"
    assert (await service.get(ticket.id)).blocked_on == ""


async def test_block_and_unblock_round_trip_and_write_events(
    service: TicketService,
) -> None:
    ticket = await _ticket_in(service, "in_progress")
    blocked = await service.block(
        ticket.id, "user", actor="system", reason="waiting for a reply", ref="tu-1"
    )
    assert blocked.blocked_on == "user"
    assert blocked.blocked_since is not None
    assert blocked.blocked_ref == "tu-1"

    (event,) = [e for e in await service.events(ticket.id) if e.kind == "block"]
    assert event.actor == "system"
    assert event.summary == "waiting for a reply"
    assert event.fields == {"from_blocked_on": "", "to_blocked_on": "user", "ref": "tu-1"}

    # The requester may answer their own "user" block.
    unblocked = await service.unblock(ticket.id, actor=f"user:{REQUESTER_ID}")
    assert unblocked.blocked_on == ""
    assert unblocked.blocked_since is None
    assert unblocked.blocked_ref == ""

    (unblock_event,) = [e for e in await service.events(ticket.id) if e.kind == "block"][1:]
    assert unblock_event.fields == {"from_blocked_on": "user", "to_blocked_on": "", "ref": ""}


async def test_requester_may_not_clear_an_approval_or_operator_block(
    service: TicketService,
) -> None:
    for blocked_on in ("approval", "operator"):
        ticket = await _ticket_in(service, "in_progress", blocked_on=blocked_on)
        with pytest.raises(BlockError) as exc:
            await service.unblock(ticket.id, actor=f"user:{REQUESTER_ID}")
        assert exc.value.code == "forbidden_actor"
        assert (await service.get(ticket.id)).blocked_on == blocked_on


async def test_only_an_operator_may_clear_an_escalated_operator_block(
    service: TicketService,
) -> None:
    """``system`` may set an ``operator`` block (escalation) but never clear one.

    Otherwise the stall sweep's own escalation could be silently undone by the
    very sweep that raised it, defeating the point of "a human has to look".
    """

    ticket = await _ticket_in(service, "in_progress", blocked_on="operator")
    with pytest.raises(BlockError) as exc:
        await service.unblock(ticket.id, actor="system")
    assert exc.value.code == "forbidden_actor"

    moved = await service.unblock(ticket.id, actor="operator:3")
    assert moved.blocked_on == ""


async def test_unblock_on_an_unblocked_ticket_is_illegal(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress")
    with pytest.raises(BlockError) as exc:
        await service.unblock(ticket.id, actor="operator:3")
    assert exc.value.code == "illegal_block"


async def test_requester_may_not_unblock_another_persons_ticket(
    service: TicketService,
) -> None:
    ticket = await _ticket_in(service, "in_progress", blocked_on="user")
    with pytest.raises(BlockError) as exc:
        await service.unblock(ticket.id, actor="user:99")
    assert exc.value.code == "forbidden_actor"


async def test_can_block_and_can_unblock_mirror_block_and_unblock(
    service: TicketService,
) -> None:
    ticket = await _ticket_in(service, "in_progress")
    assert service.can_block(ticket, "user", "operator:3") is True
    assert service.can_block(ticket, "user", f"user:{REQUESTER_ID}") is False
    assert service.can_unblock(ticket, "operator:3") is False  # nothing to unblock yet

    blocked = await service.block(ticket.id, "user", actor="system")
    assert service.can_unblock(blocked, f"user:{REQUESTER_ID}") is True
    assert service.can_unblock(blocked, "operator:3") is True


async def test_leaving_in_progress_clears_the_block(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress", blocked_on="user")
    resolved = await service.transition(ticket.id, "resolved", actor="operator:3")
    assert resolved.blocked_on == ""
    assert resolved.blocked_since is None
    assert resolved.blocked_ref == ""
    assert resolved.blocked_nudged_at is None


# -- assignment (claiming a ticket) ----------------------------------------------


async def test_only_an_operator_may_assign(service: TicketService) -> None:
    ticket = await _new_ticket(service)
    for actor in ("system", f"user:{REQUESTER_ID}", "bot:1"):
        with pytest.raises(TransitionError) as exc:
            await service.assign(ticket.id, 3, actor=actor)
        assert exc.value.code == "forbidden_actor"
    assert (await service.get(ticket.id)).assignee_user_id is None

    claimed = await service.assign(ticket.id, 3, actor="operator:3")
    assert claimed.assignee_user_id == 3
    (event,) = [e for e in await service.events(ticket.id) if e.kind == "assign"]
    assert event.actor == "operator:3"
    assert event.fields == {"from_assignee_user_id": None, "to_assignee_user_id": 3}

    unclaimed = await service.assign(ticket.id, None, actor="operator:3")
    assert unclaimed.assignee_user_id is None
    # Assignment is orthogonal to state.
    assert unclaimed.state == "new"


# -- the frozen routing target -------------------------------------------------


def test_transition_has_no_agent_id_parameter() -> None:
    # Retargeting a ticket is a security control, so it must not ride along on a
    # routine state change -- it is operator-only ``reassign``.
    #
    # The exhaustive set is the guard: any parameter added here has to be looked
    # at, and the question to ask is whether it changes *where* the ticket
    # points. `resolved_by` does not -- it records who moved it (only ever
    # "triage" today), so the frozen routing target stays untouchable by a state
    # change, which is the property this test exists to keep.
    params = inspect.signature(TicketService.transition).parameters
    assert "agent_id" not in params
    assert set(params) == {"self", "ticket_id", "to_state", "actor", "reason", "resolved_by"}


async def test_only_an_operator_may_reassign(service: TicketService) -> None:
    ticket = await _new_ticket(service)
    for actor in ("system", f"user:{REQUESTER_ID}", "bot:1"):
        with pytest.raises(TransitionError) as exc:
            await service.reassign(ticket.id, "pc-other", actor=actor)
        assert exc.value.code == "forbidden_actor"
        assert exc.value.status_code == 403
    assert (await service.get(ticket.id)).agent_id == "pc-lena"

    moved = await service.reassign(ticket.id, "pc-other", actor="superuser:1")
    assert moved.agent_id == "pc-other"
    (handoff,) = [e for e in await service.events(ticket.id) if e.kind == "handoff"]
    assert handoff.actor == "superuser:1"
    assert handoff.fields == {"from_agent_id": "pc-lena", "to_agent_id": "pc-other"}
    # The state is untouched by a handoff.
    assert (await service.get(ticket.id)).state == "new"


# -- the audit trail -----------------------------------------------------------


async def test_every_state_change_writes_exactly_one_event(
    service: TicketService,
) -> None:
    ticket = await _new_ticket(service)
    path = ["in_progress", "resolved", "in_progress", "resolved", "closed"]
    for to_state in path:
        await service.transition(ticket.id, to_state, actor="operator:3")

    events = [e for e in await service.events(ticket.id) if e.kind == "state"]
    assert [e.to_state for e in events] == ["new", *path]
    assert [e.from_state for e in events] == [None, "new", *path[:-1]]
    assert len(events) == len(path) + 1  # + the genesis event

    # A refused transition leaves no trace at all.
    with pytest.raises(TransitionError):
        await service.transition(ticket.id, "in_progress", actor="operator:3")
    assert len([e for e in await service.events(ticket.id) if e.kind == "state"]) == len(
        events
    )


async def test_append_event_refuses_lifecycle_kinds(service: TicketService) -> None:
    ticket = await _new_ticket(service)
    for kind in ("state", "handoff", "block", "assign", "nonsense"):
        with pytest.raises(ValueError):
            await service.append_event(ticket.id, kind=kind, actor="system")


async def test_append_event_records_a_note(service: TicketService) -> None:
    ticket = await _new_ticket(service)
    await service.append_event(
        ticket.id, kind="note", actor="operator:3", summary="called the user"
    )
    (note,) = [e for e in await service.events(ticket.id) if e.kind == "note"]
    assert note.summary == "called the user"
    assert note.fields is None


# -- redaction -----------------------------------------------------------------


def test_redact_args_by_key_name() -> None:
    assert redact_args({"password": "hunter2"}) == {"password": "***"}
    assert redact_args({"Password": "hunter2"}) == {"Password": "***"}
    assert redact_args({"api_token": "t"}) == {"api_token": "***"}
    assert redact_args({"client_secret": "s"}) == {"client_secret": "***"}
    assert redact_args({"ssh_key": "k"}) == {"ssh_key": "***"}
    assert redact_args({"username": "lena"}) == {"username": "lena"}


def test_redact_args_recurses_into_nested_structures() -> None:
    args = {
        "username": "lena",
        "account": {"password": "hunter2", "groups": ["users"]},
        "hosts": [
            {"name": "pc-lena", "credentials": {"api_key": "abc", "user": "lena"}},
            {"name": "pc-tom", "tokens": ["a", "b"]},
        ],
        "count": 3,
        "flag": None,
    }
    assert redact_args(args) == {
        "username": "lena",
        "account": {"password": "***", "groups": ["users"]},
        "hosts": [
            {"name": "pc-lena", "credentials": {"api_key": "***", "user": "lena"}},
            {"name": "pc-tom", "tokens": "***"},
        ],
        "count": 3,
        "flag": None,
    }
    # Non-mapping input is returned unchanged.
    assert redact_args("plain") == "plain"
    assert redact_args([1, {"secret": 2}]) == [1, {"secret": "***"}]


async def test_tool_call_args_are_redacted_before_they_are_persisted(
    service: TicketService,
) -> None:
    ticket = await _new_ticket(service)
    await service.append_event(
        ticket.id,
        kind="tool_call",
        actor="system",
        tool="account_create",
        tool_class="admin",
        ok=True,
        args={"username": "lena", "password": "hunter2", "opts": {"token": "t"}},
        summary="created account",
    )
    (event,) = [e for e in await service.events(ticket.id) if e.kind == "tool_call"]
    assert event.fields == {
        "args": {"username": "lena", "password": "***", "opts": {"token": "***"}}
    }
    assert "hunter2" not in str(event.as_dict())


# -- gates ---------------------------------------------------------------------


async def test_open_approval_is_exclusive_per_ticket(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress")
    approval = await service.open_approval(
        ticket.id,
        tool_use_id="tu1",
        tool="account_create",
        tool_class="admin",
        args={"username": "lena", "password": "hunter2"},
        agent_id="pc-lena",
    )
    assert approval.status == "pending"
    assert approval.expires_at == to_iso(
        service.now() + timedelta(seconds=service.approval_ttl_secs)
    )
    # The pending payload is kept verbatim -- it is what will be executed ...
    assert approval.args["password"] == "hunter2"
    # ... while the trail entry is redacted.
    (event,) = [e for e in await service.events(ticket.id) if e.kind == "approval"]
    assert event.fields is not None
    assert event.fields["args"]["password"] == "***"
    assert event.fields["approval_id"] == approval.id

    with pytest.raises(ApprovalConflictError) as exc:
        await service.open_approval(
            ticket.id,
            tool_use_id="tu2",
            tool="reboot",
            tool_class="admin",
            args={},
        )
    assert exc.value.status_code == 409


async def test_decide_approval_records_and_refuses_twice(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress", blocked_on="approval")
    approval = await service.open_approval(
        ticket.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
    )
    decided = await service.decide_approval(
        approval.id, approve=True, decided_by=3, decided_via="dashboard"
    )
    assert decided.status == "approved"
    assert decided.decided_by == 3
    assert decided.decided_at == to_iso(service.now())

    trail = [e for e in await service.events(ticket.id) if e.kind == "approval"]
    assert trail[-1].ok is True
    assert trail[-1].actor == "operator:3"

    with pytest.raises(ApprovalConflictError):
        await service.decide_approval(approval.id, approve=False)
    # Authorization is checked before the lookup, so an unknown id still needs a
    # caller who could have approved a real one.
    with pytest.raises(ApprovalNotFoundError):
        await service.decide_approval("nope", approve=True, decided_by=3)


async def test_denied_approval_does_not_move_or_unblock_the_ticket(
    service: TicketService,
) -> None:
    ticket = await _ticket_in(service, "in_progress", blocked_on="approval")
    approval = await service.open_approval(
        ticket.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
    )
    await service.decide_approval(approval.id, approve=False, decided_by=3)
    reloaded = await service.get(ticket.id)
    assert reloaded.state == "in_progress"
    # Deciding the gate and clearing the block are deliberately separate calls.
    assert reloaded.blocked_on == "approval"


async def test_open_approval_validates_kind(service: TicketService) -> None:
    ticket = await _ticket_in(service, "in_progress")
    with pytest.raises(ValueError):
        await service.open_approval(
            ticket.id,
            tool_use_id="tu1",
            tool="reboot",
            tool_class="admin",
            args={},
            kind="whatever",
        )


# -- housekeeping --------------------------------------------------------------


async def test_expire_due_only_touches_overdue_gates(tmp_path) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, approval_ttl_secs=600)
    try:
        ticket = await _ticket_in(svc, "in_progress", blocked_on="approval")
        approval = await svc.open_approval(
            ticket.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
        )
        clock.advance(599)
        assert await svc.expire_due() == []

        clock.advance(2)
        (expired,) = await svc.expire_due()
        assert expired.id == approval.id
        assert expired.status == "expired"
        assert expired.decided_via == "timeout"
        # Expiring a gate closes the gate, not the ticket: leaving the
        # "approval" block is an operator (or resumer) decision.
        reloaded = await svc.get(ticket.id)
        assert reloaded.state == "in_progress"
        assert reloaded.blocked_on == "approval"
        trail = [e for e in await svc.events(ticket.id) if e.kind == "approval"]
        assert trail[-1].ok is False
        assert await svc.expire_due() == []
    finally:
        await store.close()


async def test_auto_close_resolved_respects_the_window(tmp_path) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, autoclose_secs=3600)
    try:
        stale = await _ticket_in(svc, "resolved")
        clock.advance(3601)
        fresh = await _ticket_in(svc, "resolved")

        closed = await svc.auto_close_resolved()
        assert [t.id for t in closed] == [stale.id]
        assert (await svc.get(stale.id)).state == "closed"
        assert (await svc.get(fresh.id)).state == "resolved"
        # The auto-close is a normal, recorded, system-driven transition --
        # routed through the same chokepoint (transition()) any other
        # resolved -> closed move would go through, not a store bypass.
        (event,) = [
            e for e in await svc.events(stale.id) if e.to_state == "closed"
        ]
        assert event.kind == "state"
        assert event.actor == "system"
        assert await svc.auto_close_resolved() == []
    finally:
        await store.close()


async def test_nudge_stalled_reminds_once_then_stops(tmp_path) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, stall_nudge_secs=3600, stall_giveup_secs=0)
    try:
        notified: list[tuple[str, str]] = []

        async def notifier(ticket, blocked_on):
            notified.append((ticket.id, blocked_on))

        svc.set_stall_notifier(notifier)
        waiting = await _ticket_in(svc, "in_progress", blocked_on="user")
        clock.advance(1800)
        fresh = await _ticket_in(svc, "in_progress", blocked_on="operator")

        clock.advance(1799)
        assert await svc.nudge_stalled() == []
        assert notified == []

        clock.advance(2)
        touched = await svc.nudge_stalled()
        assert {t.id for t in touched} == {waiting.id}
        assert notified == [(waiting.id, "user")]
        assert (await svc.get(waiting.id)).blocked_nudged_at is not None
        note = [e for e in await svc.events(waiting.id) if e.kind == "note"][-1]
        assert "stall reminder" in note.summary

        # Already nudged -- not reminded a second time.
        assert await svc.nudge_stalled() == []
        assert notified == [(waiting.id, "user")]

        # ``fresh`` never crossed its own window.
        assert (await svc.get(fresh.id)).blocked_nudged_at is None
    finally:
        await store.close()


async def test_nudge_stalled_escalates_an_unanswered_user_block(tmp_path) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, stall_nudge_secs=0, stall_giveup_secs=7200)
    try:
        ticket = await _ticket_in(svc, "in_progress", blocked_on="user")
        clock.advance(7201)
        touched = await svc.nudge_stalled()
        assert [t.id for t in touched] == [ticket.id]
        reloaded = await svc.get(ticket.id)
        assert reloaded.blocked_on == "operator"
        # The escalation restarts the clock.
        assert reloaded.blocked_since == to_iso(clock())

        (event,) = [e for e in await svc.events(ticket.id) if e.kind == "block"][1:]
        assert event.actor == "system"
        assert event.fields["from_blocked_on"] == "user"
        assert event.fields["to_blocked_on"] == "operator"

        # An "operator" block never escalates further -- there is nowhere left
        # to go, and a second pass finds nothing more to touch.
        clock.advance(100000)
        assert await svc.nudge_stalled() == []
    finally:
        await store.close()


async def test_nudge_stalled_never_touches_an_approval_block(tmp_path) -> None:
    """``approval`` has its own clock (the gate TTL) — the stall sweep must
    leave it alone entirely, or a ticket could be nudged/escalated twice by
    two different mechanisms for the same wait."""

    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, stall_nudge_secs=1, stall_giveup_secs=1)
    try:
        ticket = await _ticket_in(svc, "in_progress", blocked_on="approval")
        clock.advance(1000)
        assert await svc.nudge_stalled() == []
        assert (await svc.get(ticket.id)).blocked_on == "approval"
    finally:
        await store.close()


async def test_sweep_loop_expires_nudges_autocloses_and_survives_a_bad_pass(
    tmp_path, monkeypatch
) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, approval_ttl_secs=60, autoclose_secs=999999)
    try:
        gated = await _ticket_in(svc, "in_progress", blocked_on="approval")
        approval = await svc.open_approval(
            gated.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
        )
        resolved = await _ticket_in(svc, "resolved")
        clock.advance(7200)

        settings = {
            "KENNY_TICKET_SWEEP_INTERVAL_SECS": 60,
            "KENNY_TICKET_AUTOCLOSE_SECS": 3600,
            "KENNY_TICKET_STALL_NUDGE_SECS": 0,
            "KENNY_TICKET_STALL_GIVEUP_SECS": 0,
        }
        real_sleep = asyncio.sleep
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)
            await real_sleep(0)
            if len(slept) >= 3:
                raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        # The first pass blows up; the loop must keep going regardless.
        real_sweep = svc.sweep
        passes: list[int] = []

        async def flaky_sweep(*args, **kwargs):
            passes.append(1)
            if len(passes) == 1:
                raise RuntimeError("boom")
            return await real_sweep(*args, **kwargs)

        monkeypatch.setattr(svc, "sweep", flaky_sweep)

        with pytest.raises(asyncio.CancelledError):
            await ticket_sweep_loop(svc, settings.get, 300, 5.0)

        assert len(passes) == 2
        # initial delay, then the cadence re-read from the getter each pass.
        assert slept == [5.0, 60, 60]
        assert (await svc.store.get_approval(approval.id)).status == "expired"
        assert (await svc.get(resolved.id)).state == "closed"
    finally:
        await store.close()


async def test_sweep_loop_falls_back_when_the_getter_knows_nothing(
    tmp_path, monkeypatch
) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    svc = TicketService(store, now=Clock())
    try:
        real_sleep = asyncio.sleep
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)
            await real_sleep(0)
            if len(slept) >= 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await ticket_sweep_loop(svc, lambda key: None, 120, 0.0)
        assert slept == [0.0, 120]
    finally:
        await store.close()


async def test_update_patches_fields_without_touching_state(
    service: TicketService,
) -> None:
    ticket = await _ticket_in(service, "in_progress")
    patched = await service.update(
        ticket.id, summary="spooler stuck", resolution="restarted", priority="high"
    )
    assert patched.summary == "spooler stuck"
    assert patched.resolution == "restarted"
    assert patched.priority == "high"
    assert patched.state == "in_progress"
    with pytest.raises(TicketNotFoundError):
        await service.update("nope", summary="x")


# -- the triage trigger --------------------------------------------------------


async def test_creating_a_ticket_starts_an_investigation(service: TicketService) -> None:
    """A new ticket schedules the registered investigation, and hands it the ticket."""

    seen: list[str] = []
    started = asyncio.Event()

    async def runner(ticket) -> None:  # type: ignore[no-untyped-def]
        seen.append(ticket.id)
        started.set()

    service.set_triage(runner)
    ticket = await service.create(title="t", origin="alert", agent_id="pc1")
    await asyncio.wait_for(started.wait(), timeout=2)
    assert seen == [ticket.id]


async def test_without_a_registered_investigator_nothing_is_scheduled(
    service: TicketService,
) -> None:
    """The default. Every test and every deployment without a key gets this."""

    ticket = await service.create(title="t", origin="alert", agent_id="pc1")
    assert ticket.state == "new"
    assert service._triage is None
    assert not service._triage_tasks


async def test_an_investigation_that_blows_up_still_leaves_the_ticket(
    service: TicketService, caplog
) -> None:
    """The creator must not be able to lose a ticket to a broken investigator.

    ``TriageService.run`` swallows its own failures, so this exercises the
    scheduling side: a runner that raises anyway must be contained here too,
    because a bare ``create_task`` exception would otherwise surface as an
    unretrieved-exception warning on the loop and nowhere useful.
    """

    failed = asyncio.Event()

    async def runner(_ticket) -> None:  # type: ignore[no-untyped-def]
        failed.set()
        raise RuntimeError("the model is on fire")

    service.set_triage(runner)
    ticket = await service.create(title="t", origin="alert", agent_id="pc1")
    await asyncio.wait_for(failed.wait(), timeout=2)
    # Wait for the done-callback rather than yielding a fixed number of times:
    # how many loop iterations that takes is an implementation detail, and
    # guessing it is how a test becomes flaky on a busy machine.
    async with asyncio.timeout(2):
        while service._triage_tasks:
            await asyncio.sleep(0)

    assert await service.get(ticket.id) is not None


async def test_the_investigation_does_not_delay_the_creator(
    service: TicketService,
) -> None:
    """``create`` returns before the investigation has run a single step.

    It is called from the alert loop and from a request handler, and an
    investigation takes seconds: neither may wait on it.
    """

    release = asyncio.Event()
    entered = asyncio.Event()

    async def runner(_ticket) -> None:  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()

    service.set_triage(runner)
    ticket = await service.create(title="t", origin="alert", agent_id="pc1")
    assert ticket.id  # returned while the runner is still parked
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert service._triage_tasks
    release.set()
    await asyncio.sleep(0)

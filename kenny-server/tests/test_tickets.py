"""``TicketService`` lifecycle, authorization, redaction and sweeper tests."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.ticketstore import TicketStore, to_iso
from kenny_server.tickets import (
    _ACTORS,
    _ALLOWED,
    STATES,
    ApprovalConflictError,
    ApprovalForbiddenError,
    ApprovalNotFoundError,
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


async def _ticket_in(svc: TicketService, state: str):
    """A ticket forced into ``state`` through the store (test scaffolding only)."""

    ticket = await _new_ticket(svc)
    if state != "new":
        forced = await svc.store.set_state(
            ticket.id, state, actor="system", reason="test setup", now=to_iso(svc.now())
        )
        assert forced is not None
        return forced
    return ticket


# -- creation ------------------------------------------------------------------


async def test_create_mints_new_and_records_genesis(service: TicketService) -> None:
    ticket = await _new_ticket(
        service, role_snapshot="user", profile_snapshot="family", priority="high"
    )
    assert ticket.state == "new"
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
        ("new", "in_progress"),
        ("new", "resolved"),
        ("new", "closed"),
        ("triage", "resolved"),
        ("triage", "closed"),
        ("in_progress", "triage"),
        ("in_progress", "closed"),
        ("awaiting_user", "awaiting_approval"),
        ("awaiting_approval", "resolved"),
        ("awaiting_agent", "resolved"),
        ("resolved", "cancelled"),
        ("resolved", "awaiting_user"),
        ("in_progress", "in_progress"),
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


async def test_transition_on_unknown_ticket_raises(service: TicketService) -> None:
    with pytest.raises(TicketNotFoundError):
        await service.transition("nope", "triage", actor="system")


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


async def test_requester_may_not_release_their_own_gate(service: TicketService) -> None:
    ticket = await _ticket_in(service, "awaiting_approval")
    with pytest.raises(TransitionError) as exc:
        await service.transition(
            ticket.id, "in_progress", actor=f"user:{REQUESTER_ID}"
        )
    assert exc.value.code == "forbidden_actor"
    assert exc.value.status_code == 403
    assert (await service.get(ticket.id)).state == "awaiting_approval"
    # ... but the operator may.
    moved = await service.transition(ticket.id, "in_progress", actor="operator:3")
    assert moved.state == "in_progress"


async def test_only_an_operator_may_approve_a_gate(service: TicketService) -> None:
    """Approving is the boundary — not leaving ``awaiting_approval``.

    The system has to be able to move the ticket on, or an expired gate would
    park it forever. What it must never do is mark the held call approved.
    """

    ticket = await _ticket_in(service, "awaiting_approval")
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

    # ...and the system may then resume the ticket to report the refusal.
    moved = await service.transition(ticket.id, "in_progress", actor="system")
    assert moved.state == "in_progress"


async def test_requester_may_not_release_a_gate(service: TicketService) -> None:
    ticket = await _ticket_in(service, "awaiting_approval")
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, "in_progress", actor=f"user:{REQUESTER_ID}")
    assert exc.value.code == "forbidden_actor"


async def test_requester_may_not_touch_another_persons_ticket(
    service: TicketService,
) -> None:
    ticket = await _ticket_in(service, "awaiting_user")
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, "in_progress", actor="user:99")
    assert exc.value.code == "forbidden_actor"
    assert exc.value.status_code == 403

    # An alert-origin ticket has no requester at all: nobody owns it.
    orphan = await service.create(title="disk full", origin="alert")
    await service.store.set_state(orphan.id, "awaiting_user", actor="system")
    with pytest.raises(TransitionError):
        await service.transition(orphan.id, "in_progress", actor=f"user:{REQUESTER_ID}")


async def test_unknown_actor_prefix_is_never_authorized(service: TicketService) -> None:
    ticket = await _ticket_in(service, "new")
    with pytest.raises(TransitionError) as exc:
        await service.transition(ticket.id, "triage", actor="discord-bot:1")
    assert exc.value.code == "forbidden_actor"


async def test_can_transition_mirrors_transition(service: TicketService) -> None:
    ticket = await _ticket_in(service, "awaiting_approval")
    assert service.can_transition(ticket, "in_progress", "operator:3") is True
    assert service.can_transition(ticket, "in_progress", f"user:{REQUESTER_ID}") is False
    assert service.can_transition(ticket, "resolved", "operator:3") is False


# -- the frozen routing target -------------------------------------------------


def test_transition_has_no_agent_id_parameter() -> None:
    # Retargeting a ticket is a security control, so it must not ride along on a
    # routine state change -- it is operator-only ``reassign``.
    params = inspect.signature(TicketService.transition).parameters
    assert "agent_id" not in params
    assert set(params) == {"self", "ticket_id", "to_state", "actor", "reason"}


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
    path = ["triage", "in_progress", "awaiting_user", "in_progress", "resolved", "closed"]
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
    for kind in ("state", "handoff", "nonsense"):
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
    ticket = await _ticket_in(service, "awaiting_approval")
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


async def test_denied_approval_does_not_move_the_ticket(service: TicketService) -> None:
    ticket = await _ticket_in(service, "awaiting_approval")
    approval = await service.open_approval(
        ticket.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
    )
    await service.decide_approval(approval.id, approve=False, decided_by=3)
    assert (await service.get(ticket.id)).state == "awaiting_approval"


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
        ticket = await _ticket_in(svc, "awaiting_approval")
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
        # Expiring a gate closes the gate, not the ticket: leaving
        # awaiting_approval is an operator decision.
        assert (await svc.get(ticket.id)).state == "awaiting_approval"
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
        # The auto-close is a normal, recorded, system-driven transition.
        (event,) = [
            e for e in await svc.events(stale.id) if e.to_state == "closed"
        ]
        assert event.kind == "state"
        assert event.actor == "system"
        assert await svc.auto_close_resolved() == []
    finally:
        await store.close()


async def test_sweep_loop_expires_autocloses_and_survives_a_bad_pass(
    tmp_path, monkeypatch
) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    await store.connect()
    clock = Clock()
    svc = TicketService(store, now=clock, approval_ttl_secs=60, autoclose_secs=999999)
    try:
        gated = await _ticket_in(svc, "awaiting_approval")
        approval = await svc.open_approval(
            gated.id, tool_use_id="tu1", tool="reboot", tool_class="admin", args={}
        )
        resolved = await _ticket_in(svc, "resolved")
        clock.advance(7200)

        settings = {
            "KENNY_TICKET_SWEEP_INTERVAL_SECS": 60,
            "KENNY_TICKET_AUTOCLOSE_SECS": 3600,
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

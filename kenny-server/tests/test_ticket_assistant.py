"""``TicketAssistant`` — the surface-agnostic half of the Discord extraction.

These tests are the regression proof for the two behaviour changes the plan
called for (session context built from the *acting* principal, turn cap/rate
limit exempt for operator+), plus the handful of invariants that must survive
the move out of ``discord_service.py`` completely unchanged: the frozen-target
discard/handoff record, and consent-before-approval gate ordering.

A minimal, Discord-free world: no gateway, no identity store — a ticket is
opened directly through ``TicketService.create`` and driven by
``TicketAssistant`` alone, so nothing here depends on the Discord surface even
existing.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from kenny_server.auth import Principal
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.ticket_assistant import (
    _MAX_TRAIL_TEXT_CHARS,
    TicketAssistant,
    TicketPolicy,
)
from kenny_server.ticketstore import TicketStore
from kenny_server.tickets import TicketService
from kenny_server.tool_classes import STANDARD_CHANGE, classify
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.toolloop import Allow, Hold, ToolExecutor
from kenny_server.tunnel import AgentTunnel
from kenny_server.userstore import UserStore

AGENT = "kid-pc"


# -- fake Anthropic client (shape copied from tests/test_discord_service.py) --


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


def text_block(text: str) -> _Block:
    return _Block(type="text", text=text)


def tool_use_block(tool_id: str, name: str, inp: dict[str, Any]) -> _Block:
    return _Block(type="tool_use", id=tool_id, name=name, input=inp)


def _chunks(text: str) -> list[str]:
    return re.findall(r"\S+\s*", text) or ([text] if text else [])


class _StreamCtx:
    def __init__(self, response: _Response) -> None:
        self._response = response
        self.text_stream = [
            chunk
            for b in response.content
            if getattr(b, "type", None) == "text"
            for chunk in _chunks(b.text)
        ]

    def __enter__(self) -> _StreamCtx:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def get_final_message(self) -> _Response:
        return self._response


class FakeMessages:
    def __init__(self, scripted: list[_Response]) -> None:
        self._scripted = scripted
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _StreamCtx:
        self.calls.append(kwargs)
        if not self._scripted:
            raise AssertionError("the model was called more times than scripted")
        return _StreamCtx(self._scripted.pop(0))


class FakeAnthropic:
    def __init__(self, scripted: list[_Response]) -> None:
        self.messages = FakeMessages(scripted)


def text_turn(text: str) -> _Response:
    return _Response([text_block(text)], "end_turn")


def tool_turn(*blocks: _Block) -> _Response:
    return _Response(list(blocks), "tool_use")


# -- world ---------------------------------------------------------------------


class World:
    """Every store plus an assistant factory over one DB file. No Discord."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.sent: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {}

    async def setup(self) -> None:
        self.telemetry = TelemetryStore(db_path=self.db_path)
        await self.telemetry.connect()
        self.ticket_store = TicketStore(self.db_path)
        await self.ticket_store.connect()
        self.users = UserStore(self.db_path)
        await self.users.connect()
        self.tickets = TicketService(self.ticket_store)

        self.registry = AgentRegistry(tokens={AGENT: "t"})
        self.tunnel = AgentTunnel(
            self.registry, self.telemetry, EventStore(db_path=self.db_path)
        )

        async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
            self.sent.append({"agent_id": agent_id, "tool": tool, "args": dict(args)})
            return self.results.get(tool, {"ok": True, "tool": tool})

        self.tunnel.send_request = fake_send_request  # type: ignore[assignment]
        self.executor = ToolExecutor(
            registry=self.registry,
            store=self.telemetry,
            tunnel=self.tunnel,
            call_log=CallLog(),
            screenshots=ScreenshotStore(),
        )

        self.kid = await self.users.create_user("kid", "pw-123456", "user")
        self.root = await self.users.create_user("root", "pw-123456", "operator")
        await self.users.set_user_hosts(self.kid["id"], [AGENT])

    async def close(self) -> None:
        for store in (self.telemetry, self.ticket_store, self.users):
            await store.close()

    def assistant(self, *scripted: _Response, **kwargs: Any) -> TicketAssistant:
        self.client = FakeAnthropic(list(scripted))
        return TicketAssistant(
            tickets=self.tickets,
            users=self.users,
            executor=self.executor,
            client=self.client,
            model="fake-model",
            **kwargs,
        )

    def kid_principal(self, *, role: str = "user") -> Principal:
        return Principal(
            user_id=self.kid["id"],
            username="kid",
            role=role,
            hosts=frozenset({AGENT}) if role == "user" else frozenset(),
            email=self.kid["email"],
            avatar=self.kid["avatar"],
        )

    def root_principal(self) -> Principal:
        return Principal(
            user_id=self.root["id"],
            username="root",
            role="operator",
            hosts=frozenset(),
            email=self.root["email"],
            avatar=self.root["avatar"],
        )


@pytest.fixture
async def world(tmp_path):
    w = World(str(tmp_path / "kenny.sqlite"))
    await w.setup()
    yield w
    await w.close()


async def _open_ticket(world: World, *, profile_snapshot: str | None = "self-service-basic"):
    return await world.tickets.create(
        title="my pc is slow",
        origin="dashboard",
        requester_user_id=world.kid["id"],
        agent_id=AGENT,
        role_snapshot="user",
        profile_snapshot=profile_snapshot,
        actor=f"user:{world.kid['id']}",
    )


# -- session_for: built from the actor, snapshot narrows only the requester ----


async def test_requester_session_is_narrowed_by_the_frozen_snapshot(world: World) -> None:
    """The requester's own frozen profile still cuts their own future turns."""

    ticket = await _open_ticket(world)
    assistant = world.assistant()

    session = await assistant.session_for(ticket, actor=world.kid_principal())

    assert session is not None
    # self-service-basic does not carry winget_install; the ticket froze it at
    # creation, and the requester's live profile (None -> "everything") must
    # not widen a turn already in flight.
    assert "winget_install" not in session.allowed_tools
    assert "diag_services" in session.allowed_tools  # in the frozen profile


async def test_operator_session_ignores_someone_elses_frozen_snapshot(world: World) -> None:
    """An operator working a ticket that isn't theirs gets their own live context.

    The ticket's ``role_snapshot``/``profile_snapshot`` belong to the
    requester; a third party's session must be neither narrower nor wider for
    having read them.
    """

    ticket = await _open_ticket(world)
    assistant = world.assistant()

    session = await assistant.session_for(ticket, actor=world.root_principal())

    assert session is not None
    assert session.principal.role == "operator"
    assert session.principal.scoped is False
    # Not narrowed by the requester's frozen self-service-basic profile: an
    # operator's own (unset) profile allows everything.
    assert "winget_install" in session.allowed_tools
    # Not host-scoped either -- fleet-wide tools stay visible to an operator.
    assert "fleet_overview" in session.allowed_tools


async def test_requester_session_narrows_role_too_not_only_tools(world: World) -> None:
    """A promoted account still has its *ticket* narrowed to the frozen role.

    The ticket froze ``role_snapshot="user"`` when ``kid`` opened it. If the
    account is later promoted, a turn driven *as the requester* on this
    specific ticket is still the lower of the two roles -- exactly
    ``_narrower_role``'s job, preserved by the move.
    """

    ticket = await _open_ticket(world, profile_snapshot=None)
    assistant = world.assistant()
    promoted = world.kid_principal(role="operator")

    session = await assistant.session_for(ticket, actor=promoted)

    assert session is not None
    assert session.principal.role == "user"
    assert session.principal.scoped is True
    assert session.principal.hosts == frozenset({AGENT})


async def test_session_for_returns_none_without_a_ticket(world: World) -> None:
    assistant = world.assistant()
    assert await assistant.session_for(None, actor=world.root_principal()) is None


async def test_session_for_returns_none_for_a_disabled_account(world: World) -> None:
    ticket = await _open_ticket(world)
    assistant = world.assistant()
    await world.users.update_user(world.kid["id"], disabled=True)

    assert await assistant.session_for(ticket, actor=world.kid_principal()) is None


# -- turn cap: operator-driven turns are exempt, scoped-user turns are not -----


async def test_operator_turn_does_not_count_against_the_cap(world: World) -> None:
    assistant = world.assistant(
        text_turn("one"), text_turn("two"), text_turn("three"), max_turns_per_ticket=1
    )
    ticket = await _open_ticket(world)
    session = await assistant.session_for(ticket, actor=world.root_principal())
    assert session is not None

    for _ in range(3):
        session.messages.append({"role": "user", "content": "hi"})
        async for _event in assistant.run_turn(session, ticket):
            pass

    assert session.turns == 0
    assert world.client.messages.calls  # the model really was called each time


async def test_scoped_user_turn_hits_the_cap(world: World) -> None:
    assistant = world.assistant(text_turn("one"), max_turns_per_ticket=1)
    ticket = await _open_ticket(world, profile_snapshot=None)
    ticket = await world.tickets.transition(ticket.id, "in_progress", actor="system")
    session = await assistant.session_for(ticket, actor=world.kid_principal())
    assert session is not None

    session.messages.append({"role": "user", "content": "first"})
    events = [e async for e in assistant.run_turn(session, ticket)]
    assert session.turns == 1
    assert events[-1]["type"] == "done"

    # A completed turn with nothing held blocks the ticket on "user"; mirrors
    # what a real caller (DiscordService/the dashboard route) does between
    # turns — unblock before driving the next one.
    ticket = await world.tickets.unblock(ticket.id, actor="system")
    session.messages.append({"role": "user", "content": "second"})
    events = [e async for e in assistant.run_turn(session, ticket)]
    # No second model call: the cap tripped before drive_events ran.
    assert len(world.client.messages.calls) == 1
    assert session.turns == 1
    assert events[-1]["type"] == "done"
    assert "automatic-work limit" in events[-1]["assistant_text"]

    refreshed = await world.ticket_store.get(ticket.id)
    assert refreshed is not None
    assert refreshed.state == "in_progress"
    assert refreshed.blocked_on == "operator"


# -- the frozen target: a model-supplied agent_id is discarded, not adopted ----


async def test_a_model_supplied_agent_id_is_discarded_and_recorded(world: World) -> None:
    assistant = world.assistant(
        tool_turn(tool_use_block("t1", "diag_services", {"agent_id": "someone-elses-pc"})),
        text_turn("done"),
    )
    ticket = await _open_ticket(world, profile_snapshot=None)
    session = await assistant.session_for(ticket, actor=world.kid_principal())
    assert session is not None
    session.messages.append({"role": "user", "content": "what is running?"})

    async for _event in assistant.run_turn(session, ticket):
        pass

    # The call still ran against the ticket's frozen target, never the claim.
    assert world.sent == [
        {"agent_id": AGENT, "tool": "diag_services", "args": {}}
    ]
    events = await world.tickets.events(ticket.id)
    handoffs = [e for e in events if e.kind == "handoff"]
    assert len(handoffs) == 1
    assert handoffs[0].fields["applied"] is False
    assert handoffs[0].fields["attempted_agent_id"] == "someone-elses-pc"
    assert handoffs[0].fields["frozen_agent_id"] == AGENT


# -- gate ordering: consent precedes the tier check -----------------------------


async def test_consent_is_checked_before_the_change_tier(world: World) -> None:
    """``remotehelp_start`` is sensitive *and* a standard change -- the one real
    call both gates meet. Held for consent first; once granted, the tier gate
    (not a second consent hold) is what actually runs it."""

    ticket = await _open_ticket(world, profile_snapshot=None)
    assistant = world.assistant()
    session = await assistant.session_for(ticket, actor=world.kid_principal())
    assert session is not None
    policy = TicketPolicy(world.tickets, session)

    assert classify("remotehelp_start") == STANDARD_CHANGE
    first = await policy.gate(session, "remotehelp_start", {}, AGENT)
    assert isinstance(first, Hold) and first.kind == "user_consent"

    session.consented.add("remotehelp_start")
    second = await policy.gate(session, "remotehelp_start", {}, AGENT)
    assert isinstance(second, Allow)

    autonomous = [
        e
        for e in await world.tickets.events(ticket.id)
        if e.kind == "tool_call" and "standard change" in e.summary
    ]
    assert len(autonomous) == 1


# -- append_message: verbatim text, capped ---------------------------------------


async def test_append_message_writes_text_only_when_verbatim(world: World) -> None:
    ticket = await _open_ticket(world)
    assistant = world.assistant()

    await assistant.append_message(
        ticket, actor="kenny", text="the full reply", actionable=False,
        surface="dashboard", verbatim=True,
    )
    await assistant.append_message(
        ticket, actor="user:1", text="a family message", actionable=True,
        surface="discord", verbatim=False,
    )

    events = await world.tickets.events(ticket.id)
    messages = [e for e in events if e.kind == "message"]
    assert len(messages) == 2
    verbatim_row, summary_row = messages
    assert verbatim_row.fields["text"] == "the full reply"
    assert verbatim_row.fields["surface"] == "dashboard"
    assert "text" not in summary_row.fields
    assert summary_row.fields["surface"] == "discord"


async def test_append_message_truncates_past_the_trail_cap(world: World) -> None:
    ticket = await _open_ticket(world)
    assistant = world.assistant()
    long_text = "x" * (_MAX_TRAIL_TEXT_CHARS + 500)

    await assistant.append_message(
        ticket, actor="kenny", text=long_text, actionable=False,
        surface="dashboard", verbatim=True,
    )

    events = await world.tickets.events(ticket.id)
    stored = [e for e in events if e.kind == "message"][0].fields["text"]
    assert stored.endswith("\n\n[truncated]")
    assert len(stored) == _MAX_TRAIL_TEXT_CHARS + len("\n\n[truncated]")

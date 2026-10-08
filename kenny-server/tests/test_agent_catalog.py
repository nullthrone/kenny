"""The shipped agent catalog, and the seam between it and the code it describes.

``CATALOG["triage"]`` *declares* the unprompted triage that ``triage.py`` and
``ticket_assistant.py`` *run*. Two places that must agree, so the last tests
here build the real triage session and compare it to the declaration; they fail
when either side moves alone.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kenny_server import ticket_assistant, toolloop
from kenny_server.agents import catalog
from kenny_server.agents.catalog import CATALOG, build, get
from kenny_server.agents.catalog.triage import TRIAGE
from kenny_server.agents.spec import AgentSpec, Budget, SpecError, Trigger, validate
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.ticket_assistant import TicketAssistant, TicketPolicy
from kenny_server.ticketstore import TicketStore
from kenny_server.tickets import TicketService
from kenny_server.toolloop import ToolExecutor
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.triage import DEFAULT_MAX_ITERATIONS
from kenny_server.tunnel import AgentTunnel
from kenny_server.userstore import UserStore

HOST = "thomas-pc"


# -- the catalog ---------------------------------------------------------------


def test_triage_is_in_the_catalog() -> None:
    assert CATALOG["triage"] is TRIAGE
    assert get("triage") is TRIAGE


def test_get_unknown_is_none() -> None:
    assert get("nope") is None
    assert get("") is None


def test_every_catalog_spec_is_valid_and_keyed_by_its_id() -> None:
    assert CATALOG
    for agent_id, spec in CATALOG.items():
        assert spec.id == agent_id
        assert validate(spec) is spec


def test_the_catalog_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        CATALOG["x"] = TRIAGE  # type: ignore[index]


def test_a_duplicate_id_is_refused() -> None:
    with pytest.raises(SpecError, match="duplicate"):
        build((TRIAGE, replace(TRIAGE, title="Again")))


def test_an_invalid_spec_fails_the_build() -> None:
    bad = replace(TRIAGE, id="Bad Id")
    with pytest.raises(SpecError):
        build((TRIAGE, bad))


def test_build_validates_every_spec_not_just_the_first() -> None:
    ok = replace(TRIAGE, id="second")
    unclassified = replace(TRIAGE, id="third", tools=frozenset({"no_such_tool"}), verdict_tool=None)
    assert set(build((TRIAGE, ok))) == {"triage", "second"}
    with pytest.raises(SpecError, match="unclassified"):
        build((TRIAGE, ok, unclassified))


def test_import_is_where_the_catalog_is_validated() -> None:
    # CATALOG is built by ``build`` at import, so a bad spec in the module's
    # tuple would stop the server booting. Pin that it goes through build().
    assert catalog.CATALOG == build((TRIAGE,))


def test_triage_declares_the_documented_shape() -> None:
    assert TRIAGE.id == "triage"
    assert TRIAGE.trigger == Trigger(kind="event", event="ticket_created")
    assert TRIAGE.default_mode == "shadow"
    assert TRIAGE.sensitive_ok is False
    assert TRIAGE.verdict_tool == toolloop.TRIAGE_VERDICT_TOOL
    assert TRIAGE.budget == Budget(max_iterations=DEFAULT_MAX_ITERATIONS)
    assert TRIAGE.title and TRIAGE.description


def test_the_catalog_is_a_stable_set_of_agent_specs() -> None:
    assert all(isinstance(s, AgentSpec) for s in CATALOG.values())
    assert CATALOG["triage"].spec_hash == CATALOG["triage"].spec_hash


# -- the joined seam: the declaration vs the running triage --------------------


class _World:
    """Just enough of kenny to build a real triage session."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def setup(self) -> None:
        self.telemetry = TelemetryStore(db_path=self.db_path)
        await self.telemetry.connect()
        self.ticket_store = TicketStore(self.db_path)
        await self.ticket_store.connect()
        self.users = UserStore(self.db_path)
        await self.users.connect()
        self.tickets = TicketService(self.ticket_store)
        registry = AgentRegistry(tokens={HOST: "t"})
        tunnel = AgentTunnel(registry, self.telemetry, EventStore(db_path=self.db_path))
        executor = ToolExecutor(
            registry=registry,
            store=self.telemetry,
            tunnel=tunnel,
            call_log=CallLog(),
            screenshots=ScreenshotStore(),
        )
        self.assistant = TicketAssistant(
            tickets=self.tickets,
            users=self.users,
            executor=executor,
            client=object(),
            model="fake-model",
        )

    async def close(self) -> None:
        for store in (self.telemetry, self.ticket_store, self.users):
            await store.close()


@pytest.fixture
async def world(tmp_path):
    w = _World(str(tmp_path / "kenny.sqlite"))
    await w.setup()
    yield w
    await w.close()


async def _triage_session(world: _World):
    ticket = await world.tickets.create(
        title=f"{HOST} health: crit",
        origin="alert",
        requester_user_id=None,
        agent_id=HOST,
        category="alert",
        summary="reliability: crit",
        actor="system",
    )
    session = await world.assistant.triage_session_for(ticket)
    assert session is not None
    return session


def test_the_declared_tools_are_the_ones_triage_is_handed() -> None:
    assert CATALOG["triage"].tools == ticket_assistant.TRIAGE_TOOLS


def test_the_declared_prompt_is_the_triage_prompt() -> None:
    assert CATALOG["triage"].prompt == ticket_assistant._TRIAGE_SYSTEM_PROMPT


def test_the_declared_budget_is_the_one_triage_enforces() -> None:
    assert CATALOG["triage"].budget.max_iterations == DEFAULT_MAX_ITERATIONS


async def test_a_real_triage_session_stays_inside_the_declared_tools(world: _World) -> None:
    session = await _triage_session(world)
    assert session.triage is True
    assert session.allowed_tools, "an empty tool set would make the subset check vacuous"
    assert session.allowed_tools <= CATALOG["triage"].tools


async def test_the_real_triage_prompt_is_the_declared_one(world: _World) -> None:
    session = await _triage_session(world)
    policy = TicketPolicy(world.tickets, session)
    blocks = policy.system_blocks(session)
    assert blocks[0]["text"] == CATALOG["triage"].prompt

    # And the support prompt is not: a session that is not triage must not be
    # described by the triage declaration.
    session.triage = False
    assert policy.system_blocks(session)[0]["text"] != CATALOG["triage"].prompt


async def test_the_verdict_tool_is_offered_to_the_real_session(world: _World) -> None:
    session = await _triage_session(world)
    verdict = CATALOG["triage"].verdict_tool
    assert verdict is not None
    assert verdict in session.allowed_tools

"""The agent runner: the one place a specialized agent's run starts (ADR-0071).

What these tests pin down, each of which is a seam between two halves that
must agree:

* **Triage's mode is its settings.** ``mode_of("triage")`` is derived from
  ``KENNY_TRIAGE_ENABLED``/``KENNY_TRIAGE_RESOLVE`` and the AI availability
  behind them, and ``set_mode("triage", …)`` writes those settings — so the
  dashboard's settings page and the agents API can never disagree.
* **The bounds come before the model.** The global switch, an agent-caused
  ticket and both global caps are checked before any model call; the
  assertions are on the fake client's call list, not on what the runner says.
* **A run is recorded joined.** The triage run goes through the real
  ``TriageService`` -> ``TicketAssistant`` -> ``drive_events`` -> real
  ``ToolExecutor`` -> ``CallLog`` on a real ``EventStore``, so "the audit row
  carries the run id" means the id travelled the whole way. ``run_generic`` goes
  through the real ``AgentPolicy`` and ``drive_events`` with only the tunnel's
  wire send replaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from kenny_server.agents.catalog import CATALOG
from kenny_server.agents.policy import AgentSession
from kenny_server.agents.runner import (
    DAILY_TOKENS_SETTING,
    ENABLED_SETTING,
    MAX_CONCURRENT_SETTING,
    TICKET_CREATED_TRIGGER,
    AgentRunner,
    _redacted,
    caused_by_agent,
)
from kenny_server.agents.spec import AgentSpec, ArgConstraint, Trigger
from kenny_server.agents.store import INTERRUPTED_ERROR, AgentStore
from kenny_server.ai import AiAccess
from kenny_server.config import Settings
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, SettingsStore, TelemetryStore
from kenny_server.ticket_assistant import TicketAssistant
from kenny_server.ticketstore import AGENT_ORIGIN, TRIAGE_ACTOR, TicketStore
from kenny_server.tickets import TicketService
from kenny_server.tool_classes import STANDARD_CHANGE
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.toolloop import TRIAGE_VERDICT_TOOL, ToolExecutor
from kenny_server.triage import AUDIT_ACTOR, TriageService
from kenny_server.tunnel import AgentTunnel

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

HOST = "thomas-pc"
FIREFOX = "Mozilla.Firefox"
KEY_ENV = {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"}


# -- fakes ---------------------------------------------------------------------


class _UsageResponse(_Response):
    """A scripted model message whose final message carries ``usage``."""

    def __init__(self, content: list[Any], stop_reason: str, i: int = 0, o: int = 0) -> None:
        super().__init__(content, stop_reason)
        self.usage = SimpleNamespace(
            input_tokens=i,
            output_tokens=o,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


def _tool(tool_id: str, name: str, args: dict[str, Any], i: int = 100, o: int = 10) -> _Response:
    return _UsageResponse([tool_use_block(tool_id, name, args)], "tool_use", i, o)


def _text(text: str, i: int = 100, o: int = 10) -> _Response:
    return _UsageResponse([text_block(text)], "end_turn", i, o)


def _verdict(verdict: str = "phantom") -> _Response:
    return _tool(
        "v1",
        TRIAGE_VERDICT_TOOL,
        {
            "verdict": verdict,
            "finding": "the device the message names is not on this PC",
            "evidence": "diag_services returned no such device",
        },
    )


def _settings(env: dict[str, str] | None = None, store: Any = None) -> Settings:
    return Settings(store, env={**KEY_ENV, **(env or {})})


def _patcher(**overrides: Any) -> AgentSpec:
    """A test-only agent of the kind Phase 2 adds: one constrained change."""

    fields: dict[str, Any] = {
        "id": "patcher",
        "title": "Patcher",
        "description": "Applies one pending package update.",
        "prompt": "You keep this machine's packages up to date.",
        "trigger": Trigger(kind="on_demand"),
        "tools": frozenset({"winget_list", "winget_update"}),
        "constraints": (ArgConstraint("winget_update", "id", frozenset({FIREFOX})),),
    }
    fields.update(overrides)
    return AgentSpec(**fields)


def _catalog(*specs: AgentSpec) -> Any:
    return MappingProxyType({"triage": CATALOG["triage"], **{s.id: s for s in specs}})


# -- the world -----------------------------------------------------------------


class World:
    """Real stores, real ticket service and assistant; only the wire send is fake."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.sent: list[dict[str, Any]] = []

    async def setup(self, env: dict[str, str] | None = None) -> None:
        self.settings_store = SettingsStore(self.db_path)
        await self.settings_store.connect()
        self.settings = _settings(env, self.settings_store)
        await self.settings.load()
        self.ai = AiAccess(self.settings)
        self.telemetry = TelemetryStore(db_path=self.db_path)
        await self.telemetry.connect()
        self.event_store = EventStore(db_path=self.db_path)
        await self.event_store.connect()
        self.ticket_store = TicketStore(self.db_path)
        await self.ticket_store.connect()
        from kenny_server.userstore import UserStore

        self.users = UserStore(self.db_path)
        await self.users.connect()
        self.agent_store = AgentStore(self.db_path)
        await self.agent_store.connect()
        self.tickets = TicketService(self.ticket_store)
        self.registry = AgentRegistry(tokens={HOST: "t"})
        self.tunnel = AgentTunnel(self.registry, self.telemetry, self.event_store)

        async def send_request(agent_id: str, tool: str, args: dict[str, Any], timeout_s: float):
            self.sent.append({"agent_id": agent_id, "tool": tool, "args": dict(args)})
            return {"ok": True, "tool": tool, "services": [], "packages": [], "log": "done"}

        self.tunnel.send_request = send_request  # type: ignore[method-assign]
        self.call_log = CallLog(event_store=self.event_store)
        self.executor = ToolExecutor(
            registry=self.registry,
            store=self.telemetry,
            tunnel=self.tunnel,
            call_log=self.call_log,
            screenshots=ScreenshotStore(),
        )

    async def close(self) -> None:
        for store in (
            self.agent_store,
            self.users,
            self.ticket_store,
            self.event_store,
            self.telemetry,
            self.settings_store,
        ):
            await store.close()

    def runner(
        self, *scripted: _Response, catalog: Any = CATALOG, resolve_enabled: bool | None = None
    ) -> AgentRunner:
        self.client = FakeAnthropic(list(scripted))
        assistant = TicketAssistant(
            tickets=self.tickets,
            users=self.users,
            executor=self.executor,
            client=self.client,
            model="fake-model",
        )
        if resolve_enabled is None:
            resolve_enabled = bool(self.settings.get("KENNY_TRIAGE_RESOLVE"))
        self.triage = TriageService(
            tickets=self.tickets, assistant=assistant, resolve_enabled=resolve_enabled
        )
        self.triage.register(self.executor)
        return AgentRunner(
            store=self.agent_store,
            settings=self.settings,
            ai_access=self.ai,
            triage=self.triage,
            catalog=catalog,
            event_store=self.event_store,
        )

    async def alert_ticket(self, *, origin: str = "alert", agent_id: str | None = HOST):
        return await self.tickets.create(
            title=f"{HOST} health: crit",
            origin=origin,
            agent_id=agent_id,
            category="alert",
            summary="reliability: crit",
            actor="system",
        )


@pytest.fixture
async def world(tmp_path):
    w = World(str(tmp_path / "runner.sqlite"))
    await w.setup()
    yield w
    await w.close()


async def _world_with(tmp_path, env: dict[str, str]) -> World:
    w = World(str(tmp_path / "runner-env.sqlite"))
    await w.setup(env)
    return w


# -- identity ------------------------------------------------------------------


def test_triage_audit_actor_is_the_catalog_agent_identity() -> None:
    # The triage session sets its audit actor itself (no import of the catalog
    # from triage.py, which the catalog imports); joined here.
    spec = CATALOG["triage"]
    assert AUDIT_ACTOR == f"agent:{spec.id}"
    assert AgentSession(id="r", spec=spec, mode="shadow").audit_actor == AUDIT_ACTOR
    assert AUDIT_ACTOR != TRIAGE_ACTOR  # the ticket trail keeps its own actor


def test_an_agent_origin_ticket_is_caused_by_an_agent() -> None:
    assert AGENT_ORIGIN == "agent"
    assert caused_by_agent(SimpleNamespace(origin=AGENT_ORIGIN))  # type: ignore[arg-type]
    for origin in ("alert", "discord", "dashboard"):
        assert not caused_by_agent(SimpleNamespace(origin=origin))  # type: ignore[arg-type]


def test_run_entries_are_redacted() -> None:
    entries = [{"tool": "account_create", "args": {"name": "kid", "password": "hunter2"}}]
    assert _redacted(entries) == [
        {"tool": "account_create", "args": {"name": "kid", "password": "[redacted]"}}
    ]
    assert entries[0]["args"]["password"] == "hunter2"  # input untouched


# -- modes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "shadow"),  # the defaults: enabled, resolve off
        ({"KENNY_TRIAGE_RESOLVE": "1"}, "act"),
        ({"KENNY_TRIAGE_ENABLED": "0"}, "off"),
        ({"KENNY_TRIAGE_ENABLED": "0", "KENNY_TRIAGE_RESOLVE": "1"}, "off"),
        ({"KENNY_TRIAGE_ENABLED": "1", "KENNY_TRIAGE_RESOLVE": "0"}, "shadow"),
        ({"KENNY_TRIAGE_ENABLED": "1", "KENNY_TRIAGE_RESOLVE": "1"}, "act"),
        # AI unavailable: no key and no gateway, or the master switch off.
        ({"ANTHROPIC_API_KEY": ""}, "off"),
        ({"ANTHROPIC_API_KEY": "", "KENNY_TRIAGE_RESOLVE": "1"}, "off"),
        ({"ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": "http://gateway:8080"}, "shadow"),
        ({"KENNY_AI_ENABLED": "0", "KENNY_TRIAGE_RESOLVE": "1"}, "off"),
    ],
)
async def test_triage_mode_is_derived_from_its_settings(
    env: dict[str, str], expected: str
) -> None:
    settings = _settings(env)
    runner = AgentRunner(store=AgentStore(":memory:"), settings=settings, ai_access=AiAccess(settings))
    # Derived only: the store is never read for triage (it is not even connected).
    assert await runner.mode_of("triage") == expected


async def test_set_mode_for_triage_writes_its_settings(world: World) -> None:
    runner = world.runner()
    order: list[str] = []
    world.settings.on_change("KENNY_TRIAGE_RESOLVE", lambda _v: order.append("resolve"))
    world.settings.on_change("KENNY_TRIAGE_ENABLED", lambda _v: order.append("enabled"))

    assert await runner.set_mode("triage", "act", actor="admin") == "act"
    assert world.settings.get("KENNY_TRIAGE_ENABLED") is True
    assert world.settings.get("KENNY_TRIAGE_RESOLVE") is True
    # Resolve first, so a ticket created between the writes is never run in
    # the mode being left.
    assert order == ["resolve", "enabled"]
    # Persisted through the settings store the dashboard writes to.
    assert (await world.settings_store.all())["KENNY_TRIAGE_RESOLVE"] == "1"

    assert await runner.set_mode("triage", "shadow", actor="admin") == "shadow"
    assert world.settings.get("KENNY_TRIAGE_RESOLVE") is False
    assert await runner.set_mode("triage", "off", actor="admin") == "off"
    assert world.settings.get("KENNY_TRIAGE_ENABLED") is False
    assert await runner.mode_of("triage") == "off"
    assert await runner.set_mode("triage", "shadow", actor="admin") == "shadow"
    # No agent_settings row: the settings are triage's only source of truth.
    assert await world.agent_store.get_mode("triage") is None

    # Every change is on the event log with who made it.
    logs = [
        e for e in await world.event_store.query(kind="log") if e.get("target") == "kenny.agents"
    ]
    assert [(e["fields"]["mode"], e["fields"]["actor"]) for e in reversed(logs)] == [
        ("act", "admin"),
        ("shadow", "admin"),
        ("off", "admin"),
        ("shadow", "admin"),
    ]


async def test_set_mode_for_a_store_backed_agent(world: World) -> None:
    runner = world.runner(catalog=_catalog(_patcher()))
    assert await runner.mode_of("patcher") == "shadow"  # the spec's default
    assert await runner.set_mode("patcher", "act", actor="admin") == "act"
    assert await world.agent_store.get_mode("patcher") == "act"
    assert await runner.mode_of("patcher") == "act"
    async with world.agent_store._conn.execute(
        "SELECT updated_by FROM agent_settings WHERE agent_id = 'patcher'"
    ) as cur:
        assert (await cur.fetchone())["updated_by"] == "admin"
    # Writing a store-backed agent touches no triage setting.
    assert world.settings.get("KENNY_TRIAGE_RESOLVE") is False


async def test_set_mode_refuses_an_unknown_agent_or_mode(world: World) -> None:
    runner = world.runner()
    with pytest.raises(KeyError):
        await runner.mode_of("nope")
    with pytest.raises(KeyError):
        await runner.set_mode("nope", "act", actor="admin")
    with pytest.raises(ValueError):
        await runner.set_mode("triage", "ACT", actor="admin")
    assert world.settings.get("KENNY_TRIAGE_RESOLVE") is False


# -- triage on a new ticket ----------------------------------------------------


async def _runs(world: World) -> list[Any]:
    return await world.agent_store.list_runs(limit=50)


async def test_a_new_ticket_records_a_completed_triage_run(world: World) -> None:
    runner = world.runner(
        _tool("t1", "diag_services", {}, i=120, o=15),
        _verdict(),
        _text("done", i=80, o=5),
    )
    ticket = await world.alert_ticket()
    await runner.on_ticket_created(ticket)

    [run] = await _runs(world)
    assert run.status == "completed" and run.error is None
    assert run.agent_id == "triage"
    assert run.spec_hash == CATALOG["triage"].spec_hash
    assert run.trigger == TICKET_CREATED_TRIGGER
    assert run.subject == f"ticket:{ticket.id}"
    assert (run.host_id, run.ticket_id) == (HOST, ticket.id)
    assert run.mode == "shadow"
    assert run.verdict == "phantom"
    # 120+100+80 in, 15+10+5 out, from the usage on each final message.
    assert (run.input_tokens, run.output_tokens) == (300, 30)
    assert run.finished_at is not None
    # Shadow: the verdict is recorded, the ticket is not resolved.
    after = await world.ticket_store.get(ticket.id)
    assert after is not None and after.state != "resolved"


async def test_an_act_triage_run_resolves_through_the_unchanged_gate(tmp_path) -> None:
    w = await _world_with(tmp_path, {"KENNY_TRIAGE_RESOLVE": "1"})
    try:
        runner = w.runner(_tool("t1", "diag_services", {}), _verdict(), _text("done"))
        ticket = await w.alert_ticket()
        await runner.on_ticket_created(ticket)
        [run] = await _runs(w)
        assert (run.mode, run.status, run.verdict) == ("act", "completed", "phantom")
        after = await w.ticket_store.get(ticket.id)
        assert after is not None and after.state == "resolved"
        assert after.resolved_by == TRIAGE_ACTOR
    finally:
        await w.close()


async def test_a_run_started_in_shadow_does_not_resolve_when_promoted_mid_run(
    world: World,
) -> None:
    # resolve=False at start; the live switch on afterwards changes nothing.
    runner = world.runner(
        _tool("t1", "diag_services", {}), _verdict(), _text("done"), resolve_enabled=True
    )
    ticket = await world.alert_ticket()
    await runner.on_ticket_created(ticket)
    [run] = await _runs(world)
    assert run.mode == "shadow"
    after = await world.ticket_store.get(ticket.id)
    assert after is not None and after.state != "resolved"


async def test_triage_calls_are_audited_as_the_agent_with_the_run_id(world: World) -> None:
    runner = world.runner(_tool("t1", "diag_services", {}), _verdict(), _text("done"))
    ticket = await world.alert_ticket()
    await runner.on_ticket_created(ticket)

    [run] = await _runs(world)
    assert world.sent == [{"agent_id": HOST, "tool": "diag_services", "args": {}}]
    audit = await world.call_log.list()
    assert [(a["tool"], a["actor"], a["run_id"]) for a in audit] == [
        ("diag_services", AUDIT_ACTOR, run.id)
    ]
    # The ticket trail still names triage, as it always has.
    notes = await world.ticket_store.list_events(ticket.id, kind="note")
    assert any(e.actor == TRIAGE_ACTOR for e in notes)


async def test_a_failed_triage_run_is_recorded_as_failed(world: World) -> None:
    runner = world.runner()  # nothing scripted: the fake client raises
    ticket = await world.alert_ticket()
    await runner.on_ticket_created(ticket)  # must not raise
    [run] = await _runs(world)
    assert run.status == "failed" and run.error
    assert run.verdict is None
    after = await world.ticket_store.get(ticket.id)
    assert after is not None


async def test_an_agent_origin_ticket_starts_nothing(world: World) -> None:
    runner = world.runner(_verdict(), _text("done"))
    ticket = await world.alert_ticket(origin=AGENT_ORIGIN)
    await runner.on_ticket_created(ticket)
    assert await _runs(world) == []
    assert world.client.messages.calls == []


async def test_a_ticket_without_a_machine_starts_nothing(world: World) -> None:
    runner = world.runner(_verdict(), _text("done"))
    await runner.on_ticket_created(await world.alert_ticket(agent_id=None))
    assert await _runs(world) == []
    assert world.client.messages.calls == []


async def test_the_global_switch_off_starts_nothing(tmp_path) -> None:
    w = await _world_with(tmp_path, {ENABLED_SETTING: "0"})
    try:
        runner = w.runner(_verdict(), _text("done"))
        await runner.on_ticket_created(await w.alert_ticket())
        assert await _runs(w) == []
        assert w.client.messages.calls == []
    finally:
        await w.close()


async def test_triage_off_starts_nothing(tmp_path) -> None:
    w = await _world_with(tmp_path, {"KENNY_TRIAGE_ENABLED": "0"})
    try:
        runner = w.runner(_verdict(), _text("done"))
        await runner.on_ticket_created(await w.alert_ticket())
        assert await _runs(w) == []
        assert w.client.messages.calls == []
    finally:
        await w.close()


async def test_the_concurrency_cap_records_a_skipped_run_and_calls_no_model(tmp_path) -> None:
    w = await _world_with(tmp_path, {MAX_CONCURRENT_SETTING: "1"})
    try:
        runner = w.runner(_verdict(), _text("done"))
        busy = await w.agent_store.start_run(
            agent_id="other", spec_hash="h", trigger="on_demand", mode="shadow"
        )
        ticket = await w.alert_ticket()
        await runner.on_ticket_created(ticket)

        assert w.client.messages.calls == []
        skipped = [r for r in await _runs(w) if r.id != busy.id]
        assert [(r.status, r.ticket_id) for r in skipped] == [("skipped", ticket.id)]
        assert "limit is 1" in (skipped[0].error or "")
        assert (await w.agent_store.get_run(busy.id)).status == "running"  # type: ignore[union-attr]
    finally:
        await w.close()


async def test_the_daily_token_cap_records_a_skipped_run_and_calls_no_model(tmp_path) -> None:
    w = await _world_with(tmp_path, {DAILY_TOKENS_SETTING: "100"})
    try:
        runner = w.runner(_verdict(), _text("done"))
        spent = await w.agent_store.start_run(
            agent_id="triage", spec_hash="h", trigger="on_demand", mode="shadow"
        )
        await w.agent_store.finish_run(
            spent.id, status="completed", usage={"input_tokens": 90, "output_tokens": 10}
        )
        await runner.on_ticket_created(await w.alert_ticket())

        assert w.client.messages.calls == []
        latest = (await _runs(w))[0]
        assert latest.status == "skipped"
        assert "cap is 100" in (latest.error or "")
    finally:
        await w.close()


async def test_a_token_cap_of_zero_is_no_cap(tmp_path) -> None:
    w = await _world_with(tmp_path, {DAILY_TOKENS_SETTING: "0"})
    try:
        spent = await w.agent_store.start_run(
            agent_id="triage", spec_hash="h", trigger="on_demand", mode="shadow"
        )
        await w.agent_store.finish_run(
            spent.id, status="completed", usage={"input_tokens": 10**9}
        )
        runner = w.runner(_tool("t1", "diag_services", {}), _verdict(), _text("done"))
        await runner.on_ticket_created(await w.alert_ticket())
        assert (await _runs(w))[0].status == "completed"
    finally:
        await w.close()


# -- startup -------------------------------------------------------------------


async def test_startup_fails_interrupted_runs_and_prunes_old_ones(world: World) -> None:
    runner = world.runner()
    stale = await world.agent_store.start_run(
        agent_id="triage", spec_hash="h", trigger="t", mode="shadow"
    )
    old = await world.agent_store.start_run(
        agent_id="triage", spec_hash="h", trigger="t", mode="shadow"
    )
    await world.agent_store.finish_run(old.id, status="completed")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    await world.agent_store._conn.execute(
        "UPDATE agent_runs SET started_at = ? WHERE id = ?", (long_ago, old.id)
    )
    await world.agent_store._conn.commit()

    await runner.startup()

    assert await world.agent_store.get_run(old.id) is None
    after = await world.agent_store.get_run(stale.id)
    assert after is not None and (after.status, after.error) == ("failed", INTERRUPTED_ERROR)


# -- run_generic: the real gate and the real loop ------------------------------


async def _generic(world: World, runner: AgentRunner, spec: AgentSpec, *calls: tuple[str, dict]):
    scripted = [_tool(f"tu{i}", name, args) for i, (name, args) in enumerate(calls)]
    scripted.append(_text("done"))
    client = FakeAnthropic(scripted)
    run = await runner.run_generic(
        spec,
        host_id=HOST,
        trigger="on_demand",
        brief="Update what may be updated.",
        client=client,
        model="test-model",
        executor=world.executor,
    )
    return run, client


async def test_run_generic_in_shadow_recommends_and_never_reaches_the_host(world: World) -> None:
    spec = _patcher()
    runner = world.runner(catalog=_catalog(spec))
    run, client = await _generic(
        world, runner, spec, ("winget_list", {}), ("winget_update", {"id": FIREFOX})
    )

    assert run is not None
    assert world.sent == [{"agent_id": HOST, "tool": "winget_list", "args": {}}]
    assert (run.status, run.mode, run.agent_id) == ("completed", "shadow", "patcher")
    assert (run.trigger, run.subject, run.host_id) == ("on_demand", f"host:{HOST}", HOST)
    assert run.spec_hash == spec.spec_hash
    assert run.actions == []
    assert run.recommendations == [
        {"tool": "winget_update", "args": {"id": FIREFOX}, "agent_id": HOST,
         "tool_class": STANDARD_CHANGE}
    ]
    assert (run.input_tokens, run.output_tokens) == (300, 30)
    assert run.summary == "done"
    assert client.messages.calls[0]["system"][0]["text"] == spec.prompt
    # The read it did make is audited as the agent, under this run.
    assert [(a["tool"], a["actor"], a["run_id"]) for a in await world.call_log.list()] == [
        ("winget_list", "agent:patcher", run.id)
    ]


async def test_run_generic_in_act_runs_an_allowed_constrained_change(world: World) -> None:
    spec = _patcher()
    runner = world.runner(catalog=_catalog(spec))
    await runner.set_mode("patcher", "act", actor="admin")
    run, _ = await _generic(
        world,
        runner,
        spec,
        ("winget_update", {"id": FIREFOX}),
        ("winget_update", {"id": "Evil.Package"}),
    )

    assert run is not None and (run.status, run.mode) == ("completed", "act")
    assert world.sent == [{"agent_id": HOST, "tool": "winget_update", "args": {"id": FIREFOX}}]
    assert run.actions == [
        {"tool": "winget_update", "args": {"id": FIREFOX}, "agent_id": HOST,
         "tool_class": STANDARD_CHANGE, "ok": True}
    ]
    # Outside its constraints is neither an action nor a recommendation.
    assert run.recommendations == []


async def test_run_generic_off_or_globally_off_starts_nothing(tmp_path) -> None:
    spec = _patcher()
    w = await _world_with(tmp_path, {ENABLED_SETTING: "0"})
    try:
        runner = w.runner(catalog=_catalog(spec))
        run, client = await _generic(w, runner, spec, ("winget_list", {}))
        assert run is None and client.messages.calls == []

        await w.settings.set(ENABLED_SETTING, "1")
        await runner.set_mode("patcher", "off", actor="admin")
        run, client = await _generic(w, runner, spec, ("winget_list", {}))
        assert run is None and client.messages.calls == []
        assert await _runs(w) == []
        assert w.sent == []
    finally:
        await w.close()


async def test_run_generic_respects_the_caps(tmp_path) -> None:
    spec = _patcher()
    w = await _world_with(tmp_path, {MAX_CONCURRENT_SETTING: "1"})
    try:
        runner = w.runner(catalog=_catalog(spec))
        await w.agent_store.start_run(
            agent_id="triage", spec_hash="h", trigger="t", mode="shadow"
        )
        run, client = await _generic(w, runner, spec, ("winget_list", {}))
        assert run is not None and run.status == "skipped"
        assert client.messages.calls == [] and w.sent == []
    finally:
        await w.close()


async def test_run_generic_records_a_failure(world: World) -> None:
    spec = _patcher()
    runner = world.runner(catalog=_catalog(spec))
    run = await runner.run_generic(
        spec,
        host_id=HOST,
        trigger="on_demand",
        brief="go",
        client=FakeAnthropic([]),  # raises on the first call
        model="test-model",
        executor=world.executor,
    )
    assert run is not None and run.status == "failed" and run.error


async def test_run_generic_refuses_a_spec_it_must_not_run(world: World) -> None:
    spec = _patcher()
    runner = world.runner(catalog=_catalog(spec))
    kwargs: dict[str, Any] = {
        "host_id": HOST,
        "trigger": "on_demand",
        "brief": "go",
        "client": FakeAnthropic([]),
        "model": "m",
        "executor": world.executor,
    }
    with pytest.raises(ValueError):  # the catalog says something else
        await runner.run_generic(_patcher(prompt="Update everything."), **kwargs)
    with pytest.raises(ValueError):  # triage runs on its ticket, not here
        await runner.run_generic(CATALOG["triage"], **kwargs)
    assert await _runs(world) == []


async def test_overview_lists_every_catalog_agent_with_mode_and_latest_run(world: World) -> None:
    spec = _patcher()
    runner = world.runner(
        _tool("t1", "diag_services", {}), _verdict(), _text("done"), catalog=_catalog(spec)
    )
    await runner.on_ticket_created(await world.alert_ticket())
    overview = {a["id"]: a for a in await runner.overview()}
    assert set(overview) == {"triage", "patcher"}
    assert overview["triage"]["mode"] == "shadow"
    assert overview["triage"]["spec_hash"] == CATALOG["triage"].spec_hash
    assert overview["triage"]["latest_run"]["verdict"] == "phantom"
    assert overview["patcher"]["latest_run"] is None

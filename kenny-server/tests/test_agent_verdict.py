"""The verdict tool of a specialized agent run (ADR-0071): what a verdict does.

Unit tests call the handler with an :class:`AgentSession`; the joined tests drive
the real tool loop and gate (``drive_events`` + ``AgentPolicy`` + a real
``ToolExecutor``) so "a shadow run opens a ticket" means the gate let the verdict
through and the registered handler ran — not that the handler works when called
by hand. The last tests join the ticket it opens to the runner's rule that an
agent's effect never starts an agent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kenny_server.agents.catalog import CATALOG
from kenny_server.agents.policy import AgentPolicy, AgentSession
from kenny_server.agents.runner import AgentRunner
from kenny_server.agents.store import AgentStore
from kenny_server.agents.verdict import AgentVerdictService, dedup_key, register
from kenny_server.alert_subject import parse as parse_alert_key
from kenny_server.config import Settings
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.ticketstore import AGENT_ORIGIN, TicketStore
from kenny_server.tickets import TicketService
from kenny_server.toolloop import (
    AGENT_VERDICT_TOOL,
    AGENT_VERDICTS,
    SERVER_TOOLS,
    ToolExecutor,
    drive_events,
)
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel, ToolError

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

HOST = "thomas-pc"
OTHER = "bob-pc"
FINDING = "A remote-access service nobody installed starts with Windows."
EVIDENCE = "diag_services listed 'AnyDeskSvc' running from a temp folder."


class Rig:
    """Real ticket service, real executor and tunnel; only the wire is absent."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def setup(self) -> None:
        self.telemetry = TelemetryStore(db_path=self.db_path)
        await self.telemetry.connect()
        self.event_store = EventStore(db_path=self.db_path)
        await self.event_store.connect()
        self.ticket_store = TicketStore(self.db_path)
        await self.ticket_store.connect()
        self.tickets = TicketService(self.ticket_store)
        registry = AgentRegistry(tokens={HOST: "t1", OTHER: "t2"})
        tunnel = AgentTunnel(registry, self.telemetry, self.event_store)
        self.executor = ToolExecutor(
            registry=registry,
            store=self.telemetry,
            tunnel=tunnel,
            call_log=CallLog(),
            screenshots=ScreenshotStore(),
        )
        self.service = register(self.executor, tickets=self.tickets)

    async def close(self) -> None:
        for store in (self.ticket_store, self.event_store, self.telemetry):
            await store.close()

    def session(self, agent: str = "posture", mode: str = "shadow", host: str | None = HOST):
        return AgentSession(id=f"run-{agent}-{host}-{mode}", spec=CATALOG[agent], mode=mode, agent_id=host)

    async def agent_tickets(self) -> list[Any]:
        return [t for t in await self.ticket_store.list(limit=500) if t.origin == AGENT_ORIGIN]


@pytest.fixture
async def rig(tmp_path):
    r = Rig(str(tmp_path / "verdict.sqlite"))
    await r.setup()
    yield r
    await r.close()


def _args(verdict: str, finding: str = FINDING, evidence: str = EVIDENCE) -> dict[str, str]:
    return {"verdict": verdict, "finding": finding, "evidence": evidence}


# -- the handler ---------------------------------------------------------------


def test_the_handler_covers_every_verdict_the_tool_offers() -> None:
    # The enum the model sees and the set the handler accepts are one tuple.
    assert tuple(SERVER_TOOLS[AGENT_VERDICT_TOOL]["properties"]["verdict"]["enum"]) == AGENT_VERDICTS


async def test_every_offered_verdict_is_accepted(rig: Rig) -> None:
    for verdict in AGENT_VERDICTS:
        session = rig.session(host=f"h-{verdict}")
        result = await rig.service.record_verdict(_args(verdict), session=session)
        assert result["recorded"] is True and result["verdict"] == verdict
        assert session.verdict["verdict"] == verdict


async def test_an_unknown_verdict_is_a_malformed_answer(rig: Rig) -> None:
    session = rig.session()
    with pytest.raises(ToolError) as caught:
        await rig.service.record_verdict(_args("phantom"), session=session)
    assert caught.value.code == "bad_verdict"
    assert getattr(session, "verdict", None) is None
    assert await rig.agent_tickets() == []


async def test_a_call_outside_an_agent_run_is_refused(rig: Rig) -> None:
    with pytest.raises(ToolError) as caught:
        await rig.service.record_verdict(_args("actionable"), session=None)
    assert caught.value.code == "no_run"
    assert await rig.agent_tickets() == []


async def test_a_run_reports_once(rig: Rig) -> None:
    session = rig.session()
    await rig.service.record_verdict(_args("clean"), session=session)
    with pytest.raises(ToolError) as caught:
        await rig.service.record_verdict(_args("actionable"), session=session)
    assert caught.value.code == "already_recorded"
    assert session.verdict["verdict"] == "clean"
    assert await rig.agent_tickets() == []


async def test_the_text_is_clipped(rig: Rig) -> None:
    session = rig.session()
    await rig.service.record_verdict(_args("clean", "x" * 10_000, "y" * 10_000), session=session)
    assert len(session.verdict["finding"]) <= 2000
    assert len(session.verdict["evidence"]) <= 2000


@pytest.mark.parametrize("verdict", ["clean", "acted", "inconclusive"])
async def test_only_an_actionable_verdict_opens_a_ticket(rig: Rig, verdict: str) -> None:
    # ``acted`` does not: the run record says what changed, and a ticket per
    # routine update night is the noise tickets are meant not to be.
    session = rig.session(agent="patch", mode="act")
    result = await rig.service.record_verdict(_args(verdict), session=session)
    assert "ticket" not in result
    assert session.verdict["ticket_id"] is None
    assert await rig.agent_tickets() == []


@pytest.mark.parametrize("mode", ["shadow", "act"])
async def test_an_actionable_verdict_opens_an_agent_ticket_in_either_mode(
    rig: Rig, mode: str
) -> None:
    session = rig.session(mode=mode)
    result = await rig.service.record_verdict(_args("actionable"), session=session)
    [ticket] = await rig.agent_tickets()
    assert result["ticket"] == f"#{ticket.number}"
    assert ticket.origin == AGENT_ORIGIN == "agent"
    assert ticket.agent_id == HOST
    assert ticket.requester_user_id is None
    assert ticket.dedup_key == dedup_key("posture", HOST)
    assert FINDING in ticket.title and ticket.title.startswith("Posture review on thomas-pc")
    assert FINDING in ticket.summary and EVIDENCE in ticket.summary
    assert session.id in ticket.summary
    assert session.verdict["ticket_id"] == ticket.id


async def test_the_ticket_is_opened_by_the_agent_in_its_run_not_by_system(rig: Rig) -> None:
    session = rig.session()
    await rig.service.record_verdict(_args("actionable"), session=session)
    [ticket] = await rig.agent_tickets()
    [genesis] = [e for e in await rig.tickets.events(ticket.id) if e.kind == "state"]
    assert genesis.actor == "agent:posture"
    assert genesis.fields["run"] == session.id
    assert (genesis.fields["origin"], genesis.fields["agent_id"]) == (AGENT_ORIGIN, HOST)


async def test_the_ticket_title_is_one_bounded_line(rig: Rig) -> None:
    session = rig.session()
    await rig.service.record_verdict(
        _args("actionable", "first line\nsecond line " + "z" * 500), session=session
    )
    [ticket] = await rig.agent_tickets()
    assert "\n" not in ticket.title and len(ticket.title) <= 160


async def test_an_agent_key_is_not_an_alert_key() -> None:
    assert parse_alert_key(dedup_key("posture", HOST)) is None


async def test_a_shadow_ticket_lists_what_the_run_only_proposed(rig: Rig) -> None:
    session = rig.session(agent="patch", mode="shadow")
    session.recommendations.append(
        {"tool": "winget_update", "args": {"id": "Mozilla.Firefox"}, "agent_id": HOST}
    )
    await rig.service.record_verdict(_args("actionable"), session=session)
    [ticket] = await rig.agent_tickets()
    assert "proposed and did not make" in ticket.summary
    assert "winget_update(id=Mozilla.Firefox)" in ticket.summary


async def test_a_finding_while_one_is_open_is_added_to_it(rig: Rig) -> None:
    first = rig.session()
    await rig.service.record_verdict(_args("actionable"), session=first)
    second = AgentSession(id="run-two", spec=CATALOG["posture"], mode="act", agent_id=HOST)
    result = await rig.service.record_verdict(
        _args("actionable", "Still there."), session=second
    )
    [ticket] = await rig.agent_tickets()  # still exactly one
    assert result["ticket"] == f"#{ticket.number}"
    assert "already open" in result["note"]
    assert second.verdict["ticket_id"] == ticket.id
    notes = [e for e in await rig.tickets.events(ticket.id) if e.kind == "note"]
    assert [(e.actor, e.fields["run"]) for e in notes] == [("agent:posture", "run-two")]
    assert "Still there." in notes[0].summary


async def test_a_resolved_ticket_does_not_absorb_the_next_finding(rig: Rig) -> None:
    await rig.service.record_verdict(_args("actionable"), session=rig.session())
    [first] = await rig.agent_tickets()
    await rig.tickets.transition(first.id, "resolved", actor="system", reason="looked at it")
    second = AgentSession(id="run-two", spec=CATALOG["posture"], mode="shadow", agent_id=HOST)
    await rig.service.record_verdict(_args("actionable"), session=second)
    assert len(await rig.agent_tickets()) == 2


async def test_findings_are_deduplicated_per_agent_and_host(rig: Rig) -> None:
    await rig.service.record_verdict(_args("actionable"), session=rig.session())
    await rig.service.record_verdict(_args("actionable"), session=rig.session(host=OTHER))
    await rig.service.record_verdict(_args("actionable"), session=rig.session(agent="patch"))
    keys = sorted(t.dedup_key for t in await rig.agent_tickets())
    assert keys == sorted(
        [dedup_key("posture", HOST), dedup_key("posture", OTHER), dedup_key("patch", HOST)]
    )


async def test_a_ticket_that_cannot_be_opened_is_an_error_the_run_sees(rig: Rig) -> None:
    async def broken(**_: Any) -> Any:
        raise RuntimeError("database is locked")

    rig.tickets.create = broken  # type: ignore[method-assign]
    session = rig.session()
    with pytest.raises(ToolError) as caught:
        await rig.service.record_verdict(_args("actionable"), session=session)
    assert caught.value.code == "ticket_failed"
    # Nothing was reported, so the run may try again.
    assert getattr(session, "verdict", None) is None


# -- joined: the real loop and gate ---------------------------------------------


def _scripted(*calls: tuple[str, dict[str, Any]]) -> FakeAnthropic:
    scripted = [
        _Response([tool_use_block(f"tu{i}", name, args)], "tool_use")
        for i, (name, args) in enumerate(calls)
    ]
    scripted.append(_Response([text_block("done")], "end_turn"))
    return FakeAnthropic(scripted)


async def _drive(rig: Rig, session: AgentSession, client: FakeAnthropic) -> list[dict[str, Any]]:
    session.messages.append({"role": "user", "content": "start"})
    return [
        ev
        async for ev in drive_events(
            session,
            rig.executor,
            client=client,
            model="test-model",
            policy=AgentPolicy(session),
            max_iterations=session.spec.budget.max_iterations,
        )
    ]


@pytest.mark.parametrize("mode", ["shadow", "act"])
async def test_joined_a_run_reports_through_the_gate_in_either_mode(rig: Rig, mode: str) -> None:
    session = rig.session(mode=mode)
    events = await _drive(rig, session, _scripted((AGENT_VERDICT_TOOL, _args("actionable"))))
    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["tool"] == AGENT_VERDICT_TOOL and result["ok"] is True
    assert not [e for e in events if e["type"] == "denied"]
    [ticket] = await rig.agent_tickets()
    assert ticket.origin == AGENT_ORIGIN and ticket.agent_id == HOST
    assert session.verdict["verdict"] == "actionable"


async def test_joined_a_malformed_verdict_is_an_error_result_not_a_run_failure(rig: Rig) -> None:
    session = rig.session()
    events = await _drive(rig, session, _scripted((AGENT_VERDICT_TOOL, _args("nope"))))
    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["ok"] is False
    assert await rig.agent_tickets() == []


# -- joined: an agent's ticket never starts an agent -----------------------------


class _CountingTriage:
    """Stands in for ``TriageService``: counts the investigations the runner starts."""

    def __init__(self) -> None:
        self.investigated: list[str] = []
        self.still_acting: Any = None

    async def investigate(self, ticket: Any, **_: Any) -> str | None:
        self.investigated.append(ticket.id)
        return "inconclusive"


async def test_joined_the_ticket_an_agent_opens_does_not_start_triage(
    rig: Rig, tmp_path
) -> None:
    store = AgentStore(str(tmp_path / "agents.sqlite"))
    await store.connect()
    try:
        triage = _CountingTriage()
        runner = AgentRunner(
            store=store,
            settings=Settings(None, env={"ANTHROPIC_API_KEY": "sk-test"}),
            triage=triage,  # type: ignore[arg-type]
        )
        assert runner.enabled() and await runner.mode_of("triage") == "shadow"
        # The very wire main.py makes: the ticket service hands every new ticket to the runner.
        rig.tickets.set_triage(runner.on_ticket_created)

        # The control: an alert-origin ticket on the same host does start triage.
        alert = await rig.tickets.create(
            title="health: crit", origin="alert", agent_id=HOST, actor="system"
        )
        await _drain(rig.tickets)
        assert triage.investigated == [alert.id]

        # An agent's finding opens a ticket through the real verdict path ...
        session = rig.session()
        await _drive(rig, session, _scripted((AGENT_VERDICT_TOOL, _args("actionable"))))
        await _drain(rig.tickets)
        [ticket] = await rig.agent_tickets()
        assert ticket.agent_id == HOST and ticket.state == "new"
        # ... and nothing investigated it.
        assert triage.investigated == [alert.id]
        assert {r.ticket_id for r in await store.list_runs(agent_id="triage")} == {alert.id}
    finally:
        await store.close()


async def _drain(tickets: TicketService) -> None:
    import asyncio

    while tickets._triage_tasks:
        await asyncio.gather(*list(tickets._triage_tasks))


def test_the_service_registers_on_an_executor_like_triage_does() -> None:
    executor = SimpleNamespace(registered={})
    executor.register_server_tool = lambda name, fn: executor.registered.__setitem__(name, fn)
    service = AgentVerdictService(tickets=SimpleNamespace(store=object()))  # type: ignore[arg-type]
    service.register(executor)  # type: ignore[arg-type]
    assert list(executor.registered) == [AGENT_VERDICT_TOOL]

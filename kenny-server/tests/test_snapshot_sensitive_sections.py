"""``agent_snapshot`` is not a way round a sensitive tool.

A read-only call passes every gate unasked, and ``agent_snapshot`` is read-only.
Without a filter, a whole snapshot — or ``section="web_activity"`` — hands an
unattended run the browsing history that ``web_activity_query`` exists to guard
(``tool_classes.SENSITIVE_TOOLS``). These tests join the real sessions to the
real executor: the posture agent through ``run_generic`` and its gate, triage
through the session ``TicketAssistant`` builds for it, and the operator's
copilot, which must keep seeing everything.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kenny_server import toolloop
from kenny_server.agents.catalog import CATALOG
from kenny_server.agents.policy import AgentSession
from kenny_server.chat import FleetSession
from kenny_server.tool_classes import SENSITIVE_TOOLS, TOOL_CLASSES
from kenny_server.toolloop import SENSITIVE_SECTIONS, withheld_sections

from test_agent_runner import HOST, World, _text, _tool
from test_chat import FakeAnthropic

FIXTURE = Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "telemetry_snapshot.json"
SNAPSHOT: dict[str, Any] = json.loads(FIXTURE.read_text())["snapshot"]
#: A visited domain in the fixture's ``web_activity`` and nowhere else in it.
VISITED = "cdn.example.net"


def test_the_marker_is_only_in_the_browsing_section() -> None:
    assert VISITED in json.dumps(SNAPSHOT["web_activity"])
    rest = {k: v for k, v in SNAPSHOT.items() if k != "web_activity"}
    assert VISITED not in json.dumps(rest)


def test_every_guarded_section_names_a_sensitive_tool_and_a_real_section() -> None:
    assert SENSITIVE_SECTIONS, "an empty map would make every check here vacuous"
    for section, tool in SENSITIVE_SECTIONS.items():
        assert tool in SENSITIVE_TOOLS and tool in TOOL_CLASSES
        assert section in SNAPSHOT, f"{section} is not a section the agent sends"


@pytest.fixture
async def world(tmp_path):
    w = World(str(tmp_path / "snapshot.sqlite"))
    await w.setup()
    await w.telemetry.insert(HOST, "2026-10-08T03:00:00+00:00", SNAPSHOT)
    yield w
    await w.close()


def _tool_results(client: FakeAnthropic) -> str:
    """Everything the model was shown as a tool result, as one string."""

    shown: list[Any] = []
    for message in client.messages.calls[-1]["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            shown += [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert shown
    return json.dumps(shown)


# -- an unattended agent run --------------------------------------------------------


async def test_joined_a_posture_run_never_sees_browsing_history(world: World) -> None:
    runner = world.runner(catalog=CATALOG)
    client = FakeAnthropic(
        [
            _tool("t1", "agent_snapshot", {"id": HOST}),
            _tool("t2", "agent_snapshot", {"id": HOST, "section": "web_activity"}),
            _tool("t3", "agent_snapshot", {"id": HOST, "section": "local_accounts"}),
            _text("done"),
        ]
    )
    run = await runner.run_generic(
        CATALOG["posture"],
        host_id=HOST,
        trigger="schedule:test",
        brief="Review the posture.",
        client=client,
        model="test-model",
        executor=world.executor,
    )
    assert run is not None and run.status == "completed"
    shown = _tool_results(client)
    assert VISITED not in shown
    assert "withheld" in shown
    # The section it is meant to read still arrives.
    assert SNAPSHOT["local_accounts"]["summary"] in shown


async def test_an_agent_that_opted_into_the_tool_reads_the_section(world: World) -> None:
    spec = replace(
        CATALOG["posture"],
        tools=CATALOG["posture"].tools | {"web_activity_query"},
        sensitive_ok=True,
    )
    session = AgentSession(id="r", spec=spec, mode="shadow", agent_id=HOST)
    assert withheld_sections(session) == frozenset()
    got = await world.executor.run_server_tool(
        "agent_snapshot", {"id": HOST, "section": "web_activity"}, session=session
    )
    assert VISITED in json.dumps(got["payload"])


def test_sensitive_ok_alone_or_the_tool_alone_is_not_enough() -> None:
    posture = CATALOG["posture"]
    ok_only = AgentSession(id="r", spec=replace(posture, sensitive_ok=True), mode="shadow")
    assert "web_activity" in withheld_sections(ok_only)
    # A spec naming the tool without sensitive_ok never validates; the session
    # rule does not rely on that.
    tool_only = AgentSession.__new__(AgentSession)
    tool_only.spec = replace(posture, tools=posture.tools | {"web_activity_query"})
    assert "web_activity" in withheld_sections(tool_only)


# -- triage ------------------------------------------------------------------------


async def test_joined_a_triage_session_never_sees_browsing_history(world: World) -> None:
    runner = world.runner()
    assert runner.triage is not None
    ticket = await world.alert_ticket()
    session = await world.triage.assistant.triage_session_for(ticket)
    assert session is not None and "web_activity_query" not in session.allowed_tools

    whole, failed = await toolloop._execute_one(
        world.executor, "agent_snapshot", {"id": HOST}, session=session
    )
    assert not failed
    assert "web_activity" not in whole["snapshot"]
    assert whole["withheld_sections"] == ["web_activity"]
    assert "local_accounts" in whole["snapshot"]

    one, failed = await toolloop._execute_one(
        world.executor, "agent_snapshot", {"id": HOST, "section": "web_activity"}, session=session
    )
    assert not failed
    assert one["payload"] is None and "withheld" in one
    assert VISITED not in json.dumps([whole, one])


async def test_a_ticket_session_given_the_tool_reads_the_section(world: World) -> None:
    world.runner()
    session = await world.triage.assistant.triage_session_for(await world.alert_ticket())
    assert session is not None
    session.allowed_tools = session.allowed_tools | {"web_activity_query"}
    got = await world.executor.run_server_tool("agent_snapshot", {"id": HOST}, session=session)
    assert VISITED in json.dumps(got["snapshot"])


# -- a person is present: unchanged -------------------------------------------------


async def test_the_operator_copilot_still_sees_every_section(world: World) -> None:
    session = FleetSession(id="chat-1", agent_id=HOST)
    whole, failed = await toolloop._execute_one(
        world.executor, "agent_snapshot", {"id": HOST}, session=session
    )
    assert not failed and VISITED in json.dumps(whole["snapshot"])
    assert "withheld_sections" not in whole
    one = await world.executor.run_server_tool(
        "agent_snapshot", {"id": HOST, "section": "web_activity"}, session=session
    )
    assert VISITED in json.dumps(one["payload"])


async def test_no_session_is_unchanged(world: World) -> None:
    got = await world.executor.run_server_tool("agent_snapshot", {"id": HOST}, session=None)
    assert VISITED in json.dumps(got["snapshot"])

"""Chat tool-use loop tests with a FAKE Anthropic client (no real API key).

Covers the two behaviours the confirm-gate hinges on:

* a read-only tool (``fleet_overview``) auto-executes and the assistant gets a
  ``tool_result`` fed back, ending the turn with text;
* a state-changing tool (``winget_install``) does NOT execute — a pending
  confirmation is surfaced — and only runs after ``confirm_pending(approve=True)``.

The fake client scripts ``messages.create`` responses; the capability path stubs
``tunnel.send_request`` so no real agent is needed.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from kenny_server.chat import (
    ChatExecutor,
    ChatSession,
    ChatSessions,
    build_tool_schemas,
    confirm_pending,
    is_state_changing,
    run_turn,
)
from kenny_server.registry import AgentRegistry
from kenny_server.store import TelemetryStore
from kenny_server.tools import CallLog
from kenny_server.tunnel import AgentTunnel


# -- fake Anthropic client ------------------------------------------------


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


class FakeMessages:
    def __init__(self, scripted: list[_Response]) -> None:
        self._scripted = scripted
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self._scripted.pop(0)


class FakeAnthropic:
    def __init__(self, scripted: list[_Response]) -> None:
        self.messages = FakeMessages(scripted)


# -- fixtures -------------------------------------------------------------


@pytest.fixture
async def store(tmp_path) -> TelemetryStore:
    s = TelemetryStore(db_path=str(tmp_path / "chat.sqlite"))
    await s.connect()
    yield s
    await s.close()


def _executor(store: TelemetryStore) -> tuple[ChatExecutor, AgentRegistry, AgentTunnel]:
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store)
    call_log = CallLog()
    executor = ChatExecutor(
        registry=registry, store=store, tunnel=tunnel, call_log=call_log
    )
    return executor, registry, tunnel


# -- tests ----------------------------------------------------------------


def test_tool_schemas_cover_all_tools() -> None:
    from kenny_server.tools import CAPABILITY_TOOLS

    names = {t["name"] for t in build_tool_schemas()}
    for server_tool in ("list_agents", "select_agent", "fleet_overview",
                        "agent_health", "agent_snapshot"):
        assert server_tool in names
    assert set(CAPABILITY_TOOLS) <= names


def test_tool_names_match_anthropic_constraint() -> None:
    """Regression for issue #12: the Anthropic Messages API rejects tool names
    that do not match ``^[a-zA-Z0-9_-]{1,128}$`` (notably, no dots)."""

    pattern = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
    for schema in build_tool_schemas():
        name = schema["name"]
        assert "." not in name, f"tool name contains a dot: {name!r}"
        assert pattern.match(name), f"tool name violates Anthropic constraint: {name!r}"


def test_classification() -> None:
    assert not is_state_changing("fleet_overview")
    assert not is_state_changing("diag_processes")
    assert not is_state_changing("fs_read")
    assert is_state_changing("winget_install")
    assert is_state_changing("powershell_exec")
    assert is_state_changing("net_dns_flush")


async def test_read_only_tool_auto_executes(store: TelemetryStore) -> None:
    executor, _registry, _tunnel = _executor(store)
    session = ChatSession(id="s1")

    # Turn 1: model asks for fleet_overview. Turn 2: model replies with text.
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu1", "fleet_overview", {})], "tool_use"),
            _Response([text_block("The fleet is healthy.")], "end_turn"),
        ]
    )

    result = await run_turn(
        session, "How is the fleet?", executor=executor, client=client
    )

    assert result.done is True
    assert result.pending is None
    assert result.assistant_text == "The fleet is healthy."
    # The read-only tool ran and produced a tool_result event.
    assert any(e["type"] == "tool_result" and e["tool"] == "fleet_overview"
               for e in result.tool_events)

    # A tool_result was fed back to the model (user message with tool_result block).
    second_call_messages = client.messages.calls[1]["messages"]
    tool_results = [
        b
        for m in second_call_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results and tool_results[0]["tool_use_id"] == "tu1"
    # The fed-back payload is the fleet_overview result.
    payload = json.loads(tool_results[0]["content"])
    assert "overall" in payload and "agents" in payload


async def test_state_changing_tool_requires_confirmation(store: TelemetryStore) -> None:
    executor, registry, tunnel = _executor(store)
    session = ChatSession(id="s2")

    # Stub the capability path so no real agent is needed.
    sent: list[dict[str, Any]] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append({"agent_id": agent_id, "tool": tool, "args": args})
        return {"installed": True, "id": args.get("id")}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    registry._active_agent = "dev"  # an active agent is selected

    # Turn 1: model selects nothing new but asks to install. Turn 2 (after
    # confirm): model summarises.
    client = FakeAnthropic(
        [
            _Response(
                [tool_use_block("tu2", "winget_install", {"id": "Git.Git"})],
                "tool_use",
            ),
            _Response([text_block("Git is installed.")], "end_turn"),
        ]
    )

    result = await run_turn(
        session, "Install git on dev", executor=executor, client=client
    )

    # It paused: pending surfaced, NOT executed, turn not done.
    assert result.done is False
    assert result.pending is not None
    assert result.pending["tool"] == "winget_install"
    assert result.pending["args"] == {"id": "Git.Git"}
    assert result.pending["agent_id"] == "dev"
    assert sent == []  # the tunnel was never called — nothing executed
    assert session.pending is not None
    # Only one model call so far (the install was not executed nor fed back).
    assert len(client.messages.calls) == 1

    # Now the operator confirms. The tool executes and the turn resumes.
    result2 = await confirm_pending(
        session, approve=True, executor=executor, client=client
    )
    assert result2.done is True
    assert result2.pending is None
    assert result2.assistant_text == "Git is installed."
    assert len(sent) == 1 and sent[0]["tool"] == "winget_install"
    assert session.pending is None


async def test_state_changing_tool_denied(store: TelemetryStore) -> None:
    executor, registry, tunnel = _executor(store)
    session = ChatSession(id="s3")

    sent: list[dict[str, Any]] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(tool)
        return {}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    registry._active_agent = "dev"

    client = FakeAnthropic(
        [
            _Response(
                [tool_use_block("tu3", "powershell_exec", {"script": "rm -rf /"})],
                "tool_use",
            ),
            _Response([text_block("Understood, I won't run that.")], "end_turn"),
        ]
    )

    result = await run_turn(session, "run a script", executor=executor, client=client)
    assert result.pending is not None
    assert sent == []

    result2 = await confirm_pending(
        session, approve=False, executor=executor, client=client
    )
    # Denied: the tunnel was never called, but the model still got a result and
    # produced a final reply.
    assert sent == []
    assert result2.done is True
    assert result2.assistant_text == "Understood, I won't run that."
    # The denied result was fed back as an error tool_result.
    resume_messages = client.messages.calls[1]["messages"]
    denied = [
        b
        for m in resume_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert denied and denied[0].get("is_error") is True


def test_sessions_registry_round_trips() -> None:
    sessions = ChatSessions()
    a = sessions.get_or_create(None)
    assert sessions.get(a.id) is a
    b = sessions.get_or_create(a.id)
    assert b is a

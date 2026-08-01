"""The extracted tool-use loop, driven by a stub policy and a fake client.

``toolloop.drive_events`` is the surface-independent core: the dashboard's
confirm-gate is one policy over it, not part of it. These tests drive it with a
policy that can answer ``Allow``/``Deny``/``Hold`` per tool and a session object
that is *not* a :class:`~kenny_server.chat.ChatSession` — the loop must stay
duck-typed over ``.id``, ``.messages``, ``.agent_id``, ``.pending``, ``._queue``
and ``._staged_results``.

The fake Anthropic client (no API key, no network) and the mock-agent-free
tunnel stub come from ``test_chat``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.toolloop import (
    _MAX_TOOL_RESULT_CHARS,
    _resolve_chat_target,
    SERVER_TOOLS,
    Allow,
    Deny,
    GateDecision,
    Hold,
    PendingCall,
    ToolExecutor,
    apply_confirmation,
    build_tool_schemas,
    drive_events,
)
from kenny_server.tools import CAPABILITY_TOOLS, CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block


# -- duck-typed session + stub policy --------------------------------------


@dataclass
class FakeSession:
    """Everything the loop is allowed to touch on a session — and nothing else."""

    id: str
    agent_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending: PendingCall | None = None
    _staged_results: list[dict[str, Any]] = field(default_factory=list)
    _queue: list[dict[str, Any]] = field(default_factory=list)


class StubPolicy:
    """Answers the loop's four questions; records what it was asked."""

    def __init__(self, decisions: dict[str, GateDecision] | None = None) -> None:
        self._decisions = decisions or {}
        self.gated: list[tuple[str, str | None]] = []
        self.holds: list[PendingCall] = []
        self.system_calls = 0

    def system_blocks(self, session: Any) -> list[dict[str, Any]]:
        self.system_calls += 1
        return [{"type": "text", "text": "stub system prompt"}]

    def tool_schemas(self) -> list[dict[str, Any]]:
        return build_tool_schemas()

    def resolve_target(self, session: Any, tool: str, args: dict[str, Any]) -> str | None:
        return None if tool in SERVER_TOOLS else _resolve_chat_target(session, args)

    async def gate(
        self, session: Any, tool: str, args: dict[str, Any], agent_id: str | None
    ) -> GateDecision:
        self.gated.append((tool, agent_id))
        return self._decisions.get(tool, Allow())

    async def on_hold(self, session: Any, pending: PendingCall) -> None:
        self.holds.append(pending)


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
async def store(tmp_path) -> TelemetryStore:
    s = TelemetryStore(db_path=str(tmp_path / "loop.sqlite"))
    await s.connect()
    yield s
    await s.close()


def _executor(store: TelemetryStore) -> tuple[ToolExecutor, AgentRegistry, AgentTunnel]:
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    executor = ToolExecutor(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        screenshots=ScreenshotStore(),
    )
    return executor, registry, tunnel


async def _collect(events: Any) -> list[dict[str, Any]]:
    return [ev async for ev in events]


async def _drive(session: FakeSession, executor: ToolExecutor, client: Any, policy: StubPolicy):
    return await _collect(
        drive_events(session, executor, client=client, model="test-model", policy=policy)
    )


def _fed_back_tool_results(client: FakeAnthropic, call_index: int) -> list[dict[str, Any]]:
    return [
        b
        for m in client.messages.calls[call_index]["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]


# -- tool schemas -----------------------------------------------------------


def test_schemas_unfiltered_by_default() -> None:
    """``allowed=None`` must emit exactly what the loop emitted before."""

    full = build_tool_schemas()
    assert full == build_tool_schemas(None)
    names = [t["name"] for t in full]
    assert names[: len(SERVER_TOOLS)] == list(SERVER_TOOLS)  # server tools first, in order
    assert set(CAPABILITY_TOOLS) <= set(names)


def test_schemas_filtered_to_an_allowlist() -> None:
    narrowed = build_tool_schemas(frozenset({"fleet_overview", "diag_processes"}))
    assert [t["name"] for t in narrowed] == ["fleet_overview", "diag_processes"]
    # Filtering narrows the set only — each surviving schema is untouched.
    full = {t["name"]: t for t in build_tool_schemas()}
    assert all(t == full[t["name"]] for t in narrowed)


# -- Allow ------------------------------------------------------------------


async def test_allow_executes_and_feeds_the_result_back(store: TelemetryStore) -> None:
    executor, _registry, _tunnel = _executor(store)
    session = FakeSession(id="allow")
    policy = StubPolicy()
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu1", "fleet_overview", {})], "tool_use"),
            _Response([text_block("All green.")], "end_turn"),
        ]
    )
    session.messages.append({"role": "user", "content": "how is the fleet?"})

    events = await _drive(session, executor, client, policy)

    assert policy.gated == [("fleet_overview", None)]  # server tools carry no target
    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["tool"] == "fleet_overview" and results[0]["ok"] is True
    assert events[-1] == {
        "type": "done",
        "session_id": "allow",
        "assistant_text": "All green.",
        "pending": None,
        "done": True,
    }
    # The policy supplied the system blocks and tool schemas, not the loop.
    assert client.messages.calls[0]["system"] == [{"type": "text", "text": "stub system prompt"}]
    assert policy.system_calls == 2
    fed_back = _fed_back_tool_results(client, 1)
    assert fed_back and fed_back[0]["tool_use_id"] == "tu1"


# -- Hold -------------------------------------------------------------------


async def test_hold_pauses_the_turn_and_notifies_the_policy(store: TelemetryStore) -> None:
    executor, _registry, tunnel = _executor(store)
    sent: list[str] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(tool)
        return {}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="hold", agent_id="dev")
    policy = StubPolicy({"winget_install": Hold("operator_approval")})
    client = FakeAnthropic(
        [_Response([tool_use_block("tu2", "winget_install", {"id": "Git.Git"})], "tool_use")]
    )
    session.messages.append({"role": "user", "content": "install git"})

    events = await _drive(session, executor, client, policy)

    assert sent == []  # nothing ran
    pending_events = [e for e in events if e["type"] == "pending"]
    assert pending_events == [
        {"type": "pending", "tool": "winget_install", "args": {"id": "Git.Git"}, "agent_id": "dev"}
    ]
    assert events[-1]["type"] == "done" and events[-1]["done"] is False
    assert events[-1]["pending"] == session.pending.to_public()

    # The hold is on the session, tagged with its tier and who must decide.
    assert session.pending is not None
    assert session.pending.tool == "winget_install"
    assert session.pending.agent_id == "dev"
    assert session.pending.tool_class == "normal_change"
    assert session.pending.gate_kind == "operator_approval"
    # ...and the policy was told, exactly once, with that same object.
    assert policy.holds == [session.pending]


async def test_hold_freezes_the_target_before_the_gate(store: TelemetryStore) -> None:
    """The target is resolved *before* the gate, so a later switch can't retarget."""

    executor, _registry, tunnel = _executor(store)
    sent: list[str] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(agent_id)
        return {"ok": True}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="freeze", agent_id="alpha")
    policy = StubPolicy({"net_dns_flush": Hold("operator_approval")})
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu3", "net_dns_flush", {})], "tool_use"),
            _Response([text_block("Flushed.")], "end_turn"),
        ]
    )
    session.messages.append({"role": "user", "content": "flush dns"})

    await _drive(session, executor, client, policy)
    assert policy.gated == [("net_dns_flush", "alpha")]  # gate saw the frozen target

    # The dashboard switches machines while the confirmation is open.
    session.agent_id = "beta"
    resume = await apply_confirmation(session, approve=True, executor=executor)

    assert sent == ["alpha"]  # not "beta"
    assert resume == {"type": "tool_result", "tool": "net_dns_flush", "args": {}, "ok": True}
    assert session.pending is None
    assert session._staged_results and session._staged_results[0]["tool_use_id"] == "tu3"


async def test_apply_confirmation_denial_stages_an_error(store: TelemetryStore) -> None:
    executor, _registry, tunnel = _executor(store)
    sent: list[str] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(tool)
        return {}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="deny-confirm", agent_id="dev")
    session.pending = PendingCall(
        id="p1",
        tool_use_id="tu4",
        tool="powershell_exec",
        args={"script": "rm -rf /"},
        agent_id="dev",
    )

    resume = await apply_confirmation(session, approve=False, executor=executor)

    assert sent == []
    assert resume["type"] == "denied" and resume["tool"] == "powershell_exec"
    staged = session._staged_results[0]
    assert staged["is_error"] is True
    assert json.loads(staged["content"])["error"]["code"] == "denied"


# -- Deny -------------------------------------------------------------------


async def test_deny_stages_an_error_block_and_the_loop_continues(
    store: TelemetryStore,
) -> None:
    executor, _registry, tunnel = _executor(store)
    sent: list[str] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(tool)
        return {}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="deny", agent_id="dev")
    policy = StubPolicy({"powershell_exec": Deny("forbidden", "not on this surface")})
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu5", "powershell_exec", {"script": "whoami"})], "tool_use"),
            _Response([text_block("I can't run that here.")], "end_turn"),
        ]
    )
    session.messages.append({"role": "user", "content": "run whoami"})

    events = await _drive(session, executor, client, policy)

    assert sent == []  # refused, never forwarded
    assert session.pending is None  # a denial does not pause the turn
    denied = [e for e in events if e["type"] == "denied"]
    assert denied == [
        {
            "type": "denied",
            "tool": "powershell_exec",
            "args": {"script": "whoami"},
            "agent_id": "dev",
        }
    ]
    # The turn ran to completion and the model saw an error-shaped tool_result.
    assert events[-1]["type"] == "done" and events[-1]["done"] is True
    fed_back = _fed_back_tool_results(client, 1)
    assert fed_back[0]["is_error"] is True
    payload = json.loads(fed_back[0]["content"])
    assert payload["error"] == {"code": "forbidden", "message": "not on this surface"}


# -- truncation -------------------------------------------------------------


async def test_oversized_result_is_still_truncated(store: TelemetryStore) -> None:
    """A hostile/huge agent payload stays bounded on the extracted path too."""

    executor, _registry, tunnel = _executor(store)

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        return {"content": "A" * (_MAX_TOOL_RESULT_CHARS * 2)}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="huge", agent_id="dev")
    policy = StubPolicy()
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu6", "fs_read", {"path": "C:\\big.txt"})], "tool_use"),
            _Response([text_block("That file is large.")], "end_turn"),
        ]
    )
    session.messages.append({"role": "user", "content": "read it"})

    await _drive(session, executor, client, policy)

    fed_back = _fed_back_tool_results(client, 1)
    assert len(fed_back[0]["content"]) <= _MAX_TOOL_RESULT_CHARS + 40
    assert "truncated" in fed_back[0]["content"]


# -- routing failure --------------------------------------------------------


async def test_unresolvable_target_is_reported_without_pausing(store: TelemetryStore) -> None:
    """``resolve_target`` failing closed stages an error and drains the queue."""

    executor, _registry, _tunnel = _executor(store)
    session = FakeSession(id="no-agent")  # no agent selected anywhere
    policy = StubPolicy()
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu7", "diag_processes", {})], "tool_use"),
            _Response([text_block("Pick a machine first.")], "end_turn"),
        ]
    )
    session.messages.append({"role": "user", "content": "list processes"})

    events = await _drive(session, executor, client, policy)

    assert policy.gated == []  # never reached the gate
    failed = [e for e in events if e["type"] == "tool_result"]
    assert failed and failed[0]["ok"] is False
    fed_back = _fed_back_tool_results(client, 1)
    assert json.loads(fed_back[0]["content"])["error"]["code"] == "no_agent"
    assert events[-1]["done"] is True

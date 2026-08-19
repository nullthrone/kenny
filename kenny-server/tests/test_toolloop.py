"""The extracted tool-use loop, driven by a stub policy and a fake client.

``toolloop.drive_events`` is the surface-independent core: the dashboard's
confirm-gate is one policy over it, not part of it. These tests drive it with a
policy that can answer ``Allow``/``Deny``/``Hold`` per tool and a session object
that is *not* a :class:`~kenny_server.chat.FleetSession` — the loop must stay
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
    SURFACE_ONLY_TOOLS,
    Allow,
    Deny,
    GateDecision,
    Hold,
    PendingCall,
    ToolExecutor,
    apply_confirmation,
    build_tool_schemas,
    drive_events,
    stage_missing_tool_results,
)
from kenny_server.tools import CAPABILITY_TOOLS, CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel, ToolError

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
    """``allowed=None`` emits the catalog, minus the tools no default may see."""

    full = build_tool_schemas()
    assert full == build_tool_schemas(None)
    names = [t["name"] for t in full]
    default_server_tools = [t for t in SERVER_TOOLS if t not in SURFACE_ONLY_TOOLS]
    assert names[: len(default_server_tools)] == default_server_tools  # server tools first, in order
    assert set(CAPABILITY_TOOLS) <= set(names)


def test_surface_only_tools_are_absent_unless_named() -> None:
    """A surface-only tool must not reach a caller that passes no allowlist.

    The dashboard copilot builds its schemas that way (``chat.py``), so the
    default is what decides whether a tool meant for one surface leaks into
    every surface. Withholding is the safe direction: a name added to
    ``SERVER_TOOLS`` for one caller is invisible to the rest until asked for.
    """

    assert SURFACE_ONLY_TOOLS, "the exception is pointless if the set is empty"
    default = {t["name"] for t in build_tool_schemas()}
    assert not (SURFACE_ONLY_TOOLS & default)
    for name in SURFACE_ONLY_TOOLS:
        named = {t["name"] for t in build_tool_schemas(frozenset({name}))}
        assert named == {name}


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


async def test_hold_is_recorded_before_it_is_announced(store: TelemetryStore) -> None:
    """A consumer that stops at the first event still gets the hold persisted.

    A surface that stores the pending call durably (to resolve it after a
    restart) must not depend on the caller draining the generator — otherwise it
    would show a gate it never recorded.
    """

    executor, _registry, tunnel = _executor(store)

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        raise AssertionError("nothing may run while a gate is open")

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="hold-early", agent_id="dev")
    policy = StubPolicy({"winget_install": Hold("operator_approval")})
    client = FakeAnthropic(
        [_Response([tool_use_block("tu9", "winget_install", {"id": "Git.Git"})], "tool_use")]
    )
    session.messages.append({"role": "user", "content": "install git"})

    # Break out at the very first event, as an impatient consumer would.
    stream = drive_events(session, executor, client=client, model="m", policy=policy)
    first = await anext(stream)
    await stream.aclose()

    assert first["type"] == "pending"
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
    # ``auto_run`` is false: this call was held and explicitly confirmed.
    assert resume == {
        "type": "tool_result",
        "tool": "net_dns_flush",
        "args": {},
        "ok": True,
        "auto_run": False,
    }
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
    assert resume["code"] == "denied"
    assert resume["message"] == "operator denied this action"
    staged = session._staged_results[0]
    assert staged["is_error"] is True
    assert json.loads(staged["content"])["error"]["code"] == "denied"


async def test_apply_confirmation_approval_that_fails_reports_its_error(
    store: TelemetryStore,
) -> None:
    """A confirmed call that fails on execution must carry error detail on the
    resumed event too, the same as a fresh (non-confirmed) call.
    """

    executor, _registry, tunnel = _executor(store)

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        raise ToolError("exec_failed", "powershell exited 1")

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="approve-fails", agent_id="dev")
    session.pending = PendingCall(
        id="p2",
        tool_use_id="tu9",
        tool="powershell_exec",
        args={"script": "exit 1"},
        agent_id="dev",
    )

    resume = await apply_confirmation(session, approve=True, executor=executor)

    assert resume["type"] == "tool_result" and resume["ok"] is False
    assert resume["error"] == {"code": "exec_failed", "message": "powershell exited 1"}


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
            "code": "forbidden",
            "message": "not on this surface",
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
    assert failed[0]["error"]["code"] == "no_agent"
    fed_back = _fed_back_tool_results(client, 1)
    assert json.loads(fed_back[0]["content"])["error"]["code"] == "no_agent"
    assert events[-1]["done"] is True


async def test_an_allowed_call_that_fails_reports_its_error(store: TelemetryStore) -> None:
    """A genuine execution failure (e.g. an agent-side timeout) must carry its
    code/message on the yielded event, not just the boolean ``ok``.
    """

    executor, _registry, tunnel = _executor(store)

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        raise ToolError("timeout", "tool powershell_exec exceeded 60s")

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="times-out", agent_id="dev")
    policy = StubPolicy()
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu8", "powershell_exec", {"script": "Get-ChildItem"})], "tool_use"),
            _Response([text_block("That timed out.")], "end_turn"),
        ]
    )
    session.messages.append({"role": "user", "content": "list files"})

    events = await _drive(session, executor, client, policy)

    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is False
    assert results[0]["error"] == {"code": "timeout", "message": "tool powershell_exec exceeded 60s"}


# -- an offline agent must yield a tool_result, never an escaped exception --
#
# ``registry.send_fn_for`` raises the unrelated ``AuthError`` for an agent that
# is registered but not currently connected (``tunnel.py`` § F1). Before that
# fix this escaped ``_execute_one``'s ``except ToolError`` entirely and
# propagated out of the loop/``apply_confirmation`` — exactly the second wedge
# the ticket-assistant report hit (an approval that took long enough for the
# agent to go offline in the meantime).


async def _register_offline(registry: AgentRegistry, agent_id: str) -> None:
    async def _noop_send(_payload: dict[str, Any]) -> None:  # pragma: no cover - never called
        raise AssertionError("an offline agent's send_fn must never be invoked")

    registry.register(agent_id, "dev-token", {}, _noop_send)
    registry.mark_offline(agent_id)


async def test_offline_agent_yields_a_tool_result_not_an_exception(store: TelemetryStore) -> None:
    executor, registry, _tunnel = _executor(store)
    await _register_offline(registry, "dev")

    session = FakeSession(id="offline", agent_id="dev")
    policy = StubPolicy()
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu10", "diag_processes", {})], "tool_use"),
            _Response([text_block("couldn't reach it.")], "end_turn"),
        ]
    )
    session.messages.append({"role": "user", "content": "list processes"})

    events = await _drive(session, executor, client, policy)

    results = [e for e in events if e["type"] == "tool_result"]
    assert results and results[0]["ok"] is False
    assert results[0]["error"]["code"] == "offline"
    # The turn still completed -- the model was told, nothing escaped.
    assert events[-1]["type"] == "done" and events[-1]["done"] is True


async def test_apply_confirmation_reports_offline_not_an_exception(store: TelemetryStore) -> None:
    """Same assertion through ``apply_confirmation`` -- the path the report hit:
    an agent that went offline during a long approval wait."""

    executor, registry, _tunnel = _executor(store)
    await _register_offline(registry, "dev")

    session = FakeSession(id="offline-confirm", agent_id="dev")
    session.pending = PendingCall(
        id="p1", tool_use_id="tu11", tool="winget_install", args={"id": "Git.Git"}, agent_id="dev"
    )

    resume = await apply_confirmation(session, approve=True, executor=executor)

    assert resume["type"] == "tool_result" and resume["ok"] is False
    assert resume["error"]["code"] == "offline"
    staged = session._staged_results[0]
    assert staged["is_error"] is True
    assert json.loads(staged["content"])["error"]["code"] == "offline"


# -- stage_missing_tool_results -----------------------------------------------


def test_stage_missing_tool_results_only_heals_the_true_orphan() -> None:
    """A queued, still-pending, or explicitly exempt id survives untouched;
    only the genuinely unanswered id gets an error tool_result staged."""

    session = FakeSession(id="heal", agent_id="dev")
    session.messages = [
        {"role": "user", "content": "do stuff"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "on it"},
                {"type": "tool_use", "id": "queued", "name": "diag_processes", "input": {}},
                {"type": "tool_use", "id": "held", "name": "winget_install", "input": {}},
                {"type": "tool_use", "id": "spared", "name": "fs_read", "input": {}},
                {"type": "tool_use", "id": "orphan", "name": "net_dns_flush", "input": {}},
            ],
        },
    ]
    session._queue = [{"type": "tool_use", "id": "queued", "name": "diag_processes", "input": {}}]
    session.pending = PendingCall(
        id="p", tool_use_id="held", tool="winget_install", args={}, agent_id="dev"
    )

    healed = stage_missing_tool_results(session, exempt={"spared"})

    assert healed == ["orphan"]
    staged_ids = {b["tool_use_id"] for b in session._staged_results}
    assert staged_ids == {"orphan"}
    assert session._staged_results[0]["is_error"] is True
    assert (
        json.loads(session._staged_results[0]["content"])["error"]["code"] == "not_completed"
    )
    # Never touched: messages, the queue, and the live pending call.
    assert [b["id"] for b in session.messages[-1]["content"] if b.get("type") == "tool_use"] == [
        "queued",
        "held",
        "spared",
        "orphan",
    ]
    assert session._queue == [{"type": "tool_use", "id": "queued", "name": "diag_processes", "input": {}}]
    assert session.pending is not None and session.pending.tool_use_id == "held"


def test_stage_missing_tool_results_is_a_noop_when_nothing_is_orphaned() -> None:
    session = FakeSession(id="clean", agent_id="dev")
    session.messages = [{"role": "user", "content": "hi"}]

    assert stage_missing_tool_results(session) == []
    assert session._staged_results == []


# -- the deliberate divergence from chat.heal_session -------------------------
#
# ``chat.heal_session`` drops the trailing assistant message outright and
# unconditionally clears ``_queue``/``_staged_results`` -- both wrong for the
# ticket surface (kenny's own words are already durably in the trail per
# ADR-0050, and a second parked gate in ``_queue`` must survive). This test
# pins the two apart so nobody "unifies" them later.


def _dangling_transcript() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "do x"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "kenny's own words"},
                {"type": "tool_use", "id": "tu1", "name": "winget_install", "input": {}},
            ],
        },
    ]


def test_stage_missing_tool_results_diverges_from_chat_heal_session() -> None:
    from kenny_server.chat import heal_session

    healed_session = FakeSession(id="healed", messages=_dangling_transcript())
    dropped_session = FakeSession(id="dropped", messages=_dangling_transcript())

    healed_ids = stage_missing_tool_results(healed_session)
    heal_session(dropped_session)

    # stage_missing_tool_results: the assistant message (kenny's own words)
    # survives, and the orphaned call is answered in place.
    assert healed_ids == ["tu1"]
    assert len(healed_session.messages) == 2
    assert healed_session.messages[-1]["role"] == "assistant"
    assert healed_session._staged_results
    assert healed_session._staged_results[0]["tool_use_id"] == "tu1"

    # heal_session: the trailing assistant message is dropped wholesale --
    # kenny's words are gone from the transcript -- and nothing is staged.
    assert len(dropped_session.messages) == 1
    assert dropped_session.messages[-1]["role"] == "user"
    assert dropped_session._staged_results == []

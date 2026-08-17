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
    FleetSession,
    ChatSessions,
    _resolve_chat_target,
    build_tool_schemas,
    confirm_pending,
    confirm_pending_events,
    is_state_changing,
    persist_session,
    public_transcript,
    run_turn,
    run_turn_events,
)
from kenny_server.registry import AgentRegistry
from kenny_server.store import ChatHistoryStore, EventStore, TelemetryStore
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel, ToolError


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


def _chunks(text: str) -> list[str]:
    """Split text into word-ish chunks so streaming tests see multiple deltas."""

    return re.findall(r"\S+\s*", text) or ([text] if text else [])


class _StreamCtx:
    """Mimics ``anthropic`` ``messages.stream()``: a sync context manager that
    exposes ``text_stream`` (token chunks) and ``get_final_message()``."""

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

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self._scripted.pop(0)

    def stream(self, **kwargs: Any) -> _StreamCtx:
        # Records into the same ``calls`` list and pops the same scripted queue
        # as ``create`` so streaming and non-streaming paths stay in lockstep.
        self.calls.append(kwargs)
        return _StreamCtx(self._scripted.pop(0))


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
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    call_log = CallLog()
    executor = ChatExecutor(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        screenshots=ScreenshotStore(),
    )
    return executor, registry, tunnel


# -- tests ----------------------------------------------------------------


def test_tool_schemas_cover_all_tools() -> None:
    from kenny_server.tools import CAPABILITY_TOOLS

    names = {t["name"] for t in build_tool_schemas()}
    for server_tool in (
        "list_agents",
        "select_agent",
        "fleet_overview",
        "agent_health",
        "agent_snapshot",
    ):
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
    assert not is_state_changing("remotehelp_status")
    assert is_state_changing("winget_install")
    assert is_state_changing("powershell_exec")
    assert is_state_changing("shell_exec")
    assert is_state_changing("net_dns_flush")
    # remotehelp_start/_stop are mutating on the agent (control.rs); the chat
    # confirm-gate must agree so they aren't auto-invoked (ADR-0021, issue #55).
    assert is_state_changing("remotehelp_start")
    assert is_state_changing("remotehelp_stop")


def test_system_prompt_defers_confirmation_to_the_gate_dialog() -> None:
    """The confirm-gate dialog must be the single confirmation point.

    Regression guard: the prompt used to tell the model to "propose them, then
    wait", which made it ask for confirmation in prose AND then trigger the gate
    dialog — the operator was asked twice. The prompt must instead steer the
    model to issue the call directly and let the dialog handle approval.
    """

    from kenny_server.chat import _SYSTEM_PROMPT

    lowered = _SYSTEM_PROMPT.lower()
    # The old double-asking instruction must not come back.
    assert "propose them, then wait" not in lowered
    # The model is told not to ask for permission in prose first.
    assert "do not ask for permission in prose" in lowered
    # And that the confirmation dialog is the single place consent is given.
    assert "single place consent is given" in lowered


def test_context_note_names_the_selected_agent() -> None:
    """The dashboard's selected agent must reach the model in words, not just
    scope tool routing (see build_chat_routes' agent_id handling) — otherwise
    the model can't answer "which PC is this?" without calling a tool first."""

    from kenny_server.chat import _context_note

    # No selection is its own statement ("fleet-wide"), not silence.
    fleet = _context_note(FleetSession(id="s1"))
    assert len(fleet) == 1
    assert "fleet-wide" in fleet[0]["text"]
    assert "cache_control" not in fleet[0]

    note = _context_note(FleetSession(id="s2", agent_id="linus-pc"))
    assert len(note) == 1
    assert "linus-pc" in note[0]["text"]
    # Must stay out of the cached system block so it never busts that prefix.
    assert "cache_control" not in note[0]


def test_tool_result_payload_capped() -> None:
    from kenny_server.chat import _MAX_TOOL_RESULT_CHARS, _tool_result_block

    # A small, attacker-influenceable payload is passed through unchanged.
    small = _tool_result_block("tu_1", {"content": "hello", "truncated": False})
    assert json.loads(small["content"]) == {"content": "hello", "truncated": False}

    # A huge payload (e.g. a malicious agent's fs_read) is truncated with a marker
    # so it can't blow up context or maximise the prompt-injection surface.
    huge = _tool_result_block("tu_2", {"content": "A" * (_MAX_TOOL_RESULT_CHARS * 2)})
    assert len(huge["content"]) <= _MAX_TOOL_RESULT_CHARS + 40
    assert "truncated" in huge["content"]


async def test_read_only_tool_auto_executes(store: TelemetryStore) -> None:
    executor, _registry, _tunnel = _executor(store)
    session = FleetSession(id="s1")

    # Turn 1: model asks for fleet_overview. Turn 2: model replies with text.
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu1", "fleet_overview", {})], "tool_use"),
            _Response([text_block("The fleet is healthy.")], "end_turn"),
        ]
    )

    result = await run_turn(session, "How is the fleet?", executor=executor, client=client)

    assert result.done is True
    assert result.pending is None
    assert result.assistant_text == "The fleet is healthy."
    # The read-only tool ran and produced a tool_result event.
    assert any(
        e["type"] == "tool_result" and e["tool"] == "fleet_overview" for e in result.tool_events
    )

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


async def test_run_turn_heals_aborted_tool_use(store: TelemetryStore) -> None:
    """A turn stopped mid tool-loop can leave a trailing assistant ``tool_use``
    with no matching ``tool_result`` (invalid for the next call). The next turn
    heals the session by dropping it, so a follow-up message still succeeds."""

    executor, _registry, _tunnel = _executor(store)
    session = FleetSession(id="aborted")
    session.messages = [
        {"role": "user", "content": "check the fleet"},
        {"role": "assistant", "content": [tool_use_block("tu1", "fleet_overview", {}).__dict__]},
    ]
    session._queue = [tool_use_block("tu1", "fleet_overview", {}).__dict__]

    client = FakeAnthropic([_Response([text_block("All good now.")], "end_turn")])
    result = await run_turn(session, "are we ok now?", executor=executor, client=client)

    assert result.assistant_text == "All good now."
    assert session._queue == []
    # No dangling assistant tool_use turn remains in the history.
    assert not any(
        m["role"] == "assistant"
        and isinstance(m["content"], list)
        and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"])
        for m in session.messages
    )


async def test_state_changing_tool_requires_confirmation(store: TelemetryStore) -> None:
    executor, _registry, tunnel = _executor(store)
    session = FleetSession(id="s2")

    # Stub the capability path so no real agent is needed.
    sent: list[dict[str, Any]] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append({"agent_id": agent_id, "tool": tool, "args": args})
        return {"installed": True, "id": args.get("id")}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    session.agent_id = "dev"  # an agent is selected for this chat session

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

    result = await run_turn(session, "Install git on dev", executor=executor, client=client)

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
    result2 = await confirm_pending(session, approve=True, executor=executor, client=client)
    assert result2.done is True
    assert result2.pending is None
    assert result2.assistant_text == "Git is installed."
    assert len(sent) == 1 and sent[0]["tool"] == "winget_install"
    assert session.pending is None


async def test_state_changing_tool_denied(store: TelemetryStore) -> None:
    executor, _registry, tunnel = _executor(store)
    session = FleetSession(id="s3")

    sent: list[dict[str, Any]] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(tool)
        return {}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    session.agent_id = "dev"

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

    result2 = await confirm_pending(session, approve=False, executor=executor, client=client)
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


async def test_screen_capture_fed_back_as_image(store: TelemetryStore) -> None:
    """A screen_capture result is fed to Claude as an image content block (not a
    base64 JSON string) and the tool_event carries the image for the UI."""

    executor, _registry, tunnel = _executor(store)
    session = FleetSession(id="s4")

    tiny_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # noqa: E501

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        return {"image_b64": tiny_b64, "format": "png"}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    session.agent_id = "dev"

    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu4", "screen_capture", {})], "tool_use"),
            _Response([text_block("Here is the screen.")], "end_turn"),
        ]
    )

    result = await run_turn(session, "Show me the screen", executor=executor, client=client)

    assert result.done is True
    # The tool_event carries the image so the UI can render it inline.
    shot_events = [
        e
        for e in result.tool_events
        if e["type"] == "tool_result" and e["tool"] == "screen_capture"
    ]
    assert shot_events and shot_events[0]["image_b64"] == tiny_b64
    assert shot_events[0]["format"] == "png"

    # The message fed back to the model carries an image content block, not a
    # JSON text string.
    second_call_messages = client.messages.calls[1]["messages"]
    tool_results = [
        b
        for m in second_call_messages
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results
    content = tool_results[0]["content"]
    assert isinstance(content, list)
    image_blocks = [b for b in content if b.get("type") == "image"]
    assert image_blocks
    src = image_blocks[0]["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "image/png"
    assert src["data"] == tiny_b64

    # And the store recorded the latest screenshot for the agent.
    rec = executor.screenshots.get("dev")
    assert rec is not None and rec["image_b64"] == tiny_b64
    assert rec["format"] == "png" and "captured_at" in rec


async def _collect(events: Any) -> list[dict[str, Any]]:
    return [ev async for ev in events]


async def test_run_turn_events_streams_text(store: TelemetryStore) -> None:
    executor, _registry, _tunnel = _executor(store)
    session = FleetSession(id="se1")
    client = FakeAnthropic([_Response([text_block("The fleet is healthy.")], "end_turn")])

    events = await _collect(
        run_turn_events(session, "How is the fleet?", executor=executor, client=client)
    )

    deltas = [e["text"] for e in events if e["type"] == "text_delta"]
    assert len(deltas) > 1  # streamed in multiple chunks
    assert "".join(deltas) == "The fleet is healthy."
    done = events[-1]
    assert done["type"] == "done"
    assert done["done"] is True
    assert done["assistant_text"] == "The fleet is healthy."
    assert done["session_id"] == "se1"


async def test_stream_emits_tool_result_before_text(store: TelemetryStore) -> None:
    executor, _registry, _tunnel = _executor(store)
    session = FleetSession(id="se2")
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu1", "fleet_overview", {})], "tool_use"),
            _Response([text_block("All green.")], "end_turn"),
        ]
    )

    events = await _collect(
        run_turn_events(session, "How is the fleet?", executor=executor, client=client)
    )
    types = [e["type"] for e in events]
    first_tool = types.index("tool_result")
    first_text = types.index("text_delta")
    assert first_tool < first_text  # the tool ran (live) before the reply streamed
    assert events[first_tool]["tool"] == "fleet_overview"


async def test_stream_confirm_gate(store: TelemetryStore) -> None:
    executor, _registry, tunnel = _executor(store)
    session = FleetSession(id="se3")

    sent: list[str] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(tool)
        return {"installed": True}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    session.agent_id = "dev"

    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu2", "winget_install", {"id": "Git.Git"})], "tool_use"),
            _Response([text_block("Git is installed.")], "end_turn"),
        ]
    )

    events = await _collect(
        run_turn_events(session, "Install git", executor=executor, client=client)
    )
    pending = [e for e in events if e["type"] == "pending"]
    assert pending and pending[0]["tool"] == "winget_install"
    assert events[-1]["type"] == "done" and events[-1]["done"] is False
    assert sent == []  # nothing executed before confirmation
    assert session.pending is not None

    resumed = await _collect(
        confirm_pending_events(session, approve=True, executor=executor, client=client)
    )
    # resume_event (the executed tool_result) is yielded first.
    assert resumed[0]["type"] == "tool_result" and resumed[0]["tool"] == "winget_install"
    deltas = "".join(e["text"] for e in resumed if e["type"] == "text_delta")
    assert deltas == "Git is installed."
    assert resumed[-1]["type"] == "done" and resumed[-1]["done"] is True
    assert sent == ["winget_install"]
    assert session.pending is None


async def test_drive_batch_still_matches(store: TelemetryStore) -> None:
    """The drained (non-streaming) path produces the same public shape as before."""

    executor, _registry, _tunnel = _executor(store)
    session = FleetSession(id="se4")
    client = FakeAnthropic(
        [
            _Response([tool_use_block("tu1", "fleet_overview", {})], "tool_use"),
            _Response([text_block("The fleet is healthy.")], "end_turn"),
        ]
    )
    result = await run_turn(session, "How is the fleet?", executor=executor, client=client)
    pub = result.to_public()
    assert pub["done"] is True
    assert pub["pending"] is None
    assert pub["assistant_text"] == "The fleet is healthy."
    assert [e["type"] for e in pub["tool_events"]] == ["tool_result"]
    assert pub["tool_events"][0]["tool"] == "fleet_overview"


async def test_sessions_registry_round_trips() -> None:
    sessions = ChatSessions()
    a = sessions.get_or_create(None)
    assert await sessions.get(a.id) is a
    b = sessions.get_or_create(a.id)
    assert b is a


# -- persistence (ADR-0025) -------------------------------------------------


@pytest.fixture
async def history_store(tmp_path) -> ChatHistoryStore:
    s = ChatHistoryStore(db_path=str(tmp_path / "history.sqlite"))
    await s.connect()
    yield s
    await s.close()


async def test_sessions_get_returns_none_when_absent_everywhere(history_store: ChatHistoryStore) -> None:
    sessions = ChatSessions(store=history_store)
    assert await sessions.get("nope") is None
    # And with no store configured at all.
    assert await ChatSessions().get("nope") is None


async def test_sessions_get_falls_back_to_store(history_store: ChatHistoryStore) -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    await history_store.save(id="s1", title="Greeting", agent_id="dev", messages=messages)

    sessions = ChatSessions(store=history_store)
    session = await sessions.get("s1")
    assert session is not None
    assert session.id == "s1"
    assert session.title == "Greeting"
    assert session.agent_id == "dev"
    assert session.messages == messages

    # Second call hits the in-memory cache: same object, no re-fetch needed.
    again = await sessions.get("s1")
    assert again is session


async def test_sessions_get_heals_a_session_saved_mid_turn(history_store: ChatHistoryStore) -> None:
    """A row persisted with a dangling, unanswered tool_use is repaired on load."""

    messages = [
        {"role": "user", "content": "install git"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu1", "name": "winget_install", "input": {}}],
        },
    ]
    await history_store.save(id="s2", title="Install git", agent_id=None, messages=messages)

    sessions = ChatSessions(store=history_store)
    session = await sessions.get("s2")
    assert session is not None
    # heal_session drops the trailing, unanswered assistant tool_use message.
    assert session.messages == [{"role": "user", "content": "install git"}]


async def test_persist_session_derives_title_once(history_store: ChatHistoryStore) -> None:
    session = FleetSession(id="s3", messages=[{"role": "user", "content": "  first question  "}])
    await persist_session(history_store, session)
    assert session.title == "first question"

    session.messages.append({"role": "assistant", "content": [{"type": "text", "text": "answer"}]})
    session.messages.append({"role": "user", "content": "a completely different second question"})
    await persist_session(history_store, session)
    # Title stays as derived from the first save; FleetSession.title itself is
    # never re-derived, and ChatHistoryStore.save also refuses to overwrite it.
    assert session.title == "first question"
    row = await history_store.get("s3")
    assert row is not None
    assert row["title"] == "first question"
    assert len(row["messages"]) == 3


async def test_persist_session_noop_without_a_store() -> None:
    session = FleetSession(id="s4", messages=[{"role": "user", "content": "hi"}])
    await persist_session(None, session)
    assert session.title is None  # never touched


def test_derive_title_truncates_and_falls_back() -> None:
    from kenny_server.chat import _derive_title

    assert _derive_title("  How   is the fleet?  ") == "How is the fleet?"
    assert _derive_title("") == "New conversation"
    assert _derive_title("   ") == "New conversation"
    long_text = "x" * 200
    title = _derive_title(long_text)
    assert len(title) <= 80
    assert title.endswith("…")


def test_public_transcript_flattens_text_and_tool_result_and_omits_pending() -> None:
    messages = [
        {"role": "user", "content": "take a screenshot and tell me the fleet status"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "On it."},
                {"type": "tool_use", "id": "tu1", "name": "screen_capture", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
                        },
                        {"type": "text", "text": "screen_capture (png)"},
                    ],
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu2", "name": "winget_install", "input": {"id": "Git.Git"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu2",
                    "content": json.dumps({"error": {"code": "denied", "message": "operator denied this action"}}),
                    "is_error": True,
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Understood, I won't install it."}]},
    ]

    events = public_transcript(messages)
    types = [e["type"] for e in events]
    assert types == ["user_text", "text_delta", "tool_result", "denied", "text_delta"]
    assert "pending" not in types

    shot = events[2]
    assert shot["tool"] == "screen_capture" and shot["ok"] is True
    assert shot["image_b64"] == "aGVsbG8=" and shot["format"] == "png"

    denied = events[3]
    assert denied["tool"] == "winget_install" and denied["args"] == {"id": "Git.Git"}

    assert events[-1]["text"] == "Understood, I won't install it."


# -- session-scoped agent targeting (ADR-0038) -----------------------------
#
# The chat path used to forward every capability call to the process-global
# ``registry.active_agent`` slot (``require_active()`` with no key) — shared by
# every concurrent chat session. Two overlapping conversations (or a stale
# selection left by an earlier one) could silently retarget each other's calls.
# ``run_capability`` now takes its target as an explicit argument, resolved per
# call from the session's own ``agent_id`` (see ``_resolve_chat_target``), so
# the global slot is never consulted for routing.


async def test_run_capability_uses_passed_target_not_global_slot(
    store: TelemetryStore,
) -> None:
    executor, registry, tunnel = _executor(store)

    sent: list[str] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(agent_id)
        return {}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    # Poison the global slot the old code path would have read.
    registry._active_agent = "gamma"  # noqa: SLF001

    await executor.run_capability("diag_processes", {}, agent_id="alpha")
    await executor.run_capability("diag_processes", {}, agent_id="beta")

    assert sent == ["alpha", "beta"]  # never "gamma"


async def test_run_capability_fails_closed_without_a_target(
    store: TelemetryStore,
) -> None:
    executor, _registry, _tunnel = _executor(store)

    with pytest.raises(ToolError) as excinfo:
        await executor.run_capability("diag_processes", {}, agent_id="")
    assert excinfo.value.code == "no_agent"


def test_resolve_chat_target_prefers_explicit_override_over_session() -> None:
    session = FleetSession(id="s", agent_id="alpha")
    args = {"agent_id": "beta", "path": "C:\\"}

    assert _resolve_chat_target(session, args) == "beta"
    # Popped off — routing metadata, not forwarded to the tool.
    assert args == {"path": "C:\\"}


def test_resolve_chat_target_falls_back_to_session_selection() -> None:
    session = FleetSession(id="s", agent_id="alpha")

    assert _resolve_chat_target(session, {}) == "alpha"


def test_resolve_chat_target_fails_closed_without_either() -> None:
    session = FleetSession(id="s")

    with pytest.raises(ToolError) as excinfo:
        _resolve_chat_target(session, {})
    assert excinfo.value.code == "no_agent"


async def test_two_sessions_route_independently_despite_shared_executor(
    store: TelemetryStore,
) -> None:
    """Two concurrent chat sessions (sharing one executor/registry, exactly as
    the dashboard's chat routes do) each target a different agent and never
    cross-contaminate — the reported race, reproduced at the chat layer."""

    executor, registry, tunnel = _executor(store)

    sent: list[tuple[str, str]] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append((agent_id, tool))
        return {"processes": []}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]
    # A stale/foreign selection sitting in the global slot must have no effect.
    registry._active_agent = "gamma"  # noqa: SLF001

    session_a = FleetSession(id="a", agent_id="alpha")
    session_b = FleetSession(id="b", agent_id="beta")

    client_a = FakeAnthropic(
        [
            _Response([tool_use_block("tu1", "diag_processes", {})], "tool_use"),
            _Response([text_block("done")], "end_turn"),
        ]
    )
    client_b = FakeAnthropic(
        [
            _Response([tool_use_block("tu2", "diag_processes", {})], "tool_use"),
            _Response([text_block("done")], "end_turn"),
        ]
    )

    await run_turn(session_a, "list processes", executor=executor, client=client_a)
    await run_turn(session_b, "list processes", executor=executor, client=client_b)

    assert sent == [("alpha", "diag_processes"), ("beta", "diag_processes")]

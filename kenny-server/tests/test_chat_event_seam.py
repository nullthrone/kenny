"""The chat event vocabulary, where Python and TypeScript have to agree.

The server yields SSE events; the console's reducer folds them into a
transcript. Neither side can see the other, so an event only one of them knows
about is either a frame nothing renders or a case the console handles and the
server never sends. This is the seam, tested joined: the declared set is checked
against the console's union *and* against what a real drive actually emits, so
it cannot quietly become a list nobody maintains.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kenny_server.chat import CHAT_EVENT_TYPES
from kenny_server.store import TelemetryStore
from kenny_server.toolloop import LOOP_EVENT_TYPES, Hold, confirmation_events, drive_events

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

from test_toolloop import FakeSession, StubPolicy, _collect, _drive, _executor

_TYPES_TS = Path(__file__).resolve().parents[2] / "kenny-web/src/api/types.ts"


@pytest.fixture
async def store(tmp_path) -> TelemetryStore:
    s = TelemetryStore(db_path=str(tmp_path / "seam.sqlite"))
    await s.connect()
    yield s
    await s.close()


def _console_event_types() -> set[str]:
    """The ``type`` literals of the console's ``ChatEvent`` union."""

    source = _TYPES_TS.read_text(encoding="utf-8")
    start = source.index("export type ChatEvent =")
    end = source.index("\nexport ", start)
    return set(re.findall(r"type: '([a-z_]+)'", source[start:end]))


def test_the_console_renders_exactly_what_the_server_sends() -> None:
    console = _console_event_types()
    assert console, "the ChatEvent union could not be read — has it been renamed?"
    assert console == set(CHAT_EVENT_TYPES), (
        "the chat event vocabulary drifted: "
        f"only the server sends {sorted(set(CHAT_EVENT_TYPES) - console)}, "
        f"only the console handles {sorted(console - set(CHAT_EVENT_TYPES))}"
    )


@pytest.mark.asyncio
async def test_a_gated_turn_emits_only_declared_events(store: TelemetryStore) -> None:
    """The joined half: drive a real turn through a gate and its confirmation.

    Covers the whole path the operator's screen shows — the held call, the
    decision, the confirmed run and the reply after it — and pins that every
    frame it produces is one the console knows how to fold in.
    """

    executor, _registry, tunnel = _executor(store)

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        return {"stdout": "449 GB"}

    tunnel.send_request = fake_send_request  # type: ignore[assignment]

    session = FakeSession(id="seam", agent_id="linus-pc")
    session.messages.append({"role": "user", "content": "where has the space gone?"})
    policy = StubPolicy({"powershell_exec": Hold("operator_approval")})
    client = FakeAnthropic(
        [
            _Response(
                [tool_use_block("tu1", "fs_disk_usage", {})],
                "tool_use",
            ),
            _Response(
                [tool_use_block("tu2", "powershell_exec", {"script": "Get-ChildItem C:\\"})],
                "tool_use",
            ),
            _Response([text_block("Program Files (x86) holds 449 GB.")], "end_turn"),
        ]
    )

    held = await _drive(session, executor, client, policy)
    assert session.pending is not None  # the gate really opened
    resumed = await _collect(confirmation_events(session, approve=True, executor=executor))
    finished = await _collect(
        drive_events(session, executor, client=client, model="test-model", policy=policy)
    )

    emitted = {ev["type"] for ev in [*held, *resumed, *finished]}
    assert emitted <= LOOP_EVENT_TYPES, (
        f"the loop emitted {sorted(emitted - LOOP_EVENT_TYPES)}, which is not in "
        "LOOP_EVENT_TYPES and therefore not in the console's union either"
    )
    # And the path really did exercise the states this is about.
    assert {"pending", "tool_started", "tool_result", "done"} <= emitted

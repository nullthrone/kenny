"""A malformed frame from a connected agent must be dropped, not crash the tunnel.

``AgentTunnel._serve``/``_handshake`` read one WebSocket text message at a time and
feed it to ``parse_frame``. Every other malformed-input path in this file logs and
either closes (4400) or ``continue``s past it — but ``parse_frame`` itself was called
unguarded, so a single non-conforming JSON payload (or plain garbage) raised an
uncaught ``pydantic.ValidationError`` out of the ``_serve``/``_handshake`` loop
instead of being dropped like any other bad frame. Found by fuzzing the tunnel with
malformed wire input; regression test for both call sites.
"""

from __future__ import annotations

import pytest

from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tunnel import AgentTunnel


class _FakeWebSocket:
    """Yields queued frames, then raises so the test can detect the loop exiting."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.closed_code: int | None = None

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise _OutOfFrames

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class _OutOfFrames(Exception):
    """Sentinel: the fake socket ran out of queued frames."""


@pytest.mark.parametrize(
    "garbage",
    [
        "not json at all {{{",
        "null",
        "42",
        "[]",
        '"just a string"',
        '{"type": "telemetry"}',  # valid JSON, missing required fields
        '{"type": "nonexistent_frame_kind"}',
    ],
)
@pytest.mark.asyncio
async def test_serve_drops_malformed_frame_without_crashing(tmp_path, garbage) -> None:
    store = TelemetryStore(str(tmp_path / "t.sqlite"))
    events = EventStore(str(tmp_path / "t.sqlite"))
    await store.connect()
    await events.connect()
    registry = AgentRegistry(tokens={"agent-x": "tok"})
    registry.register("agent-x", "tok", {}, send_fn=lambda payload: None)

    ws = _FakeWebSocket([garbage])
    with pytest.raises(_OutOfFrames):
        await tunnel_serve(registry, store, events, ws)

    await store.close()
    await events.close()


async def tunnel_serve(registry, store, events, ws) -> None:
    tunnel = AgentTunnel(registry, store, events)
    await tunnel._serve(ws, "agent-x")


@pytest.mark.asyncio
async def test_handshake_closes_4400_on_unparseable_first_frame(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "t.sqlite"))
    events = EventStore(str(tmp_path / "t.sqlite"))
    await store.connect()
    await events.connect()
    registry = AgentRegistry(tokens={})
    tunnel = AgentTunnel(registry, store, events)

    ws = _FakeWebSocket(["{{{ not json"])
    agent_id = await tunnel._handshake(ws)

    assert agent_id is None
    assert ws.closed_code == 4400

    await store.close()
    await events.close()

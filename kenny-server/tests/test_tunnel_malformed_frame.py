"""A malformed frame must be rejected/dropped, never crash the tunnel.

``parse_frame`` raises ``pydantic.ValidationError`` for anything that isn't
valid JSON or doesn't match one of the known frame shapes -- including for the
first (pre-auth) frame on ``/agent/ws``, reachable by anyone who can open a
socket, and for every later frame an already-authenticated agent can push at
will. Neither path may let that exception propagate: the handshake closes
4400 (same as "first frame was not register"); ``_serve`` logs and drops the
one bad frame, keeping the tunnel open, the same way it already drops an
oversized frame.
"""

from __future__ import annotations

import json

import pytest

from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tunnel import AgentTunnel


class _FakeWebSocket:
    """Yields queued frames, then records the close code."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.closed_code: int | None = None
        self.sent: list[dict] = []

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise AssertionError("receive_text called after the socket should have closed")

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


async def _make_tunnel(tmp_path):
    store = TelemetryStore(str(tmp_path / "t.sqlite"))
    events = EventStore(str(tmp_path / "t.sqlite"))
    await store.connect()
    await events.connect()
    registry = AgentRegistry(tokens={"pc1": "tok"})
    tunnel = AgentTunnel(registry, store, events)
    return tunnel, registry, store, events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{",
        "null",
        '{"type": "unknown_type"}',
        '{"type": "register"}',  # valid JSON, but missing required fields
    ],
)
async def test_handshake_rejects_malformed_first_frame(tmp_path, raw: str) -> None:
    tunnel, registry, store, events = await _make_tunnel(tmp_path)
    ws = _FakeWebSocket([raw])

    agent_id = await tunnel._handshake(ws)

    assert agent_id is None
    assert ws.closed_code == 4400

    await store.close()
    await events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{",
        '{"type": "telemetry"}',  # missing required fields
        '{"type": "telemetry", "agent_id": "pc1", "collected_at": "x", "snapshot": null}',
        '{"type": "unknown_type"}',
    ],
)
async def test_serve_drops_malformed_frame_and_keeps_the_tunnel_open(
    tmp_path, raw: str
) -> None:
    tunnel, registry, store, events = await _make_tunnel(tmp_path)
    await registry.register_async(
        "pc1", "tok", {"hostname": "h", "os": "windows", "version": "1"}, lambda p: None
    )

    good_frame = json.dumps(
        {
            "type": "telemetry",
            "agent_id": "pc1",
            "collected_at": "2026-07-01T00:00:00+00:00",
            "snapshot": {},
        }
    )
    ws = _FakeWebSocket([raw, good_frame])

    # A malformed frame must not raise out of _serve; the loop keeps going and
    # processes the next (valid) frame, which then exhausts the fake socket's
    # queue and raises the sentinel AssertionError -- proof the loop survived.
    with pytest.raises(AssertionError, match="receive_text called"):
        await tunnel._serve(ws, "pc1")

    assert ws.closed_code is None
    assert await store.latest("pc1") is not None

    await store.close()
    await events.close()

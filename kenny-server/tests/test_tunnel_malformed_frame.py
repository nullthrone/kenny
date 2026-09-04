"""A malformed/adversarial frame must never crash the tunnel (fuzzing sweep).

``protocol.parse_frame`` raises ``pydantic.ValidationError`` for anything that
doesn't match one of the seven frame shapes -- bad JSON, an unknown ``type``
discriminator, missing required fields, or an extra field (every frame model is
``extra="forbid"``). Before this fix, both call sites in ``tunnel.py`` let that
exception escape unhandled: one malformed frame from an unauthenticated peer
(the first frame of the handshake) or from an already-authenticated agent (any
frame in the serve loop) took down that connection's handling coroutine with an
uncaught traceback instead of the same graceful drop the module already applies
to an oversized frame.
"""

from __future__ import annotations

import json

import pytest

from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tunnel import AgentTunnel


class _FakeWebSocket:
    """Yields queued frames, then records the close code / sent payloads."""

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


@pytest.mark.asyncio
async def test_handshake_rejects_malformed_first_frame(tmp_path) -> None:
    """A first frame that fails to parse closes 4400, instead of raising."""

    store = TelemetryStore(str(tmp_path / "t.sqlite"))
    events = EventStore(str(tmp_path / "t.sqlite"))
    await store.connect()
    await events.connect()
    registry = AgentRegistry(tokens={})
    tunnel = AgentTunnel(registry, store, events)

    ws = _FakeWebSocket(["{not even json"])

    result = await tunnel._handshake(ws)

    assert result is None
    assert ws.closed_code == 4400

    await store.close()
    await events.close()


@pytest.mark.asyncio
async def test_serve_drops_malformed_frame_and_keeps_connection(tmp_path) -> None:
    """A malformed frame mid-session is dropped + logged, not raised.

    The next, well-formed frame (a ``ping``) is still processed on the same
    connection -- proof the malformed frame did not tear down the tunnel.
    """

    store = TelemetryStore(str(tmp_path / "t.sqlite"))
    events = EventStore(str(tmp_path / "t.sqlite"))
    await store.connect()
    await events.connect()
    registry = AgentRegistry(tokens={"pc1": "tok"})
    registry.register(
        "pc1", "tok", {"hostname": "h", "os": "windows", "version": "1"}, lambda payload: None
    )
    tunnel = AgentTunnel(registry, store, events)

    ws = _FakeWebSocket(["{not even json", json.dumps({"type": "ping"})])

    with pytest.raises(AssertionError, match="receive_text called after"):
        # `_serve` loops forever; it only stops once the fake socket runs out
        # of queued frames, which is the expected way to end this test.
        await tunnel._serve(ws, "pc1")

    # The malformed frame was dropped (no crash) and the following `ping` was
    # still served with a `pong` on the same connection.
    assert ws.sent == [{"type": "pong"}]

    await store.close()
    await events.close()

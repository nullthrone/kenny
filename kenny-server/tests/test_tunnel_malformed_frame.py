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

import base64
import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kenny_server.main import build_app
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


# -- binary WebSocket messages ------------------------------------------------
#
# Every kenny frame is a JSON text message. Starlette's ``receive_text`` raises
# ``KeyError`` on a binary one, so these go through the real ASGI app: a fake
# socket cannot show what Starlette does with the message.


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_AGENT_TOKENS", "pc1=tok")
    app = build_app(db_path=str(tmp_path / "binary.sqlite"))
    with TestClient(app) as c:
        yield c


_REGISTER = {
    "type": "register",
    "agent_id": "pc1",
    "token": "tok",
    "meta": {"hostname": "h", "os": "windows", "version": "1"},
}


def test_binary_first_frame_closes_4400(client) -> None:
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_bytes(b"\x00\x01")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4400


def test_binary_auth_reply_closes_4401(client) -> None:
    signed = {
        **_REGISTER,
        "protocol": "0.8",
        "client_nonce": base64.b64encode(bytes(32)).decode(),
    }
    del signed["token"]
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_text(json.dumps(signed))
        assert ws.receive_json()["type"] == "challenge"
        ws.send_bytes(b"\x00")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4401


def test_binary_frame_from_a_connected_agent_is_dropped(client) -> None:
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_text(json.dumps(_REGISTER))
        ws.send_bytes(b"\x00\x01")
        ws.send_text(json.dumps({"type": "ping"}))
        # The policy frame(s) come first; the pong proves the tunnel survived.
        while ws.receive_json()["type"] != "pong":
            pass

"""SSE chat endpoints: ``/api/chat/stream`` and ``/api/chat/confirm/stream``.

These drive the streaming routes through Starlette's ``TestClient`` with the
fake Anthropic client from ``test_chat`` (no real API key), and assert the wire
shape: ordered ``text_delta`` frames ending in ``done``, live ``tool_result``
frames, the confirm-gate round-trip, and in-band ``error`` frames (status stays
200 once streaming starts). Pre-stream validation returns JSON status codes.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from kenny_server.chat import ChatSessions
from kenny_server.main import build_app
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel
from kenny_server.webui import build_chat_routes

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block


class _BoomMessages:
    def stream(self, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")


class _BoomAnthropic:
    def __init__(self) -> None:
        self.messages = _BoomMessages()


def _build_app(tmp_path, scripts: list[list[_Response]]):
    """A minimal app exposing only the chat routes; one fake client per request.

    ``scripts`` is a list of scripted-response lists — one per HTTP request, in
    order (e.g. [turn, resume] for a confirm-gate flow).
    """

    store = TelemetryStore(db_path=str(tmp_path / "stream.sqlite"))
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    sessions = ChatSessions()
    queue = list(scripts)

    def factory() -> Any:
        return FakeAnthropic(queue.pop(0)) if queue else FakeAnthropic([])

    routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=sessions,
        screenshots=ScreenshotStore(),
        client_factory=factory,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await store.connect()
        yield
        await store.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.registry = registry  # let tests stub the active agent / tunnel
    app.state.tunnel = tunnel
    return app


def _frames(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


def test_stream_endpoint_streams_text(tmp_path):
    app = _build_app(tmp_path, [[_Response([text_block("The fleet is healthy.")], "end_turn")]])
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"message": "How is the fleet?"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = _frames(r.text)
        deltas = [f["text"] for f in frames if f["type"] == "text_delta"]
        assert len(deltas) > 1
        assert "".join(deltas) == "The fleet is healthy."
        assert frames[-1]["type"] == "done" and frames[-1]["done"] is True
        assert frames[-1]["session_id"]


def test_stream_endpoint_tool_then_text(tmp_path):
    app = _build_app(
        tmp_path,
        [
            [
                _Response([tool_use_block("tu1", "fleet_overview", {})], "tool_use"),
                _Response([text_block("All green.")], "end_turn"),
            ]
        ],
    )
    with TestClient(app) as c:
        frames = _frames(c.post("/api/chat/stream", json={"message": "status?"}).text)
        types = [f["type"] for f in frames]
        assert types.index("tool_result") < types.index("text_delta")


def test_stream_confirm_gate_round_trip(tmp_path):
    app = _build_app(
        tmp_path,
        [
            [_Response([tool_use_block("tu2", "winget_install", {"id": "Git.Git"})], "tool_use")],
            [_Response([text_block("Git is installed.")], "end_turn")],
        ],
    )
    # Stub the capability path so no real agent is needed.
    sent: list[str] = []

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        sent.append(tool)
        return {"installed": True}

    app.state.tunnel.send_request = fake_send_request  # type: ignore[assignment]
    app.state.registry._active_agent = "dev"

    with TestClient(app) as c:
        frames = c.post("/api/chat/stream", json={"message": "install git"}).text
        f1 = _frames(frames)
        pending = [f for f in f1 if f["type"] == "pending"]
        assert pending and pending[0]["tool"] == "winget_install"
        assert f1[-1]["type"] == "done" and f1[-1]["done"] is False
        sid = f1[-1]["session_id"]
        assert sent == []

        f2 = _frames(
            c.post("/api/chat/confirm/stream", json={"session_id": sid, "approve": True}).text
        )
        assert f2[0]["type"] == "tool_result" and f2[0]["tool"] == "winget_install"
        assert "".join(f["text"] for f in f2 if f["type"] == "text_delta") == "Git is installed."
        assert f2[-1]["type"] == "done" and f2[-1]["done"] is True
        assert sent == ["winget_install"]


def test_stream_error_in_band(tmp_path):
    store = TelemetryStore(db_path=str(tmp_path / "boom.sqlite"))
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=ChatSessions(),
        screenshots=ScreenshotStore(),
        client_factory=_BoomAnthropic,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await store.connect()
        yield
        await store.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"message": "hi"})
        # The stream already started (headers sent) so the status is fixed at 200;
        # the failure surfaces as an in-band error frame.
        assert r.status_code == 200
        frames = _frames(r.text)
        assert frames and frames[-1]["type"] == "error"
        assert "boom" in frames[-1]["error"]


def test_stream_empty_message_is_400(tmp_path):
    app = _build_app(tmp_path, [])
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"message": "   "})
        assert r.status_code == 400
        assert "content-type" in r.headers and "json" in r.headers["content-type"]


def test_confirm_stream_unknown_session_is_404(tmp_path):
    app = _build_app(tmp_path, [])
    with TestClient(app) as c:
        r = c.post("/api/chat/confirm/stream", json={"session_id": "nope", "approve": True})
        assert r.status_code == 404


def test_stream_requires_auth(tmp_path):
    # The real app wraps /api/* in OperatorAuthMiddleware; the stream route is
    # gated like every other /api endpoint.
    app = build_app(db_path=str(tmp_path / "auth.sqlite"))
    with TestClient(app) as c:
        assert c.post("/api/chat/stream", json={"message": "hi"}).status_code == 401

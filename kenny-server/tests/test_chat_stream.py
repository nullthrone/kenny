"""SSE chat endpoints: ``/api/chat/stream`` and ``/api/chat/confirm/stream``.

These drive the streaming routes through Starlette's ``TestClient`` with the
fake Anthropic client from ``test_chat`` (no real API key), and assert the wire
shape: ordered ``text_delta`` frames ending in ``done``, live ``tool_result``
frames, the confirm-gate round-trip, and in-band ``error`` frames (status stays
200 once streaming starts). Pre-stream validation returns JSON status codes.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from kenny_server.chat import ChatSessions
from kenny_server.main import build_app
from kenny_server.registry import AgentRegistry
from kenny_server.store import ChatHistoryStore, EventStore, TelemetryStore
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
    history_store = ChatHistoryStore(db_path=store.db_path)
    sessions = ChatSessions(store=history_store)
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
        history_store=history_store,
        client_factory=factory,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await store.connect()
        await history_store.connect()
        yield
        await store.close()
        await history_store.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.registry = registry  # let tests stub the active agent / tunnel
    app.state.tunnel = tunnel
    app.state.history_store = history_store
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
    history_store = ChatHistoryStore(db_path=store.db_path)
    routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=ChatSessions(store=history_store),
        screenshots=ScreenshotStore(),
        history_store=history_store,
        client_factory=_BoomAnthropic,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await store.connect()
        await history_store.connect()
        yield
        await store.close()
        await history_store.close()

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


# -- persistence (ADR-0027) --------------------------------------------------


def test_stream_persists_after_turn_completes(tmp_path):
    app = _build_app(tmp_path, [[_Response([text_block("The fleet is healthy.")], "end_turn")]])
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"message": "How is the fleet?"})
        sid = _frames(r.text)[-1]["session_id"]

        row = asyncio.run(app.state.history_store.get(sid))
        assert row is not None
        assert row["messages"][-1]["content"] == [{"type": "text", "text": "The fleet is healthy."}]


def test_stream_confirm_gate_persists_and_heals_cleanly_on_reload(tmp_path):
    """A conversation persisted mid-pause still carries the unresolved tool_use

    (that's the honest state of the turn — pending state itself is never
    persisted). Loading it back through ``ChatSessions.get`` — as happens after
    a restart — runs ``heal_session`` and produces a usable session with that
    dangling call dropped, per ADR-0027.
    """

    app = _build_app(
        tmp_path,
        [
            [_Response([tool_use_block("tu2", "winget_install", {"id": "Git.Git"})], "tool_use")],
            [_Response([text_block("Git is installed.")], "end_turn")],
        ],
    )

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        return {"installed": True}

    app.state.tunnel.send_request = fake_send_request  # type: ignore[assignment]
    app.state.registry._active_agent = "dev"

    with TestClient(app) as c:
        f1 = _frames(c.post("/api/chat/stream", json={"message": "install git"}).text)
        sid = f1[-1]["session_id"]

        row = asyncio.run(app.state.history_store.get(sid))
        assert row is not None
        last = row["messages"][-1]
        assert last["role"] == "assistant"
        assert any(b.get("type") == "tool_use" for b in last["content"])

        # A fresh ChatSessions (simulating a restart: no in-memory cache) heals
        # the dangling tool_use on load into a clean, model-callable session.
        from kenny_server.chat import ChatSessions, heal_session

        fresh = ChatSessions(store=app.state.history_store)
        healed = asyncio.run(fresh.get(sid))
        assert healed is not None
        assert not healed.messages or healed.messages[-1]["role"] != "assistant"
        heal_session(healed)  # idempotent — a second heal is a no-op
        assert not healed.messages or healed.messages[-1]["role"] != "assistant"

        c.post("/api/chat/confirm/stream", json={"session_id": sid, "approve": True})
        row = asyncio.run(app.state.history_store.get(sid))
        assert row is not None
        assert row["messages"][-1]["content"] == [{"type": "text", "text": "Git is installed."}]


def test_history_list_route(tmp_path):
    app = _build_app(
        tmp_path,
        [
            [_Response([text_block("first answer")], "end_turn")],
            [_Response([text_block("second answer")], "end_turn")],
        ],
    )
    with TestClient(app) as c:
        c.post("/api/chat/stream", json={"message": "first question"})
        c.post("/api/chat/stream", json={"message": "second question"})
        r = c.get("/api/chat/history")
        assert r.status_code == 200
        rows = r.json()["conversations"]
        assert len(rows) == 2
        assert all("messages" not in row for row in rows)
        assert {row["title"] for row in rows} == {"first question", "second question"}


def test_history_get_route_returns_transcript(tmp_path):
    app = _build_app(
        tmp_path,
        [[_Response([tool_use_block("tu1", "fleet_overview", {})], "tool_use"),
          _Response([text_block("All green.")], "end_turn")]],
    )
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"message": "status?"})
        sid = _frames(r.text)[-1]["session_id"]

        got = c.get(f"/api/chat/history/{sid}")
        assert got.status_code == 200
        body = got.json()
        assert body["id"] == sid
        types = [e["type"] for e in body["transcript"]]
        assert types == ["user_text", "tool_result", "text_delta"]


def test_history_get_route_404_for_unknown_id(tmp_path):
    app = _build_app(tmp_path, [])
    with TestClient(app) as c:
        assert c.get("/api/chat/history/nope").status_code == 404


def test_history_delete_route(tmp_path):
    app = _build_app(tmp_path, [[_Response([text_block("hi there")], "end_turn")]])
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"message": "hello"})
        sid = _frames(r.text)[-1]["session_id"]

        assert c.delete(f"/api/chat/history/{sid}").json() == {"ok": True}
        assert c.delete(f"/api/chat/history/{sid}").status_code == 404
        assert c.get(f"/api/chat/history/{sid}").status_code == 404


def test_history_routes_require_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "auth2.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/chat/history").status_code == 401
        assert c.get("/api/chat/history/x").status_code == 401
        assert c.delete("/api/chat/history/x").status_code == 401


def test_dashboard_agent_selection_reaches_the_model_and_clears(tmp_path):
    """Regression: the dashboard's "context: <agent>" pill only ever scoped
    tool routing — the model itself had no lexical signal of the selection and
    couldn't answer "which PC is this?" without calling a tool first. The
    ``agent_id`` on a chat request must now also land in the outgoing
    ``system`` blocks (``chat._context_note``), and must clear again once the
    dashboard switches back to fleet-wide instead of lingering.
    """

    store = TelemetryStore(db_path=str(tmp_path / "ctxnote.sqlite"))
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    history_store = ChatHistoryStore(db_path=store.db_path)
    sessions = ChatSessions(store=history_store)
    scripts = [
        [_Response([text_block("linus-pc looks fine.")], "end_turn")],
        [_Response([text_block("Here is the fleet overview.")], "end_turn")],
    ]
    clients: list[FakeAnthropic] = []

    def factory() -> Any:
        client = FakeAnthropic(scripts.pop(0))
        clients.append(client)
        return client

    routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=sessions,
        screenshots=ScreenshotStore(),
        history_store=history_store,
        client_factory=factory,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await store.connect()
        await history_store.connect()
        yield
        await store.close()
        await history_store.close()

    app = Starlette(routes=routes, lifespan=lifespan)

    with TestClient(app) as c:
        r1 = c.post(
            "/api/chat/stream",
            json={"message": "which pc is this?", "agent_id": "linus-pc"},
        )
        sid = _frames(r1.text)[-1]["session_id"]
        system1 = clients[0].messages.calls[-1]["system"]
        assert any("linus-pc" in block["text"] for block in system1)

        r2 = c.post(
            "/api/chat/stream",
            json={"session_id": sid, "message": "and the whole fleet?", "agent_id": ""},
        )
        assert r2.status_code == 200
        system2 = clients[1].messages.calls[-1]["system"]
        assert not any("linus-pc" in block["text"] for block in system2)

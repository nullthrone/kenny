"""A request body that is valid JSON but not a JSON object must get a 400,
never an unhandled exception.

Several ``/api/*`` handlers called ``body.get(...)`` (or ``"x" not in body`` /
``body["x"]``) straight off ``await request.json()`` with no guard against the
body being something other than an object -- an array, a string, a number, or
``null`` all parse as valid JSON but have no ``.get``, so the handler raised an
unhandled ``AttributeError``/``TypeError`` that surfaced as a bare 500 instead
of the ``400`` every malformed-JSON-body path already returns. ``/api/chat``
and its streaming/confirm twins additionally had no ``try/except`` around
``request.json()`` at all, so even syntactically malformed JSON crashed them.

These are a fuzzing find (adversarial JSON bodies against every ``/api/*``
handler that parses one), not a hypothetical: a bare ``[]`` body reproduced
both shapes of the bug against a real running app.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kenny_server.chat import ChatSessions
from kenny_server.main import build_app
from kenny_server.registry import AgentRegistry
from kenny_server.store import ChatHistoryStore, EventStore, TelemetryStore
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel
from kenny_server.webui import build_chat_routes

from test_chat import FakeAnthropic


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chat routes 503-gate on the AI feature being enabled; these tests are
    about JSON-body handling ahead of that, not about AI itself."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def test_policy_add_rejects_non_object_body_with_400(tmp_path) -> None:
    """``POST /api/policy/rules`` used to crash with ``AttributeError`` on a
    body that is valid JSON but not an object (``body.get`` on a ``list``)."""

    app = build_app(db_path=str(tmp_path / "api.sqlite"))
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/api/policy/rules",
            headers=_bearer(app),
            content=b"[]",
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "body must be a JSON object"}


def _build_chat_app(tmp_path) -> Starlette:
    """A minimal app exposing only the chat routes (mirrors test_chat_stream.py)."""

    store = TelemetryStore(db_path=str(tmp_path / "chat.sqlite"))
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    history_store = ChatHistoryStore(db_path=store.db_path)
    sessions = ChatSessions(store=history_store)

    routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=sessions,
        screenshots=ScreenshotStore(),
        history_store=history_store,
        client_factory=lambda: FakeAnthropic([]),
    )

    @asynccontextmanager
    async def lifespan(_app: Any):
        await store.connect()
        await history_store.connect()
        yield
        await store.close()
        await history_store.close()

    return Starlette(routes=routes, lifespan=lifespan)


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_rejects_malformed_json_body_with_400(tmp_path, path: str) -> None:
    """``request.json()`` used to be called with no ``try/except`` at all here,
    so syntactically malformed JSON raised ``JSONDecodeError`` straight out of
    the handler instead of the ``400`` every other JSON-body route returns."""

    app = _build_chat_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(path, content=b"not json", headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid JSON body"}


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_rejects_non_object_json_body_with_400(tmp_path, path: str) -> None:
    """A body that is valid JSON but not an object (e.g. ``[]``) used to reach
    ``body.get("message")`` and crash with ``AttributeError``."""

    app = _build_chat_app(tmp_path)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(path, content=b"[]", headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json() == {"error": "body must be a JSON object"}

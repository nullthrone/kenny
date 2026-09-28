"""The AI gateway (ADR-0068), joined end to end.

The gateway here is a real HTTP server on loopback that speaks just enough of
the Anthropic Messages API. The server under test uses the real SDK client —
no ``client_factory`` — so what these tests see is what goes on the wire: the
URL, the headers, the key and the model id each feature sends, set the way the
dashboard sets them (``PUT /api/settings/{key}``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server import ai, event_categories, forecast, recommend
from kenny_server.main import build_app

_GATEWAY_KEYS = (
    "ANTHROPIC_API_KEY",
    ai.BASE_URL_SETTING,
    ai.HEADERS_SETTING,
    ai.FAST_MODEL_SETTING,
    ai.MASTER_SETTING,
    *ai.FEATURES.values(),
)


@pytest.fixture(autouse=True)
def _no_ambient_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _GATEWAY_KEYS:
        monkeypatch.delenv(key, raising=False)
    recommend._cache.clear()
    forecast._cache.clear()
    yield
    recommend._cache.clear()
    forecast._cache.clear()


# -- the fake gateway -------------------------------------------------------


def _message(model: str, text: str) -> dict[str, Any]:
    return {
        "id": "msg_gw",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _sse(model: str, text: str) -> bytes:
    start = _message(model, "")
    start["content"], start["stop_reason"] = [], None
    events = [
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


class FakeGateway:
    """Records every request; answers with a message, a stream, or ``reject``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reject: tuple[int, dict[str, Any]] | None = None
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 - the http.server API
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                gateway.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if gateway.reject is not None:
                    status, payload = gateway.reject
                    self._send(status, "application/json", json.dumps(payload).encode())
                elif body.get("stream"):
                    self._send(200, "text/event-stream", _sse(body["model"], "Diagnosis: fine."))
                else:
                    self._send(
                        200, "application/json", json.dumps(_message(body["model"], "ok")).encode()
                    )

            def _send(self, status: int, ctype: str, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeGateway:
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def gateway() -> Iterator[FakeGateway]:
    with FakeGateway() as gw:
        yield gw


def _bearer(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _put(c: TestClient, app: Any, key: str, value: Any) -> None:
    r = c.put(f"/api/settings/{key}", headers=_bearer(app), json={"value": value})
    assert r.status_code == 200, r.text


def _configure(c: TestClient, app: Any, gw: FakeGateway) -> None:
    _put(c, app, ai.BASE_URL_SETTING, gw.url)
    _put(c, app, ai.HEADERS_SETTING, "X-Gateway-Key: gw-secret; X-Gateway-Route: fleet")
    _put(c, app, ai.FAST_MODEL_SETTING, "gateway-fast")


# -- the seam: every fast-model feature goes through the gateway ------------


def test_every_fast_model_call_reaches_the_gateway_as_configured(tmp_path, gateway) -> None:
    app = build_app(db_path=str(tmp_path / "gw.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        _configure(c, app, gateway)

        # No key: the gateway holds the provider credentials, so AI is on.
        status = c.get("/api/ai/status", headers=h).json()
        assert status["configured"] is True
        assert status["source"] == "none"
        assert status["gateway"] == "127.0.0.1"
        assert all(status["features"].values())
        assert app.state.tickets._triage is not None

        assert c.post("/api/ai/test", headers=h).json() == {"ok": True, "error": None}

        client = ai.current().client()
        groups = [{"source": "disk", "event_id": 51, "sample": "bad block"}]
        asyncio.run(event_categories._classify(client, groups))
        facts = {"section": "disk", "status": "warn", "summary": "C: 91%", "reason": "C: 91%"}
        asyncio.run(_drain(recommend.recommend_events(client, facts)))
        asyncio.run(
            _drain(forecast.forecast_events(client, forecast.build_facts(None, [], None, [])))
        )

    assert len(gateway.requests) == 4  # probe, classify, recommend, forecast
    for req in gateway.requests:
        assert req["path"] == "/v1/messages"
        assert req["body"]["model"] == "gateway-fast"
        assert req["headers"]["x-gateway-key"] == "gw-secret"
        assert req["headers"]["x-gateway-route"] == "fleet"
        assert req["headers"]["x-api-key"] == ai.GATEWAY_PLACEHOLDER_KEY
    assert [bool(r["body"].get("stream")) for r in gateway.requests] == [False, False, True, True]


async def _drain(events: Any) -> list[dict[str, Any]]:
    out = [ev async for ev in events]
    assert not [ev for ev in out if ev["type"] == "error"], out
    return out


def test_a_key_is_sent_to_the_gateway_when_one_is_set(tmp_path, gateway) -> None:
    app = build_app(db_path=str(tmp_path / "gwkey.sqlite"))
    with TestClient(app) as c:
        _configure(c, app, gateway)
        _put(c, app, "ANTHROPIC_API_KEY", "sk-provider")
        assert c.post("/api/ai/test", headers=_bearer(app)).json()["ok"] is True
    assert gateway.requests[-1]["headers"]["x-api-key"] == "sk-provider"


def test_the_verdict_tag_follows_the_fast_model(tmp_path, gateway) -> None:
    """A changed model re-classifies on the next start, as a model upgrade did."""

    app = build_app(db_path=str(tmp_path / "tag.sqlite"))
    with TestClient(app) as c:
        assert event_categories.verdict_model_tag().startswith(f"{ai.DEFAULT_FAST_MODEL}/")
        _put(c, app, ai.FAST_MODEL_SETTING, "gateway-fast")
        assert event_categories.verdict_model_tag().startswith("gateway-fast/")


# -- what the gateway says back reaches the operator ------------------------


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "gateway key rejected"), (446, "blocked by content policy")],
)
def test_a_refusal_from_the_gateway_is_reported_verbatim(
    tmp_path, gateway, status, message
) -> None:
    gateway.reject = (status, {"type": "error", "error": {"type": "gateway", "message": message}})
    app = build_app(db_path=str(tmp_path / "refuse.sqlite"))
    with TestClient(app) as c:
        _configure(c, app, gateway)
        result = c.post("/api/ai/test", headers=_bearer(app)).json()
    assert result["ok"] is False
    assert message in result["error"]
    assert len(gateway.requests) == 1  # a refusal is not retried


def test_an_invalid_environment_value_is_reported_without_calling_out(gateway) -> None:
    access = ai.AiAccess(env={ai.BASE_URL_SETTING: gateway.url, ai.HEADERS_SETTING: "Host: evil"})
    result = access.probe()
    assert result["ok"] is False and ai.HEADERS_SETTING in result["error"]
    assert gateway.requests == []


# -- settings: validated on write, never read back, never backed up ---------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (ai.BASE_URL_SETTING, "ftp://gateway.example"),
        (ai.BASE_URL_SETTING, "http://gateway.example"),
        (ai.BASE_URL_SETTING, "https://user:pw@gateway.example"),
        (ai.BASE_URL_SETTING, "https://gateway.example/?tenant=a"),
        (ai.HEADERS_SETTING, "no colon here"),
        (ai.HEADERS_SETTING, "Content-Length: 0"),
        (ai.HEADERS_SETTING, "X-Api-Key: sk-sneaky"),
        (ai.HEADERS_SETTING, "X-A: 1; x-a: 2"),
    ],
)
def test_an_invalid_gateway_setting_is_rejected(tmp_path, key, value) -> None:
    app = build_app(db_path=str(tmp_path / "invalid.sqlite"))
    with TestClient(app) as c:
        r = c.put(f"/api/settings/{key}", headers=_bearer(app), json={"value": value})
        assert r.status_code == 400, r.text
        assert c.get("/api/ai/status", headers=_bearer(app)).json()["configured"] is False


def test_the_headers_are_never_read_back_nor_backed_up(tmp_path, gateway) -> None:
    app = build_app(db_path=str(tmp_path / "live.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        _configure(c, app, gateway)
        assert "gw-secret" not in c.get("/api/settings", headers=h).text
        assert "gw-secret" not in c.get("/api/ai/status", headers=h).text

        result = c.portal.call(app.state.backup_mgr.create, "manual")
        copy = Path(app.state.backup_mgr.backup_dir) / result["name"]
        with sqlite3.connect(copy) as conn:
            stored = dict(conn.execute("SELECT key, value FROM settings"))
        assert ai.HEADERS_SETTING not in stored
        assert stored[ai.BASE_URL_SETTING] == gateway.url
        assert b"gw-secret" not in copy.read_bytes()


def test_clearing_the_gateway_without_a_key_switches_ai_off(tmp_path, gateway) -> None:
    app = build_app(db_path=str(tmp_path / "clear.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        _configure(c, app, gateway)
        assert app.state.tickets._triage is not None
        c.delete(f"/api/settings/{ai.BASE_URL_SETTING}", headers=h)
        status = c.get("/api/ai/status", headers=h).json()
        assert status["configured"] is False and status["gateway"] is None
        assert app.state.tickets._triage is None


# -- the client and the parser ---------------------------------------------


def test_the_client_is_rebuilt_when_the_gateway_changes(monkeypatch) -> None:
    built: list[tuple[str, str, dict[str, str]]] = []
    monkeypatch.setattr(
        ai,
        "_default_factory",
        lambda key, url, headers: built.append((key, url, headers)) or object(),
    )
    env = {ai.BASE_URL_SETTING: "https://one.example/"}
    access = ai.AiAccess(env=env)
    first = access.client()
    assert access.client() is first
    env[ai.HEADERS_SETTING] = "X-Gateway-Key: k"
    second = access.client()
    assert second is not first
    env[ai.BASE_URL_SETTING] = "https://two.example"
    assert access.client() is not second
    assert built == [
        (ai.GATEWAY_PLACEHOLDER_KEY, "https://one.example", {}),
        (ai.GATEWAY_PLACEHOLDER_KEY, "https://one.example", {"X-Gateway-Key": "k"}),
        (ai.GATEWAY_PLACEHOLDER_KEY, "https://two.example", {"X-Gateway-Key": "k"}),
    ]


def test_a_refused_header_is_never_echoed() -> None:
    """The error reaches the log and the dashboard; a pasted secret must not."""

    for raw in ("sk-pasted-without-a-name", "Bad Name sk-pasted: v"):
        with pytest.raises(ValueError) as caught:
            ai.parse_headers(raw)
        assert "sk-pasted" not in str(caught.value)


def test_headers_parse_from_one_line_or_many() -> None:
    raw = 'X-Gateway-Key: a:b\n\nX-Gateway-Config: {"retry": 2};X-Trace: on\r\n'
    assert ai.parse_headers(raw) == {
        "X-Gateway-Key": "a:b",
        "X-Gateway-Config": '{"retry": 2}',
        "X-Trace": "on",
    }
    assert ai.parse_headers("") == {}


@pytest.mark.parametrize(
    "raw",
    ["X-A: one\x00two", "X-A:", "Bad Name: v", "Host: gateway.example", "Anthropic-Version: 1"],
)
def test_headers_that_must_not_reach_the_wire_are_refused(raw) -> None:
    with pytest.raises(ValueError):
        ai.parse_headers(raw)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("", True),
        ("https://gateway.example", True),
        ("https://gateway.example/tenant/a", True),
        ("http://localhost:8080", True),
        ("http://127.0.0.1:4000", True),
        ("http://gateway:8080", True),  # a container-network name
        ("http://gateway.example", False),
        ("http://[2001:db8::1]:8080", False),  # no dot, but not local
        ("http://[::1]:8080", True),
        ("gateway.example", False),
    ],
)
def test_base_url_check(url, ok) -> None:
    if ok:
        ai.check_base_url(url)
    else:
        with pytest.raises(ValueError):
            ai.check_base_url(url)

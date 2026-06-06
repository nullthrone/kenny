"""AI Recommendation generation, caching, and the SSE route.

Uses the fake Anthropic streaming client from ``test_chat`` (no API key). Covers
the fixed-shape generation (sentinel stripped from the visible prose), the
remediation directive parse, the result-cache replay (no second model call), and
the route's validation + happy path.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kenny_server import recommend
from kenny_server.chat import ChatSessions
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel
from kenny_server.webui import build_chat_routes

from test_chat import FakeAnthropic, _Response, text_block
from test_chat_stream import _BoomAnthropic, _build_app, _frames

_DISK_FACTS = {
    "section": "disk",
    "status": "warn",
    "summary": "C: 91% full",
    "reason": "C: 91% full (>80%)",
}

_YES = (
    "Diagnosis: The C: drive is nearly full.\n"
    "Action: Clear temporary files and the Recycle Bin.\n"
    "Urgency: soon - low free space risks failures.\n"
    "---\n"
    "REMEDIATE: yes\n"
    "PROMPT: Free up space on the selected PC by clearing temp files."
)

_NO = (
    "Diagnosis: A reboot is pending.\n"
    "Action: Restart the PC at a convenient time.\n"
    "Urgency: can wait - no immediate risk.\n"
    "---\n"
    "REMEDIATE: no\n"
    "PROMPT:"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    recommend._cache.clear()
    # No replay delay in tests so cache-hit assertions stay fast.
    recommend._REPLAY_DELAY = 0.0
    yield
    recommend._cache.clear()


async def _collect(client: Any, facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [ev async for ev in recommend.recommend_events(client, facts)]


# -- generation -----------------------------------------------------------


async def test_generation_strips_sentinel_and_parses_remediation():
    client = FakeAnthropic([_Response([text_block(_YES)], "end_turn")])
    evs = await _collect(client, dict(_DISK_FACTS))

    prose = "".join(e["text"] for e in evs if e["type"] == "text_delta")
    assert "Diagnosis: The C: drive is nearly full." in prose
    assert "Urgency: soon" in prose
    # The machine block never leaks into the visible prose.
    assert "---" not in prose and "REMEDIATE" not in prose and "PROMPT" not in prose

    rem = [e for e in evs if e["type"] == "remediation"]
    assert rem and rem[0]["available"] is True
    assert rem[0]["prompt"] == "Free up space on the selected PC by clearing temp files."
    assert evs[-1]["type"] == "done"


async def test_generation_remediate_no_has_no_prompt():
    facts = {"section": "reboot_pending", "status": "warn", "summary": "Reboot required", "reason": ""}
    client = FakeAnthropic([_Response([text_block(_NO)], "end_turn")])
    evs = await _collect(client, facts)
    rem = [e for e in evs if e["type"] == "remediation"]
    assert rem and rem[0]["available"] is False and rem[0]["prompt"] == ""


async def test_cache_replays_without_a_second_model_call():
    first = await _collect(FakeAnthropic([_Response([text_block(_YES)], "end_turn")]), dict(_DISK_FACTS))
    # A client that raises if its stream is touched proves the replay is cached.
    second = await _collect(_BoomAnthropic(), dict(_DISK_FACTS))

    p1 = "".join(e["text"] for e in first if e["type"] == "text_delta")
    p2 = "".join(e["text"] for e in second if e["type"] == "text_delta")
    assert p2 == p1.strip()
    assert not any(e["type"] == "error" for e in second)
    rem = [e for e in second if e["type"] == "remediation"]
    assert rem and rem[0]["available"] is True
    assert second[-1]["type"] == "done"


# -- warning_facts --------------------------------------------------------


def test_warning_facts_only_for_flagged_sections():
    snap = {
        "disk": {"status": "warn", "summary": "C: 91% full",
                 "volumes": [{"mount": "C:", "percent_used": 91}]},
        "memory": {"status": "ok", "summary": "42% used", "percent_used": 42},
    }
    assert recommend.warning_facts(snap, "disk")["status"] == "warn"
    assert recommend.warning_facts(snap, "memory") is None  # ok → no recommendation
    assert recommend.warning_facts(snap, "absent") is None
    assert recommend.warning_facts(None, "disk") is None


def test_ai_available_reflects_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert recommend.ai_available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert recommend.ai_available() is True


# -- SSE route ------------------------------------------------------------


def _build_with_snapshot(tmp_path, scripts, snapshots):
    store = TelemetryStore(db_path=str(tmp_path / "rec.sqlite"))
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    queue = list(scripts)

    def factory() -> Any:
        return FakeAnthropic(queue.pop(0)) if queue else FakeAnthropic([])

    routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=ChatSessions(),
        screenshots=ScreenshotStore(),
        client_factory=factory,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await store.connect()
        for agent_id, snap in snapshots.items():
            await store.insert(agent_id, "2026-06-06T00:00:00Z", snap)
        yield
        await store.close()

    return Starlette(routes=routes, lifespan=lifespan)


def test_route_503_when_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = _build_app(tmp_path, [])
    with TestClient(app) as c:
        r = c.post("/api/recommendation/stream", json={"agent_id": "pc", "section": "disk"})
        assert r.status_code == 503


def test_route_400_missing_section(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = _build_app(tmp_path, [])
    with TestClient(app) as c:
        assert c.post("/api/recommendation/stream", json={"agent_id": "pc"}).status_code == 400


def test_route_400_when_section_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = _build_with_snapshot(tmp_path, [], {})  # no snapshot → store.latest None
    with TestClient(app) as c:
        r = c.post("/api/recommendation/stream", json={"agent_id": "pc", "section": "disk"})
        assert r.status_code == 400


def test_route_streams_recommendation(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    snap = {"disk": {"status": "warn", "summary": "C: 91% full",
                     "volumes": [{"mount": "C:", "percent_used": 91}]}}
    app = _build_with_snapshot(tmp_path, [[_Response([text_block(_YES)], "end_turn")]], {"pc": snap})
    with TestClient(app) as c:
        r = c.post("/api/recommendation/stream", json={"agent_id": "pc", "section": "disk"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = _frames(r.text)
        prose = "".join(f["text"] for f in frames if f["type"] == "text_delta")
        assert "Diagnosis:" in prose and "---" not in prose
        rem = [f for f in frames if f["type"] == "remediation"]
        assert rem and rem[0]["available"] is True and rem[0]["prompt"]
        assert frames[-1]["type"] == "done"

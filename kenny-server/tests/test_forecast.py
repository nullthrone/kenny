"""AI Forecast fact assembly, deterministic fallback, streaming, and the SSE route.

Uses the fake Anthropic streaming client from ``test_chat`` (no API key). Covers
``build_facts`` shaping/caps, the prose ``deterministic_summary`` fallback, the
cache-replayed generation, and the route — which (unlike the recommendation
route) always streams 200: with a key it streams the model's prose, without one
it streams the deterministic summary, and an empty store streams a
"no telemetry" line.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kenny_server import forecast
from kenny_server.chat import ChatSessions
from kenny_server.registry import AgentRegistry
from kenny_server.store import ChatHistoryStore, EventStore, TelemetryStore
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel
from kenny_server.webui import build_chat_routes

from test_chat import FakeAnthropic, _Response, text_block
from test_chat_stream import _BoomAnthropic, _frames

_DISK = [
    {"mount": "C:", "current_percent": 91, "slope_percent_per_day": 0.6, "days_until_full": 15.0},
    {"mount": "D:", "current_percent": 40, "slope_percent_per_day": 0.0, "days_until_full": None},
    {"mount": "E:", "current_percent": 80, "slope_percent_per_day": 1.0, "days_until_full": 5.0},
]
_BATTERY = {"current_percent": 78, "percent_per_30d": -2.1, "points": 6}


def _many_changes() -> list[dict[str, Any]]:
    rows = [
        {"section": "autostart", "kind": "added", "key": f"app{i}", "detail": ""}
        for i in range(25)
    ]
    rows.append(
        {"section": "local_accounts", "kind": "changed", "key": "guest",
         "detail": "enabled: False -> True"}
    )
    return rows


@pytest.fixture(autouse=True)
def _clear_cache():
    forecast._cache.clear()
    forecast._REPLAY_DELAY = 0.0  # keep cache-hit assertions fast
    yield
    forecast._cache.clear()


async def _collect(client: Any, facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [ev async for ev in forecast.forecast_events(client, facts)]


# -- build_facts ----------------------------------------------------------


def test_build_facts_caps_changes_and_flags_high_priority():
    facts = forecast.build_facts(None, _DISK, _BATTERY, _many_changes())
    # Change rows are bounded; the total and the dropped count are preserved.
    assert facts["change_total"] == 26
    assert len(facts["changes"]) == forecast._MAX_CHANGES == 20
    assert facts["changes_truncated"] == 6
    # local_accounts is counted as high-priority even though the cap dropped it.
    assert facts["high_priority_changes"] == 1
    # Only volumes with a forecast survive, soonest-to-full first.
    assert [d["mount"] for d in facts["disks_filling"]] == ["E:", "C:"]
    assert facts["battery"] == _BATTERY
    # No snapshot -> unknown health, nothing flagged.
    assert facts["overall"] == "unknown"
    assert facts["flagged"] == []


def test_build_facts_reads_health_from_snapshot():
    snap = {"disk": {"volumes": [{"mount": "C:", "percent_used": 96}]}}
    facts = forecast.build_facts(snap, [], None, [])
    assert facts["overall"] in ("warn", "crit")
    assert "disk" in facts["flagged"]


# -- deterministic_summary ------------------------------------------------


def test_deterministic_summary_is_bounded_prose():
    facts = forecast.build_facts(None, _DISK, _BATTERY, _many_changes())
    text = forecast.deterministic_summary(facts)
    # Prose (ends in a period), most-urgent disk first, capped to 3 sentences.
    assert "Drive E:" in text and "in about 5 days" in text
    assert "Battery health is slipping" in text
    assert "26 inventory items changed" in text and "local account" in text
    assert text.count(".") <= 3
    # No markdown / table artifacts.
    assert "|" not in text and "*" not in text


def test_deterministic_summary_nothing_on_the_horizon():
    facts = forecast.build_facts(None, [], None, [])
    assert forecast.deterministic_summary(facts).startswith("Nothing on the horizon")


# -- forecast_events (generation + cache) --------------------------------


async def test_generation_streams_prose_then_done():
    prose = "Drive C: should fill in about two weeks. Nothing else stands out."
    client = FakeAnthropic([_Response([text_block(prose)], "end_turn")])
    facts = forecast.build_facts(None, _DISK, None, [])
    evs = await _collect(client, facts)

    text = "".join(e["text"] for e in evs if e["type"] == "text_delta")
    assert text == prose
    assert evs[-1]["type"] == "done"
    assert not any(e["type"] == "error" for e in evs)


async def test_cache_replays_without_a_second_model_call():
    prose = "Drive C: should fill in about two weeks."
    facts = forecast.build_facts(None, _DISK, None, [])
    first = await _collect(FakeAnthropic([_Response([text_block(prose)], "end_turn")]), facts)
    # A client that raises if its stream is touched proves the replay is cached.
    second = await _collect(_BoomAnthropic(), forecast.build_facts(None, _DISK, None, []))

    p1 = "".join(e["text"] for e in first if e["type"] == "text_delta")
    p2 = "".join(e["text"] for e in second if e["type"] == "text_delta")
    assert p2 == p1.strip()
    assert not any(e["type"] == "error" for e in second)
    assert second[-1]["type"] == "done"


# -- SSE route ------------------------------------------------------------


def _build_with_snapshots(tmp_path, scripts, snapshots):
    store = TelemetryStore(db_path=str(tmp_path / "fc.sqlite"))
    registry = AgentRegistry(tokens={"dev": "dev-token"})
    tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))
    history_store = ChatHistoryStore(db_path=store.db_path)
    queue = list(scripts)

    def factory() -> Any:
        return FakeAnthropic(queue.pop(0)) if queue else FakeAnthropic([])

    routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=ChatSessions(store=history_store),
        screenshots=ScreenshotStore(),
        history_store=history_store,
        client_factory=factory,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await store.connect()
        await history_store.connect()
        for agent_id, snap in snapshots.items():
            await store.insert(agent_id, "2026-06-06T00:00:00Z", snap)
        yield
        await store.close()
        await history_store.close()

    return Starlette(routes=routes, lifespan=lifespan)


def test_route_400_missing_agent_id(tmp_path):
    app = _build_with_snapshots(tmp_path, [], {})
    with TestClient(app) as c:
        assert c.post("/api/forecast/stream", json={}).status_code == 400


def test_route_streams_ai_forecast_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    prose = "Drive C: is trending toward full within a few weeks."
    snap = {"disk": {"volumes": [{"mount": "C:", "percent_used": 70}]}}
    app = _build_with_snapshots(
        tmp_path, [[_Response([text_block(prose)], "end_turn")]], {"pc": snap}
    )
    with TestClient(app) as c:
        r = c.post("/api/forecast/stream", json={"agent_id": "pc"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = _frames(r.text)
        text = "".join(f["text"] for f in frames if f["type"] == "text_delta")
        assert text == prose
        assert frames[-1]["type"] == "done"


def test_route_streams_deterministic_summary_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    snap = {"disk": {"volumes": [{"mount": "C:", "percent_used": 55}]}}
    app = _build_with_snapshots(tmp_path, [], {"pc": snap})
    with TestClient(app) as c:
        r = c.post("/api/forecast/stream", json={"agent_id": "pc"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = _frames(r.text)
        text = "".join(f["text"] for f in frames if f["type"] == "text_delta")
        # A single healthy snapshot yields no forecast/changes -> the calm line.
        assert text.startswith("Nothing on the horizon")
        assert frames[-1]["type"] == "done"


def test_route_no_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = _build_with_snapshots(tmp_path, [], {})  # no snapshot inserted
    with TestClient(app) as c:
        r = c.post("/api/forecast/stream", json={"agent_id": "ghost"})
        assert r.status_code == 200
        frames = _frames(r.text)
        text = "".join(f["text"] for f in frames if f["type"] == "text_delta")
        assert "No telemetry yet" in text
        assert frames[-1]["type"] == "done"

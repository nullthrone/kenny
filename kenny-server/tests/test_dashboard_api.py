"""Dashboard API enrichments: per-agent fleet summary + global audit log."""

from __future__ import annotations

from starlette.testclient import TestClient

from kenny_server.main import build_app
from kenny_server.webui import _fleet_summary


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def test_fleet_summary_all_green():
    health = {"overall": "ok", "sections": {"disk": {"status": "ok", "summary": "C: 41% full"}}}
    assert _fleet_summary(health, {"disk": {}}) == "all green"


def test_fleet_summary_worst_section_with_reason():
    health = {
        "overall": "crit",
        "sections": {
            "disk": {"status": "crit", "summary": "C: 96% full", "reason": "C: 96% full (>=95%)"},
            "defender": {"status": "crit", "summary": "Real-time protection OFF"},
            "reboot_pending": {"status": "warn", "summary": "Reboot required"},
        },
    }
    out = _fleet_summary(health, {"disk": {}, "defender": {}, "reboot_pending": {}})
    # worst severity (crit) wins; the rule reason is preferred over the summary
    assert out.startswith("C: 96% full (>=95%)")
    assert "+1 more" in out  # the second crit section


def test_fleet_summary_warn_when_no_crit():
    health = {
        "overall": "warn",
        "sections": {"win_update": {"status": "warn", "summary": "14 updates pending"}},
    }
    assert _fleet_summary(health, {"win_update": {}}) == "14 updates pending"


def test_fleet_summary_no_telemetry():
    assert _fleet_summary({"overall": "unknown", "sections": {}}, None) == "no telemetry yet"


def test_agent_endpoint_reports_ai_enabled(tmp_path, monkeypatch):
    """/api/agent/{id} carries ``ai_enabled`` so the UI knows whether to offer
    the AI Recommendation block; it mirrors whether an API key is configured."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = build_app(db_path=str(tmp_path / "ai.sqlite"))
    with TestClient(app) as c:
        body = c.get("/api/agent/papa-pc", headers=_bearer(app)).json()
        assert body["ai_enabled"] is True

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app2 = build_app(db_path=str(tmp_path / "ai2.sqlite"))
    with TestClient(app2) as c:
        body = c.get("/api/agent/papa-pc", headers=_bearer(app2)).json()
        assert body["ai_enabled"] is False


def test_audit_endpoint_shape_and_classification(tmp_path):
    app = build_app(db_path=str(tmp_path / "audit.sqlite"))
    with TestClient(app) as c:
        from functools import partial

        es = app.state.event_store
        c.portal.call(partial(es.insert_audit, agent_id="papa-pc", tool="telemetry_collect", ok=True))
        c.portal.call(partial(es.insert_audit, agent_id="papa-pc", tool="winget_update", ok=True))
        r = c.get("/api/audit", headers=_bearer(app))
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 2
        by_tool = {e["tool"]: e for e in entries}
        assert by_tool["telemetry_collect"]["state_changing"] is False
        assert by_tool["winget_update"]["state_changing"] is True
        assert set(entries[0]) == {"at", "agent_id", "tool", "ok", "error", "state_changing"}


def test_audit_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "audit2.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/audit").status_code == 401


def test_events_endpoint_filters(tmp_path):
    app = build_app(db_path=str(tmp_path / "events.sqlite"))
    with TestClient(app) as c:
        from functools import partial

        es = app.state.event_store
        c.portal.call(
            partial(es.insert_log, source="agent", at="2026-06-05T10:00:00Z",
                    level="warn", target="kenny_agent::tunnel", message="backing off",
                    agent_id="papa-pc")
        )
        c.portal.call(
            partial(es.insert_log, source="server", at="2026-06-05T10:00:01Z",
                    level="info", target="kenny.tunnel", message="papa-pc connected")
        )
        c.portal.call(partial(es.insert_audit, agent_id="papa-pc", tool="winget_update", ok=True))

        # Unfiltered: all three events, newest-first.
        entries = c.get("/api/events", headers=_bearer(app)).json()["entries"]
        assert len(entries) == 3
        assert set(entries[0]) >= {"at", "agent_id", "source", "level", "kind", "message"}

        # kind filter.
        logs = c.get("/api/events?kind=log", headers=_bearer(app)).json()["entries"]
        assert {e["kind"] for e in logs} == {"log"}
        assert len(logs) == 2

        # agent + level filters compose.
        warns = c.get("/api/events?agent=papa-pc&level=warn", headers=_bearer(app)).json()["entries"]
        assert len(warns) == 1
        assert warns[0]["message"] == "backing off"


def test_events_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "events2.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/events").status_code == 401


_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def test_screenshot_get_404_then_200(tmp_path):
    """GET /api/agent/{id}/screenshot is 404 until a screenshot is stored, then
    returns the decoded PNG bytes."""

    import base64

    app = build_app(db_path=str(tmp_path / "shot.sqlite"))
    with TestClient(app) as c:
        r = c.get("/api/agent/papa-pc/screenshot", headers=_bearer(app))
        assert r.status_code == 404

        app.state.screenshots.put("papa-pc", _TINY_PNG_B64, "png")
        r = c.get("/api/agent/papa-pc/screenshot", headers=_bearer(app))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content == base64.b64decode(_TINY_PNG_B64)


def test_screenshot_post_triggers_capture(tmp_path):
    """POST /api/agent/{id}/screenshot forwards a screen_capture via the tunnel
    and stores the result for later GET."""

    app = build_app(db_path=str(tmp_path / "shot2.sqlite"))

    async def fake_send_request(agent_id, tool, args, timeout_s):  # noqa: ANN001, ANN202
        assert tool == "screen_capture"
        return {"image_b64": _TINY_PNG_B64, "format": "png"}

    app.state.tunnel.send_request = fake_send_request
    with TestClient(app) as c:
        r = c.post("/api/agent/papa-pc/screenshot", headers=_bearer(app))
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # The capture was stored and is now retrievable.
        assert app.state.screenshots.get("papa-pc") is not None
        r = c.get("/api/agent/papa-pc/screenshot", headers=_bearer(app))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"


def test_screenshot_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "shot3.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/agent/papa-pc/screenshot").status_code == 401
        assert c.post("/api/agent/papa-pc/screenshot").status_code == 401


def test_brand_asset_served_and_public(tmp_path):
    """The dashboard's brand assets (logo/favicon) are served and reachable
    without an operator token (the login page itself loads them)."""

    app = build_app(db_path=str(tmp_path / "assets.sqlite"))
    with TestClient(app) as c:
        r = c.get("/assets/kenny-mark-64.png")  # no auth header
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 0


def test_asset_route_rejects_unknown_and_traversal(tmp_path):
    app = build_app(db_path=str(tmp_path / "assets2.sqlite"))
    with TestClient(app) as c:
        assert c.get("/assets/nope.png").status_code == 404
        # a non-whitelisted extension is refused
        assert c.get("/assets/secret.txt").status_code == 404
        # path traversal cannot escape the assets dir
        assert c.get("/assets/..%2f..%2f__init__.py").status_code in (404, 400)

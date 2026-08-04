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
        body = c.get("/api/agent/example-pc", headers=_bearer(app)).json()
        assert body["ai_enabled"] is True

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app2 = build_app(db_path=str(tmp_path / "ai2.sqlite"))
    with TestClient(app2) as c:
        body = c.get("/api/agent/example-pc", headers=_bearer(app2)).json()
        assert body["ai_enabled"] is False


def test_audit_endpoint_shape_and_classification(tmp_path):
    app = build_app(db_path=str(tmp_path / "audit.sqlite"))
    with TestClient(app) as c:
        from functools import partial

        es = app.state.event_store
        c.portal.call(partial(es.insert_audit, agent_id="example-pc", tool="telemetry_collect", ok=True))
        c.portal.call(partial(es.insert_audit, agent_id="example-pc", tool="winget_update", ok=True))
        r = c.get("/api/audit", headers=_bearer(app))
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 2
        by_tool = {e["tool"]: e for e in entries}
        assert by_tool["telemetry_collect"]["state_changing"] is False
        assert by_tool["winget_update"]["state_changing"] is True
        # The three-tier class is carried alongside the boolean, not instead of
        # it: the dashboard keeps reading `state_changing`, the ticket trail and
        # the Discord surface grade the same call more finely.
        assert by_tool["telemetry_collect"]["tool_class"] == "read_only"
        assert by_tool["winget_update"]["tool_class"] == "standard_change"
        assert set(entries[0]) == {
            "at", "agent_id", "tool", "ok", "error", "state_changing", "tool_class",
        }


def test_audit_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "audit2.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/audit").status_code == 401


# -- runtime settings API ------------------------------------------------------


def test_settings_list_shape_and_secret_masking(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("KENNY_DIGEST_HOUR", "9")
    app = build_app(db_path=str(tmp_path / "settings.sqlite"))
    with TestClient(app) as c:
        r = c.get("/api/settings", headers=_bearer(app))
        assert r.status_code == 200
        groups = r.json()["groups"]
        assert [g["name"] for g in groups][0] == "Alerting & Digest"
        # the settings sidebar routes on this slug (#/settings/{slug})
        assert {g["name"]: g["slug"] for g in groups}["Alerting & Digest"] == "alerting-digest"
        assert all(g["slug"] for g in groups)
        flat = {s["key"]: s for g in groups for s in g["settings"]}
        # env source is reported
        assert flat["KENNY_DIGEST_HOUR"]["value"] == 9
        assert flat["KENNY_DIGEST_HOUR"]["source"] == "env"
        # a default-valued live setting
        assert flat["KENNY_ALERT_COOLDOWN_SECS"]["source"] == "default"
        # the operator token (a secret) is never serialised
        tok = flat["KENNY_OPERATOR_TOKEN"]
        assert tok["value"] is None and tok["is_set"] is True
        assert "op-secret" not in r.text


def test_settings_put_and_reset_roundtrip(tmp_path):
    app = build_app(db_path=str(tmp_path / "settings-put.sqlite"))
    with TestClient(app) as c:
        r = c.put("/api/settings/KENNY_ALERT_COOLDOWN_SECS",
                  headers=_bearer(app), json={"value": 120})
        assert r.status_code == 200
        assert r.json() == {"key": "KENNY_ALERT_COOLDOWN_SECS", "source": "db",
                            "lifecycle": "live", "value": 120}
        # reflected in the list
        flat = {s["key"]: s for g in c.get("/api/settings", headers=_bearer(app)).json()["groups"]
                for s in g["settings"]}
        assert flat["KENNY_ALERT_COOLDOWN_SECS"]["value"] == 120
        # reset drops the override
        r = c.delete("/api/settings/KENNY_ALERT_COOLDOWN_SECS", headers=_bearer(app))
        assert r.status_code == 200 and r.json()["source"] == "default"
        assert r.json()["value"] == 3600


def test_settings_put_validation_and_guards(tmp_path):
    app = build_app(db_path=str(tmp_path / "settings-guard.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        # invalid value -> 400
        assert c.put("/api/settings/KENNY_DIGEST_HOUR", headers=h, json={"value": 99}).status_code == 400
        # env_only -> 403
        r = c.put("/api/settings/KENNY_OPERATOR_TOKEN", headers=h, json={"value": "x"})
        assert r.status_code == 403
        # unknown key -> 400
        assert c.put("/api/settings/NOPE", headers=h, json={"value": "x"}).status_code == 400
        # missing value -> 400
        assert c.put("/api/settings/KENNY_CHAT_MODEL", headers=h, json={}).status_code == 400


def test_settings_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "settings-auth.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/settings").status_code == 401
        assert c.put("/api/settings/KENNY_CHAT_MODEL", json={"value": "m"}).status_code == 401


def test_events_endpoint_filters(tmp_path):
    app = build_app(db_path=str(tmp_path / "events.sqlite"))
    with TestClient(app) as c:
        from functools import partial

        es = app.state.event_store
        c.portal.call(
            partial(es.insert_log, source="agent", at="2026-06-05T10:00:00Z",
                    level="warn", target="kenny_agent::tunnel", message="backing off",
                    agent_id="example-pc")
        )
        c.portal.call(
            partial(es.insert_log, source="server", at="2026-06-05T10:00:01Z",
                    level="info", target="kenny.tunnel", message="example-pc connected")
        )
        c.portal.call(partial(es.insert_audit, agent_id="example-pc", tool="winget_update", ok=True))

        # Unfiltered: all three events, newest-first.
        entries = c.get("/api/events", headers=_bearer(app)).json()["entries"]
        assert len(entries) == 3
        assert set(entries[0]) >= {"at", "agent_id", "source", "level", "kind", "message"}

        # kind filter.
        logs = c.get("/api/events?kind=log", headers=_bearer(app)).json()["entries"]
        assert {e["kind"] for e in logs} == {"log"}
        assert len(logs) == 2

        # agent + level filters compose.
        warns = c.get("/api/events?agent=example-pc&level=warn", headers=_bearer(app)).json()["entries"]
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
        r = c.get("/api/agent/example-pc/screenshot", headers=_bearer(app))
        assert r.status_code == 404

        app.state.screenshots.put("example-pc", _TINY_PNG_B64, "png")
        r = c.get("/api/agent/example-pc/screenshot", headers=_bearer(app))
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
        r = c.post("/api/agent/example-pc/screenshot", headers=_bearer(app))
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # The capture was stored and is now retrievable.
        assert app.state.screenshots.get("example-pc") is not None
        r = c.get("/api/agent/example-pc/screenshot", headers=_bearer(app))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"


def test_screenshot_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "shot3.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/agent/example-pc/screenshot").status_code == 401
        assert c.post("/api/agent/example-pc/screenshot").status_code == 401


def test_remotehelp_post_starts_quick_assist(tmp_path):
    """POST /api/agent/{id}/remotehelp forwards remotehelp_start and passes the
    agent's ``note`` (human-in-the-loop reminder) back to the UI."""

    app = build_app(db_path=str(tmp_path / "rh.sqlite"))
    note = "Quick Assist opened on the user's desktop. A helper must share the code."

    async def fake_send_request(agent_id, tool, args, timeout_s):  # noqa: ANN001, ANN202
        assert tool == "remotehelp_start"
        return {"launched": True, "pid": 4812, "note": note}

    app.state.tunnel.send_request = fake_send_request
    with TestClient(app) as c:
        r = c.post("/api/agent/papa-pc/remotehelp", headers=_bearer(app))
        assert r.status_code == 200
        assert r.json() == {"ok": True, "note": note}


def test_remotehelp_post_surfaces_tool_error(tmp_path):
    """A refused start (e.g. kill switch off) surfaces as a 502 with the message."""

    from kenny_server.tunnel import ToolError

    app = build_app(db_path=str(tmp_path / "rh2.sqlite"))

    async def fake_send_request(agent_id, tool, args, timeout_s):  # noqa: ANN001, ANN202
        raise ToolError("disabled", "remote control is disabled at the endpoint")

    app.state.tunnel.send_request = fake_send_request
    with TestClient(app) as c:
        r = c.post("/api/agent/papa-pc/remotehelp", headers=_bearer(app))
        assert r.status_code == 502
        assert r.json()["ok"] is False
        assert "disabled" in r.json()["error"]


def test_remotehelp_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "rh3.sqlite"))
    with TestClient(app) as c:
        assert c.post("/api/agent/papa-pc/remotehelp").status_code == 401


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


def test_agent_changes_endpoint(tmp_path):
    """/api/agent/{id}/changes diffs the daily baseline against the latest snapshot."""

    from datetime import datetime, timedelta, timezone

    app = build_app(db_path=str(tmp_path / "changes.sqlite"))
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).isoformat()
    today = now.isoformat()
    with TestClient(app) as c:
        from functools import partial

        store = app.state.store

        def autostart(names):
            return {
                "autostart": {
                    "status": "ok",
                    "summary": "",
                    "entries": [
                        {"name": n, "location": "HKCU\\Run", "command": f"{n}.exe"}
                        for n in names
                    ],
                }
            }

        c.portal.call(partial(store.insert, "example-pc", yesterday, autostart(["OneDrive"])))
        c.portal.call(
            partial(store.insert, "example-pc", today, autostart(["OneDrive", "Sketchy"]))
        )
        body = c.get("/api/agent/example-pc/changes?days=2", headers=_bearer(app)).json()
        assert body["latest"] == today
        assert body["baseline"] == yesterday
        assert any(ch["kind"] == "added" and "Sketchy" in ch["key"] for ch in body["changes"])

    # Unknown agent: empty, not an error.
    with TestClient(app) as c:
        body = c.get("/api/agent/nope/changes", headers=_bearer(app)).json()
        assert body == {"agent_id": "nope", "days": 1, "baseline": None, "latest": None, "changes": []}


def test_agent_trends_endpoint(tmp_path):
    from datetime import datetime, timedelta, timezone

    app = build_app(db_path=str(tmp_path / "trends.sqlite"))
    now = datetime.now(timezone.utc)
    with TestClient(app) as c:
        from functools import partial

        store = app.state.store
        for i in range(6):
            at = (now - timedelta(days=5 - i)).isoformat()
            snap = {
                "disk": {
                    "status": "ok",
                    "summary": "",
                    "volumes": [{"mount": "C:", "percent_used": 70.0 + 2.0 * i}],
                }
            }
            c.portal.call(partial(store.insert, "example-pc", at, snap))
        body = c.get("/api/agent/example-pc/trends", headers=_bearer(app)).json()
        assert body["battery"] is None
        (forecast,) = body["disk"]
        assert forecast["mount"] == "C:"
        assert forecast["days_until_full"] == 10.0

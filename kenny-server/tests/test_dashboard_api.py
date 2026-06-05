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


def test_audit_endpoint_shape_and_classification(tmp_path):
    app = build_app(db_path=str(tmp_path / "audit.sqlite"))
    with TestClient(app) as c:
        app.state.call_log.record("papa-pc", "telemetry.collect", {}, ok=True)
        app.state.call_log.record("papa-pc", "winget.update", {"id": "VLC"}, ok=True)
        r = c.get("/api/audit", headers=_bearer(app))
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 2
        by_tool = {e["tool"]: e for e in entries}
        assert by_tool["telemetry.collect"]["state_changing"] is False
        assert by_tool["winget.update"]["state_changing"] is True
        assert set(entries[0]) == {"at", "agent_id", "tool", "ok", "error", "state_changing"}


def test_audit_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "audit2.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/audit").status_code == 401


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

"""Fleet-wide aggregation for the Overview dashboard.

Covers the pure aggregation functions in ``fleet_stats`` against synthetic
multi-agent snapshots, and the ``/api/fleet/overview`` + ``/api/fleet/trend``
routes end-to-end via the dashboard test harness.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import partial

from starlette.testclient import TestClient

from kenny_server import fleet_stats
from kenny_server.main import build_app
from kenny_server.tools import build_health

NOW = datetime(2026, 6, 7, tzinfo=timezone.utc)


def _agent(agent_id: str, snapshot: dict | None, *, online: bool = True, meta: dict | None = None) -> dict:
    """Build the agent record the web layer hands to the aggregator."""

    return {
        "agent_id": agent_id,
        "online": online,
        "meta": meta or {},
        "snapshot": snapshot,
        "health": build_health(snapshot),
        "collected_at": "2026-06-07T00:00:00Z",
    }


def _fleet() -> list[dict]:
    """Three diverse hosts: one healthy desktop, one warn laptop, one crit host."""

    healthy = {
        "disk": {"status": "ok", "summary": "C: 40% full", "volumes": [{"mount": "C:", "percent_used": 40}]},
        "memory": {"status": "ok", "summary": "ok", "percent_used": 35, "total_bytes": 16 * 1024**3},
        "os_support": {"status": "ok", "summary": "Win 11", "name": "Windows 11", "version": "23H2", "build": "22631"},
        "defender": {"status": "ok", "summary": "ok", "realtime_protection": True},
        "firewall": {"status": "ok", "summary": "ok", "profiles": [{"name": "Domain", "enabled": True}]},
        "encryption": {"status": "ok", "summary": "ok", "volumes": [{"mount": "C:", "protection_status": 1, "encryption_percent": 100}]},
        "reliability": {"status": "ok", "summary": "ok", "recent_crashes": 1, "stability_index": 9.2},
        "uptime": {"status": "ok", "summary": "ok", "uptime_secs": 3 * 86400},
    }
    warn_laptop = {
        "disk": {"status": "warn", "summary": "C: 88% full", "volumes": [{"mount": "C:", "percent_used": 88}]},
        "memory": {"status": "ok", "summary": "ok", "percent_used": 50, "total_bytes": 8 * 1024**3},
        "os_support": {"status": "ok", "summary": "Win 11", "name": "Windows 11", "version": "23H2", "build": "22631"},
        "battery": {"status": "ok", "summary": "ok", "present": True, "charge_percent": 72, "health_percent": 80},
        "defender": {"status": "ok", "summary": "ok", "realtime_protection": True},
        "firewall": {"status": "warn", "summary": "public off", "profiles": [{"name": "Public", "enabled": False}]},
        "app_updates": {"status": "warn", "summary": "3 updates", "available": 3},
        "reboot_pending": {"status": "warn", "summary": "reboot", "pending": True, "reasons": ["WindowsUpdate"]},
        "reliability": {"status": "ok", "summary": "ok", "recent_crashes": 4},
        "uptime": {"status": "ok", "summary": "ok", "uptime_secs": 10 * 86400},
    }
    crit_host = {
        "disk": {"status": "crit", "summary": "C: 97% full", "volumes": [{"mount": "C:", "percent_used": 97}]},
        "memory": {"status": "crit", "summary": "97%", "percent_used": 97, "total_bytes": 8 * 1024**3},
        "os_support": {"status": "ok", "summary": "Win 10", "name": "Windows 10", "version": "22H2", "build": "19045"},
        "defender": {"status": "crit", "summary": "off", "enabled": False, "realtime_protection": False},
        "encryption": {"status": "ok", "summary": "off", "volumes": [{"mount": "C:", "protection_status": 0}]},
        "win_update": {"status": "warn", "summary": "1 failed", "recent": [{"kb": "KB5039211", "result": "failed"}]},
        "defender_quarantine": {"status": "warn", "summary": "1", "items": [{"name": "EICAR"}]},
        "app_updates": {"status": "warn", "summary": "5 updates", "available": 5},
        "reliability": {"status": "ok", "summary": "ok", "recent_crashes": 22},
        "uptime": {"status": "ok", "summary": "ok", "uptime_secs": 40 * 86400},
    }
    return [
        _agent("desktop-1", healthy, meta={"hostname": "DESKTOP-1"}),
        _agent("laptop-1", warn_laptop),
        _agent("crit-1", crit_host, online=False),
    ]


def _linux_server(agent_id: str = "server-1", *, os_support: dict | None = None) -> dict:
    """A headless, batteryless Linux host (no Windows-only telemetry)."""

    snap: dict = {
        "disk": {"status": "ok", "summary": "/ 30% full", "volumes": [{"mount": "/", "percent_used": 30}]},
        "memory": {"status": "ok", "summary": "ok", "percent_used": 40, "total_bytes": 32 * 1024**3},
        "uptime": {"status": "ok", "summary": "ok", "uptime_secs": 100 * 86400},
    }
    if os_support is not None:
        snap["os_support"] = os_support
    return _agent(agent_id, snap, meta={"os": "linux"})


def test_os_inventory_buckets_linux_agent_without_telemetry():
    # No os_support telemetry -> bucket by the declared OS family, never "Windows".
    out = fleet_stats.aggregate_overview(_fleet() + [_linux_server()], now=NOW)
    labels = {s["label"]: s["value"] for s in out["os"]["segments"]}
    assert labels.get("Linux") == 1
    assert "Unknown" not in labels


def test_os_inventory_uses_linux_os_support_name():
    fleet = [_linux_server(os_support={"status": "ok", "summary": "Ubuntu", "name": "Ubuntu", "version": "24.04"})]
    out = fleet_stats.aggregate_overview(fleet, now=NOW)
    assert {s["label"] for s in out["os"]["segments"]} == {"Ubuntu 24.04"}


def test_device_mix_linux_batteryless_is_server():
    out = fleet_stats.aggregate_overview(_fleet() + [_linux_server()], now=NOW)
    dev = {s["label"]: s["value"] for s in out["device"]["segments"]}
    assert dev.get("Server") == 1
    # Batteryless *Windows* hosts still bucket as Desktop.
    assert dev.get("Desktop") == 2
    assert dev.get("Laptop") == 1


def test_security_posture_marks_linux_na_not_unknown():
    out = fleet_stats.aggregate_overview(_fleet() + [_linux_server()], now=NOW)
    metrics = {m["key"]: m for m in out["posture"]["metrics"]}
    for key in ("encryption", "defender_realtime", "firewall"):
        m = metrics[key]
        assert m["na"] == 1
        assert [x["agent_id"] for x in m["members_na"]] == ["server-1"]
    # The Linux host is excluded, so the Windows unknown counts are unchanged.
    assert metrics["encryption"]["unknown"] == 1


def test_overview_top_level_counts():
    out = fleet_stats.aggregate_overview(_fleet(), now=NOW)
    assert out["agent_count"] == 3
    assert out["online_count"] == 2


def test_health_mix_donut():
    out = fleet_stats.aggregate_overview(_fleet(), now=NOW)
    seg = {s["key"]: s["value"] for s in out["health"]["segments"]}
    # one ok (healthy desktop), one warn (laptop), one crit (crit-1)
    assert seg == {"ok": 1, "warn": 1, "crit": 1}
    # the crit segment's members drill down to the offending host
    crit_seg = next(s for s in out["health"]["segments"] if s["key"] == "crit")
    assert [m["agent_id"] for m in crit_seg["members"]] == ["crit-1"]


def test_kpis_action_totals_and_members():
    kpis = {k["key"]: k for k in fleet_stats.aggregate_overview(_fleet(), now=NOW)["kpis"]}
    assert kpis["online"]["value"] == 2 and kpis["online"]["suffix"] == "/ 3"
    assert kpis["reboot"]["value"] == 1
    assert [m["agent_id"] for m in kpis["reboot"]["members"]] == ["laptop-1"]
    # open app updates is a sum (3 + 5) with per-host drill-down
    assert kpis["app_updates"]["value"] == 8
    assert {m["agent_id"] for m in kpis["app_updates"]["members"]} == {"laptop-1", "crit-1"}
    assert kpis["failed_updates"]["value"] == 1
    assert kpis["quarantine"]["value"] == 1


def test_section_severity_sorted_with_drilldown():
    rows = fleet_stats.aggregate_overview(_fleet(), now=NOW)["sections"]["rows"]
    by_section = {r["section"]: r for r in rows}
    # disk has both a warn (laptop) and a crit (crit-1)
    assert by_section["disk"]["warn"] == 1 and by_section["disk"]["crit"] == 1
    assert [m["agent_id"] for m in by_section["disk"]["members_crit"]] == ["crit-1"]
    # crit-heavy sections sort ahead of warn-only ones
    severities = [(r["crit"], r["warn"]) for r in rows]
    assert severities == sorted(severities, reverse=True)


def test_os_and_device_inventory():
    out = fleet_stats.aggregate_overview(_fleet(), now=NOW)
    os_seg = {s["label"]: s["value"] for s in out["os"]["segments"]}
    assert os_seg == {"Windows 11 23H2": 2, "Windows 10 22H2": 1}
    dev = {s["label"]: s["value"] for s in out["device"]["segments"]}
    # only laptop-1 reports a present battery
    assert dev == {"Laptop": 1, "Desktop": 2}


def test_security_posture_compliance_counts():
    metrics = {m["key"]: m for m in fleet_stats.aggregate_overview(_fleet(), now=NOW)["posture"]["metrics"]}
    enc = metrics["encryption"]
    assert enc["compliant"] == 1 and enc["noncompliant"] == 1 and enc["unknown"] == 1
    assert [m["agent_id"] for m in enc["members_noncompliant"]] == ["crit-1"]
    fw = metrics["firewall"]
    assert fw["compliant"] == 1 and fw["noncompliant"] == 1


def test_top_metrics_ranked():
    top = {m["key"]: m for m in fleet_stats.aggregate_overview(_fleet(), now=NOW)["top"]["metrics"]}
    disk = top["disk"]["entries"]
    assert [e["agent_id"] for e in disk] == ["crit-1", "laptop-1", "desktop-1"]
    assert disk[0]["value"] == 97
    crashes = top["crashes"]["entries"]
    assert crashes[0]["agent_id"] == "crit-1" and crashes[0]["value"] == 22


def test_sankey_nodes_and_links():
    sankey = fleet_stats.aggregate_overview(_fleet(), now=NOW)["sankey"]
    kinds = {n["name"]: n["kind"] for n in sankey["nodes"]}
    assert kinds["Critical"] == "crit" and kinds["Warning"] == "warn"
    # a host->severity link exists for the crit host's crit sections
    crit_link = next(lk for lk in sankey["links"] if lk["source"] == "crit-1" and lk["target"] == "Critical")
    assert crit_link["value"] >= 1
    # every link carries member observations for drill-down
    assert all("members" in lk and lk["members"] for lk in sankey["links"])


def test_overview_handles_agent_without_snapshot():
    agents = _fleet() + [_agent("new-pc", None)]
    out = fleet_stats.aggregate_overview(agents, now=NOW)
    assert out["agent_count"] == 4
    unknown = next(s for s in out["health"]["segments"] if s["key"] == "unknown")
    assert [m["agent_id"] for m in unknown["members"]] == ["new-pc"]


def test_trend_buckets_by_day():
    points = {
        "a": [
            {"collected_at": "2026-06-05T08:00:00Z", "overall": "ok"},
            {"collected_at": "2026-06-05T20:00:00Z", "overall": "warn"},  # last of the day wins
            {"collected_at": "2026-06-06T09:00:00Z", "overall": "ok"},
        ],
        "b": [
            {"collected_at": "2026-06-05T10:00:00Z", "overall": "crit"},
            {"collected_at": "2026-06-06T10:00:00Z", "overall": "ok"},
        ],
    }
    days = fleet_stats.aggregate_trend(points, 30, now=NOW)["days"]
    by_date = {d["date"]: d for d in days}
    assert by_date["2026-06-05"]["warn"] == 1 and by_date["2026-06-05"]["crit"] == 1
    assert by_date["2026-06-06"]["ok"] == 2
    # drill-down members are present per status bucket
    assert [m["agent_id"] for m in by_date["2026-06-05"]["members"]["warn"]] == ["a"]


def test_trend_drops_points_outside_window():
    points = {"a": [
        {"collected_at": "2026-01-01T00:00:00Z", "overall": "ok"},  # well outside 30d
        {"collected_at": "2026-06-06T00:00:00Z", "overall": "ok"},
    ]}
    days = fleet_stats.aggregate_trend(points, 30, now=NOW)["days"]
    assert [d["date"] for d in days] == ["2026-06-06"]


def test_reliability_categories_aggregate():
    a1 = _agent("pc1", {"reliability": {
        "status": "warn", "summary": "", "recent_crashes": 50, "events": [
            {"source": "Application Error", "event_id": 1000, "level": "error", "count": 30,
             "category": "App crash / hang"},
            {"source": "disk", "event_id": 51, "level": "error", "count": 20,
             "category": "Disk & storage"}]}})
    a2 = _agent("pc2", {"reliability": {
        "status": "warn", "summary": "", "recent_crashes": 5, "events": [
            {"source": "Kernel-Power", "event_id": 41, "level": "critical", "count": 5,
             "category": "Power & boot"}]}})
    out = fleet_stats.aggregate_overview([a1, a2], now=NOW)["reliability_categories"]
    assert out["agents"] == ["pc1", "pc2"]
    assert set(out["categories"]) == {"App crash / hang", "Disk & storage", "Power & boot"}
    by = {(c["agent_id"], c["category"]): c for c in out["cells"]}
    assert by[("pc1", "App crash / hang")]["count"] == 30
    assert by[("pc2", "Power & boot")]["crit"] is True
    assert "Application Error ×30" in by[("pc1", "App crash / hang")]["detail"]
    # Each cell carries a member for drill-down.
    assert by[("pc1", "Disk & storage")]["members"][0]["agent_id"] == "pc1"


def test_reliability_categories_empty_without_events():
    # The baseline fleet's reliability sections carry only a count, no breakdown.
    out = fleet_stats.aggregate_overview(_fleet(), now=NOW)["reliability_categories"]
    assert out["cells"] == []


# -- route-level tests -----------------------------------------------------


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def test_fleet_overview_route(tmp_path):
    app = build_app(db_path=str(tmp_path / "overview.sqlite"))
    with TestClient(app) as c:
        store = app.state.store
        for a in _fleet():
            c.portal.call(partial(store.insert, a["agent_id"], "2026-06-07T00:00:00Z", a["snapshot"]))
        r = c.get("/api/fleet/overview", headers=_bearer(app))
        assert r.status_code == 200
        body = r.json()
        assert body["agent_count"] == 3
        assert {s["key"] for s in body["health"]["segments"]} == {"ok", "warn", "crit"}
        assert any(row["section"] == "disk" for row in body["sections"]["rows"])


def test_fleet_overview_requires_auth(tmp_path):
    app = build_app(db_path=str(tmp_path / "overview2.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/fleet/overview").status_code == 401
        assert c.get("/api/fleet/trend").status_code == 401


def test_fleet_trend_route(tmp_path):
    app = build_app(db_path=str(tmp_path / "trend.sqlite"))
    snap = {"disk": {"status": "warn", "summary": "C: 88% full", "volumes": [{"mount": "C:", "percent_used": 88}]}}
    with TestClient(app) as c:
        store = app.state.store
        c.portal.call(partial(store.insert, "laptop-1", "2026-06-06T09:00:00Z", snap))
        c.portal.call(partial(store.insert, "laptop-1", "2026-06-06T21:00:00Z", snap))
        r = c.get("/api/fleet/trend?days=30", headers=_bearer(app))
        assert r.status_code == 200
        days = r.json()["days"]
        assert len(days) == 1
        assert days[0]["date"] == "2026-06-06"
        assert days[0]["warn"] == 1


def test_reliability_categories_route_fallback(tmp_path, monkeypatch):
    # With no API key the read path stamps every event group "Other" (graceful
    # degradation) but the heatmap still aggregates counts + drill-down detail.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = build_app(db_path=str(tmp_path / "relcat.sqlite"))
    snap = {"reliability": {"status": "warn", "summary": "", "recent_crashes": 40, "events": [
        {"source": "disk", "event_id": 51, "level": "error", "count": 40,
         "sample": "paging error", "last_seen": "2026-06-07T00:00:00Z"}]}}
    with TestClient(app) as c:
        store = app.state.store
        c.portal.call(partial(store.insert, "pc1", "2026-06-07T00:00:00Z", snap))
        r = c.get("/api/fleet/overview", headers=_bearer(app))
        assert r.status_code == 200
        rc = r.json()["reliability_categories"]
        assert rc["agents"] == ["pc1"]
        assert rc["categories"] == ["Other"]
        cell = rc["cells"][0]
        assert cell["count"] == 40
        assert "disk ×40" in cell["detail"]


def test_echarts_asset_served_with_js_mime(tmp_path):
    """The vendored charting library is served from /assets with a JS mime type."""

    app = build_app(db_path=str(tmp_path / "echarts.sqlite"))
    with TestClient(app) as c:
        r = c.get("/assets/echarts.min.js")  # no auth: same public asset route as the logo
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")
        assert len(r.content) > 0

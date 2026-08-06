"""Operator-managed reliability alarm suppression (issue #166 / ADR-0041).

Covers the pure matcher/mirror (:mod:`kenny_server.reliability_suppression`),
the ``ReliabilitySuppressionStore`` CRUD round-trip, the ``/api/reliability/
suppressions`` routes (auth + validation), the ``TelemetryStore.annotate``
read-path seam that makes suppression reach every health consumer, and the
three MCP tools.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from starlette.testclient import TestClient

from kenny_server.main import build_app
from kenny_server.reliability_suppression import SuppressionList, rule_id
from kenny_server.store import ReliabilitySuppressionStore, TelemetryStore
from kenny_server.tools import register_tools


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


# -- SuppressionList: pure matcher, no store -------------------------------


def test_match_exact_fleet_rule() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [{"id": rule_id("", "Microsoft-Windows-CAPI2", 4176), "agent_id": "",
          "source": "Microsoft-Windows-CAPI2", "event_id": 4176, "note": "",
          "created_at": "t", "created_by": ""}]
    )
    rule = sup.match("PC-A", "Microsoft-Windows-CAPI2", 4176)
    assert rule is not None and rule["id"].endswith("|4176")
    assert sup.match("PC-A", "Other-Source", 4176) is None
    assert sup.match("PC-A", "Microsoft-Windows-CAPI2", 9999) is None


def test_match_wildcard_source_matches_any_source() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [{"id": rule_id("", "", 4176), "agent_id": "", "source": "", "event_id": 4176,
          "note": "", "created_at": "t", "created_by": ""}]
    )
    assert sup.match("PC-A", "Microsoft-Windows-CAPI2", 4176) is not None
    assert sup.match("PC-A", "Anything-Else", 4176) is not None
    assert sup.match("PC-A", "Anything-Else", 41) is None


def test_match_precedence_most_specific_wins() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [
            {"id": "host-exact", "agent_id": "PC-A", "source": "S", "event_id": 1,
             "note": "", "created_at": "t", "created_by": ""},
            {"id": "host-wild", "agent_id": "PC-A", "source": "", "event_id": 1,
             "note": "", "created_at": "t", "created_by": ""},
            {"id": "fleet-exact", "agent_id": "", "source": "S", "event_id": 1,
             "note": "", "created_at": "t", "created_by": ""},
            {"id": "fleet-wild", "agent_id": "", "source": "", "event_id": 1,
             "note": "", "created_at": "t", "created_by": ""},
        ]
    )
    assert sup.match("PC-A", "S", 1)["id"] == "host-exact"
    assert sup.match("PC-A", "T", 1)["id"] == "host-wild"
    assert sup.match("PC-B", "S", 1)["id"] == "fleet-exact"
    assert sup.match("PC-B", "T", 1)["id"] == "fleet-wild"


def test_match_host_rule_does_not_apply_to_other_host() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [{"id": "r", "agent_id": "PC-A", "source": "S", "event_id": 1, "note": "",
          "created_at": "t", "created_by": ""}]
    )
    assert sup.match("PC-A", "S", 1) is not None
    assert sup.match("PC-B", "S", 1) is None


def test_match_ignores_non_integer_event_id() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [{"id": "r", "agent_id": "", "source": "S", "event_id": 4176, "note": "",
          "created_at": "t", "created_by": ""}]
    )
    assert sup.match("PC-A", "S", "4176") is not None  # numeric string coerces
    assert sup.match("PC-A", "S", "abc") is None
    assert sup.match("PC-A", "S", None) is None


def test_mark_stamps_and_preserves_llm_annotation() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [{"id": "r", "agent_id": "", "source": "Microsoft-Windows-CAPI2",
          "event_id": 4176, "note": "known CryptSvc quirk", "created_at": "t",
          "created_by": "admin"}]
    )
    snapshot = {
        "reliability": {
            "events": [
                {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "count": 3439,
                 "level": "error", "category": "Windows service", "severity": "unknown",
                 "suspected_cause": "guess"},
                {"source": "Kernel-Power", "event_id": 41, "count": 1, "level": "critical"},
            ]
        }
    }
    sup.mark("PC-A", snapshot)
    e0, e1 = snapshot["reliability"]["events"]
    assert e0["suppressed"] is True
    assert e0["suppressed_by"]["scope"] == "fleet"
    assert e0["suppressed_by"]["note"] == "known CryptSvc quirk"
    # The LLM annotation is untouched -- a different claim from suppression.
    assert e0["category"] == "Windows service"
    assert e0["severity"] == "unknown"
    assert e0["suspected_cause"] == "guess"
    assert "suppressed" not in e1


def test_mark_unstamps_when_rule_removed() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [{"id": "r", "agent_id": "", "source": "S", "event_id": 1, "note": "",
          "created_at": "t", "created_by": ""}]
    )
    snapshot = {"reliability": {"events": [{"source": "S", "event_id": 1, "count": 5}]}}
    sup.mark("PC-A", snapshot)
    assert snapshot["reliability"]["events"][0]["suppressed"] is True
    sup.set_rules([])
    sup.mark("PC-A", snapshot)
    assert "suppressed" not in snapshot["reliability"]["events"][0]


def test_mark_is_noop_with_no_rules_or_missing_section() -> None:
    sup = SuppressionList(None)
    snapshot = {"reliability": {"events": [{"source": "S", "event_id": 1, "count": 5}]}}
    sup.mark("PC-A", snapshot)
    assert "suppressed" not in snapshot["reliability"]["events"][0]
    sup.mark("PC-A", None)  # must not raise
    sup.mark("PC-A", {})  # no reliability section -- must not raise


def test_rules_filters_by_agent_scope() -> None:
    sup = SuppressionList(None)
    sup.set_rules(
        [
            {"id": "fleet", "agent_id": "", "source": "S", "event_id": 1, "note": "",
             "created_at": "1", "created_by": ""},
            {"id": "host-a", "agent_id": "PC-A", "source": "S", "event_id": 2, "note": "",
             "created_at": "2", "created_by": ""},
            {"id": "host-b", "agent_id": "PC-B", "source": "S", "event_id": 3, "note": "",
             "created_at": "3", "created_by": ""},
        ]
    )
    ids_for_a = {r["id"] for r in sup.rules("PC-A")}
    assert ids_for_a == {"fleet", "host-a"}
    assert {r["id"] for r in sup.rules()} == {"fleet", "host-a", "host-b"}


# -- ReliabilitySuppressionStore CRUD --------------------------------------


@pytest.mark.asyncio
async def test_store_add_list_remove_roundtrip(tmp_path) -> None:
    store = ReliabilitySuppressionStore(str(tmp_path / "sup.sqlite"))
    await store.connect()
    try:
        assert await store.list() == []
        await store.add(id="r1", agent_id="", source="S", event_id=1, note="n1", created_by="admin")
        await store.add(id="r2", agent_id="PC-A", source="", event_id=2)
        rules = await store.list()
        assert [r["id"] for r in rules] == ["r1", "r2"]
        assert rules[0]["note"] == "n1"
        assert rules[0]["created_by"] == "admin"
        assert rules[1]["agent_id"] == "PC-A"
        assert rules[1]["source"] == ""
        # INSERT OR REPLACE: same id updates in place, no duplicate row.
        await store.add(id="r1", agent_id="", source="S", event_id=1, note="updated")
        rules = await store.list()
        assert len(rules) == 2
        assert next(r for r in rules if r["id"] == "r1")["note"] == "updated"
        assert await store.remove("r1") is True
        assert await store.remove("r1") is False
        assert {r["id"] for r in await store.list()} == {"r2"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_store_delete_agent_keeps_fleet_rules(tmp_path) -> None:
    store = ReliabilitySuppressionStore(str(tmp_path / "sup2.sqlite"))
    await store.connect()
    try:
        await store.add(id="fleet", agent_id="", source="S", event_id=1)
        await store.add(id="host", agent_id="PC-A", source="S", event_id=2)
        n = await store.delete_agent("PC-A")
        assert n == 1
        assert {r["id"] for r in await store.list()} == {"fleet"}
    finally:
        await store.close()


# -- SuppressionList.add validation -----------------------------------------


@pytest.mark.asyncio
async def test_suppression_list_add_validates_event_id(tmp_path) -> None:
    store = ReliabilitySuppressionStore(str(tmp_path / "sup3.sqlite"))
    await store.connect()
    try:
        sup = SuppressionList(store)
        with pytest.raises(ValueError):
            await sup.add(event_id="not-a-number")
        with pytest.raises(ValueError):
            await sup.add(event_id=-1)
        with pytest.raises(ValueError):
            await sup.add(event_id=1, source="bad|source")
        with pytest.raises(ValueError):
            await sup.add(event_id=1, agent_id="bad|agent")
        rules = await sup.add(event_id=4176, source="Microsoft-Windows-CAPI2")
        assert len(rules) == 1
    finally:
        await store.close()


# -- TelemetryStore.annotate seam -------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_store_annotate_hook_fires_on_every_read(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "tel.sqlite"))
    await store.connect()
    try:
        calls = []

        def annotate(agent_id, snapshot):
            calls.append(agent_id)
            snapshot["marked"] = True

        store.annotate = annotate
        await store.insert("PC-A", "2026-07-01T00:00:00Z", {"reliability": {}})
        await store.insert("PC-A", "2026-07-02T00:00:00Z", {"reliability": {}})

        latest = await store.latest("PC-A")
        assert latest["snapshot"]["marked"] is True
        history = await store.history("PC-A")
        assert all(h["snapshot"]["marked"] for h in history)
        daily = await store.daily_latest("PC-A", "2026-01-01")
        assert all(d["snapshot"]["marked"] for d in daily)
        assert calls  # fired at least once per accessor
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_telemetry_store_annotate_default_none_is_noop(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "tel2.sqlite"))
    await store.connect()
    try:
        await store.insert("PC-A", "2026-07-01T00:00:00Z", {"reliability": {}})
        latest = await store.latest("PC-A")
        assert "marked" not in latest["snapshot"]
    finally:
        await store.close()


# -- /api/reliability/suppressions ------------------------------------------


def test_suppression_api_crud_roundtrip(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api.sqlite"))
    with TestClient(app) as c:
        assert c.get("/api/reliability/suppressions", headers=_bearer(app)).json() == {
            "rules": []
        }
        resp = c.post(
            "/api/reliability/suppressions",
            headers=_bearer(app),
            json={"event_id": 4176, "source": "Microsoft-Windows-CAPI2"},
        )
        assert resp.status_code == 200
        rules = resp.json()["rules"]
        assert len(rules) == 1
        assert rules[0]["agent_id"] == ""
        assert rules[0]["source"] == "Microsoft-Windows-CAPI2"
        assert rules[0]["event_id"] == 4176

        body = c.get("/api/reliability/suppressions", headers=_bearer(app)).json()
        assert len(body["rules"]) == 1

        rule_id_ = rules[0]["id"]
        resp = c.request(
            "DELETE",
            f"/api/reliability/suppressions/{rule_id_}",
            headers=_bearer(app),
        )
        assert resp.status_code == 200
        assert resp.json()["removed"] is True
        assert c.get("/api/reliability/suppressions", headers=_bearer(app)).json() == {
            "rules": []
        }
        # Removing again is a 200 with removed: False, matching the policy idiom.
        resp = c.request(
            "DELETE",
            f"/api/reliability/suppressions/{rule_id_}",
            headers=_bearer(app),
        )
        assert resp.status_code == 200
        assert resp.json()["removed"] is False


def test_suppression_api_validation(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_bad.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        assert c.post("/api/reliability/suppressions", headers=h, json={}).status_code == 400
        assert c.post(
            "/api/reliability/suppressions", headers=h, json={"event_id": "abc"}
        ).status_code == 400
        assert c.post(
            "/api/reliability/suppressions", headers=h, json={"event_id": 1, "agent_id": "unknown-pc"}
        ).status_code == 400
        assert c.post(
            "/api/reliability/suppressions", headers=h, json={"event_id": 1, "source": "a|b"}
        ).status_code == 400


def test_suppression_write_requires_operator(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "rbac_sup.sqlite"))
    with TestClient(app) as c:
        r = c.post(
            "/setup", data={"username": "admin", "password": "pw-123456"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        c.post("/api/users", json={
            "username": "kid", "password": "pw-123456", "role": "user",
        })
        users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
        kid_id = users["kid"]["id"]
        kid_pat = c.post(f"/api/users/{kid_id}/pats", json={"label": "t"}).json()["token"]
        h = {"Authorization": f"Bearer {kid_pat}"}

        assert c.get("/api/reliability/suppressions", headers=h).status_code == 200
        assert c.post(
            "/api/reliability/suppressions", headers=h, json={"event_id": 1}
        ).status_code == 403
        assert c.request(
            "DELETE", "/api/reliability/suppressions/none", headers=h
        ).status_code == 403


def test_remove_host_purges_host_rules_but_keeps_fleet_rules(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "purge.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.post("/api/reliability/suppressions", headers=h, json={"event_id": 1})
        c.post(
            "/api/reliability/suppressions", headers=h,
            json={"event_id": 2, "agent_id": "GHOST-PC"},
        )
        r = c.delete("/api/agent/GHOST-PC", headers=h)
        assert r.status_code == 200
        assert r.json()["purged"]["reliability_suppressions"] == "ok"
        rules = c.get("/api/reliability/suppressions", headers=h).json()["rules"]
        assert [rule["event_id"] for rule in rules] == [1]


def test_suppression_affects_agent_health_response(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "affect.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        import asyncio

        events = [
            {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "level": "error",
             "count": 3439, "sample": "AuthSafes count", "last_seen": "2026-07-29T00:00:00Z",
             "by_day": {}},
            {"source": "Microsoft-Windows-Kernel-Power", "event_id": 41, "level": "critical",
             "count": 1, "sample": "unexpected shutdown", "last_seen": "2026-07-29T00:00:00Z",
             "by_day": {}},
        ]
        snapshot = {
            "reliability": {
                "status": "crit", "summary": "3440 error/critical events in 7d",
                "recent_crashes": 3440, "window_days": 7, "events": events,
            }
        }
        asyncio.run(app.state.store.insert("PC-166", "2026-07-29T21:00:00Z", snapshot))

        before = c.get("/api/agent/PC-166", headers=h).json()
        assert before["health"]["sections"]["reliability"]["status"] == "crit"

        resp = c.post(
            "/api/reliability/suppressions",
            headers=h,
            json={"event_id": 4176, "source": "Microsoft-Windows-CAPI2",
                  "note": "known CryptSvc quirk"},
        )
        assert resp.status_code == 200

        after = c.get("/api/agent/PC-166", headers=h).json()
        rel = after["health"]["sections"]["reliability"]
        assert "CAPI2" not in rel["reason"]
        assert "Kernel-Power" in rel["reason"]
        assert "suppressed" in rel["reason"]
        # Raw counts are untouched -- only scoring changed.
        assert after["snapshot"]["reliability"]["recent_crashes"] == 3440
        stamped = {e["source"]: e for e in after["snapshot"]["reliability"]["events"]}
        assert stamped["Microsoft-Windows-CAPI2"]["suppressed"] is True
        assert stamped["Microsoft-Windows-CAPI2"]["count"] == 3439


# -- MCP tools ---------------------------------------------------------------


async def _build_mcp(tmp_path):
    """A minimal FastMCP server with only the reliability suppression tools,
    wired to a real (temp-file) store -- mirrors the pattern in test_tools.py
    but exercises the tools through a real ``fastmcp.Client`` in-memory
    transport rather than calling closures directly (they aren't importable).
    """

    from kenny_server.registry import AgentRegistry
    from kenny_server.store import TelemetryStore
    from kenny_server.tools import CallLog
    from kenny_server.tunnel import AgentTunnel

    store = ReliabilitySuppressionStore(str(tmp_path / "mcp_sup.sqlite"))
    await store.connect()
    suppression = SuppressionList(store)
    await suppression.load()

    registry = AgentRegistry()
    tel_store = TelemetryStore(str(tmp_path / "mcp_tel.sqlite"))
    await tel_store.connect()
    tunnel = AgentTunnel(registry, tel_store, event_store=None)
    call_log = CallLog(event_store=None)

    mcp = FastMCP("test")
    register_tools(
        mcp, registry=registry, store=tel_store, tunnel=tunnel, call_log=call_log,
        suppression=suppression,
    )
    return mcp, suppression, store, tel_store


@pytest.mark.asyncio
async def test_reliability_suppression_mcp_tools_roundtrip(tmp_path) -> None:
    mcp, _suppression, store, tel_store = await _build_mcp(tmp_path)
    try:
        async with Client(mcp) as client:
            listed = (await client.call_tool("reliability_suppression_list", {})).data
            assert listed["rules"] == []

            added = (
                await client.call_tool(
                    "reliability_suppression_add",
                    {"event_id": 4176, "source": "Microsoft-Windows-CAPI2"},
                )
            ).data
            assert len(added["rules"]) == 1
            rule_id_ = added["rules"][0]["id"]

            listed = (await client.call_tool("reliability_suppression_list", {})).data
            assert len(listed["rules"]) == 1

            removed = (
                await client.call_tool("reliability_suppression_remove", {"rule_id": rule_id_})
            ).data
            assert removed["removed"] is True
            listed = (await client.call_tool("reliability_suppression_list", {})).data
            assert listed["rules"] == []
    finally:
        await store.close()
        await tel_store.close()


@pytest.mark.asyncio
async def test_reliability_suppression_add_bad_event_id_is_tool_error(tmp_path) -> None:
    mcp, _suppression, store, tel_store = await _build_mcp(tmp_path)
    try:
        async with Client(mcp) as client:
            from fastmcp.exceptions import ToolError as ClientToolError

            with pytest.raises(ClientToolError):
                await client.call_tool("reliability_suppression_add", {"event_id": -1})
    finally:
        await store.close()
        await tel_store.close()


@pytest.mark.asyncio
async def test_agent_snapshot_stamps_suppression(tmp_path) -> None:
    """agent_snapshot (a raw-store read, no LLM call) must carry the
    ``suppressed`` marker via the TelemetryStore.annotate hook, so a caller
    comparing the breakdown can tell which patterns are already muted."""

    mcp, suppression, store, tel_store = await _build_mcp(tmp_path)
    try:
        tel_store.annotate = suppression.mark
        await suppression.add(event_id=4176, source="Microsoft-Windows-CAPI2")
        await tel_store.insert(
            "PC-A", "2026-07-29T00:00:00Z",
            {"reliability": {"events": [
                {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "count": 3439},
            ]}},
        )
        async with Client(mcp) as client:
            result = (
                await client.call_tool(
                    "agent_snapshot", {"id": "PC-A", "section": "reliability"}
                )
            ).data
            assert result["payload"]["events"][0]["suppressed"] is True
    finally:
        await store.close()
        await tel_store.close()

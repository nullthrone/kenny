"""Operator-configurable auto-ticket rules (ADR-0053).

Covers the pure matcher (:func:`kenny_server.ticket_rules.decide`), the
``TicketRuleList`` mirror, the ``TicketRuleStore`` CRUD round-trip, the
``/api/ticket-rules*`` routes (auth + validation), the three MCP tools, and
two seam tests asserting the vocabulary the alert engine actually emits stays
in step with what the rule validator (and the API) accept -- per
kenny-server/CLAUDE.md's "every seam two places must agree on gets a test
that fails when they diverge."
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from starlette.testclient import TestClient

from kenny_server import diffs, health_rules
from kenny_server.main import build_app
from kenny_server.notify import Notification
from kenny_server.store import TicketRuleStore
from kenny_server.ticket_rules import (
    DECISIONS,
    EVENT_TYPES,
    KNOWN_SECTIONS,
    NEVER_TICKETED_KINDS,
    TicketRuleList,
    decide,
    rule_id,
)
from kenny_server.tools import register_tools


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _decide(rules, **kw):
    kw.setdefault("agent_id", "")
    kw.setdefault("priority", "default")
    kw.setdefault("sections", {})
    return decide(rules, **kw)


# -- decide(): pure matcher, no store ---------------------------------------


def test_no_rules_reproduces_todays_behaviour() -> None:
    """Empty rule set: every genuine alert opens, nothing else does."""

    assert _decide({}, kind="alert", event_type="health", sections={"disk": "crit"}).open
    assert _decide({}, kind="alert", event_type="offline").open
    assert _decide({}, kind="alert", event_type="disk_forecast").open
    assert not _decide({}, kind="change", event_type="change", sections={"autostart": ""}).open
    assert not _decide({}, kind="recovery", event_type="health", sections={"disk": "ok"}).open
    assert not _decide({}, kind="digest", event_type="digest").open


def test_absent_event_type_falls_through_to_the_default() -> None:
    """A back-compat ``Notification()`` with no discriminator behaves as today."""

    assert _decide({}, kind="alert", event_type="").open
    assert not _decide({}, kind="change", event_type="").open


def test_never_rule_suppresses_a_default_open() -> None:
    rules = {
        ("", "health", "defender"): {
            "id": rule_id("", "health", "defender"), "decision": "never",
        }
    }
    assert not _decide(
        rules, kind="alert", event_type="health", sections={"defender": "crit"}
    ).open


def test_any_matching_section_opens_the_ticket() -> None:
    """A ``never`` on one section doesn't block a ticket from another subject."""

    rules = {
        ("", "health", "defender"): {
            "id": rule_id("", "health", "defender"), "decision": "never",
        }
    }
    decision = _decide(
        rules, kind="alert", event_type="health",
        sections={"defender": "crit", "disk": "warn"},
    )
    assert decision.open
    assert decision.subject == ("disk", "warn")

    rules[("", "health", "disk")] = {
        "id": rule_id("", "health", "disk"), "decision": "never",
    }
    assert not _decide(
        rules, kind="alert", event_type="health",
        sections={"defender": "crit", "disk": "warn"},
    ).open


def test_open_all_promotes_a_change_notification() -> None:
    rules = {
        ("", "change", "local_accounts"): {
            "id": rule_id("", "change", "local_accounts"), "decision": "open_all",
        }
    }
    assert _decide(
        rules, kind="change", event_type="change", sections={"local_accounts": ""}
    ).open
    # A different section on the same event_type is untouched.
    assert not _decide(
        rules, kind="change", event_type="change", sections={"autostart": ""}
    ).open


def test_open_crit_only_fires_on_crit_severity() -> None:
    rules = {
        ("", "health", "battery"): {
            "id": rule_id("", "health", "battery"), "decision": "open_crit",
        }
    }
    assert not _decide(
        rules, kind="alert", event_type="health", sections={"battery": "warn"}
    ).open
    assert _decide(
        rules, kind="alert", event_type="health", sections={"battery": "crit"}
    ).open


def test_open_crit_reads_offline_severity_from_priority() -> None:
    """Offline/disk_forecast have no section axis -- severity comes from
    ``Notification.priority`` (high/urgent -> crit), matched against the
    section-less subject slot ("")."""

    rules = {("", "offline", ""): {"id": rule_id("", "offline", ""), "decision": "open_crit"}}
    assert _decide(rules, kind="alert", event_type="offline", priority="default").open is False
    assert _decide(rules, kind="alert", event_type="offline", priority="high").open is True


def test_precedence_most_specific_wins() -> None:
    # decision="open_all" on every slot of the lattice: since the notification
    # itself is a "change" (default-closed), only the matched rule can make it
    # open, so which rule ended up as `.rule` is directly observable.
    rules = {
        ("PC-A", "change", "disk"): {"id": "host-exact", "decision": "open_all"},
        ("PC-A", "change", ""): {"id": "host-wild", "decision": "open_all"},
        ("", "change", "disk"): {"id": "fleet-exact", "decision": "open_all"},
        ("", "change", ""): {"id": "fleet-wild", "decision": "open_all"},
    }

    def matched_id(agent_id: str, section: str) -> str | None:
        d = _decide(
            rules, kind="change", agent_id=agent_id, event_type="change",
            sections={section: ""},
        )
        return d.rule["id"] if d.rule else None

    assert matched_id("PC-A", "disk") == "host-exact"
    assert matched_id("PC-A", "memory") == "host-wild"
    assert matched_id("PC-B", "disk") == "fleet-exact"
    assert matched_id("PC-B", "memory") == "fleet-wild"


def test_host_rule_does_not_apply_to_another_host() -> None:
    rules = {("PC-A", "offline", ""): {"id": "r", "decision": "never"}}
    assert not _decide(rules, kind="alert", agent_id="PC-A", event_type="offline").open
    assert _decide(rules, kind="alert", agent_id="PC-B", event_type="offline").open


def test_one_notification_yields_one_decision() -> None:
    """A multi-section bundle still resolves to a single Decision -- ticket
    granularity stays one-per-notification, not one-per-section."""

    d = _decide(
        {}, kind="alert", event_type="health",
        sections={"defender": "crit", "disk": "warn", "memory": "warn"},
    )
    assert d.open
    assert d.subject is not None


@pytest.mark.parametrize("kind", sorted(NEVER_TICKETED_KINDS))
def test_recovery_and_digest_are_never_ticketable_even_with_an_explicit_open_rule(kind) -> None:
    """The kind-based invariant beats any rule, however it is written --
    including a hand-forged row bypassing TicketRuleList.add's validation."""

    rules = {("", "health", ""): {"id": "r", "decision": "open_all"}}
    assert not _decide(
        rules, kind=kind, event_type="health", sections={"disk": "crit"}
    ).open


def test_rule_id_is_deterministic() -> None:
    assert rule_id("PC-A", "health", "disk") == "PC-A|health|disk"
    assert rule_id("", "offline", "") == "|offline|"


# -- TicketRuleList (in-memory mirror) ---------------------------------------


def test_rules_filters_by_agent_scope() -> None:
    trl = TicketRuleList(None)
    trl.set_rules(
        [
            {"id": "fleet", "agent_id": "", "event_type": "offline", "section": "",
             "decision": "never", "created_at": "1"},
            {"id": "host-a", "agent_id": "PC-A", "event_type": "offline", "section": "",
             "decision": "never", "created_at": "2"},
            {"id": "host-b", "agent_id": "PC-B", "event_type": "offline", "section": "",
             "decision": "never", "created_at": "3"},
        ]
    )
    assert {r["id"] for r in trl.rules("PC-A")} == {"fleet", "host-a"}
    assert {r["id"] for r in trl.rules()} == {"fleet", "host-a", "host-b"}


def test_should_open_wraps_decide() -> None:
    trl = TicketRuleList(None)
    trl.set_rules(
        [{"id": rule_id("", "offline", ""), "agent_id": "", "event_type": "offline",
          "section": "", "decision": "never", "created_at": "1"}]
    )
    assert not trl.should_open(Notification(title="t", body="b", event_type="offline"))
    assert trl.should_open(Notification(title="t", body="b", event_type="health",
                                         sections={"disk": "crit"}))


# -- TicketRuleStore CRUD -----------------------------------------------------


@pytest.mark.asyncio
async def test_store_add_list_remove_roundtrip(tmp_path) -> None:
    store = TicketRuleStore(str(tmp_path / "tr.sqlite"))
    await store.connect()
    try:
        assert await store.list() == []
        await store.add(
            id="r1", agent_id="", event_type="offline", section="", decision="never",
            note="n1", created_by="admin",
        )
        await store.add(
            id="r2", agent_id="PC-A", event_type="health", section="disk",
            decision="open_crit",
        )
        rules = await store.list()
        assert [r["id"] for r in rules] == ["r1", "r2"]
        assert rules[0]["note"] == "n1"
        assert rules[0]["created_by"] == "admin"
        assert rules[1]["agent_id"] == "PC-A"
        assert rules[1]["decision"] == "open_crit"
        # INSERT OR REPLACE: same id updates in place, no duplicate row.
        await store.add(
            id="r1", agent_id="", event_type="offline", section="", decision="open_all",
        )
        rules = await store.list()
        assert len(rules) == 2
        assert next(r for r in rules if r["id"] == "r1")["decision"] == "open_all"
        assert await store.remove("r1") is True
        assert await store.remove("r1") is False
        assert {r["id"] for r in await store.list()} == {"r2"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_store_delete_agent_keeps_fleet_rules(tmp_path) -> None:
    store = TicketRuleStore(str(tmp_path / "tr2.sqlite"))
    await store.connect()
    try:
        await store.add(id="fleet", agent_id="", event_type="offline", section="", decision="never")
        await store.add(id="host", agent_id="PC-A", event_type="offline", section="", decision="never")
        n = await store.delete_agent("PC-A")
        assert n == 1
        assert {r["id"] for r in await store.list()} == {"fleet"}
    finally:
        await store.close()


# -- TicketRuleList.add validation -------------------------------------------


@pytest.mark.asyncio
async def test_add_validates_event_type_and_decision(tmp_path) -> None:
    store = TicketRuleStore(str(tmp_path / "tr3.sqlite"))
    await store.connect()
    try:
        trl = TicketRuleList(store)
        with pytest.raises(ValueError):
            await trl.add(event_type="digest", decision="never")
        with pytest.raises(ValueError):
            await trl.add(event_type="recovery", decision="never")
        with pytest.raises(ValueError):
            await trl.add(event_type="health", decision="not-a-decision")
        with pytest.raises(ValueError):
            await trl.add(event_type="health", decision="never", section="bad|section")
        with pytest.raises(ValueError):
            await trl.add(event_type="health", decision="never", agent_id="bad|agent")
        rules, warnings = await trl.add(event_type="offline", decision="never")
        assert len(rules) == 1
        assert warnings == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_add_warns_but_accepts_an_unknown_section(tmp_path) -> None:
    """Section validation is lenient (see the module docstring): evaluate_section
    scores every reported section, not just the ones with a dedicated rule."""

    store = TicketRuleStore(str(tmp_path / "tr4.sqlite"))
    await store.connect()
    try:
        trl = TicketRuleList(store)
        rules, warnings = await trl.add(
            event_type="health", decision="never", section="not_a_real_section"
        )
        assert len(rules) == 1
        assert warnings and "not_a_real_section" in warnings[0]
    finally:
        await store.close()


# -- /api/ticket-rules ---------------------------------------------------------


def test_ticket_rules_api_crud_roundtrip(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        assert c.get("/api/ticket-rules", headers=h).json() == {"rules": []}
        resp = c.post(
            "/api/ticket-rules", headers=h,
            json={"event_type": "offline", "decision": "never", "note": "PC is off overnight"},
        )
        assert resp.status_code == 201
        rules = resp.json()["rules"]
        assert len(rules) == 1
        assert rules[0]["event_type"] == "offline"
        assert rules[0]["decision"] == "never"
        assert resp.json()["warnings"] == []

        body = c.get("/api/ticket-rules", headers=h).json()
        assert len(body["rules"]) == 1

        rid = rules[0]["id"]
        resp = c.request("DELETE", f"/api/ticket-rules/{rid}", headers=h)
        assert resp.status_code == 200
        assert resp.json()["removed"] is True
        assert c.get("/api/ticket-rules", headers=h).json() == {"rules": []}
        resp = c.request("DELETE", f"/api/ticket-rules/{rid}", headers=h)
        assert resp.json()["removed"] is False


def test_ticket_rules_api_validation(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_bad.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        assert c.post("/api/ticket-rules", headers=h, json={}).status_code == 400
        assert c.post(
            "/api/ticket-rules", headers=h, json={"event_type": "digest", "decision": "never"}
        ).status_code == 400
        assert c.post(
            "/api/ticket-rules", headers=h, json={"event_type": "recovery", "decision": "never"}
        ).status_code == 400
        assert c.post(
            "/api/ticket-rules", headers=h,
            json={"event_type": "health", "decision": "not-a-decision"},
        ).status_code == 400
        assert c.post(
            "/api/ticket-rules", headers=h,
            json={"event_type": "health", "decision": "never", "agent_id": "a|b"},
        ).status_code == 400
        # An unknown section is accepted with a warning, not rejected.
        resp = c.post(
            "/api/ticket-rules", headers=h,
            json={"event_type": "health", "decision": "never", "section": "not_real"},
        )
        assert resp.status_code == 201
        assert resp.json()["warnings"]


def test_ticket_rules_require_operator(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "rbac_tr.sqlite"))
    with TestClient(app) as c:
        r = c.post(
            "/setup", data={"username": "admin", "password": "pw-123456"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        c.post("/api/users", json={"username": "kid", "password": "pw-123456", "role": "user"})
        users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
        kid_id = users["kid"]["id"]
        kid_pat = c.post(f"/api/users/{kid_id}/pats", json={"label": "t"}).json()["token"]
        h = {"Authorization": f"Bearer {kid_pat}"}

        # A scoped `user` is denied on every route, including the read -- an
        # alert-origin ticket is itself operator-only, so the rules that decide
        # when one opens carry no legitimate use for a scoped user.
        assert c.get("/api/ticket-rules", headers=h).status_code == 403
        assert c.get("/api/ticket-rules/vocabulary", headers=h).status_code == 403
        assert c.post(
            "/api/ticket-rules", headers=h, json={"event_type": "offline", "decision": "never"}
        ).status_code == 403
        assert c.request("DELETE", "/api/ticket-rules/none", headers=h).status_code == 403

        c.post("/api/users", json={"username": "op", "password": "pw-123456", "role": "operator"})
        users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
        op_id = users["op"]["id"]
        op_pat = c.post(f"/api/users/{op_id}/pats", json={"label": "t"}).json()["token"]
        ho = {"Authorization": f"Bearer {op_pat}"}
        assert c.get("/api/ticket-rules", headers=ho).status_code == 200


def test_vocabulary_endpoint_matches_the_module(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "vocab.sqlite"))
    with TestClient(app) as c:
        body = c.get("/api/ticket-rules/vocabulary", headers=_bearer(app)).json()
        assert set(body["event_types"]) == set(EVENT_TYPES)
        assert set(body["decisions"]) == set(DECISIONS)
        assert set(body["sections"]) == set(KNOWN_SECTIONS)
        assert set(body["sections"]["change"]) == set(diffs.SPECS)


def test_removing_a_host_purges_its_ticket_rules_but_keeps_fleet_rules(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "purge.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.post("/api/ticket-rules", headers=h, json={"event_type": "offline", "decision": "never"})
        c.post(
            "/api/ticket-rules", headers=h,
            json={"event_type": "offline", "decision": "never", "agent_id": "GHOST-PC"},
        )
        r = c.delete("/api/agent/GHOST-PC", headers=h)
        assert r.status_code == 200
        assert r.json()["purged"]["ticket_rules"] == "ok"
        rules = c.get("/api/ticket-rules", headers=h).json()["rules"]
        assert [rule["agent_id"] for rule in rules] == [""]


# -- MCP tools -----------------------------------------------------------------


async def _build_mcp(tmp_path):
    from kenny_server.registry import AgentRegistry
    from kenny_server.store import TelemetryStore
    from kenny_server.tools import CallLog
    from kenny_server.tunnel import AgentTunnel

    store = TicketRuleStore(str(tmp_path / "mcp_tr.sqlite"))
    await store.connect()
    ticket_rules = TicketRuleList(store)
    await ticket_rules.load()

    registry = AgentRegistry()
    tel_store = TelemetryStore(str(tmp_path / "mcp_tel.sqlite"))
    await tel_store.connect()
    tunnel = AgentTunnel(registry, tel_store, event_store=None)
    call_log = CallLog(event_store=None)

    mcp = FastMCP("test")
    register_tools(
        mcp, registry=registry, store=tel_store, tunnel=tunnel, call_log=call_log,
        ticket_rules=ticket_rules,
    )
    return mcp, ticket_rules, store, tel_store


@pytest.mark.asyncio
async def test_ticket_rule_mcp_tools_roundtrip(tmp_path) -> None:
    mcp, _tr, store, tel_store = await _build_mcp(tmp_path)
    try:
        async with Client(mcp) as client:
            listed = (await client.call_tool("ticket_rule_list", {})).data
            assert listed["rules"] == []

            added = (
                await client.call_tool(
                    "ticket_rule_set",
                    {"event_type": "offline", "decision": "never", "note": "off overnight"},
                )
            ).data
            assert len(added["rules"]) == 1
            rid = added["rules"][0]["id"]

            listed = (await client.call_tool("ticket_rule_list", {})).data
            assert len(listed["rules"]) == 1

            removed = (
                await client.call_tool("ticket_rule_remove", {"rule_id": rid})
            ).data
            assert removed["removed"] is True
            listed = (await client.call_tool("ticket_rule_list", {})).data
            assert listed["rules"] == []
    finally:
        await store.close()
        await tel_store.close()


@pytest.mark.asyncio
async def test_ticket_rule_set_bad_event_type_is_tool_error(tmp_path) -> None:
    mcp, _tr, store, tel_store = await _build_mcp(tmp_path)
    try:
        async with Client(mcp) as client:
            from fastmcp.exceptions import ToolError as ClientToolError

            with pytest.raises(ClientToolError):
                await client.call_tool(
                    "ticket_rule_set", {"event_type": "digest", "decision": "never"}
                )
    finally:
        await store.close()
        await tel_store.close()


# -- seam tests: vocabulary agreement (kenny-server/CLAUDE.md) ---------------


def test_known_sections_covers_every_health_rule_and_diff_spec() -> None:
    """Seam: the section names a rule may name (KNOWN_SECTIONS) vs. the
    sections health_rules.RULES and diffs.SPECS can actually produce. Fails
    when a new health rule or diff spec ships without a matching vocabulary
    entry, in either direction for `change` (a closed producer)."""

    assert set(health_rules.RULES) <= KNOWN_SECTIONS["health"]
    assert KNOWN_SECTIONS["change"] == set(diffs.SPECS)


def test_protocol_telemetry_sections_are_all_advertised() -> None:
    """Every section documented in docs/protocol.md's Telemetry sections list
    must be a member of KNOWN_SECTIONS["health"] (directly, via RULES, or via
    the module's explicit _EXTRA_HEALTH_SECTIONS) -- otherwise the dashboard's
    add-rule form silently cannot address it."""

    documented = {
        "disk", "peripherals", "network", "routing", "processes", "services",
        "defender", "win_update", "disk_smart", "battery", "memory", "thermals",
        "firewall", "encryption", "av_thirdparty", "defender_quarantine",
        "reboot_pending", "os_support", "reliability", "app_updates",
        "uptime", "time_sync", "printers", "wifi_quality", "autostart",
        "web_activity", "screen_time", "installed_software", "browser_extensions",
        "listening_ports", "scheduled_tasks", "local_accounts", "backup_status",
        "net_quality",
    }
    missing = documented - KNOWN_SECTIONS["health"]
    assert not missing, f"documented telemetry sections missing from KNOWN_SECTIONS: {missing}"


def test_event_types_the_engine_can_emit_are_exactly_the_validated_ones() -> None:
    """Seam: the event_type slugs alerting.py's producers emit vs. the slugs
    ticket_rules validates/advertises. Both directions -- new/renamed on
    either side fails this."""

    emitted_by_producers = {"health", "offline", "disk_forecast", "change"}
    assert emitted_by_producers == set(EVENT_TYPES)

"""Per-host web-filter enforcement and history (ADR-0069).

Covers the two settings (``enforcement`` × ``history``) end to end: the store
mapping and the upgrade migration, what the insert path keeps in *both* stores
(``web_activity_events`` and the stored snapshot) for every combination, the
fail-closed path, the purge on ``full`` -> ``violations``, the per-host
``policy.collect`` frame and its re-send on a config change, and that only
``protect`` ever pushes a block.

The insert-path, policy-frame and push tests run joined: a real server built by
``build_app`` and a mock agent over ``/agent/ws``, so the tunnel, the service,
both stores and the ``main.py`` wiring are exercised together.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError as ClientToolError

from kenny_server import store as store_mod
from kenny_server.config import CATALOG
from kenny_server.main import build_app
from kenny_server.protocol import Policy, dump_frame, parse_frame
from kenny_server.store import (
    WEBFILTER_HISTORY_MODES,
    TelemetryStore,
    WebFilterStore,
)
from kenny_server.webfilter import (
    WebFilterService,
    collects_web_activity,
    list_drift,
)
from test_server_e2e import SERVER_SEED_B64, _free_port, _Server
from test_webfilter import WebfilterMockAgent, _StubCache

FIXTURE = Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "policy_collect.json"

COMBOS = [
    (enforcement, history)
    for enforcement in ("off", "log_only", "protect")
    for history in ("violations", "full")
]


# --- store: enforcement <-> booleans, history, migration ----------------------


@pytest.fixture
async def wstore(tmp_path) -> WebFilterStore:
    s = WebFilterStore(db_path=str(tmp_path / "wf.sqlite"))
    await s.connect()
    yield s
    await s.close()


def _raw_toggles(db_path: str, agent_id: str) -> tuple[int, int, str]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT enabled, block_mode, history FROM webfilter_config WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    return row


@pytest.mark.parametrize(
    "enforcement, enabled, block_mode",
    [("off", 0, 0), ("log_only", 1, 0), ("protect", 1, 1)],
)
async def test_enforcement_is_stored_in_the_legacy_columns(
    wstore: WebFilterStore, enforcement: str, enabled: int, block_mode: int
) -> None:
    config = await wstore.set_config("pc1", enforcement=enforcement)
    assert config["enforcement"] == enforcement
    assert config["enabled"] is bool(enabled)
    assert config["block_mode"] is bool(block_mode)
    assert _raw_toggles(wstore.db_path, "pc1")[:2] == (enabled, block_mode)


@pytest.mark.parametrize(
    "enabled, block_mode, expected",
    [(0, 0, "off"), (1, 0, "log_only"), (1, 1, "protect"), (0, 1, "off")],
)
async def test_stored_booleans_read_as_enforcement(
    wstore: WebFilterStore, enabled: int, block_mode: int, expected: str
) -> None:
    await wstore.set_config("pc1", enabled=bool(enabled), block_mode=bool(block_mode))
    # The legacy toggles write exactly what they always wrote...
    assert _raw_toggles(wstore.db_path, "pc1")[:2] == (enabled, block_mode)
    config = await wstore.get_config("pc1")
    # ...and read back normalised from the level: block_mode only under protect.
    assert config["enforcement"] == expected
    assert config["enabled"] is (expected != "off")
    assert config["block_mode"] is (expected == "protect")


async def test_legacy_toggles_one_at_a_time_keep_their_old_meaning(
    wstore: WebFilterStore,
) -> None:
    # A stored block_mode without enabled reads as off, but is not forgotten:
    # enabling afterwards gives the host the block it always would have had.
    assert (await wstore.set_config("pc1", block_mode=True))["enforcement"] == "off"
    assert (await wstore.set_config("pc1", enabled=True))["enforcement"] == "protect"
    # enforcement wins over the legacy toggles in the same call.
    config = await wstore.set_config("pc1", enforcement="log_only", block_mode=True)
    assert config["enforcement"] == "log_only"


async def test_invalid_values_are_rejected_before_any_write(wstore: WebFilterStore) -> None:
    with pytest.raises(ValueError):
        await wstore.set_config("pc1", enforcement="block")
    with pytest.raises(ValueError):
        await wstore.set_config("pc1", history="everything")
    with sqlite3.connect(wstore.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM webfilter_config").fetchone()[0] == 0


async def test_default_history_applies_only_to_unconfigured_hosts(
    wstore: WebFilterStore,
) -> None:
    assert (await wstore.get_config("pc1"))["history"] == "violations"
    assert (await wstore.get_config("pc1", default_history="full"))["history"] == "full"
    # Configuring a host materialises the default it had at that moment.
    await wstore.set_config("pc1", doh_policy="leave", default_history="full")
    assert (await wstore.get_config("pc1", default_history="violations"))["history"] == "full"
    # So does the first push to a never-configured host.
    await wstore.set_applied_state("pc2", None, "now", True, default_history="full")
    assert (await wstore.get_config("pc2"))["history"] == "full"


_PRE_0069_SCHEMA = store_mod._WEBFILTER_SCHEMA.replace(
    ",\n    history               TEXT NOT NULL DEFAULT 'violations'", ""
)


def test_pre_0069_schema_fixture_really_lacks_history() -> None:
    assert "history" not in _PRE_0069_SCHEMA
    assert "history" in store_mod._WEBFILTER_SCHEMA


async def test_migration_keeps_every_recording_host_recording(tmp_path) -> None:
    db = str(tmp_path / "old.sqlite")
    with sqlite3.connect(db) as conn:
        conn.executescript(_PRE_0069_SCHEMA)
        conn.execute("ALTER TABLE webfilter_config ADD COLUMN categories TEXT")
        conn.execute("ALTER TABLE webfilter_domains ADD COLUMN category TEXT")
        conn.execute(
            "INSERT INTO webfilter_config (agent_id, enabled, block_mode) VALUES ('pc1', 1, 0)"
        )
        for agent_id, domain in (("pc1", "a.example"), ("pc2", "b.example")):
            conn.execute(
                "INSERT INTO web_activity_events (agent_id, domain, last_seen) "
                "VALUES (?, ?, '2026-09-01T00:00:00Z')",
                (agent_id, domain),
            )

    s = WebFilterStore(db_path=db)
    await s.connect()
    try:
        pc1 = await s.get_config("pc1")
        assert (pc1["enforcement"], pc1["history"]) == ("log_only", "full")
        pc2 = await s.get_config("pc2")  # recording, never configured
        assert (pc2["enforcement"], pc2["history"]) == ("off", "full")
        assert pc2["use_external_adult"] is True  # other columns at their defaults
        pc3 = await s.get_config("pc3")  # never configured, never observed
        assert (pc3["enforcement"], pc3["history"]) == ("off", "violations")
        await s.upsert_events("pc4", [{"domain": "c.example", "last_seen": "x"}])
    finally:
        await s.close()

    # The materialisation runs once, when the column appears — not on every boot.
    s = WebFilterStore(db_path=db)
    await s.connect()
    try:
        assert (await s.get_config("pc4"))["history"] == "violations"
        assert _raw_toggles(db, "pc4") is None
    finally:
        await s.close()


# --- pure derivations ---------------------------------------------------------


@pytest.mark.parametrize("enforcement, history", COMBOS)
def test_collects_web_activity(enforcement: str, history: str) -> None:
    expected = not (enforcement == "off" and history == "violations")
    assert collects_web_activity({"enforcement": enforcement, "history": history}) is expected


def test_list_drift_follows_the_level() -> None:
    applied = {"applied_at": "t", "applied_hash": "h1"}
    assert list_drift({**applied, "enforcement": "protect"}, "h1") is False
    assert list_drift({**applied, "enforcement": "protect"}, "h2") is True
    # Lowered from protect without a push: the host still carries a list.
    assert list_drift({**applied, "enforcement": "log_only"}, "h1") is True
    cleared = {"applied_at": "t", "applied_hash": None}
    assert list_drift({**cleared, "enforcement": "off"}, "h1") is False
    # Raised to protect after a clear: the host carries nothing yet.
    assert list_drift({**cleared, "enforcement": "protect"}, "h1") is True
    assert list_drift({"applied_at": None, "enforcement": "protect"}, "h1") is False


# --- service: settings, schedule, purge ---------------------------------------


class _Settings:
    def __init__(self, **values: str) -> None:
        self.values = values

    def get(self, key: str) -> str:
        return self.values[key]


def test_default_history_setting_is_declared() -> None:
    spec = CATALOG["KENNY_WEBFILTER_DEFAULT_HISTORY"]
    assert spec.type == "enum" and spec.lifecycle == "live"
    assert spec.group == "Web filter"
    assert spec.choices == WEBFILTER_HISTORY_MODES
    assert spec.default_raw == "violations"


async def test_service_resolves_the_default_history_live(tmp_path) -> None:
    s = WebFilterStore(db_path=str(tmp_path / "svc.sqlite"))
    await s.connect()
    try:
        assert (await WebFilterService(s, _StubCache()).get_config("pc1"))["history"] == (
            "violations"
        )
        settings = _Settings(KENNY_WEBFILTER_DEFAULT_HISTORY="full")
        service = WebFilterService(s, _StubCache(), settings=settings)
        config = await service.get_config("pc1")
        assert (config["history"], config["collecting"]) == ("full", True)
        settings.values["KENNY_WEBFILTER_DEFAULT_HISTORY"] = "violations"
        config = await service.get_config("pc1")
        assert (config["history"], config["collecting"]) == ("violations", False)
        with pytest.raises(ValueError):
            await service.configure("pc1", enforcement="nope")
    finally:
        await s.close()


async def test_first_contact_fixes_the_default_for_that_host(tmp_path) -> None:
    """The default applies to new hosts only: once a host has been heard from,
    changing the setting must not change what that host records."""

    s = WebFilterStore(db_path=str(tmp_path / "first.sqlite"))
    await s.connect()
    try:
        settings = _Settings(KENNY_WEBFILTER_DEFAULT_HISTORY="full")
        service = WebFilterService(s, _StubCache(), settings=settings)
        payload = {"status": "ok", "summary": "1 domain", "domains": [
            {"domain": "example.com", "first_seen": None, "last_seen": None,
             "hits": 1, "sources": ["dns_cache"]},
        ]}
        stored = await service.record_activity("seen-pc", payload)
        assert stored["domains"], "full history keeps the domain"

        settings.values["KENNY_WEBFILTER_DEFAULT_HISTORY"] = "violations"
        assert (await service.get_config("seen-pc"))["history"] == "full"
        assert (await service.get_config("new-pc"))["history"] == "violations"
        assert (await service.record_activity("seen-pc", payload))["domains"]
    finally:
        await s.close()


@pytest.mark.parametrize("enforcement", ["off", "log_only", "protect"])
async def test_schedule_due_only_under_protect(tmp_path, enforcement: str) -> None:
    s = WebFilterStore(db_path=str(tmp_path / "sched.sqlite"))
    await s.connect()
    try:
        service = WebFilterService(s, _StubCache())
        await service.set_config("pc1", enforcement=enforcement, categories=[])
        await service.add_domain("pc1", "chat.example", "block", None, "chat")
        await service.add_window(
            "pc1", days="daily", start="00:00", end="23:59", categories=["chat"], tz="UTC"
        )
        due = [d["agent_id"] for d in await service.schedule_due()]
        assert due == (["pc1"] if enforcement == "protect" else [])
        # The legacy half-state (block_mode without enabled) never pushes.
        await service.set_config("pc1", enforcement="off")
        await service.set_config("pc1", block_mode=True)
        assert await service.schedule_due() == []
    finally:
        await s.close()


def _snapshot(domains: list[str], flagged: list[str]) -> dict:
    return {
        "web_activity": {
            "status": "ok",
            "summary": "s",
            "domains": [{"domain": d} for d in domains],
            "flagged": [{"domain": d} for d in flagged],
        },
        "disk": {"status": "ok", "summary": "fine"},
    }


async def test_purge_on_full_to_violations_touches_that_host_only(
    tmp_path, monkeypatch
) -> None:
    db = str(tmp_path / "purge.sqlite")
    tel = TelemetryStore(db)
    wf = WebFilterStore(db)
    await tel.connect()
    await wf.connect()
    # Small chunks, so the chunked rewrite has to loop and still terminates.
    monkeypatch.setattr(TelemetryStore, "_PRUNE_CHUNK", 2)
    try:
        service = WebFilterService(wf, _StubCache(), telemetry_store=tel)
        for agent_id in ("pc1", "pc2"):
            await service.set_config(agent_id, enforcement="log_only", history="full")
            await wf.upsert_events(
                agent_id,
                [
                    {"domain": "bad.example", "last_seen": "2026-09-01", "flagged": True,
                     "category": "custom"},
                    {"domain": "good.example", "last_seen": "2026-09-01"},
                ],
            )
            for i in range(5):
                await tel.insert(
                    agent_id, f"2026-09-0{i + 1}T00:00:00Z",
                    _snapshot(["bad.example", "good.example"], ["bad.example"]),
                )
        # A snapshot without the section, and one already empty, are not rewritten.
        await tel.insert("pc1", "2026-09-06T00:00:00Z", {"disk": {"status": "ok", "summary": "x"}})
        await tel.insert("pc1", "2026-09-07T00:00:00Z", _snapshot([], []))

        outcome = await service.configure("pc1", history="violations")
        assert outcome["purged"] == {"events": 1, "snapshots": 5}

        assert [e["domain"] for e in await wf.activity("pc1", "2000")] == ["bad.example"]
        for record in await tel.history("pc1"):
            wa = record["snapshot"].get("web_activity")
            if wa is not None:
                assert wa["domains"] == []
                assert wa["summary"] == "s"
        kept = [r["snapshot"]["web_activity"]["flagged"] for r in await tel.history("pc1")
                if "web_activity" in r["snapshot"] and r["snapshot"]["web_activity"]["flagged"]]
        assert kept and all(f == [{"domain": "bad.example"}] for f in kept)

        # The other host is untouched.
        assert len(await wf.activity("pc2", "2000")) == 2
        for record in await tel.history("pc2"):
            assert len(record["snapshot"]["web_activity"]["domains"]) == 2

        # No purge unless history actually moves from full to violations.
        assert (await service.configure("pc1", history="violations"))["purged"] is None
        assert (await service.configure("pc2", enforcement="off"))["purged"] is None
    finally:
        await wf.close()
        await tel.close()


# --- joined: server + mock agent ----------------------------------------------


class RecordingAgent(WebfilterMockAgent):
    """Webfilter mock agent that also keeps every ``policy`` frame and tool call."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.policies: list[dict] = []
        self.tools: list[str] = []

    async def _loop(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            frame = json.loads(raw)
            if frame.get("type") == "policy":
                self.policies.append(frame)
            elif frame.get("type") == "request":
                self.tools.append(frame["tool"])
                await self._handle_request(frame)
            elif frame.get("type") == "ping":
                await self.ws.send(json.dumps({"type": "pong"}))


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


class _Harness:
    def __init__(self, app, port: int, agent: RecordingAgent) -> None:
        self.app = app
        self.base = f"http://127.0.0.1:{port}"
        self.port = port
        self.agent = agent
        self.http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {app.state.operator_token}"}
        )

    async def put_config(self, **body) -> httpx.Response:
        return await self.http.put(f"{self.base}/api/agent/dev/webfilter/config", json=body)

    def mcp(self) -> Client:
        return Client(
            StreamableHttpTransport(
                f"{self.base}/mcp",
                headers={"Authorization": f"Bearer {self.app.state.operator_token}"},
            )
        )

    def raw_snapshots(self) -> list[str]:
        with sqlite3.connect(self.app.state.store.db_path) as conn:
            return [r[0] for r in conn.execute("SELECT snapshot FROM snapshots")]


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    monkeypatch.setenv("KENNY_WEBFILTER_REFRESH_SECS", "0")  # no external fetch
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "wf_history.sqlite"))
    async with _Server(app, port):
        agent = RecordingAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await _wait_for(lambda: agent.policies)
        h = _Harness(app, port, agent)
        try:
            yield h
        finally:
            await h.http.aclose()
            await agent.stop()


@pytest.mark.parametrize("enforcement, history", COMBOS)
async def test_insert_path_keeps_what_the_host_config_says(
    harness: _Harness, enforcement: str, history: str
) -> None:
    h = harness
    r = await h.put_config(enforcement=enforcement, history=history, categories=[])
    assert r.status_code == 200, r.text
    r = await h.http.post(
        f"{h.base}/api/agent/dev/webfilter/domains",
        json={"domain": "badsite.example", "action": "block"},
    )
    assert r.status_code == 200

    await h.agent.push_web_activity(["sub.badsite.example", "good.example"])
    await _wait_for(lambda: h.raw_snapshots())
    latest = await h.app.state.store.latest("dev")
    wa = latest["snapshot"]["web_activity"]
    events = {e["domain"]: e for e in await h.app.state.webfilter.activity("dev")}

    matching = enforcement != "off"
    if history == "full":
        assert set(events) == {"sub.badsite.example", "good.example"}
        assert {d["domain"] for d in wa["domains"]} == {"sub.badsite.example", "good.example"}
    else:
        assert wa["domains"] == []
        assert set(events) == ({"sub.badsite.example"} if matching else set())
        # Nothing unmatched survives in either store, in any form.
        assert all("good.example" not in raw for raw in h.raw_snapshots())
        with sqlite3.connect(h.app.state.store.db_path) as conn:
            stored = [r[0] for r in conn.execute("SELECT domain FROM web_activity_events")]
        assert "good.example" not in stored
    if matching:
        assert [f["domain"] for f in wa["flagged"]] == ["sub.badsite.example"]
        assert wa["flagged_count_24h"] == 1
        assert events["sub.badsite.example"]["flagged"] is True
    else:
        assert "flagged" not in wa
        assert all(not e["flagged"] for e in events.values())
    # The rest of the section is kept as the agent sent it.
    assert wa["summary"] == "2 domains observed (24h)" and wa["truncated"] is False


async def test_enrichment_failure_stores_the_section_without_domains(
    harness: _Harness, monkeypatch
) -> None:
    h = harness
    await h.put_config(enforcement="log_only", history="full")

    async def boom(agent_id, payload):
        raise RuntimeError("enrichment bug")

    monkeypatch.setattr(h.app.state.webfilter, "record_activity", boom)
    await h.agent.push_web_activity(["good.example"])
    await _wait_for(lambda: h.raw_snapshots())
    wa = (await h.app.state.store.latest("dev"))["snapshot"]["web_activity"]
    assert wa["domains"] == []
    assert wa["summary"] == "1 domains observed (24h)"  # the snapshot itself is kept
    assert "good.example" not in h.raw_snapshots()[0]


async def test_policy_frame_carries_the_per_host_collect_gate(harness: _Harness) -> None:
    h = harness
    fixture = json.loads(FIXTURE.read_text())

    # Never configured, default history `violations`: off + violations.
    first = h.agent.policies[-1]
    assert first["collect"] == {"web_activity": False}
    # The frame is a valid `policy` frame of the fixture's shape.
    assert isinstance(parse_frame(first), Policy)
    assert dump_frame(parse_frame(first)) == first
    assert set(fixture) <= set(first)
    assert set(first["collect"]) == set(fixture["collect"])

    # log_only -> collect; the change is pushed to that agent right away.
    r = await h.put_config(enforcement="log_only")
    assert r.status_code == 200 and r.json()["config"]["collecting"] is True
    await _wait_for(lambda: len(h.agent.policies) == 2)
    assert h.agent.policies[-1]["collect"] == {"web_activity": True}

    # off + full still collects: nothing changed, nothing re-sent.
    r = await h.put_config(enforcement="off", history="full")
    assert r.json()["config"]["collecting"] is True
    assert (await h.app.state.tunnel._policy_frame("dev"))["collect"] == {"web_activity": True}
    await asyncio.sleep(0.1)
    assert len(h.agent.policies) == 2

    # Back to off + violations over MCP: pushed, and the purge is reported.
    async with h.mcp() as client:
        result = (
            await client.call_tool("webfilter_set", {"id": "dev", "history": "violations"})
        ).data
    assert result["config"]["collecting"] is False
    assert result["purged"] == {"events": 0, "snapshots": 0}
    await _wait_for(lambda: len(h.agent.policies) == 3)
    assert h.agent.policies[-1]["collect"] == {"web_activity": False}


async def test_config_put_reports_the_purge_and_rejects_bad_values(
    harness: _Harness,
) -> None:
    h = harness
    await h.put_config(enforcement="log_only", history="full", categories=[])
    await h.agent.push_web_activity(["good.example"])
    await _wait_for(lambda: h.raw_snapshots())

    r = await h.put_config(history="violations")
    assert r.status_code == 200
    assert r.json()["purged"] == {"events": 1, "snapshots": 1}
    assert all("good.example" not in raw for raw in h.raw_snapshots())
    # No purge -> no `purged` key.
    assert "purged" not in (await h.put_config(history="violations")).json()

    assert (await h.put_config(enforcement="block")).status_code == 400
    assert (await h.put_config(history="all")).status_code == 400
    async with h.mcp() as client:
        with pytest.raises(ClientToolError, match="bad_args"):
            await client.call_tool("webfilter_set", {"id": "dev", "enforcement": "block"})


@pytest.mark.parametrize(
    "config, blocks",
    [
        ({"enforcement": "protect"}, True),
        ({"enforcement": "log_only"}, False),
        ({"enforcement": "off"}, False),
        # The legacy half-state: block_mode without enabled is off, never a block.
        ({"enabled": False, "block_mode": True}, False),
    ],
)
async def test_push_and_apply_block_only_under_protect(
    harness: _Harness, config: dict, blocks: bool
) -> None:
    h = harness
    assert (await h.put_config(**config)).status_code == 200
    expected = "webfilter_apply" if blocks else "webfilter_clear"

    r = await h.http.post(f"{h.base}/api/agent/dev/webfilter/apply")
    assert r.status_code == 200 and r.json()["block_mode"] is blocks
    assert h.agent.tools[-1] == expected

    async with h.mcp() as client:
        result = (await client.call_tool("webfilter_push", {"id": "dev"})).data
    assert result["tool"] == expected
    assert h.agent.tools[-1] == expected

    overview = (await h.http.get(f"{h.base}/api/agent/dev/webfilter")).json()
    assert overview["drift"] is False
    assert (overview["applied"]["hash"] is not None) is blocks


async def test_bare_policy_frame_without_webfilter_has_no_collect() -> None:
    # A tunnel with no web-filter service omits `collect`: a v0.19 frame.
    assert "collect" not in dump_frame(Policy(rules=[]))
    assert dump_frame(Policy(rules=[], collect={"web_activity": True}))["collect"] == {
        "web_activity": True
    }

"""Tests for the parental-controls web-filter feature (ADR-0026).

Covers the pure matching core, the ``WebFilterStore`` CRUD/merge/prune, the
``ExternalListCache`` (via ``httpx.MockTransport``), the ``web_activity`` health
rule, and an integration test that drives a mock agent through telemetry
enrichment + the webfilter API (apply forwarding, applied-state, disabled).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kenny_server import health_rules, webfilter
from kenny_server.store import WebFilterStore
from kenny_server.webfilter import (
    ExternalListCache,
    _max_block_domains,
    build_apply_args,
    classify,
    effective_list,
    matches,
    normalize_domain,
)

# --- pure matching core -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Pornhub.com", "pornhub.com"),
        ("https://www.Bad.Example.com/path?q=1", "www.bad.example.com"),
        ("user@host.example:8080", "host.example"),
        ("trailing.dot.example.", "trailing.dot.example"),
        ("  spaced.example  ", "spaced.example"),
        ("localhost", None),
        ("", None),
        (None, None),
        ("0.0.0.0", None),
        ("192.168.1.1", None),
        ("no_spaces here.example", None),
    ],
)
def test_normalize_domain(raw, expected) -> None:
    assert normalize_domain(raw) == expected


def test_matches_suffix_and_subdomains() -> None:
    assert matches("bad.example", "bad.example")
    assert matches("sub.bad.example", "bad.example")
    assert matches("a.b.bad.example", "bad.example")
    assert not matches("notbad.example", "bad.example")
    assert not matches("bad.example.org", "bad.example")


def test_classify_categories_and_allow_precedence() -> None:
    effective = {
        "blocks": {"bad.example": "seed", "deep.bad.example": "custom"},
        "allows": {"safe.bad.example"},
    }
    # subdomain of a blocked entry -> flagged with that entry's category
    assert classify("x.bad.example", effective) == ("seed", "bad.example")
    # most-specific block entry wins
    assert classify("deep.bad.example", effective) == ("custom", "deep.bad.example")
    # an equal-or-more-specific allow overrides the block
    assert classify("safe.bad.example", effective) is None
    # a broader allow does NOT unblock a narrower block
    eff2 = {"blocks": {"deep.bad.example": "custom"}, "allows": {"bad.example"}}
    assert classify("deep.bad.example", eff2) == ("custom", "deep.bad.example")
    # nothing matches
    assert classify("good.example", effective) is None


class _StubCache:
    def __init__(self, adult=(), bypass=()) -> None:
        self._data = {"adult": frozenset(adult), "bypass": frozenset(bypass)}

    def get(self, source: str) -> frozenset[str]:
        return self._data.get(source, frozenset())

    def max_block_domains(self) -> int:
        return _max_block_domains()


def test_effective_list_layers() -> None:
    cache = _StubCache(adult={"extern.adult"}, bypass={"vpn.bypass"})
    config = {"use_external_adult": True, "use_bypass_protection": True}
    rows = [
        {"domain": "watch.example", "action": "watch"},
        {"domain": "block.example", "action": "block"},
        {"domain": "allow.example", "action": "allow"},
    ]
    eff = effective_list(config, rows, cache)
    assert eff["blocks"]["watch.example"] == "custom"
    assert eff["blocks"]["block.example"] == "custom"
    assert eff["blocks"]["extern.adult"] == "external_adult"
    assert eff["blocks"]["vpn.bypass"] == "bypass"
    assert "allow.example" in eff["allows"]
    # seed always contributes (exact dict-key lookup, not a URL substring check)
    assert eff["blocks"].get("pornhub.com") == "seed"


def test_effective_list_toggles_off() -> None:
    cache = _StubCache(adult={"extern.adult"}, bypass={"vpn.bypass"})
    config = {"use_external_adult": False, "use_bypass_protection": False}
    eff = effective_list(config, [], cache)
    assert "extern.adult" not in eff["blocks"]
    assert "vpn.bypass" not in eff["blocks"]


def test_build_apply_args_hash_stable_and_excludes_watch() -> None:
    cache = _StubCache(adult={"extern.adult"})
    config = {"use_external_adult": True, "use_bypass_protection": False, "doh_policy": "disable"}
    rows = [
        {"domain": "watch.example", "action": "watch"},
        {"domain": "block.example", "action": "block"},
        {"domain": "allow.example", "action": "allow"},
    ]
    a1 = build_apply_args(config, rows, cache)
    a2 = build_apply_args(config, rows, cache)
    assert a1 == a2  # deterministic
    assert a1["domains"] == sorted(a1["domains"])
    assert "block.example" in a1["domains"]
    assert "extern.adult" in a1["domains"]
    assert "watch.example" not in a1["domains"]  # watch is matchable, not blocked
    assert a1["doh_policy"] == "disable"
    assert len(a1["list_hash"]) == 16


def test_build_apply_args_allow_removes_seed_entry() -> None:
    cache = _StubCache()
    config = {"use_external_adult": False, "use_bypass_protection": False, "doh_policy": "disable"}
    rows = [{"domain": "pornhub.com", "action": "allow"}]
    args = build_apply_args(config, rows, cache)
    assert "pornhub.com" not in args["domains"]


def test_build_apply_args_external_cap(monkeypatch) -> None:
    monkeypatch.setenv("KENNY_WEBFILTER_MAX_BLOCK_DOMAINS", "3")
    cache = _StubCache(adult={f"d{i}.adult" for i in range(50)})
    config = {"use_external_adult": True, "use_bypass_protection": False, "doh_policy": "disable"}
    args = build_apply_args(config, [], cache)
    extern = [d for d in args["domains"] if d.endswith(".adult")]
    assert len(extern) == 3


def test_build_apply_args_hard_cap(monkeypatch) -> None:
    monkeypatch.setattr(webfilter, "_HARD_CAP", 5)
    cache = _StubCache()
    config = {"use_external_adult": False, "use_bypass_protection": False, "doh_policy": "disable"}
    args = build_apply_args(config, [], cache)
    assert len(args["domains"]) == 5


# --- external list parsing / cache -------------------------------------------


def test_parse_hosts_and_domain_formats() -> None:
    hosts = "0.0.0.0 evil.example\n0.0.0.0 0.0.0.0\n127.0.0.1 localhost\n# comment\n0.0.0.0 bad.test\n"
    parsed = webfilter._parse_list(hosts)
    assert parsed == frozenset({"evil.example", "bad.test"})
    domains = "# header\nproxy.example\nvpn.test\n\n0.0.0.0\n"
    assert webfilter._parse_list(domains) == frozenset({"proxy.example", "vpn.test"})


@pytest.mark.asyncio
async def test_external_cache_success(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "porn" in str(request.url):
            return httpx.Response(200, text="0.0.0.0 evil.example\n0.0.0.0 bad.test\n")
        return httpx.Response(200, text="proxy.example\nvpn.test\n")

    transport = httpx.MockTransport(handler)
    cache = ExternalListCache(
        str(tmp_path), client_factory=lambda: httpx.AsyncClient(transport=transport)
    )
    await cache.refresh_all()
    assert "evil.example" in cache.get("adult")
    assert "proxy.example" in cache.get("bypass")
    stats = cache.stats()
    assert stats["adult"]["count"] == 2
    assert stats["adult"]["last_fetch"] is not None
    # write-through disk cache: a fresh instance loads it without fetching.
    reloaded = ExternalListCache(str(tmp_path))
    assert "evil.example" in reloaded.get("adult")


@pytest.mark.asyncio
async def test_external_cache_404_keeps_stale(tmp_path) -> None:
    state = {"ok": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["ok"]:
            return httpx.Response(404, text="nope")
        if "porn" in str(request.url):
            return httpx.Response(200, text="0.0.0.0 evil.example\n")
        return httpx.Response(200, text="proxy.example\n")

    transport = httpx.MockTransport(handler)
    cache = ExternalListCache(
        str(tmp_path), client_factory=lambda: httpx.AsyncClient(transport=transport)
    )
    await cache.refresh_all()
    assert "evil.example" in cache.get("adult")
    state["ok"] = False
    await cache.refresh_all()
    # stale copy retained after the 404
    assert "evil.example" in cache.get("adult")


@pytest.mark.asyncio
async def test_external_cache_oversized_rejected(tmp_path) -> None:
    big = "0.0.0.0 x.example\n" + ("0.0.0.0 pad.example\n" * 400_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big)

    transport = httpx.MockTransport(handler)
    cache = ExternalListCache(
        str(tmp_path), client_factory=lambda: httpx.AsyncClient(transport=transport)
    )
    await cache.refresh_all()
    assert cache.get("adult") == frozenset()  # oversized body rejected


# --- WebFilterStore -----------------------------------------------------------


@pytest.fixture
async def wstore(tmp_path) -> WebFilterStore:
    s = WebFilterStore(db_path=str(tmp_path / "wf.sqlite"))
    await s.connect()
    yield s
    await s.close()


async def test_store_config_defaults_and_set(wstore: WebFilterStore) -> None:
    cfg = await wstore.get_config("pc1")
    assert cfg["enabled"] is False
    assert cfg["use_external_adult"] is True
    assert cfg["doh_policy"] == "disable"
    updated = await wstore.set_config("pc1", enabled=True, block_mode=True)
    assert updated["enabled"] is True
    assert updated["block_mode"] is True
    # partial update preserves other fields
    updated2 = await wstore.set_config("pc1", doh_policy="leave")
    assert updated2["enabled"] is True
    assert updated2["doh_policy"] == "leave"


async def test_store_applied_state_preserved_across_config(wstore: WebFilterStore) -> None:
    await wstore.set_applied_state("pc1", "hash123", "2026-07-02T09:00:00Z", True)
    await wstore.set_config("pc1", enabled=True)
    cfg = await wstore.get_config("pc1")
    assert cfg["applied_hash"] == "hash123"
    assert cfg["applied_ok"] is True


async def test_store_domains_crud(wstore: WebFilterStore) -> None:
    await wstore.add_domain("pc1", "bad.example", "block", "note")
    await wstore.add_domain("pc1", "watch.example", "watch")
    rows = await wstore.list_domains("pc1")
    assert {r["domain"] for r in rows} == {"bad.example", "watch.example"}
    # upsert changes action
    await wstore.add_domain("pc1", "bad.example", "allow")
    rows = await wstore.list_domains("pc1")
    assert next(r for r in rows if r["domain"] == "bad.example")["action"] == "allow"
    assert await wstore.remove_domain("pc1", "bad.example") is True
    assert await wstore.remove_domain("pc1", "bad.example") is False


async def test_store_upsert_events_merge(wstore: WebFilterStore) -> None:
    await wstore.upsert_events(
        "pc1",
        [
            {
                "domain": "bad.example",
                "first_seen": "2026-07-01T10:00:00Z",
                "last_seen": "2026-07-01T11:00:00Z",
                "hits": 2,
                "sources": ["dns_cache"],
                "flagged": True,
                "category": "seed",
            }
        ],
    )
    await wstore.upsert_events(
        "pc1",
        [
            {
                "domain": "bad.example",
                "first_seen": "2026-07-01T09:00:00Z",
                "last_seen": "2026-07-01T12:00:00Z",
                "hits": 3,
                "sources": ["browser_history"],
                "flagged": True,
                "category": "seed",
            }
        ],
    )
    rows = await wstore.activity("pc1", "2026-07-01T00:00:00Z")
    assert len(rows) == 1
    row = rows[0]
    assert row["first_seen"] == "2026-07-01T09:00:00Z"  # min
    assert row["last_seen"] == "2026-07-01T12:00:00Z"  # max
    assert row["hits"] == 5  # summed
    assert set(row["sources"]) == {"dns_cache", "browser_history"}  # union


async def test_store_activity_flagged_only(wstore: WebFilterStore) -> None:
    await wstore.upsert_events(
        "pc1",
        [
            {"domain": "a.example", "first_seen": "2026-07-01T10:00:00Z",
             "last_seen": "2026-07-01T11:00:00Z", "hits": 1, "sources": [],
             "flagged": False, "category": None},
            {"domain": "b.example", "first_seen": "2026-07-01T10:00:00Z",
             "last_seen": "2026-07-01T11:00:00Z", "hits": 1, "sources": [],
             "flagged": True, "category": "custom"},
        ],
    )
    assert len(await wstore.activity("pc1", "2026-07-01T00:00:00Z")) == 2
    flagged = await wstore.activity("pc1", "2026-07-01T00:00:00Z", flagged_only=True)
    assert [r["domain"] for r in flagged] == ["b.example"]


async def test_store_prune(wstore: WebFilterStore) -> None:
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    recent = (now - timedelta(days=2)).isoformat()
    await wstore.upsert_events(
        "pc1",
        [
            {"domain": "old.example", "first_seen": old, "last_seen": old, "hits": 1,
             "sources": [], "flagged": False, "category": None},
            {"domain": "recent.example", "first_seen": recent, "last_seen": recent,
             "hits": 1, "sources": [], "flagged": False, "category": None},
        ],
    )
    deleted = await wstore.prune(now=now)
    assert deleted == 1
    rows = await wstore.activity("pc1", (now - timedelta(days=60)).isoformat())
    assert [r["domain"] for r in rows] == ["recent.example"]


# --- health rule --------------------------------------------------------------

NOW = datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc)


def _flag(domain: str, category: str, *, age_h: float = 1.0) -> dict:
    last = (NOW - timedelta(hours=age_h)).isoformat()
    return {"domain": domain, "category": category, "matched_entry": domain,
            "first_seen": last, "last_seen": last}


def test_rule_web_activity_defers_without_annotation() -> None:
    out = health_rules.evaluate_section(
        "web_activity", {"status": "ok", "summary": "x"}, now=NOW
    )
    # no `flagged` key => rule returns None => agent status kept, no reason
    assert out["status"] == "ok"
    assert "reason" not in out


def test_rule_web_activity_serious_is_crit() -> None:
    for category in ("custom", "seed", "external_adult"):
        out = health_rules.evaluate_section(
            "web_activity",
            {"status": "ok", "summary": "x", "flagged": [_flag("bad.example", category)]},
            now=NOW,
        )
        assert out["status"] == "crit", category
        assert "bad.example" in out["reason"]


def test_rule_web_activity_bypass_is_warn() -> None:
    out = health_rules.evaluate_section(
        "web_activity",
        {"status": "ok", "summary": "x", "flagged": [_flag("vpn.example", "bypass")]},
        now=NOW,
    )
    assert out["status"] == "warn"


def test_rule_web_activity_aged_out_is_ok() -> None:
    out = health_rules.evaluate_section(
        "web_activity",
        {"status": "ok", "summary": "x",
         "flagged": [_flag("bad.example", "seed", age_h=48)]},
        now=NOW,
    )
    assert out["status"] == "ok"
    assert "no flagged" in out["reason"]


# --- integration: mock agent + tunnel enrichment + API -----------------------

from test_server_e2e import (  # noqa: E402
    SERVER_SEED_B64,
    MockAgent,
    _fixture,
    _free_port,
    _Server,
)
from kenny_server.main import build_app  # noqa: E402


class WebfilterMockAgent(MockAgent):
    """Mock agent that also replays the webfilter_* fixtures."""

    def __init__(self, *args, apply_disabled: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.apply_disabled = apply_disabled

    async def _handle_request(self, frame: dict) -> None:
        assert self.ws is not None
        tool = frame["tool"]
        fixtures = {
            "webfilter_apply": "response_webfilter_apply.json",
            "webfilter_clear": "response_webfilter_clear.json",
            "webfilter_status": "response_webfilter_status.json",
        }
        if tool in fixtures:
            if tool == "webfilter_apply" and self.apply_disabled:
                await self.ws.send(json.dumps({
                    "type": "response", "id": frame["id"], "ok": False,
                    "error": {"code": "disabled", "message": "remote control off"},
                }))
                return
            result = _fixture(fixtures[tool])["result"]
            await self.ws.send(json.dumps(
                {"type": "response", "id": frame["id"], "ok": True, "result": result}
            ))
            return
        await super()._handle_request(frame)

    async def push_web_activity(self, domains: list[str]) -> None:
        assert self.ws is not None
        last = datetime.now(timezone.utc).isoformat()
        frame = {
            "type": "telemetry",
            "agent_id": self.agent_id,
            "collected_at": last,
            "snapshot": {
                "web_activity": {
                    "status": "ok",
                    "summary": f"{len(domains)} domains observed (24h)",
                    "window_hours": 24,
                    "sources": ["dns_cache", "browser_history"],
                    "domains": [
                        {"domain": d, "first_seen": last, "last_seen": last,
                         "hits": 3, "sources": ["dns_cache"]}
                        for d in domains
                    ],
                    "truncated": False,
                    "browser_profiles_read": 1,
                    "errors": [],
                }
            },
        }
        await self.ws.send(json.dumps(frame))


@pytest.mark.asyncio
async def test_integration_webfilter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    monkeypatch.setenv("KENNY_WEBFILTER_REFRESH_SECS", "0")  # no external fetch
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "wf_e2e.sqlite"))
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {app.state.operator_token}"}

    async with _Server(app, port):
        agent = WebfilterMockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        async with httpx.AsyncClient(headers=headers) as c:
            # Enable the feature + block mode, add a custom block domain.
            r = await c.put(
                f"{base}/api/agent/dev/webfilter/config",
                json={"enabled": True, "block_mode": True},
            )
            assert r.status_code == 200 and r.json()["config"]["enabled"] is True
            r = await c.post(
                f"{base}/api/agent/dev/webfilter/domains",
                json={"domain": "badsite.example", "action": "block"},
            )
            assert r.status_code == 200
            # invalid domain rejected
            r = await c.post(
                f"{base}/api/agent/dev/webfilter/domains",
                json={"domain": "not a domain", "action": "block"},
            )
            assert r.status_code == 400

            # Agent pushes web activity that includes a subdomain of the block.
            await agent.push_web_activity(["sub.badsite.example", "good.example"])
            await asyncio.sleep(0.2)

            # Stored snapshot annotated with `flagged`.
            latest = await app.state.store.latest("dev")
            wa = latest["snapshot"]["web_activity"]
            assert any(f["domain"] == "sub.badsite.example" for f in wa["flagged"])
            assert wa["flagged_count_24h"] >= 1

            # Health shows crit for web_activity.
            body = (await c.get(f"{base}/api/agent/dev")).json()
            assert body["health"]["sections"]["web_activity"]["status"] == "crit"

            # Events upserted + queryable flagged-only.
            act = (await c.get(f"{base}/api/agent/dev/webfilter/activity?flagged=1")).json()
            assert any(e["domain"] == "sub.badsite.example" for e in act["events"])

            # Apply forwards webfilter_apply (replays the fixture) + persists state.
            r = await c.post(f"{base}/api/agent/dev/webfilter/apply")
            assert r.status_code == 200
            payload = r.json()
            assert payload["ok"] is True and payload["block_mode"] is True
            wf = (await c.get(f"{base}/api/agent/dev/webfilter")).json()
            assert wf["applied"]["hash"] == wf["current_hash"]
            assert wf["drift"] is False
            assert wf["seed_count"] >= 30

        await agent.stop()


@pytest.mark.asyncio
async def test_integration_webfilter_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    monkeypatch.setenv("KENNY_WEBFILTER_REFRESH_SECS", "0")
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "wf_disabled.sqlite"))
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {app.state.operator_token}"}

    async with _Server(app, port):
        agent = WebfilterMockAgent(
            f"ws://127.0.0.1:{port}/agent/ws", "dev", apply_disabled=True
        )
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        async with httpx.AsyncClient(headers=headers) as c:
            await c.put(
                f"{base}/api/agent/dev/webfilter/config",
                json={"enabled": True, "block_mode": True},
            )
            r = await c.post(f"{base}/api/agent/dev/webfilter/apply")
            # Kill switch: agent refuses with `disabled`, surfaced distinctly.
            assert r.status_code == 200
            assert r.json() == {"ok": False, "error": "disabled"}

        await agent.stop()

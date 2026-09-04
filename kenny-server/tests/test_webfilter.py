"""Tests for the parental-controls web-filter feature (ADR-0024).

Covers the pure matching core, the ``WebFilterStore`` CRUD/merge/prune, the
``ExternalListCache`` (via ``httpx.MockTransport``), the ``web_activity`` health
rule, and an integration test that drives a mock agent through telemetry
enrichment + the webfilter API (apply forwarding, applied-state, disabled).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kenny_server import health_rules, webfilter
from kenny_server.store import WebFilterStore
from kenny_server.webfilter import (
    ExternalListCache,
    ListTooLargeError,
    WebFilterService,
    _max_block_domains,
    build_apply_args,
    classify,
    effective_list,
    make_window,
    matches,
    normalize_domain,
    requested_domains,
    schedule_state,
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


def test_requested_domains_finds_mentions_in_free_text() -> None:
    domains = requested_domains(
        "please unblock discord.com", "also need roblox.com for a school project"
    )
    assert domains == ["discord.com", "roblox.com"]


def test_requested_domains_ignores_non_string_input() -> None:
    assert requested_domains(None, 123, ["not", "a", "string"]) == []


def test_requested_domains_bounds_pathological_text(monkeypatch) -> None:
    """Regression for a ReDoS in ``_TEXT_DOMAIN_RE`` (CWE-1333).

    A title/summary built from many short "label." repeats with no valid TLD
    tail (e.g. ``"a." * n``) makes the regex's backtracking cost grow
    quadratically with input length -- ticket title/summary is untrusted,
    attacker-sized text (see the module docstring on ``requested_domains``),
    so an unbounded string here could hang the single-threaded event loop for
    the whole server. ``requested_domains`` must stay fast regardless of how
    long the input is.
    """

    pathological = "a." * 200_000 + "!"
    start = time.perf_counter()
    result = requested_domains(pathological)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"requested_domains took {elapsed:.2f}s on pathological input"
    assert result == []


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
    def __init__(self, adult=(), bypass=(), **sources) -> None:
        self._data = {
            "adult": frozenset(adult),
            "bypass": frozenset(bypass),
            **{k: frozenset(v) for k, v in sources.items()},
        }

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


def test_build_apply_args_over_hard_cap_raises(monkeypatch) -> None:
    """Past the agent's cap the server refuses instead of truncating.

    The agent rejects an over-cap list with ``bad_args`` and never truncates, so
    a silent prefix would be a filter the operator believes is complete and is
    not. The error has to carry the count and the cap so the UI can say by how
    much (see ``ListTooLargeError``).
    """

    monkeypatch.setattr(webfilter, "_HARD_CAP", 5)
    cache = _StubCache()
    config = {"use_external_adult": False, "use_bypass_protection": False, "doh_policy": "disable"}
    with pytest.raises(ListTooLargeError) as excinfo:
        build_apply_args(config, [], cache)
    assert excinfo.value.cap == 5
    assert excinfo.value.count == len(webfilter.load_seed())
    assert "5" in str(excinfo.value)


def test_build_apply_args_at_hard_cap_is_allowed(monkeypatch) -> None:
    """Exactly at the cap is fine — only *over* it is refused."""

    monkeypatch.setattr(webfilter, "_HARD_CAP", len(webfilter.load_seed()))
    cache = _StubCache()
    config = {"use_external_adult": False, "use_bypass_protection": False, "doh_policy": "disable"}
    args = build_apply_args(config, [], cache)
    assert len(args["domains"]) == len(webfilter.load_seed())


# --- named categories ---------------------------------------------------------


def test_category_catalog_shape() -> None:
    """Every catalog entry is coherent, and the two legacy toggles still exist."""

    for key, spec in webfilter.CATEGORY_CATALOG.items():
        assert spec.key == key
        assert spec.label and spec.provenance
        # A local category has no upstream; an external one must have a URL.
        assert spec.external == (spec.url is not None)
    assert webfilter.CATEGORY_CATALOG["adult"].provenance == "external_adult"
    assert webfilter.CATEGORY_CATALOG["bypass"].capped is False
    # The cache fetches exactly the external categories, no more, no less.
    assert set(webfilter._SOURCES) == {
        k for k, s in webfilter.CATEGORY_CATALOG.items() if s.external
    }


def test_validate_categories_normalizes_and_rejects() -> None:
    assert webfilter.validate_categories(["gaming", "adult", "gaming"]) == (
        "adult",
        "gaming",
    )
    assert webfilter.validate_categories(None) == ()
    with pytest.raises(ValueError):
        webfilter.validate_categories(["not_a_category"])


def test_active_categories_merges_legacy_toggles_and_extras() -> None:
    config = {
        "use_external_adult": True,
        "use_bypass_protection": False,
        "categories": ["adult", "gambling"],
    }
    assert webfilter.active_categories(config) == frozenset({"adult", "gambling"})
    # A schedule window only ever adds.
    assert webfilter.active_categories(config, ["social"]) == frozenset(
        {"adult", "gambling", "social"}
    )
    # An unknown key is ignored rather than crashing an unattended pass.
    assert webfilter.active_categories(config, ["nope"]) == frozenset(
        {"adult", "gambling"}
    )


def test_effective_list_gates_tagged_custom_entries() -> None:
    """A tagged custom entry applies only while its category is on."""

    cache = _StubCache()
    rows = [
        {"domain": "always.example", "action": "block", "category": None},
        {"domain": "social.example", "action": "block", "category": "social"},
    ]
    off = {"use_external_adult": False, "use_bypass_protection": False, "categories": []}
    eff = effective_list(off, rows, cache)
    assert "always.example" in eff["blocks"]
    assert "social.example" not in eff["blocks"]
    # ...and comes into force when a window adds it.
    eff = effective_list(off, rows, cache, extra_categories=["social"])
    assert eff["blocks"]["social.example"] == "custom"


def test_effective_list_new_external_category_provenance() -> None:
    cache = _StubCache(gambling={"bet.example"})
    config = {"use_external_adult": False, "use_bypass_protection": False,
              "categories": ["gambling"]}
    eff = effective_list(config, [], cache)
    assert eff["blocks"]["bet.example"] == "gambling"
    assert classify("x.bet.example", eff) == ("gambling", "bet.example")


def test_build_apply_args_capped_categories_share_one_budget(monkeypatch) -> None:
    """Two content categories share the extract budget; bypass is never trimmed."""

    monkeypatch.setenv("KENNY_WEBFILTER_MAX_BLOCK_DOMAINS", "4")
    cache = _StubCache(
        adult={f"a{i}.adult" for i in range(10)},
        bypass={f"b{i}.bypass" for i in range(10)},
        gambling={f"g{i}.bet" for i in range(10)},
    )
    config = {
        "use_external_adult": True,
        "use_bypass_protection": True,
        "categories": ["adult", "bypass", "gambling"],
        "doh_policy": "disable",
    }
    args = build_apply_args(config, [], cache)
    capped = [d for d in args["domains"] if d.endswith((".adult", ".bet"))]
    uncapped = [d for d in args["domains"] if d.endswith(".bypass")]
    assert len(capped) == 4  # one shared budget, not four per category
    assert len(uncapped) == 10  # bypass protection is never silently trimmed


def test_build_apply_args_schedule_extra_changes_the_hash() -> None:
    """The pushed payload keeps its shape; only its contents move with the schedule."""

    cache = _StubCache()
    config = {"use_external_adult": False, "use_bypass_protection": False,
              "categories": [], "doh_policy": "disable"}
    rows = [{"domain": "chat.example", "action": "block", "category": "chat"}]
    relaxed = build_apply_args(config, rows, cache)
    strict = build_apply_args(config, rows, cache, extra_categories=["chat"])
    assert set(strict) == set(relaxed) == {"domains", "doh_policy", "list_hash"}
    assert "chat.example" in strict["domains"]
    assert "chat.example" not in relaxed["domains"]
    assert strict["list_hash"] != relaxed["list_hash"]


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


async def test_store_categories_round_trip(wstore: WebFilterStore) -> None:
    """``categories`` is the canonical read; the legacy booleans stay in step."""

    cfg = await wstore.get_config("pc1")
    assert cfg["categories"] == ["adult"]  # matches use_external_adult's default

    cfg = await wstore.set_config("pc1", categories=["adult", "bypass", "gambling"])
    assert cfg["categories"] == ["adult", "bypass", "gambling"]
    assert cfg["use_external_adult"] is True
    assert cfg["use_bypass_protection"] is True

    # Setting the list without "adult" turns the legacy boolean off too, so the
    # two representations can never disagree.
    cfg = await wstore.set_config("pc1", categories=["gambling"])
    assert cfg["use_external_adult"] is False
    assert cfg["categories"] == ["gambling"]

    # ...and the old boolean path still moves only its own category.
    cfg = await wstore.set_config("pc1", use_external_adult=True)
    assert cfg["categories"] == ["adult", "gambling"]


async def test_store_domain_category_defaults_to_null(wstore: WebFilterStore) -> None:
    await wstore.add_domain("pc1", "always.example", "block")
    await wstore.add_domain("pc1", "social.example", "block", None, "social")
    rows = {r["domain"]: r for r in await wstore.list_domains("pc1")}
    assert rows["always.example"]["category"] is None
    assert rows["social.example"]["category"] == "social"


async def test_store_windows_crud_and_fleet(wstore: WebFilterStore) -> None:
    w = make_window("pc1", days="mon,tue", start="21:00", end="07:00",
                    categories=["social"], tz="UTC")
    await wstore.add_window({**w.__dict__, "days": list(w.days),
                             "categories": list(w.categories)})
    rows = await wstore.list_windows("pc1")
    assert len(rows) == 1 and rows[0]["categories"] == ["social"]
    assert await wstore.agents_with_windows() == ["pc1"]

    # A disabled window takes the host out of the schedule loop's fleet.
    assert await wstore.set_window_enabled("pc1", w.id, False) is True
    assert await wstore.agents_with_windows() == []
    assert await wstore.remove_window("pc1", w.id) is True
    assert await wstore.list_windows("pc1") == []


async def test_store_delete_agent_removes_windows(wstore: WebFilterStore) -> None:
    w = make_window("pc1", days="mon", start="09:00", end="10:00",
                    categories=["gaming"], tz="UTC")
    await wstore.add_window({**w.__dict__, "days": list(w.days),
                             "categories": list(w.categories)})
    await wstore.delete_agent("pc1")
    assert await wstore.list_windows("pc1") == []


# --- schedule -----------------------------------------------------------------


def _win(**kwargs):
    kwargs.setdefault("tz", "Europe/Berlin")
    return make_window("pc1", **kwargs)


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_window_active_within_and_outside_range() -> None:
    w = _win(days="mon,tue", start="09:00", end="12:00", categories=["gaming"])
    # 10:00 Berlin on a Monday.
    assert webfilter.window_active_at(w, _at("2026-08-17T08:00:00+00:00")) is True
    # 13:00 Berlin, same Monday.
    assert webfilter.window_active_at(w, _at("2026-08-17T11:00:00+00:00")) is False
    # 10:00 Berlin on a Wednesday: not a listed weekday.
    assert webfilter.window_active_at(w, _at("2026-08-19T08:00:00+00:00")) is False


def test_window_wrapping_midnight_runs_into_the_next_day() -> None:
    """A 21:00-07:00 Monday window covers Tuesday morning, not Monday morning."""

    w = _win(days="mon", start="21:00", end="07:00", categories=["social"])
    assert w.wraps_midnight is True
    # Monday 22:00 Berlin -> open.
    assert webfilter.window_active_at(w, _at("2026-08-17T20:00:00+00:00")) is True
    # Tuesday 06:00 Berlin -> still open (it started Monday).
    assert webfilter.window_active_at(w, _at("2026-08-18T04:00:00+00:00")) is True
    # Tuesday 08:00 Berlin -> closed.
    assert webfilter.window_active_at(w, _at("2026-08-18T06:00:00+00:00")) is False
    # Monday 06:00 Berlin -> closed; the window has not started yet.
    assert webfilter.window_active_at(w, _at("2026-08-17T04:00:00+00:00")) is False


def test_window_disabled_is_never_active() -> None:
    w = _win(days="mon", start="00:01", end="23:59", categories=["social"],
             enabled=False)
    assert webfilter.window_active_at(w, _at("2026-08-17T10:00:00+00:00")) is False


def test_schedule_state_reports_stricter_and_revert_time() -> None:
    """The two questions an operator has, answered in one payload."""

    config = {"use_external_adult": True, "use_bypass_protection": False,
              "categories": ["adult"]}
    w = _win(days="mon", start="21:00", end="07:00", categories=["social", "gaming"])
    # Monday 22:00 Berlin: the window is open.
    state = schedule_state(config, [w], at=_at("2026-08-17T20:00:00+00:00"))
    assert state["stricter"] is True
    assert state["base_categories"] == ["adult"]
    assert state["extra_categories"] == ["gaming", "social"]
    assert state["effective_categories"] == ["adult", "gaming", "social"]
    assert [x["id"] for x in state["active_windows"]] == [w.id]
    # Reverts at 07:00 Berlin on Tuesday = 05:00 UTC.
    assert state["reverts_at"] == "2026-08-18T05:00:00+00:00"
    assert state["next_change_at"] == state["reverts_at"]

    # Tuesday noon: relaxed again, and the next change is next Monday 21:00.
    state = schedule_state(config, [w], at=_at("2026-08-18T10:00:00+00:00"))
    assert state["stricter"] is False
    assert state["extra_categories"] == []
    assert state["reverts_at"] is None
    assert state["next_change_at"] == "2026-08-24T19:00:00+00:00"


def test_schedule_state_ignores_a_window_that_adds_nothing_new() -> None:
    """A window naming a category the host already has is not "stricter"."""

    config = {"use_external_adult": True, "use_bypass_protection": False,
              "categories": ["adult"]}
    w = _win(days="mon", start="00:00", end="23:00", categories=["adult"])
    state = schedule_state(config, [w], at=_at("2026-08-17T10:00:00+00:00"))
    assert state["active_windows"] and state["stricter"] is False
    assert state["reverts_at"] is None


def test_next_change_skips_a_handover_between_equal_windows() -> None:
    """Two back-to-back windows with the same category are one continuous state."""

    early = _win(days="mon", start="08:00", end="12:00", categories=["social"])
    late = _win(days="mon", start="12:00", end="16:00", categories=["social"])
    config = {"use_external_adult": False, "use_bypass_protection": False,
              "categories": []}
    # 09:00 Berlin: the next real change is 16:00 Berlin (14:00 UTC), not 12:00.
    state = schedule_state(config, [early, late], at=_at("2026-08-17T07:00:00+00:00"))
    assert state["stricter"] is True
    assert state["reverts_at"] == "2026-08-17T14:00:00+00:00"


def test_schedule_survives_a_dst_transition() -> None:
    """A wall-clock window still ends at its local time when the clocks change."""

    # Europe/Berlin leaves DST at 03:00 local on 2026-10-25 (a Sunday).
    w = _win(days="sat", start="22:00", end="06:00", categories=["social"])
    config = {"use_external_adult": False, "use_bypass_protection": False,
              "categories": []}
    # Saturday 23:00 Berlin = 21:00 UTC.
    state = schedule_state(config, [w], at=_at("2026-10-24T21:00:00+00:00"))
    assert state["stricter"] is True
    # 06:00 Berlin on Sunday is 05:00 UTC after the clocks go back (CET, +1).
    assert state["reverts_at"] == "2026-10-25T05:00:00+00:00"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"days": "funday", "start": "09:00", "end": "10:00", "categories": ["social"]},
        {"days": "mon", "start": "9am", "end": "10:00", "categories": ["social"]},
        {"days": "mon", "start": "09:00", "end": "09:00", "categories": ["social"]},
        {"days": "mon", "start": "09:00", "end": "10:00", "categories": []},
        {"days": "mon", "start": "09:00", "end": "10:00", "categories": ["nope"]},
        {"days": [], "start": "09:00", "end": "10:00", "categories": ["social"]},
    ],
)
def test_make_window_rejects_bad_input(kwargs) -> None:
    """Nothing the unattended loop cannot interpret may reach the DB."""

    with pytest.raises(ValueError):
        make_window("pc1", tz="UTC", **kwargs)


def test_make_window_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError):
        make_window("pc1", days="mon", start="09:00", end="10:00",
                    categories=["social"], tz="Mars/Olympus")


def test_default_timezone_falls_back_to_utc(monkeypatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    assert webfilter.default_timezone() == "UTC"
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert webfilter.default_timezone() == "Europe/Berlin"
    monkeypatch.setenv("TZ", "Not/AZone")
    assert webfilter.default_timezone() == "UTC"


def test_parse_days_accepts_the_shapes_callers_send() -> None:
    assert webfilter.parse_days("mon,wed") == (0, 2)
    assert webfilter.parse_days("daily") == (0, 1, 2, 3, 4, 5, 6)
    assert webfilter.parse_days(["Monday", "sun"]) == (0, 6)
    assert webfilter.parse_days([0, 6]) == (0, 6)


# --- schedule service + due-push computation ---------------------------------


@pytest.fixture
async def service(tmp_path):
    store = WebFilterStore(db_path=str(tmp_path / "svc.sqlite"))
    await store.connect()
    yield WebFilterService(store, _StubCache())
    await store.close()


async def test_service_build_apply_follows_the_schedule(service) -> None:
    await service.set_config("pc1", enabled=True, block_mode=True, categories=[])
    await service.add_domain("pc1", "chat.example", "block", None, "chat")
    await service.add_window(
        "pc1", days="daily", start="00:00", end="23:59",
        categories=["chat"], tz="UTC",
    )
    args = await service.build_apply("pc1")
    assert "chat.example" in args["domains"]
    # The payload the agent receives is unchanged in shape.
    assert set(args) == {"domains", "doh_policy", "list_hash"}


async def test_schedule_due_only_returns_changed_enabled_hosts(service) -> None:
    await service.set_config("pc1", enabled=True, block_mode=True, categories=[])
    await service.add_domain("pc1", "chat.example", "block", None, "chat")
    await service.add_window(
        "pc1", days="daily", start="00:00", end="23:59",
        categories=["chat"], tz="UTC",
    )
    due = await service.schedule_due()
    assert [d["agent_id"] for d in due] == ["pc1"]

    # Once applied, the same pass is a no-op (idempotent, nothing on the wire).
    await service.set_applied_state("pc1", due[0]["args"]["list_hash"], "now", True)
    assert await service.schedule_due() == []

    # Block mode off -> never pushed by the schedule.
    await service.set_config("pc1", block_mode=False)
    assert await service.schedule_due() == []


async def test_schedule_due_reverts_when_a_window_closes(service) -> None:
    """The revert is the same mechanism as the tightening, not a special case.

    A host still carrying the stricter list after its window has closed differs
    from what the schedule says *now*, so the next pass pushes the relaxed list
    back. Nothing needs to remember that a window was ever open.
    """

    await service.set_config("pc1", enabled=True, block_mode=True, categories=[])
    await service.add_domain("pc1", "chat.example", "block", None, "chat")
    # A window that is open only on Mondays 00:00-01:00 UTC...
    await service.add_window(
        "pc1", days="mon", start="00:00", end="01:00",
        categories=["chat"], tz="UTC",
    )
    monday = datetime(2026, 8, 17, 0, 30, tzinfo=timezone.utc)
    tuesday = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    strict = await service.build_apply("pc1", at=monday)
    assert "chat.example" in strict["domains"]
    await service.set_applied_state("pc1", strict["list_hash"], "monday", True)

    # ...so by Tuesday the host is carrying a list the schedule no longer wants.
    due = await service.schedule_due(at=tuesday)
    assert len(due) == 1
    assert "chat.example" not in due[0]["args"]["domains"]
    assert due[0]["args"]["list_hash"] != strict["list_hash"]


async def test_schedule_due_skips_a_host_with_no_window(service) -> None:
    """Authoring a window is the opt-in; a plain host is never touched."""

    await service.set_config("pc2", enabled=True, block_mode=True)
    assert await service.schedule_due() == []


async def test_schedule_due_reports_an_over_cap_host_without_pushing(
    service, monkeypatch
) -> None:
    monkeypatch.setattr(webfilter, "_HARD_CAP", 3)
    await service.set_config("pc1", enabled=True, block_mode=True)
    await service.add_window(
        "pc1", days="daily", start="00:00", end="23:59",
        categories=["social"], tz="UTC",
    )
    due = await service.schedule_due()
    assert len(due) == 1
    assert due[0]["args"] is None
    assert "over the agent's 3 cap" in due[0]["error"]


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
from kenny_server.main import build_app, webfilter_schedule_pass  # noqa: E402


class WebfilterMockAgent(MockAgent):
    """Mock agent that also replays the webfilter_* fixtures."""

    def __init__(self, *args, apply_disabled: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.apply_disabled = apply_disabled
        #: Args of the last ``webfilter_apply`` the server sent, so a test can
        #: assert on the payload that actually crossed the wire.
        self.last_apply_args: dict | None = None

    async def _handle_request(self, frame: dict) -> None:
        assert self.ws is not None
        tool = frame["tool"]
        fixtures = {
            "webfilter_apply": "response_webfilter_apply.json",
            "webfilter_clear": "response_webfilter_clear.json",
            "webfilter_status": "response_webfilter_status.json",
        }
        if tool in fixtures:
            if tool == "webfilter_apply":
                self.last_apply_args = frame.get("args")
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


@pytest.mark.asyncio
async def test_integration_webfilter_schedule(tmp_path, monkeypatch) -> None:
    """A window is authored over the API, observed, and enacted by the loop.

    Covers the whole server-side path: the schedule routes, the observable
    state an operator reads off a host, and ``webfilter_schedule_pass`` pushing
    the stricter list unattended — with the payload the agent receives still the
    unchanged flat ``{domains, doh_policy, list_hash}``.
    """

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    monkeypatch.setenv("KENNY_WEBFILTER_REFRESH_SECS", "0")
    monkeypatch.setenv("KENNY_WEBFILTER_SCHEDULE_SECS", "0")  # drive passes by hand
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "wf_sched.sqlite"))
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {app.state.operator_token}"}

    async with _Server(app, port):
        agent = WebfilterMockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        async with httpx.AsyncClient(headers=headers) as c:
            await c.put(
                f"{base}/api/agent/dev/webfilter/config",
                json={"enabled": True, "block_mode": True, "categories": ["adult"]},
            )
            # A domain that only bites while the "social" category is on.
            r = await c.post(
                f"{base}/api/agent/dev/webfilter/domains",
                json={"domain": "social.example", "action": "block",
                      "category": "social"},
            )
            assert r.status_code == 200
            assert r.json()["custom"][0]["category"] == "social"
            # An unknown category is refused rather than silently stored.
            r = await c.post(
                f"{base}/api/agent/dev/webfilter/domains",
                json={"domain": "x.example", "action": "block", "category": "nope"},
            )
            assert r.status_code == 400

            # Bad window input is refused with a message, not a 500.
            r = await c.post(
                f"{base}/api/agent/dev/webfilter/schedule",
                json={"days": "someday", "start": "21:00", "end": "07:00",
                      "categories": ["social"]},
            )
            assert r.status_code == 400

            # An always-open window, so the assertions below don't chase a clock.
            r = await c.post(
                f"{base}/api/agent/dev/webfilter/schedule",
                json={"days": "daily", "start": "00:00", "end": "23:59",
                      "categories": ["social"], "label": "homework",
                      "timezone": "UTC"},
            )
            assert r.status_code == 200
            window_id = r.json()["window"]["id"]

            # The operator can see the list is currently the stricter one.
            sched = (await c.get(f"{base}/api/agent/dev/webfilter/schedule")).json()
            assert sched["stricter"] is True
            assert sched["base_categories"] == ["adult"]
            assert sched["extra_categories"] == ["social"]
            assert sched["reverts_at"] is not None
            assert [w["label"] for w in sched["active_windows"]] == ["homework"]
            # ...and the same state is on the host overview.
            wf = (await c.get(f"{base}/api/agent/dev/webfilter")).json()
            assert wf["schedule"]["stricter"] is True
            assert wf["config"]["categories"] == ["adult"]
            assert any(cat["key"] == "social" for cat in wf["categories"])

            # The scheduled list differs from what is applied, so a pass pushes.
            counts = await webfilter_schedule_pass(
                app.state.webfilter, app.state.tunnel, app.state.call_log
            )
            assert counts == {"pushed": 1, "failed": 0, "skipped": 0}
            pushed = agent.last_apply_args
            assert set(pushed) == {"domains", "doh_policy", "list_hash"}
            assert "social.example" in pushed["domains"]

            # Re-running the pass is a no-op: nothing changed, nothing on the wire.
            agent.last_apply_args = None
            counts = await webfilter_schedule_pass(
                app.state.webfilter, app.state.tunnel, app.state.call_log
            )
            assert counts == {"pushed": 0, "failed": 0, "skipped": 0}
            assert agent.last_apply_args is None

            wf = (await c.get(f"{base}/api/agent/dev/webfilter")).json()
            assert wf["drift"] is False

            # Deleting the last window takes the host out of the schedule loop's
            # fleet entirely, so the loop stops touching it — deliberately: the
            # loop's licence to push unattended comes from an enabled window
            # existing. What the operator gets instead is the ordinary drift
            # signal every other config edit produces, and the ordinary Apply.
            r = await c.delete(
                f"{base}/api/agent/dev/webfilter/schedule/{window_id}"
            )
            assert r.status_code == 200 and r.json()["removed"] is True
            agent.last_apply_args = None
            counts = await webfilter_schedule_pass(
                app.state.webfilter, app.state.tunnel, app.state.call_log
            )
            assert counts == {"pushed": 0, "failed": 0, "skipped": 0}
            assert agent.last_apply_args is None

            wf = (await c.get(f"{base}/api/agent/dev/webfilter")).json()
            assert wf["schedule"]["stricter"] is False
            assert wf["drift"] is True  # "the applied list is stale — push"
            r = await c.post(f"{base}/api/agent/dev/webfilter/apply")
            assert r.status_code == 200
            assert "social.example" not in agent.last_apply_args["domains"]

        await agent.stop()


@pytest.mark.asyncio
async def test_integration_webfilter_over_cap_is_refused_server_side(
    tmp_path, monkeypatch
) -> None:
    """Over the agent's cap the server refuses; it never pushes a doomed list."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    monkeypatch.setenv("KENNY_WEBFILTER_REFRESH_SECS", "0")
    monkeypatch.setenv("KENNY_WEBFILTER_SCHEDULE_SECS", "0")
    monkeypatch.setattr(webfilter, "_HARD_CAP", 5)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "wf_cap.sqlite"))
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {app.state.operator_token}"}

    async with _Server(app, port):
        agent = WebfilterMockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        async with httpx.AsyncClient(headers=headers) as c:
            await c.put(
                f"{base}/api/agent/dev/webfilter/config",
                json={"enabled": True, "block_mode": True},
            )
            r = await c.post(f"{base}/api/agent/dev/webfilter/apply")
            assert r.status_code == 400
            body = r.json()
            assert body["error"] == "list_too_large"
            assert body["cap"] == 5 and body["count"] > 5
            assert agent.last_apply_args is None  # nothing reached the agent

            # The host is still *viewable*: the operator needs to see which
            # category to turn off, so the read reports the state, not an error.
            wf = (await c.get(f"{base}/api/agent/dev/webfilter")).json()
            assert wf["current_hash"] is None
            assert wf["oversize"]["cap"] == 5

            # ...and a scheduled host in the same state is skipped, not pushed.
            await app.state.webfilter.add_window(
                "dev", days="daily", start="00:00", end="23:59",
                categories=["social"], tz="UTC",
            )
            counts = await webfilter_schedule_pass(
                app.state.webfilter, app.state.tunnel, app.state.call_log
            )
            assert counts == {"pushed": 0, "failed": 0, "skipped": 1}
            assert agent.last_apply_args is None

        await agent.stop()


@pytest.mark.asyncio
async def test_integration_bypass_request_is_a_ticket(tmp_path, monkeypatch) -> None:
    """Ask to applied, with no parallel request table anywhere in the path.

    The request is a ``web_filter`` ticket; the decision is the ticket's own
    approval gate; granting it is the ordinary allow-entry + push. This asserts
    the join: the request surfaces against the host, an approved one turns into
    an ``allow`` entry, and the pushed list actually stops blocking the domain.
    """

    from kenny_server.tickets import KNOWN_CATEGORIES

    assert "web_filter" in KNOWN_CATEGORIES

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    monkeypatch.setenv("KENNY_WEBFILTER_REFRESH_SECS", "0")
    monkeypatch.setenv("KENNY_WEBFILTER_SCHEDULE_SECS", "0")
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "wf_req.sqlite"))
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {app.state.operator_token}"}

    async with _Server(app, port):
        agent = WebfilterMockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        # The child opens a bypass request. Nothing here is webfilter-specific:
        # it is a ticket, created through the ordinary ticket service.
        await app.state.tickets.create(
            title="Please unblock studyhelp.example",
            origin="user",
            category="web_filter",
            agent_id="dev",
            summary="I need studyhelp.example for homework",
        )

        async with httpx.AsyncClient(headers=headers) as c:
            await c.put(
                f"{base}/api/agent/dev/webfilter/config",
                json={"enabled": True, "block_mode": True},
            )
            await c.post(
                f"{base}/api/agent/dev/webfilter/domains",
                json={"domain": "studyhelp.example", "action": "block"},
            )

            # The request shows against the host, with what is being asked for.
            reqs = (await c.get(f"{base}/api/agent/dev/webfilter/requests")).json()
            assert len(reqs["requests"]) == 1
            entry = reqs["requests"][0]
            assert entry["ticket"]["category"] == "web_filter"
            assert "studyhelp.example" in entry["requested_domains"]

            # The operator grants it the ordinary way: an allow entry + a push.
            r = await c.post(
                f"{base}/api/agent/dev/webfilter/domains",
                json={"domain": "studyhelp.example", "action": "allow",
                      "note": f"ticket {entry['ticket']['number']}"},
            )
            assert r.status_code == 200
            r = await c.post(f"{base}/api/agent/dev/webfilter/apply")
            assert r.status_code == 200 and r.json()["ok"] is True
            assert "studyhelp.example" not in agent.last_apply_args["domains"]

        await agent.stop()

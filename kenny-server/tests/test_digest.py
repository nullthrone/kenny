"""Tests for the weekly digest (:mod:`kenny_server.digest` + scheduling)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.alerting import AlertEngine
from kenny_server.digest import build_digest
from kenny_server.notify import Notification
from kenny_server.store import AlertStateStore, EventStore, TelemetryStore

# A Wednesday.
NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeNotifier:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class FakeRegistry:
    def __init__(self, online: set[str] | None = None) -> None:
        self._online = online or set()

    def get(self, agent_id: str):
        class _A:
            online = True

        return _A() if agent_id in self._online else None


@pytest.fixture
async def stores(tmp_path):
    db = str(tmp_path / "kenny.sqlite")
    store = TelemetryStore(db)
    events = EventStore(db)
    state = AlertStateStore(db)
    await store.connect()
    await events.connect()
    await state.connect()
    yield store, events, state
    await store.close()
    await events.close()
    await state.close()


async def test_build_digest_renders_fleet_summary(stores) -> None:
    store, events, _ = stores
    snapshot = {
        "disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 96.0}]},
        "reboot_pending": {"status": "warn", "summary": "", "pending": True, "reasons": ["WU"]},
        "screen_time": {
            "status": "ok",
            "summary": "",
            "window_days": 7,
            "days": [{"date": "2026-06-30", "active_minutes": 120}, {"date": "2026-07-01", "active_minutes": 60}],
        },
    }
    await store.insert("kids-pc", NOW.isoformat(), snapshot, received_at=NOW.isoformat())
    await events.insert_alert(
        agent_id="kids-pc", message="x", level="crit", fields={"kind": "alert"}, at=NOW.isoformat()
    )
    await events.insert_alert(
        agent_id="kids-pc", message="y", level="warn", fields={"kind": "change"}, at=NOW.isoformat()
    )
    # An old alert outside the 7-day window is not counted.
    await events.insert_alert(
        agent_id="kids-pc", message="z", level="crit",
        fields={"kind": "alert"}, at=(NOW - timedelta(days=10)).isoformat(),
    )

    digest = await build_digest(store, events, FakeRegistry({"kids-pc"}), now=NOW)
    body = digest.body
    assert "2026-07-01" in digest.title
    assert body.startswith("1 host · 1 online · 1 crit · 0 warn · 0 ok")  # disk 96% => crit
    assert "This week: 1 alert (1 crit) · 1 change" in body
    assert "1 reboot pending" in body
    assert "Screen time (7d): kids-pc 3.0h" in body


async def test_build_digest_empty_fleet(stores) -> None:
    store, events, _ = stores
    digest = await build_digest(store, events, FakeRegistry(), now=NOW, base_url="https://k.example")
    assert digest.body == digest.markdown == "No agents have reported telemetry yet."


async def test_build_digest_tolerates_malformed_list_entries(stores) -> None:
    """``win_update.recent`` and ``screen_time.days`` are unvalidated agent-reported
    lists (``Section`` allows extra fields with no shape check) -- a compromised or
    buggy agent putting non-dict entries there must not crash the digest for the
    whole fleet (it previously did: AttributeError on `.get()` of a str/int entry).
    """

    store, events, _ = stores
    snapshot = {
        "win_update": {"status": "warn", "summary": "", "recent": ["not-a-dict", 42, None]},
        "screen_time": {"status": "ok", "summary": "", "days": ["also-not-a-dict", 7]},
    }
    await store.insert("kids-pc", NOW.isoformat(), snapshot, received_at=NOW.isoformat())

    digest = await build_digest(store, events, FakeRegistry({"kids-pc"}), now=NOW)
    assert "2026-07-01" in digest.title
    assert digest.body  # did not raise


def make_engine(stores, notifier, **kwargs) -> AlertEngine:
    store, events, state = stores
    return AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=FakeRegistry(),
        notifiers=[notifier],
        digest_day="mon",
        digest_hour=8,
        **kwargs,
    )


async def test_digest_schedule_first_run_baselines_without_sending(stores) -> None:
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    assert await engine.maybe_send_digest(NOW) is False
    assert notifier.sent == []
    # Still before the next Monday-08:00 slot: nothing.
    assert await engine.maybe_send_digest(NOW + timedelta(days=1)) is False

    # After the next Monday 08:00 (2026-07-06) the digest goes out once.
    monday = datetime(2026, 7, 6, 8, 30, tzinfo=timezone.utc)
    assert await engine.maybe_send_digest(monday) is True
    assert len(notifier.sent) == 1
    assert notifier.sent[0].kind == "digest"
    assert notifier.sent[0].priority == "low"
    # ...and not a second time in the same week.
    assert await engine.maybe_send_digest(monday + timedelta(hours=2)) is False


async def test_digest_no_double_send_after_restart(stores) -> None:
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    await engine.maybe_send_digest(NOW)
    monday = datetime(2026, 7, 6, 8, 30, tzinfo=timezone.utc)
    assert await engine.maybe_send_digest(monday) is True

    # A fresh engine over the same persisted state does not re-send.
    notifier2 = FakeNotifier()
    engine2 = make_engine(stores, notifier2)
    assert await engine2.maybe_send_digest(monday + timedelta(minutes=5)) is False
    assert notifier2.sent == []


async def test_digest_disabled_or_channelless_is_silent(stores) -> None:
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, digest_enabled=False)
    monday = datetime(2026, 7, 6, 8, 30, tzinfo=timezone.utc)
    assert await engine.maybe_send_digest(monday) is False

    store, events, state = stores
    engine_no_channel = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=FakeRegistry(),
        notifiers=[],
    )
    assert await engine_no_channel.maybe_send_digest(monday) is False


async def test_digest_lists_posture_once_and_names_the_finding(stores) -> None:
    store, events, _ = stores
    snapshot = {
        "disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 96.0}]},
        "encryption": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "protection_status": 0}]},
        "listening_ports": {"status": "ok", "summary": "", "ports": [
            {"proto": "tcp", "port": 3389, "address": "0.0.0.0", "pid": 1, "process": "svchost"}]},
        "win_update": {"status": "ok", "summary": "", "recent": [
            {"kb": "KB1", "title": "x", "result": "failed", "installed_at": "2026-07-01T01:00:00Z"},
            {"kb": "KB1", "title": "x", "result": "failed", "installed_at": "2026-06-30T01:00:00Z"},
            {"kb": "KB1", "title": "x", "result": "failed", "installed_at": "2026-06-29T01:00:00Z"},
        ]},
    }
    await store.insert("kids-pc", NOW.isoformat(), snapshot, received_at=NOW.isoformat())
    body = (await build_digest(store, events, FakeRegistry({"kids-pc"}), now=NOW)).body
    # The overview names the sections; the reasons are on the host page.
    assert "\U0001F534 kids-pc: disk, win_update" in body
    assert "96%" not in body and "KB1" not in body
    # Posture is listed once, as a count per host.
    assert "Posture: kids-pc (2)" in body
    # Distinct KBs, not rows.
    assert "1 failed update on 1 host" in body


async def test_digest_aggregates_disk_forecasts_per_host(stores) -> None:
    """Several filling volumes on one host (e.g. Docker overlay mounts sharing the
    root filesystem) are one entry with the soonest estimate, not one per mount."""

    store, events, _ = stores
    for i in range(10):
        at = (NOW - timedelta(days=9 - i)).isoformat()
        volumes = [
            {"mount": "/", "percent_used": 60 + 2 * i},
            {"mount": "/var/lib/docker/overlay/abc", "percent_used": 60 + 2 * i},
            {"mount": "/data", "percent_used": 30 + 5 * i},
        ]
        await store.insert(
            "nas", at, {"disk": {"status": "ok", "summary": "", "volumes": volumes}}, received_at=at
        )
    body = (await build_digest(store, events, FakeRegistry({"nas"}), now=NOW)).body
    assert "disk filling: nas (~5d)" in body
    assert body.count("nas (~") == 1
    assert "overlay" not in body


async def test_digest_links_hosts_only_when_the_dashboard_url_is_known(stores) -> None:
    store, events, _ = stores
    snapshot = {"disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 96.0}]}}
    await store.insert("kids_pc", NOW.isoformat(), snapshot, received_at=NOW.isoformat())

    linked = await build_digest(
        store, events, FakeRegistry({"kids_pc"}), now=NOW, base_url="https://kenny.example/"
    )
    # Markdown: the host name links to its worst section, escaped for Discord.
    assert "[kids\\_pc](https://kenny.example/#/fleet/kids_pc?section=disk): disk" in linked.markdown
    assert "[0 alerts (0 crit)](https://kenny.example/#/log)" in linked.markdown
    assert linked.markdown.endswith("[Open kenny](https://kenny.example/#/fleet)")
    # Plain text: no inline links, one dashboard URL at the end.
    assert "kids_pc: disk" in linked.body
    assert linked.body.endswith("Details: https://kenny.example/#/fleet")
    assert linked.body.count("https://") == 1

    unlinked = await build_digest(store, events, FakeRegistry({"kids_pc"}), now=NOW)
    assert "http" not in unlinked.body and "http" not in unlinked.markdown


async def test_digest_caps_the_host_list(stores) -> None:
    store, events, _ = stores
    snapshot = {"disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 85.0}]}}
    for n in range(11):
        await store.insert(f"pc{n:02d}", NOW.isoformat(), snapshot, received_at=NOW.isoformat())
    body = (await build_digest(store, events, FakeRegistry(), now=NOW)).body
    assert "pc07: disk" in body and "pc08" not in body
    assert "+3 more hosts" in body


async def test_digest_reaches_discord_with_host_links(stores, monkeypatch) -> None:
    """Joined seam: scheduler -> Notification -> Discord embed carries the links."""

    import json

    import httpx

    from kenny_server.notify import DiscordNotifier

    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "m-1"})

    discord = DiscordNotifier(
        "https://discord.example/webhook",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    store, events, _ = stores
    snapshot = {"disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 96.0}]}}
    await store.insert("kids-pc", NOW.isoformat(), snapshot, received_at=NOW.isoformat())

    engine = make_engine(stores, discord)
    await engine.maybe_send_digest(NOW)
    assert await engine.maybe_send_digest(datetime(2026, 7, 6, 8, 30, tzinfo=timezone.utc)) is True

    embed = json.loads(captured[-1].content)["embeds"][0]
    assert embed["title"] == "kenny weekly digest - 2026-07-06"
    assert "[kids-pc](https://kenny.example/#/fleet/kids-pc?section=disk): disk" in embed["description"]
    assert embed["description"].endswith("[Open kenny](https://kenny.example/#/fleet)")

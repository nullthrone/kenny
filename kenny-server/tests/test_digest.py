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

    title, body = await build_digest(store, events, FakeRegistry({"kids-pc"}), now=NOW)
    assert "2026-07-01" in title
    assert "1 host(s), 1 online" in body
    assert "0 ok / 0 warn / 1 crit" in body  # disk 96% => crit
    assert "Alerts (7d): 1 (1 crit), changes: 1." in body
    assert "1 reboot(s) pending" in body
    assert "kids-pc 3.0h" in body


async def test_build_digest_empty_fleet(stores) -> None:
    store, events, _ = stores
    _, body = await build_digest(store, events, FakeRegistry(), now=NOW)
    assert body == "No agents have reported telemetry yet."


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

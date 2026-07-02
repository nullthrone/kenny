"""Tests for :class:`kenny_server.alerting.AlertEngine`.

Drives ``evaluate_once`` with a frozen clock, synthetic snapshots and a fake
notifier: transition semantics (first crit fires once, recovery, escalation
through cooldown), flap suppression, offline detection, restart persistence,
and the ``kind='alert'`` audit trail in the event store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.alerting import AlertEngine
from kenny_server.notify import Notification
from kenny_server.store import AlertStateStore, EventStore, TelemetryStore

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeNotifier:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class _Agent:
    def __init__(self, online: bool) -> None:
        self.online = online


class FakeRegistry:
    """Only ``get`` is used by the engine; agents are offline unless listed."""

    def __init__(self, online: set[str] | None = None) -> None:
        self._online = online or set()

    def get(self, agent_id: str) -> _Agent | None:
        return _Agent(True) if agent_id in self._online else None


def snapshot(disk_pct: float) -> dict:
    return {
        "disk": {
            "status": "ok",
            "summary": f"C: {disk_pct:.0f}% full",
            "volumes": [{"mount": "C:", "percent_used": disk_pct}],
        }
    }


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


def make_engine(stores, notifier: FakeNotifier, **kwargs) -> AlertEngine:
    store, events, state = stores
    return AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=kwargs.pop("registry", FakeRegistry({"pc1"})),
        notifiers=[notifier],
        **kwargs,
    )


async def insert(store: TelemetryStore, snap: dict, at: datetime, agent_id: str = "pc1") -> None:
    await store.insert(
        agent_id, at.isoformat(), snap, received_at=at.isoformat()
    )


async def test_first_seen_crit_fires_once(stores) -> None:
    store, events, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))

    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].kind == "alert"
    assert sent[0].priority == "high"
    assert "disk: ok -> crit" in sent[0].body
    assert sent[0].agent_id == "pc1"

    # Unchanged crit on the next pass: silent (transitions only, no reminders).
    sent = await engine.evaluate_once(NOW + timedelta(minutes=5))
    assert sent == []
    assert len(notifier.sent) == 1

    # The alert landed in the events table as the audit trail.
    rows = await events.query(kind="alert")
    assert len(rows) == 1
    assert rows[0]["level"] == "crit"
    assert rows[0]["agent_id"] == "pc1"
    assert "disk" in rows[0]["message"]


async def test_recovery_fires_after_notified_alert(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)

    await insert(store, snapshot(50.0), NOW + timedelta(minutes=10))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert len(sent) == 1
    assert sent[0].kind == "recovery"
    assert "disk: crit -> ok" in sent[0].body


async def test_flap_is_suppressed_by_cooldown(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, cooldown_s=3600)

    # warn fires, recovery fires, then the re-warn within the cooldown is silent.
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=1))
    assert len(await engine.evaluate_once(NOW)) == 1
    await insert(store, snapshot(50.0), NOW + timedelta(minutes=5))
    assert len(await engine.evaluate_once(NOW + timedelta(minutes=6))) == 1
    await insert(store, snapshot(85.0), NOW + timedelta(minutes=10))
    assert await engine.evaluate_once(NOW + timedelta(minutes=11)) == []

    # ...and after the cooldown expires the warn fires again.
    await insert(store, snapshot(50.0), NOW + timedelta(minutes=20))
    assert await engine.evaluate_once(NOW + timedelta(minutes=21)) == []  # silent recovery
    await insert(store, snapshot(85.0), NOW + timedelta(hours=2))
    assert len(await engine.evaluate_once(NOW + timedelta(hours=2, minutes=1))) == 1


async def test_escalation_to_crit_bypasses_cooldown(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, cooldown_s=3600)
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=1))
    assert len(await engine.evaluate_once(NOW)) == 1  # warn

    await insert(store, snapshot(96.0), NOW + timedelta(minutes=10))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert len(sent) == 1
    assert sent[0].priority == "high"
    assert "disk: warn -> crit" in sent[0].body


async def test_offline_alert_and_recovery(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, registry=FakeRegistry(set()), offline_after_s=2700)
    await insert(store, snapshot(50.0), NOW - timedelta(hours=3))

    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].kind == "alert"
    assert sent[0].priority == "high"
    assert "offline" in sent[0].title

    # Still offline on the next pass: silent.
    assert await engine.evaluate_once(NOW + timedelta(minutes=5)) == []

    # A fresh push brings it back online: recovery.
    await insert(store, snapshot(50.0), NOW + timedelta(minutes=10))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert len(sent) == 1
    assert sent[0].kind == "recovery"
    assert "back online" in sent[0].title


async def test_health_is_skipped_while_offline(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, registry=FakeRegistry(set()))
    # Stale crit snapshot: only the offline alert fires, not the health alert.
    await insert(store, snapshot(96.0), NOW - timedelta(hours=3))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert "offline" in sent[0].title


def autostart_snapshot(names: list[str]) -> dict:
    return {
        "autostart": {
            "status": "ok",
            "summary": f"{len(names)} entries",
            "entries": [
                {"name": n, "location": "HKCU\\Run", "command": f"{n.lower()}.exe"}
                for n in names
            ],
        }
    }


async def test_change_notification_on_new_autostart_entry(stores) -> None:
    store, events, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    await insert(store, autostart_snapshot(["OneDrive"]), NOW - timedelta(minutes=15))
    # First sighting only sets the cursor: no diff, no notification.
    assert await engine.evaluate_once(NOW - timedelta(minutes=10)) == []

    await insert(store, autostart_snapshot(["OneDrive", "Sketchy"]), NOW - timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].kind == "change"
    assert "autostart: added Sketchy" in sent[0].body
    assert sent[0].priority == "default"

    # Another autostart change within the cooldown stays silent...
    await insert(store, autostart_snapshot(["OneDrive"]), NOW + timedelta(minutes=5))
    assert await engine.evaluate_once(NOW + timedelta(minutes=6)) == []
    # ...and the same tick does not re-process the same snapshot.
    assert await engine.evaluate_once(NOW + timedelta(minutes=7)) == []

    rows = await events.query(kind="alert")
    assert any("Sketchy" in r["message"] for r in rows)


async def test_local_accounts_change_is_high_priority(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    def accounts(is_admin: bool) -> dict:
        return {
            "local_accounts": {
                "status": "ok",
                "summary": "",
                "accounts": [{"name": "kid", "enabled": True, "is_admin": is_admin}],
            }
        }

    await insert(store, accounts(False), NOW - timedelta(minutes=15))
    await engine.evaluate_once(NOW - timedelta(minutes=10))
    await insert(store, accounts(True), NOW - timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].priority == "high"
    assert "is_admin: False -> True" in sent[0].body


async def test_disk_forecast_alert_with_daily_cooldown(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    # Six days rising +2 %/day up to 78 % (below the 80 % warn rule):
    # ~11 days until full, under the 14-day forecast threshold.
    base = NOW - timedelta(days=5)
    for i in range(6):
        at = base + timedelta(days=i)
        await insert(store, snapshot(68.0 + 2.0 * i), at)

    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert "disk filling up" in sent[0].title
    assert "C:" in sent[0].body

    # A new snapshot within the 24 h forecast cooldown stays silent.
    await insert(store, snapshot(78.2), NOW + timedelta(hours=2))
    assert await engine.evaluate_once(NOW + timedelta(hours=3)) == []

    # After the cooldown a new snapshot re-fires the (still true) forecast.
    await insert(store, snapshot(80.0 + 0.4), NOW + timedelta(days=2))
    sent = await engine.evaluate_once(NOW + timedelta(days=2, hours=1))
    assert any("disk filling up" in n.title for n in sent)


async def test_restart_does_not_refire_persisted_state(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    assert len(await engine.evaluate_once(NOW)) == 1

    # A fresh engine over the same persisted alert_state stays silent.
    notifier2 = FakeNotifier()
    engine2 = make_engine(stores, notifier2)
    assert await engine2.evaluate_once(NOW + timedelta(minutes=5)) == []
    assert notifier2.sent == []

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


async def test_producers_carry_their_event_discriminator(stores) -> None:
    """Every notification kind alerting.py can emit carries an event_type
    -- the label the ticket-rule matcher keys on."""

    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, registry=FakeRegistry(set()))
    await insert(store, snapshot(96.0), NOW - timedelta(hours=3))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].event_type == "offline"

    engine2 = make_engine(stores, notifier, registry=FakeRegistry({"pc1"}))
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    sent = await engine2.evaluate_once(NOW)
    # Coming back online also fires an offline-recovery in the same pass (the
    # fixture reused the same alert_state as the block above); pick out the
    # health notification specifically.
    health = [n for n in sent if n.event_type == "health"]
    assert len(health) == 1
    assert health[0].sections == {"disk": "crit"}


# -- auto-ticket rules (ticket_rules.py) -------------------------------------


class FakeTicketRules:
    """A minimal ``TicketRuleList``-shaped mirror for tests: ``mapping()``
    returns whatever dict was set, or raises if constructed with a callable."""

    def __init__(self, rules=None, *, raise_on_mapping=False):
        self._rules = rules or {}
        self._raise = raise_on_mapping

    def mapping(self):
        if self._raise:
            raise RuntimeError("boom")
        return self._rules


async def test_engine_without_ticket_rules_opens_a_ticket_for_every_alert(stores) -> None:
    """No mirror wired: behaves exactly like today (kind == 'alert' opens)."""

    store, _, _ = stores
    notifier = FakeNotifier()
    opened: list[Notification] = []

    async def open_ticket(note: Notification) -> None:
        opened.append(note)

    engine = make_engine(stores, notifier, open_ticket=open_ticket)
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    assert len(opened) == 1
    assert opened[0].event_type == "health"


async def test_engine_never_rule_stops_the_ticket_but_not_the_notification(stores) -> None:
    store, events, _ = stores
    notifier = FakeNotifier()
    opened: list[Notification] = []

    async def open_ticket(note: Notification) -> None:
        opened.append(note)

    from kenny_server.ticket_rules import rule_id

    ticket_rules = FakeTicketRules(
        {("", "health", "disk"): {"id": rule_id("", "health", "disk"), "decision": "never"}}
    )
    engine = make_engine(stores, notifier, open_ticket=open_ticket, ticket_rules=ticket_rules)
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    sent = await engine.evaluate_once(NOW)

    assert len(sent) == 1  # delivery is unaffected
    assert len(notifier.sent) == 1
    assert opened == []  # but no ticket
    rows = await events.query(kind="alert")
    assert len(rows) == 1  # the audit trail row is still written


async def test_engine_open_all_rule_makes_a_change_notification_ticketable(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    opened: list[Notification] = []

    async def open_ticket(note: Notification) -> None:
        opened.append(note)

    from kenny_server.ticket_rules import rule_id

    ticket_rules = FakeTicketRules(
        {
            ("", "change", "autostart"): {
                "id": rule_id("", "change", "autostart"), "decision": "open_all",
            }
        }
    )
    engine = make_engine(stores, notifier, open_ticket=open_ticket, ticket_rules=ticket_rules)
    await insert(store, autostart_snapshot(["OneDrive"]), NOW - timedelta(minutes=15))
    await engine.evaluate_once(NOW - timedelta(minutes=10))
    await insert(store, autostart_snapshot(["OneDrive", "Sketchy"]), NOW - timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].kind == "change"
    assert len(opened) == 1  # promoted by the rule, where today it never would


async def test_a_raising_ticket_rules_lookup_never_breaks_delivery(stores) -> None:
    """ADR-0027: alerting stays best-effort even when the rule mirror itself
    misbehaves -- the notification is still delivered and recorded."""

    store, events, _ = stores
    notifier = FakeNotifier()
    opened: list[Notification] = []

    async def open_ticket(note: Notification) -> None:
        opened.append(note)

    ticket_rules = FakeTicketRules(raise_on_mapping=True)
    engine = make_engine(stores, notifier, open_ticket=open_ticket, ticket_rules=ticket_rules)
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    sent = await engine.evaluate_once(NOW)

    assert len(sent) == 1
    assert len(notifier.sent) == 1
    assert opened == []
    rows = await events.query(kind="alert")
    assert len(rows) == 1


async def test_recovery_never_opens_a_ticket_even_with_open_all_health_rule(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    opened: list[Notification] = []

    async def open_ticket(note: Notification) -> None:
        opened.append(note)

    from kenny_server.ticket_rules import rule_id

    ticket_rules = FakeTicketRules(
        {("", "health", ""): {"id": rule_id("", "health", ""), "decision": "open_all"}}
    )
    engine = make_engine(stores, notifier, open_ticket=open_ticket, ticket_rules=ticket_rules)
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    assert len(opened) == 1  # the alert itself opened via the rule
    opened.clear()

    await insert(store, snapshot(50.0), NOW + timedelta(minutes=10))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert len(sent) == 1
    assert sent[0].kind == "recovery"
    assert opened == []  # never, no matter what the rule says


# -- seam: every event_type/section the real engine emits is nameable -------


async def test_engine_emitted_vocabulary_matches_ticket_rules(stores) -> None:
    """Drive every producer through the real engine and assert every emitted
    (event_type, section) is inside ticket_rules' validated vocabulary --
    fails when a producer is added unlabelled or a section is renamed on one
    side only (kenny-server/CLAUDE.md's joined-seam-test rule)."""

    from kenny_server import ticket_rules as tr

    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    seen_event_types: set[str] = set()
    seen_sections: dict[str, set[str]] = {}

    def record(notes):
        for n in notes:
            seen_event_types.add(n.event_type)
            for section in n.sections:
                seen_sections.setdefault(n.event_type, set()).add(section)

    # health escalation
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    record(await engine.evaluate_once(NOW))
    # offline
    engine_offline = make_engine(stores, FakeNotifier(), registry=FakeRegistry(set()))
    await insert(store, snapshot(50.0), NOW - timedelta(hours=3), agent_id="pc2")
    record(await engine_offline.evaluate_once(NOW))
    # change
    await insert(store, autostart_snapshot(["A"]), NOW - timedelta(minutes=15), agent_id="pc3")
    record(await engine.evaluate_once(NOW - timedelta(minutes=10)))
    await insert(store, autostart_snapshot(["A", "B"]), NOW - timedelta(minutes=5), agent_id="pc3")
    record(await engine.evaluate_once(NOW))
    # disk_forecast
    base = NOW - timedelta(days=5)
    for i in range(6):
        await insert(store, snapshot(68.0 + 2.0 * i), base + timedelta(days=i), agent_id="pc4")
    record(await engine.evaluate_once(NOW))

    assert seen_event_types  # the scenario battery actually produced something
    assert seen_event_types <= (set(tr.EVENT_TYPES) | {"digest"})
    # every EVENT_TYPES member is exercised by this battery
    assert set(tr.EVENT_TYPES) <= seen_event_types
    for event_type, sections in seen_sections.items():
        known = tr.KNOWN_SECTIONS.get(event_type, frozenset())
        unknown = sections - known
        assert not unknown, f"{event_type}: sections {unknown} missing from KNOWN_SECTIONS"


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


# -- _maybe_prune: live retention (ADR-0051) -----------------------------------
#
# ``_prunables`` entries are (store, settings_key) pairs. A key resolves fresh
# from ``settings`` on every call -- never frozen at store construction -- and
# a *decrease* forces an immediate pass instead of waiting up to _PRUNE_EVERY,
# so tightening retention from the dashboard is visible within one alert
# cycle. A ``None`` key means "no live setting yet"; the store keeps whatever
# retention it was constructed with.


class _FakeSettings:
    """Minimal live-settings stand-in: just enough for AlertEngine._cfg."""

    def __init__(self, initial: dict) -> None:
        self._values = dict(initial)

    def get(self, key: str):
        return self._values.get(key)


class _SpyPrunable:
    """Records every ``prune()`` call instead of touching a real DB."""

    def __init__(self) -> None:
        self.calls: list[int | None] = []

    async def prune(self, *, retention_days: int | None = None) -> int:
        self.calls.append(retention_days)
        return 0


async def test_maybe_prune_resolves_retention_key_fresh_each_pass(stores) -> None:
    notifier = FakeNotifier()
    spy = _SpyPrunable()
    settings = _FakeSettings({"KENNY_TELEMETRY_RETENTION_DAYS": 30})
    engine = make_engine(
        stores, notifier, settings=settings, prunables=[(spy, "KENNY_TELEMETRY_RETENTION_DAYS")]
    )

    await engine._maybe_prune(NOW)
    assert spy.calls == [30]

    # A change between passes must be picked up -- nothing frozen at wiring time.
    settings._values["KENNY_TELEMETRY_RETENTION_DAYS"] = 14
    await engine._maybe_prune(NOW + timedelta(hours=25))  # past _PRUNE_EVERY
    assert spy.calls == [30, 14]


async def test_maybe_prune_ignores_unkeyed_prunables(stores) -> None:
    """A (store, None) pair keeps pruning on its own default -- no kwarg passed."""

    notifier = FakeNotifier()
    spy = _SpyPrunable()
    engine = make_engine(stores, notifier, prunables=[(spy, None)])

    await engine._maybe_prune(NOW)
    assert spy.calls == [None]


async def test_maybe_prune_forces_an_immediate_pass_when_retention_shrinks(stores) -> None:
    notifier = FakeNotifier()
    spy = _SpyPrunable()
    settings = _FakeSettings({"KENNY_TELEMETRY_RETENTION_DAYS": 30})
    engine = make_engine(
        stores, notifier, settings=settings, prunables=[(spy, "KENNY_TELEMETRY_RETENTION_DAYS")]
    )

    await engine._maybe_prune(NOW)
    assert spy.calls == [30]

    # Well within _PRUNE_EVERY (24h): a normal pass would be a no-op here...
    settings._values["KENNY_TELEMETRY_RETENTION_DAYS"] = 7
    await engine._maybe_prune(NOW + timedelta(minutes=1))
    # ...but the decrease forces one anyway.
    assert spy.calls == [30, 7]


async def test_maybe_prune_does_not_force_when_retention_grows(stores) -> None:
    notifier = FakeNotifier()
    spy = _SpyPrunable()
    settings = _FakeSettings({"KENNY_TELEMETRY_RETENTION_DAYS": 7})
    engine = make_engine(
        stores, notifier, settings=settings, prunables=[(spy, "KENNY_TELEMETRY_RETENTION_DAYS")]
    )

    await engine._maybe_prune(NOW)
    assert spy.calls == [7]

    # Loosening retention has nothing extra to delete -- must not force a pass.
    settings._values["KENNY_TELEMETRY_RETENTION_DAYS"] = 30
    await engine._maybe_prune(NOW + timedelta(minutes=1))
    assert spy.calls == [7]

    # The regular cadence still applies once due.
    await engine._maybe_prune(NOW + timedelta(hours=25))
    assert spy.calls == [7, 30]

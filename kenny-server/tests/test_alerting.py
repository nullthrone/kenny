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
from kenny_server.notify import Notification, Notifier
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
    # The body is the finding, not the transition (ADR-0058).
    assert sent[0].body == "[CRIT] disk: C: 96% full (>=95%)"
    assert sent[0].title == "pc1: disk: C: 96% full (>=95%)"
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
    assert sent[0].body == "[RESOLVED] disk: C: 50% full"


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
    assert sent[0].body == "[CRIT] disk: C: 96% full (>=95%)"


async def test_offline_notifies_no_one(stores) -> None:
    """A switched-off PC is not an incident: no alert, no back-online."""

    store, events, state = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, registry=FakeRegistry(set()), offline_after_s=2700)
    await insert(store, snapshot(50.0), NOW - timedelta(hours=3))

    assert await engine.evaluate_once(NOW) == []
    # The state is still tracked -- it is what gates health evaluation.
    assert (await state.get("pc1", "offline"))["status"] == "offline"

    await insert(store, snapshot(50.0), NOW + timedelta(minutes=10))
    assert await engine.evaluate_once(NOW + timedelta(minutes=11)) == []
    assert (await state.get("pc1", "offline"))["status"] == "online"
    assert notifier.sent == []
    assert await events.query(kind="alert") == []


async def test_a_missing_host_goes_to_the_daily_summary_and_back_quietly(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    opened: list[Notification] = []
    closed: list[Notification] = []

    async def _open(note: Notification) -> str:
        opened.append(note)
        return "T-1"

    async def _close(note: Notification) -> str:
        closed.append(note)
        return "T-1"

    engine = make_engine(
        stores, notifier, registry=FakeRegistry(set()), open_ticket=_open, close_ticket=_close
    )
    await insert(store, snapshot(50.0), NOW - timedelta(days=8))

    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].kind == "alert" and sent[0].event_type == "offline"
    assert sent[0].route == "daily"
    assert "8 days" in sent[0].title
    assert notifier.sent == []  # held for the daily summary...
    assert len(opened) == 1  # ...but it is work, so it is ticketed

    # Once per episode.
    assert await engine.evaluate_once(NOW + timedelta(hours=1)) == []

    await insert(store, snapshot(50.0), NOW + timedelta(hours=2))
    sent = await engine.evaluate_once(NOW + timedelta(hours=2, minutes=1))
    assert [(n.kind, n.route) for n in sent] == [("recovery", "record")]
    assert notifier.sent == []
    assert len(closed) == 1


async def test_an_offline_episode_announced_before_the_upgrade_still_closes(stores) -> None:
    """Its ticket was opened by the old offline alert; the return closes it quietly."""

    store, _, state = stores
    notifier = FakeNotifier()
    closed: list[Notification] = []

    async def _close(note: Notification) -> str:
        closed.append(note)
        return "T-1"

    engine = make_engine(stores, notifier, close_ticket=_close)
    went = NOW - timedelta(hours=2)
    await state.upsert("pc1", "offline", status="offline", since=went.isoformat(),
                       last_notified_at=went.isoformat())
    await insert(store, snapshot(50.0), NOW - timedelta(minutes=1))
    sent = await engine.evaluate_once(NOW)
    assert [(n.kind, n.event_type, n.route) for n in sent] == [("recovery", "offline", "record")]
    assert notifier.sent == []
    assert len(closed) == 1


async def test_a_host_offline_for_less_than_the_missing_window_is_not_missing(stores) -> None:
    store, _, _ = stores
    engine = make_engine(stores, FakeNotifier(), registry=FakeRegistry(set()), missing_after_days=7)
    await insert(store, snapshot(50.0), NOW - timedelta(days=6))
    assert await engine.evaluate_once(NOW) == []


async def test_health_is_skipped_while_offline(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, registry=FakeRegistry(set()))
    # A stale crit snapshot must not alarm: it describes a machine that is off.
    await insert(store, snapshot(96.0), NOW - timedelta(hours=3))
    assert await engine.evaluate_once(NOW) == []
    assert notifier.sent == []


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


async def test_disk_forecast_fires_once_per_episode_into_the_daily_summary(stores) -> None:
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
    assert sent[0].route == "daily"
    assert notifier.sent == []

    # Still true two days later: not news again.
    await insert(store, snapshot(78.4), NOW + timedelta(days=2))
    sent = await engine.evaluate_once(NOW + timedelta(days=2, hours=1))
    assert not any(n.event_type == "disk_forecast" for n in sent)


async def test_producers_carry_their_event_discriminator(stores) -> None:
    """Every notification kind alerting.py can emit carries an event_type
    -- the label the ticket-rule matcher keys on."""

    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, registry=FakeRegistry(set()))
    await insert(store, snapshot(96.0), NOW - timedelta(days=8))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert sent[0].event_type == "offline"

    engine2 = make_engine(stores, notifier, registry=FakeRegistry({"pc1"}))
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    sent = await engine2.evaluate_once(NOW)
    # Coming back also resolves the missing episode in the same pass (the
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
    # offline (a missing host)
    engine_offline = make_engine(stores, FakeNotifier(), registry=FakeRegistry(set()))
    await insert(store, snapshot(50.0), NOW - timedelta(days=8), agent_id="pc2")
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


# -- delivery channels are resolved per dispatch (ADR-0054) --------------------
#
# The engine holds a provider, not a list, so adding/changing/clearing a channel
# in the dashboard reaches the next alert without restarting the server. These
# drive a single long-lived engine across a configuration change -- rebuilding
# it would prove nothing, since that is exactly what a restart does.


class _RecordingNotifier:
    """A channel that records what it was handed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class _ExplodingNotifier:
    """A misconfigured channel whose send raises instead of swallowing."""

    name = "boom"

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, notification: Notification) -> None:
        self.attempts += 1
        raise RuntimeError("this channel is misconfigured")


async def test_a_channel_configured_later_delivers_without_a_restart(stores) -> None:
    store, events, state = stores
    channels: list[Notifier] = []
    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=FakeRegistry({"pc1"}),
        notifier_provider=lambda: channels,
    )

    # Zero channels is legitimate: the pass still evaluates and records history.
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert len(await events.query(kind="alert")) == 1

    # The operator adds a channel. No new engine, no new app, no restart.
    late = _RecordingNotifier("late")
    channels.append(late)

    await insert(store, snapshot(50.0), NOW + timedelta(minutes=1))
    recovery = await engine.evaluate_once(NOW + timedelta(minutes=2))
    assert [n.kind for n in recovery] == ["recovery"]
    assert [n.kind for n in late.sent] == ["recovery"]


async def test_a_channel_cleared_later_stops_delivering(stores) -> None:
    store, events, state = stores
    channel = _RecordingNotifier("configured")
    channels: list[Notifier] = [channel]
    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=FakeRegistry({"pc1"}),
        notifier_provider=lambda: channels,
    )

    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    assert len(channel.sent) == 1

    channels.clear()
    await insert(store, snapshot(50.0), NOW + timedelta(minutes=1))
    assert len(await engine.evaluate_once(NOW + timedelta(minutes=2))) == 1
    assert len(channel.sent) == 1  # the recovery went nowhere
    assert len(await events.query(kind="alert")) == 2  # but is still on record


async def test_a_channel_whose_send_raises_never_costs_the_others(stores) -> None:
    """One dead channel must not stall alerting for the rest."""

    store, events, state = stores
    first, last = _RecordingNotifier("first"), _RecordingNotifier("last")
    boom = _ExplodingNotifier()
    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=FakeRegistry({"pc1"}),
        # The exploding channel sits *between* two healthy ones, so a test that
        # only proved "delivery continued" by ordering would not pass.
        notifier_provider=lambda: [first, boom, last],
    )

    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    sent = await engine.evaluate_once(NOW)

    assert len(sent) == 1
    assert boom.attempts == 1
    assert len(first.sent) == 1
    assert len(last.sent) == 1
    assert len(await events.query(kind="alert")) == 1


async def test_a_provider_that_raises_never_breaks_the_pass(stores) -> None:
    store, events, state = stores

    def _explode() -> list[Notifier]:
        raise RuntimeError("resolving channels failed")

    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=FakeRegistry({"pc1"}),
        notifier_provider=_explode,
    )
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))

    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1  # evaluated and recorded; it just pushed nothing
    assert len(await events.query(kind="alert")) == 1


def test_notifiers_and_a_provider_together_are_refused(stores) -> None:
    """Two sources of truth for delivery is a mistake that must not be silent."""

    store, events, state = stores
    with pytest.raises(ValueError):
        AlertEngine(
            store=store,
            alert_state=state,
            event_store=events,
            registry=FakeRegistry(),
            notifiers=[FakeNotifier()],
            notifier_provider=lambda: [],
        )


async def test_the_engine_delivers_on_channels_written_to_settings(stores) -> None:
    """End to end: a dashboard write reaches the wire on the next alert.

    The joined seam -- the real ``Settings`` resolver, the real
    ``NotifierProvider``, the real ``WebhookNotifier`` -- against a mock
    transport. A break in any one of the three fails this.
    """

    import httpx

    from kenny_server.config import Settings
    from kenny_server.notify import NotifierProvider

    class _MemStore:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        async def all(self) -> dict[str, str]:
            return dict(self.data)

        async def set(self, key: str, value: str) -> None:
            self.data[key] = value

        async def delete(self, key: str) -> bool:
            return self.data.pop(key, None) is not None

    posted: list[httpx.Request] = []

    def _client() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(request)
            return httpx.Response(200)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    store, events, state = stores
    settings = Settings(_MemStore(), env={}, apply_hooks={})
    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=FakeRegistry({"pc1"}),
        notifier_provider=NotifierProvider(settings=settings, client_factory=_client),
        settings=settings,
    )

    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    assert posted == []  # nothing configured yet

    await settings.set("KENNY_WEBHOOK_URL", "https://hook.example/live")

    await insert(store, snapshot(50.0), NOW + timedelta(minutes=1))
    await engine.evaluate_once(NOW + timedelta(minutes=2))
    assert [str(r.url) for r in posted] == ["https://hook.example/live"]


# -- one verdict per host (ADR-0058) ----------------------------------------


def test_alert_loop_and_dashboard_agree_on_reliability(tmp_path, monkeypatch) -> None:
    """Joined across the store's annotate seam, the persisted classification
    and the alert engine: a pattern the classifier persisted as benign is
    scored benign by the alert loop too -- it never sees the raw payload the
    old volume fallback would have escalated on -- and the dashboard's
    ``/api/agent`` read says the same thing.
    """

    from functools import partial

    from starlette.testclient import TestClient

    from kenny_server import event_categories
    from kenny_server.main import build_app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KENNY_ALERT_INTERVAL_SECS", "0")
    app = build_app(db_path=str(tmp_path / "agree.sqlite"))
    now = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
    by_day = {f"2026-07-0{d}": 100 for d in range(1, 8)}
    snap = {"reliability": {"status": "ok", "summary": "700 error/critical events in 7d",
            "recent_crashes": 700, "window_days": 7, "events": [
        {"source": "DistributedCOM", "event_id": 10016, "level": "error", "count": 700,
         "sample": "stale COM permission", "by_day": by_day,
         "last_seen": "2026-07-07T23:00:00Z"},
    ]}}
    event_categories.reset_state()
    try:
        with TestClient(app) as c:
            store = app.state.store
            c.portal.call(partial(app.state.classification_store.upsert_many, [{
                "source": "DistributedCOM", "event_id": 10016, "category": "Windows service",
                "severity": "benign", "cause": "stale COM permission",
                "user_impact": "none", "symptom": "",
                "model": event_categories.VERDICT_MODEL_TAG,
            }]))
            c.portal.call(event_categories.load_persisted)
            c.portal.call(partial(store.insert, "pc1", "2026-07-07T23:30:00Z", snap,
                                  received_at="2026-07-07T23:30:00Z"))

            h = {"Authorization": f"Bearer {app.state.operator_token}"}
            section = c.get("/api/agent/pc1", headers=h).json()["health"]["sections"]["reliability"]
            assert section["status"] == "ok"
            # 700 events across all 7 days, and not a finding: the verdict
            # says nobody noticed anything, and the reason says the pattern
            # was looked at rather than going silent about it.
            assert section["reason"] == "nothing user-visible in 7d (1 pattern(s) checked)"

            notifier = FakeNotifier()
            engine = AlertEngine(
                store=store, alert_state=app.state.alert_state, event_store=app.state.event_store,
                registry=FakeRegistry({"pc1"}), notifiers=[notifier],
            )
            assert c.portal.call(partial(engine.evaluate_once, now)) == []
            assert notifier.sent == []
            row = c.portal.call(partial(app.state.alert_state.get, "pc1", "section:reliability"))
            assert row is None or row["status"] == "ok"
    finally:
        event_categories.reset_state()


# -- posture never notifies; findings carry an age (ADR-0058) ---------------


def _posture_snapshot(protected: bool, disk_pct: float = 50.0) -> dict:
    snap = snapshot(disk_pct)
    snap["encryption"] = {
        "status": "ok", "summary": "",
        "volumes": [{"mount": "C:", "protection_status": 1 if protected else 0}],
    }
    return snap


async def test_posture_transitions_never_notify_but_are_aged(stores) -> None:
    store, _, state = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    # ok -> posture: silent, but the state row (and so the finding's age) is written.
    await insert(store, _posture_snapshot(protected=False), NOW - timedelta(minutes=1))
    assert await engine.evaluate_once(NOW) == []
    row = await state.get("pc1", "section:encryption")
    assert row["status"] == "posture" and row["since"] == NOW.isoformat()
    assert row["last_notified_at"] is None

    # Unchanged posture on the next pass: nothing, and `since` keeps its date.
    await insert(store, _posture_snapshot(protected=False), NOW + timedelta(days=3))
    assert await engine.evaluate_once(NOW + timedelta(days=3, minutes=1)) == []
    assert (await state.get("pc1", "section:encryption"))["since"] == NOW.isoformat()

    # posture -> ok: silent too (there was no announced episode to resolve).
    await insert(store, _posture_snapshot(protected=True), NOW + timedelta(days=4))
    assert await engine.evaluate_once(NOW + timedelta(days=4, minutes=1)) == []
    assert notifier.sent == []


async def test_warn_to_posture_is_a_silent_demotion(stores) -> None:
    store, _, state = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    # A section the loop had recorded as warn (an older rule) now scores posture.
    await state.upsert("pc1", "section:encryption", status="warn",
                       since=(NOW - timedelta(days=2)).isoformat(),
                       last_notified_at=(NOW - timedelta(days=2)).isoformat())
    await insert(store, _posture_snapshot(protected=False), NOW - timedelta(minutes=1))
    assert await engine.evaluate_once(NOW) == []
    row = await state.get("pc1", "section:encryption")
    assert row["status"] == "posture"
    assert row["since"] == NOW.isoformat()


async def test_posture_to_incident_escalates_like_ok(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    await insert(store, _posture_snapshot(protected=False), NOW - timedelta(minutes=1))
    assert await engine.evaluate_once(NOW) == []
    # The disk fills up: that escalation fires regardless of the posture section.
    await insert(store, _posture_snapshot(protected=False, disk_pct=96.0), NOW + timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=6))
    assert [n.body for n in sent] == ["[CRIT] disk: C: 96% full (>=95%)"]
    assert sent[0].sections == {"disk": "crit"}


async def test_disk_forecast_declares_the_disk_section(stores) -> None:
    """The forecast is a statement about ``disk`` and says so structurally.

    Two things depend on it: an auto-ticket rule can target
    ``disk_forecast``/``disk`` at all (``ticket_rules.decide`` derives its
    subject from ``sections``), and the forecast shares a ticket subject with
    an acute disk finding instead of opening a second ticket for one filling
    volume (``alert_subject.dedup_key``).
    """

    store, _, _ = stores
    engine = make_engine(stores, FakeNotifier())
    base = NOW - timedelta(days=5)
    for i in range(6):
        await insert(store, snapshot(68.0 + 2.0 * i), base + timedelta(days=i))

    sent = await engine.evaluate_once(NOW)
    forecast = next(n for n in sent if n.event_type == "disk_forecast")
    assert forecast.sections == {"disk": "warn"}


async def test_the_stored_alert_carries_its_title_and_producer(stores) -> None:
    """``message`` joins title and body with a newline, so a title that
    contains one cannot be recovered by splitting it back apart."""

    store, events, _ = stores
    engine = make_engine(stores, FakeNotifier())
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1

    rows = await events.query(kind="alert")
    assert rows[0]["fields"]["title"] == sent[0].title
    assert rows[0]["fields"]["event_type"] == sent[0].event_type


# -- confirmation before alarm, and the warn that used to be dropped --------


def _reliability_snapshot(crashes: int, *, at: datetime = NOW) -> dict:
    """A snapshot whose `reliability` section is a crash finding (or clean)."""

    events = []
    if crashes:
        events.append({
            "source": "Microsoft-Windows-Kernel-Power", "event_id": 41, "level": "critical",
            "count": crashes, "sample": "unexpected shutdown",
            "last_seen": (at - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "by_day": {(at - timedelta(days=d)).date().isoformat(): 1 for d in range(crashes)},
            "user_impact": "crashed", "symptom": "The PC restarted unexpectedly",
            "classification_state": "classified",
        })
    return {"reliability": {"status": "ok", "summary": "", "recent_crashes": crashes,
                            "window_days": 7, "events": events}}


async def test_reliability_crit_needs_a_second_collection_before_it_alarms(stores) -> None:
    """A finding that appears and vanishes inside one push interval is not one.

    `reliability` reads a rolling 7-day window whose contents shift as events
    age out of it, so one evaluation over one snapshot used to be enough to
    page someone and open a ticket.
    """

    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    await insert(store, _reliability_snapshot(2), NOW - timedelta(minutes=1))
    assert await engine.evaluate_once(NOW) == []
    assert notifier.sent == []

    # A *newer* collection still saying the same thing confirms it.
    await insert(store, _reliability_snapshot(2), NOW + timedelta(minutes=15))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=16))
    assert len(sent) == 1
    assert "The PC restarted unexpectedly" in sent[0].body


async def test_reliability_finding_that_vanishes_never_alarms(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    await insert(store, _reliability_snapshot(2), NOW - timedelta(minutes=1))
    assert await engine.evaluate_once(NOW) == []

    # Gone by the next push: the candidate is dropped, not held.
    await insert(store, _reliability_snapshot(0), NOW + timedelta(minutes=15))
    assert await engine.evaluate_once(NOW + timedelta(minutes=16)) == []
    assert notifier.sent == []
    # ... and it did not leave a pending row behind to fire later.
    _, _, state = stores
    assert await state.get("pc1", "pending:section:reliability") is None


async def test_a_warn_blocked_by_cooldown_fires_on_the_next_eligible_pass(stores) -> None:
    """The cooldown used to swallow a warn outright rather than delay it.

    `status` advanced to the new value while nothing was sent, so the next
    pass saw no change and skipped it -- the warning was lost for the whole
    episode. Sections are independent scopes, so this needs one section to
    burn its own cooldown and then escalate again within the window.
    """

    store, _, state = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, cooldown_s=3600)

    # First warn fires and starts the cooldown for `section:disk`.
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=1))
    assert len(await engine.evaluate_once(NOW)) == 1

    # Recover, then degrade again inside the cooldown window.
    await insert(store, snapshot(50.0), NOW + timedelta(minutes=5))
    await engine.evaluate_once(NOW + timedelta(minutes=6))
    await insert(store, snapshot(85.0), NOW + timedelta(minutes=10))
    assert await engine.evaluate_once(NOW + timedelta(minutes=11)) == []
    # Held as a candidate rather than discarded.
    pending = await state.get("pc1", "pending:section:disk")
    assert pending is not None and pending["status"] == "warn"

    # Once the cooldown has passed it is delivered, without the condition
    # having to change again.
    sent = await engine.evaluate_once(NOW + timedelta(minutes=70))
    assert len(sent) == 1
    assert "[WARN] disk" in sent[0].body
    assert await state.get("pc1", "pending:section:disk") is None


async def test_status_age_survives_a_pass_that_changes_nothing(stores) -> None:
    """`since` is the age of the current finding, which the host page and the
    ticket both read -- a re-evaluation that changes nothing must not reset
    it."""

    store, _, state = stores
    engine = make_engine(stores, FakeNotifier())
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    first = (await state.get("pc1", "section:disk"))["since"]

    await engine.evaluate_once(NOW + timedelta(minutes=30))
    assert (await state.get("pc1", "section:disk"))["since"] == first


# -- the ticket a finding opened is the ticket its recovery closes ----------


async def test_recovery_resolves_the_ticket_the_alert_opened(stores) -> None:
    """Without this the ticket surface only ever accumulated.

    `health` alerts open a ticket by default, `crit -> warn` is silent, a
    recovery is explicitly never ticketed, and the ticket sweep never reads
    health -- so an automatically opened ticket could only be closed by a
    person. Not reliability-specific: every section closes the same way.
    """

    store, _, _ = stores
    opened: list[str] = []
    closed: list[str] = []

    async def _open(note) -> str:
        opened.append(note.title)
        return "T-1"

    async def _close(note) -> str:
        closed.append(",".join(sorted(note.sections)))
        return "T-1"

    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, open_ticket=_open, close_ticket=_close)

    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    assert len(opened) == 1 and closed == []

    await insert(store, snapshot(40.0), NOW + timedelta(minutes=5))
    await engine.evaluate_once(NOW + timedelta(minutes=6))
    assert closed == ["disk"]


async def test_suppressing_a_pattern_closes_its_ticket(tmp_path, monkeypatch) -> None:
    """The joined seam, end to end through the real app.

    A rule changes what the next read scores, but the alert state and the
    ticket were written by the alert loop -- so a muted pattern's alarm stayed
    live and its ticket stayed open until the next pass. That is what made
    suppression look like it did nothing: the status went quiet eventually,
    the work it had already created never did.
    """

    from functools import partial

    from starlette.testclient import TestClient

    from kenny_server import event_categories
    from kenny_server.main import build_app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KENNY_ALERT_INTERVAL_SECS", "0")
    app = build_app(db_path=str(tmp_path / "reconcile.sqlite"))
    event_categories.reset_state()
    try:
        with TestClient(app) as c:
            h = {"Authorization": f"Bearer {app.state.operator_token}"}
            engine = app.state.alert_engine

            # Wall-clock recent: `evaluate_agent_now` runs against the real
            # clock (it is what an operator write triggers), and a host whose
            # newest snapshot is older than the offline window is skipped.
            base = datetime.now(timezone.utc) - timedelta(minutes=20)

            # Two collections of the same crash finding: the second confirms
            # it (health_rules.CONFIRM_BEFORE_ALARM), so it alarms and tickets.
            for minutes in (0, 15):
                at = base + timedelta(minutes=minutes)
                c.portal.call(partial(
                    app.state.store.insert, "pc1", at.isoformat(),
                    _reliability_snapshot(2, at=at), received_at=at.isoformat(),
                ))
                c.portal.call(partial(engine.evaluate_once, at + timedelta(minutes=1)))

            tickets = c.portal.call(partial(app.state.ticket_store.list, states=["new"]))
            assert len(tickets) == 1
            assert tickets[0].dedup_key == "alert|pc1|state|reliability"

            # Mute the pattern. The rule write alone must take the work back.
            r = c.post(
                "/api/reliability/suppressions",
                headers=h,
                json={"event_id": 41, "source": "Microsoft-Windows-Kernel-Power",
                      "agent_id": "pc1", "note": "known-good test rig"},
            )
            assert r.status_code == 200

            ticket = c.portal.call(partial(app.state.ticket_store.get, tickets[0].id))
            assert ticket.state == "resolved"
            section = c.get("/api/agent/pc1", headers=h).json()["health"]["sections"]["reliability"]
            assert section["status"] == "ok"
    finally:
        event_categories.reset_state()


# -- routing by actionability (ADR-0067) ---------------------------------------


def two_sections(disk_pct: float, mem_pct: float) -> dict:
    snap = snapshot(disk_pct)
    snap["memory"] = {"status": "ok", "summary": "", "percent_used": mem_pct}
    return snap


async def test_a_warn_is_work_not_an_interruption(stores) -> None:
    store, events, _ = stores
    notifier = FakeNotifier()
    opened: list[Notification] = []

    async def _open(note: Notification) -> str:
        opened.append(note)
        return "T-1"

    engine = make_engine(stores, notifier, open_ticket=_open)
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=1))
    sent = await engine.evaluate_once(NOW)

    assert [(n.kind, n.route) for n in sent] == [("alert", "daily")]
    assert notifier.sent == []  # no push
    assert len(opened) == 1  # still a ticket in the inbox
    rows = await events.query(kind="alert")
    assert rows[0]["fields"]["route"] == "daily"


async def test_a_crit_pushes_with_the_pass_s_warn_lines_as_context(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    await insert(store, two_sections(96.0, 90.0), NOW - timedelta(minutes=1))
    sent = await engine.evaluate_once(NOW)

    assert len(sent) == 1  # one note, one ticket
    assert sent[0].route == "push"
    assert sent[0].sections == {"disk": "crit", "memory": "warn"}
    assert [i["section"] for i in sent[0].items] == ["disk", "memory"]
    assert notifier.sent == sent


async def test_only_a_pushed_episode_s_recovery_is_pushed(stores) -> None:
    store, _, state = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, cooldown_s=0)

    # warn only -> daily; its recovery is recorded, never pushed.
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    await insert(store, snapshot(50.0), NOW + timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=6))
    assert [(n.kind, n.route) for n in sent] == [("recovery", "record")]
    assert notifier.sent == []

    # crit -> push; its recovery is pushed (quietly, at low priority).
    await insert(store, snapshot(96.0), NOW + timedelta(minutes=10))
    await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert await state.get("pc1", "pushed:section:disk") is not None
    await insert(store, snapshot(50.0), NOW + timedelta(minutes=15))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=16))
    assert [(n.kind, n.route, n.priority) for n in sent] == [("recovery", "push", "low")]
    assert [n.kind for n in notifier.sent] == ["alert", "recovery"]
    assert await state.get("pc1", "pushed:section:disk") is None


async def test_a_recovery_an_operator_caused_is_never_pushed(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    # Wall-clock recent: ``evaluate_agent_now`` runs on the real clock.
    recent = datetime.now(timezone.utc) - timedelta(minutes=2)
    await insert(store, snapshot(96.0), recent)
    await engine.evaluate_agent_now("pc1")
    assert [n.route for n in notifier.sent] == ["push"]

    await insert(store, snapshot(50.0), recent + timedelta(minutes=1))
    sent = await engine.evaluate_agent_now("pc1")
    assert [(n.kind, n.route) for n in sent] == [("recovery", "record")]
    assert len(notifier.sent) == 1


async def test_an_allowlisted_change_pushes_and_is_never_held_by_the_cooldown(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, cooldown_s=3600)

    await insert(store, autostart_snapshot(["OneDrive"]), NOW - timedelta(minutes=15))
    await engine.evaluate_once(NOW - timedelta(minutes=10))
    await insert(store, autostart_snapshot(["OneDrive", "A"]), NOW - timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW)
    assert [(n.kind, n.route) for n in sent] == [("change", "push")]

    # A second addition inside the cooldown still pushes: dropping it would be
    # a security miss.
    await insert(store, autostart_snapshot(["OneDrive", "A", "B"]), NOW + timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW + timedelta(minutes=6))
    assert [(n.route, n.body) for n in sent] == [("push", "autostart: added B | HKCU\\Run (command=b.exe)")]
    assert len(notifier.sent) == 2


async def test_a_change_off_the_allowlist_is_only_recorded(stores) -> None:
    store, events, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)

    def software(version: str) -> dict:
        return {"installed_software": {"status": "ok", "summary": "", "apps": [
            {"name": "Firefox", "publisher": "Mozilla", "version": version},
        ]}}

    await insert(store, software("1.0"), NOW - timedelta(minutes=15))
    await engine.evaluate_once(NOW - timedelta(minutes=10))
    await insert(store, software("1.1"), NOW - timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW)
    assert [(n.kind, n.route) for n in sent] == [("change", "record")]
    assert notifier.sent == []
    assert any("Firefox" in r["message"] for r in await events.query(kind="alert"))


async def test_the_change_allowlist_is_read_live(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    settings = _FakeSettings({"KENNY_ALERT_CHANGE_PUSH": "installed_software:changed"})
    engine = make_engine(stores, notifier, settings=settings)
    await insert(store, autostart_snapshot(["OneDrive"]), NOW - timedelta(minutes=15))
    await engine.evaluate_once(NOW - timedelta(minutes=10))
    await insert(store, autostart_snapshot(["OneDrive", "A"]), NOW - timedelta(minutes=5))
    sent = await engine.evaluate_once(NOW)
    assert [n.route for n in sent] == ["record"]  # autostart is no longer listed


def test_the_change_allowlist_parser_drops_what_can_never_match() -> None:
    from kenny_server.alerting import parse_change_allowlist

    assert parse_change_allowlist("local_accounts") == {
        ("local_accounts", "added"), ("local_accounts", "removed"), ("local_accounts", "changed"),
    }
    assert parse_change_allowlist(" autostart:added , nope, autostart:exploded,") == {
        ("autostart", "added"),
    }


def test_the_default_change_allowlist_names_what_the_diff_emits() -> None:
    """Seam: the allowlist default (alerting, and the settings catalogue that
    shows it) against the sections and kinds diffs.py can actually produce."""

    from kenny_server import config
    from kenny_server.alerting import DEFAULT_CHANGE_PUSH, parse_change_allowlist
    from kenny_server.diffs import CHANGE_KINDS, SPECS

    spec = next(s for s in config._SPECS if s.key == "KENNY_ALERT_CHANGE_PUSH")
    assert spec.default_raw == DEFAULT_CHANGE_PUSH
    entries = [e.strip() for e in DEFAULT_CHANGE_PUSH.split(",")]
    assert len(parse_change_allowlist(DEFAULT_CHANGE_PUSH)) >= len(entries)
    for entry in entries:
        section, _, kind = entry.partition(":")
        assert section in SPECS
        assert not kind or kind in CHANGE_KINDS


async def test_every_route_the_engine_emits_is_one_delivery_knows(stores) -> None:
    from kenny_server.notify import ROUTES

    store, _, _ = stores
    engine = make_engine(stores, FakeNotifier(), cooldown_s=0)
    notes: list[Notification] = []
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=30))
    notes += await engine.evaluate_once(NOW - timedelta(minutes=29))
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=20))
    notes += await engine.evaluate_once(NOW - timedelta(minutes=19))
    await insert(store, snapshot(40.0), NOW - timedelta(minutes=10))
    notes += await engine.evaluate_once(NOW - timedelta(minutes=9))
    assert {n.route for n in notes} == {"daily", "push"}
    assert {n.route for n in notes} <= set(ROUTES)


# -- delivery against the real channels (joined seams) --------------------------


def _discord_transport(log: list, *, patch_status: int = 200):
    import httpx

    counter = iter(range(1, 1000))

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if request.method == "PATCH":
            return httpx.Response(patch_status, json={})
        return httpx.Response(200, json={"id": f"m-{next(counter)}"})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_pushed_alert_is_edited_in_place_when_it_recovers(stores) -> None:
    import json

    from kenny_server.notify import DiscordNotifier

    store, _, state = stores
    log: list = []
    discord = DiscordNotifier(
        "https://discord.example/hook", client_factory=_discord_transport(log), base_url=lambda: ""
    )
    engine = make_engine(stores, discord)  # type: ignore[arg-type]

    await insert(store, two_sections(96.0, 90.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    assert [r.method for r in log] == ["POST"]
    assert len(await state.open_messages("discord", "pc1")) == 1

    # disk recovers, memory stays warn: the message is edited, not answered.
    await insert(store, two_sections(40.0, 90.0), NOW + timedelta(minutes=10))
    await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert [r.method for r in log] == ["POST", "PATCH"]
    assert str(log[1].url) == "https://discord.example/hook/messages/m-1"
    embed = json.loads(log[1].content)["embeds"][0]
    assert "~~**disk**" in embed["description"] and embed["color"] == 0xE74C3C

    await insert(store, two_sections(40.0, 30.0), NOW + timedelta(minutes=20))
    await engine.evaluate_once(NOW + timedelta(minutes=21))
    assert [r.method for r in log] == ["POST", "PATCH", "PATCH"]
    assert json.loads(log[2].content)["embeds"][0]["color"] == 0x2ECC71
    assert await state.open_messages("discord", "pc1") == []


async def test_a_warn_only_episode_never_reaches_discord(stores) -> None:
    from kenny_server.notify import DiscordNotifier

    store, _, _ = stores
    log: list = []
    discord = DiscordNotifier("https://discord.example/hook", client_factory=_discord_transport(log))
    engine = make_engine(stores, discord)  # type: ignore[arg-type]
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    await insert(store, snapshot(40.0), NOW + timedelta(minutes=10))
    await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert log == []


async def test_a_message_gone_from_discord_is_forgotten(stores) -> None:
    from kenny_server.notify import DiscordNotifier

    store, _, state = stores
    log: list = []
    discord = DiscordNotifier(
        "https://discord.example/hook", client_factory=_discord_transport(log, patch_status=404)
    )
    engine = make_engine(stores, discord)  # type: ignore[arg-type]
    await insert(store, snapshot(96.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    await insert(store, snapshot(40.0), NOW + timedelta(minutes=10))
    await engine.evaluate_once(NOW + timedelta(minutes=11))
    assert [r.method for r in log] == ["POST", "PATCH"]
    assert await state.open_messages("discord", "pc1") == []


async def test_the_webhook_receives_every_route_labelled(stores) -> None:
    import json

    import httpx

    from kenny_server.notify import WebhookNotifier

    store, _, _ = stores
    log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        return httpx.Response(204)

    hook = WebhookNotifier(
        "https://hook.example/",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    engine = make_engine(stores, hook)  # type: ignore[arg-type]
    await insert(store, snapshot(85.0), NOW - timedelta(minutes=1))
    await engine.evaluate_once(NOW)
    payloads = [json.loads(r.content) for r in log]
    assert [(p["kind"], p["route"]) for p in payloads] == [("alert", "daily")]
    assert {"kind", "title", "body", "priority", "tags", "agent_id", "event_type",
            "sections", "at", "route"} <= set(payloads[0])


# -- the daily summary ----------------------------------------------------------


async def _baseline(engine: AlertEngine, at: datetime) -> None:
    assert await engine.maybe_send_daily(at) is False  # records the baseline only


async def test_the_daily_summary_lists_what_is_new_and_still_open(stores) -> None:
    store, events, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, registry=FakeRegistry({"pc1", "pc2"}), daily_hour=8,
                         digest_enabled=False)
    await _baseline(engine, NOW)

    # pc1: a warn that stays; pc2: a warn that comes and goes before the slot.
    await insert(store, snapshot(85.0), NOW + timedelta(hours=1))
    await insert(store, snapshot(86.0), NOW + timedelta(hours=1), agent_id="pc2")
    await engine.evaluate_once(NOW + timedelta(hours=1, minutes=1))
    await insert(store, snapshot(40.0), NOW + timedelta(hours=2), agent_id="pc2")
    await engine.evaluate_once(NOW + timedelta(hours=2, minutes=1))
    assert notifier.sent == []

    slot = datetime(2026, 7, 2, 8, 5, tzinfo=timezone.utc)
    assert await engine.maybe_send_daily(slot) is True
    assert len(notifier.sent) == 1
    daily = notifier.sent[0]
    assert (daily.kind, daily.event_type, daily.route) == ("digest", "daily", "push")
    assert daily.title == "kenny daily: 1 new finding(s)"
    assert daily.body.startswith("pc1 · disk: C: 85% full (>80%) (for ")
    assert "pc2" not in daily.body

    # Same day again: nothing. Next day with nothing new: nothing either.
    assert await engine.maybe_send_daily(slot + timedelta(hours=3)) is False
    assert await engine.maybe_send_daily(slot + timedelta(days=1)) is False
    assert len(notifier.sent) == 1
    assert any(r["fields"].get("event_type") == "daily" for r in await events.query(kind="alert"))


async def test_the_daily_summary_leaves_pushed_findings_to_the_open_count(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, daily_hour=8, digest_enabled=False)
    await _baseline(engine, NOW)
    await insert(store, two_sections(96.0, 40.0), NOW + timedelta(hours=1))
    await engine.evaluate_once(NOW + timedelta(hours=1, minutes=1))
    await insert(store, two_sections(96.0, 90.0), NOW + timedelta(hours=2))
    await engine.evaluate_once(NOW + timedelta(hours=2, minutes=1))
    assert [n.route for n in notifier.sent] == ["push"]

    assert await engine.maybe_send_daily(datetime(2026, 7, 2, 8, 5, tzinfo=timezone.utc))
    body = notifier.sent[-1].body
    assert "memory" in body and "disk" not in body.split("\n")[0]
    assert "1 older finding(s) still open." in body


async def test_the_daily_summary_folds_a_disk_forecast_into_the_disk_line(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, daily_hour=8, digest_enabled=False)
    await _baseline(engine, NOW - timedelta(days=6))
    base = NOW - timedelta(days=5)
    for i in range(6):
        await insert(store, snapshot(72.0 + 2.0 * i), base + timedelta(days=i))
    sent = await engine.evaluate_once(NOW)
    assert {n.event_type for n in sent} == {"health", "disk_forecast"}

    assert await engine.maybe_send_daily(datetime(2026, 7, 2, 8, 5, tzinfo=timezone.utc))
    lines = notifier.sent[-1].body.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("pc1 · disk: C: 82% full (>80%); C: full in ~")


async def test_the_daily_summary_yields_the_weekly_digest_s_slot(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    # 2026-07-06 is a Monday, the digest's default day and hour.
    engine = make_engine(stores, notifier, daily_hour=8)
    await _baseline(engine, datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc))
    await insert(store, snapshot(85.0), datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc))
    await engine.evaluate_once(datetime(2026, 7, 5, 13, 1, tzinfo=timezone.utc))
    assert await engine.maybe_send_daily(datetime(2026, 7, 6, 8, 5, tzinfo=timezone.utc)) is False
    assert notifier.sent == []


async def test_the_daily_summary_survives_a_restart(stores) -> None:
    store, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, daily_hour=8, digest_enabled=False)
    await _baseline(engine, NOW)
    await insert(store, snapshot(85.0), NOW + timedelta(hours=1))
    await engine.evaluate_once(NOW + timedelta(hours=1, minutes=1))

    slot = datetime(2026, 7, 2, 8, 5, tzinfo=timezone.utc)
    assert await engine.maybe_send_daily(slot)
    restarted = make_engine(stores, notifier, daily_hour=8, digest_enabled=False)
    assert await restarted.maybe_send_daily(slot + timedelta(minutes=1)) is False
    assert len(notifier.sent) == 1

"""The hardware history end to end (ADR-0070): rollup, forecast alert, digest, API.

Snapshots go into a real ``TelemetryStore``; the rollup, the alert engine, the digest
and the dashboard API run on top of it with a frozen clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from starlette.testclient import TestClient
from support.hardware import WHEA, disk, disk_smart, fan, fans_section, group, hardware_errors, snapshot

from kenny_server import forecast, fleet_stats, hardware_history, ticket_rules, trends
from kenny_server.alerting import AlertEngine
from kenny_server.digest import build_digest
from kenny_server.main import build_app
from kenny_server.notify import Notification
from kenny_server.store import AlertStateStore, EventStore, HardwareHistoryStore, TelemetryStore

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeNotifier:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class _Agent:
    online = True


class FakeRegistry:
    def __init__(self, online: set[str] | None = None) -> None:
        self._online = online if online is not None else {"pc1"}

    def get(self, agent_id: str):
        return _Agent() if agent_id in self._online else None


@pytest.fixture
async def stores(tmp_path):
    db = str(tmp_path / "kenny.sqlite")
    store, events, state, hw = (
        TelemetryStore(db),
        EventStore(db),
        AlertStateStore(db),
        HardwareHistoryStore(db),
    )
    for s in (store, events, state, hw):
        await s.connect()
    yield store, events, state, hw
    for s in (store, events, state, hw):
        await s.close()


def make_engine(stores, notifier: FakeNotifier | None = None, **kw) -> AlertEngine:
    store, events, state, hw = stores
    return AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=kw.pop("registry", FakeRegistry()),
        notifiers=[notifier or FakeNotifier()],
        hw_history=kw.pop("hw_history", hw),
        **kw,
    )


async def put(store: TelemetryStore, snap: dict, at: datetime, agent_id: str = "pc1") -> None:
    await store.insert(agent_id, at.isoformat(), snap, received_at=at.isoformat())


def wearing(pct: float, **disk_kw) -> dict:
    return snapshot(disk_smart=disk_smart(disk("S1", "WD_BLACK SN850X", pct=pct, spare=100, **disk_kw)))


async def put_wear_history(
    store, days: int = 40, end: datetime = NOW, agent_id: str = "pc1", step: float = 0.5
) -> None:
    """``days`` daily snapshots, ``step`` % a day, ending ``end``.

    The default is long enough for the wear forecast's evidence floor
    (``trends.SLOW_MIN_POINTS`` over ``SLOW_MIN_SPAN_DAYS``).
    """

    for i in range(days):
        at = end - timedelta(days=days - 1 - i, minutes=1)
        await put(store, wearing(70.0 + i * step), at, agent_id)


def forecast_notes(notes: list[Notification]) -> list[Notification]:
    return [n for n in notes if n.event_type == "hardware_forecast"]


# -- rollup --------------------------------------------------------------------------


async def test_the_first_rollup_backfills_every_stored_day(stores) -> None:
    store, _, _, hw = stores
    await put_wear_history(store, days=5, step=1.0)
    wrote = await hardware_history.rollup_agent(store, hw, "pc1", now=NOW)
    assert wrote > 0
    series = (await hw.series("pc1", "2000-01-01"))["disk:S1"]["percentage_used"]
    assert series == [
        ("2026-06-27", 70.0),
        ("2026-06-28", 71.0),
        ("2026-06-29", 72.0),
        ("2026-06-30", 73.0),
        ("2026-07-01", 74.0),
    ]
    # today is provisional: the state stops at yesterday
    assert await hw.last_day("pc1") == "2026-06-30"


async def test_the_rollup_is_idempotent(stores) -> None:
    store, _, _, hw = stores
    await put_wear_history(store, days=5, step=1.0)
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW)
    first = await hw.series("pc1", "2000-01-01")
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW)
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW)
    assert await hw.series("pc1", "2000-01-01") == first
    assert await hw.last_day("pc1") == "2026-06-30"


async def test_the_next_run_advances_the_state_and_rewrites_the_provisional_day(stores) -> None:
    store, _, _, hw = stores
    await put(store, wearing(10), datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc))
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW)
    assert (await hw.series("pc1", "2000-01-01"))["disk:S1"]["percentage_used"] == [("2026-07-01", 10.0)]
    assert await hw.last_day("pc1") == "2026-06-30"

    await put(store, wearing(11), datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc))
    await put(store, wearing(12), datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc))
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW + timedelta(days=1))
    assert (await hw.series("pc1", "2000-01-01"))["disk:S1"]["percentage_used"] == [
        ("2026-07-01", 11.0),  # the finished day, with its later snapshot
        ("2026-07-02", 12.0),
    ]
    assert await hw.last_day("pc1") == "2026-07-01"


async def test_a_day_that_is_final_is_never_rewritten(stores) -> None:
    store, _, _, hw = stores
    await put(store, wearing(10), datetime(2026, 6, 29, 8, 0, tzinfo=timezone.utc))
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW)
    await store.delete_agent("pc1")  # as if the snapshots were pruned
    await put(store, wearing(50), datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc))
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW + timedelta(days=1))
    assert (await hw.series("pc1", "2000-01-01"))["disk:S1"]["percentage_used"] == [
        ("2026-06-29", 10.0),
        ("2026-07-01", 50.0),
    ]


async def test_an_agent_with_no_snapshots_writes_nothing(stores) -> None:
    store, _, _, hw = stores
    assert await hardware_history.rollup_agent(store, hw, "ghost", now=NOW) == 0
    assert await hw.last_day("ghost") is None


async def test_hosts_roll_up_independently(stores) -> None:
    store, _, _, hw = stores
    await put_wear_history(store, days=3, agent_id="pc1")
    await put_wear_history(store, days=3, agent_id="pc2")
    await hardware_history.rollup_all(store, hw, now=NOW)
    assert await hw.last_day("pc1") == await hw.last_day("pc2") == "2026-06-30"
    assert set(await hw.series("pc2", "2000-01-01")) == {"disk:S1"}


async def test_one_failing_host_does_not_stop_the_rest(stores, monkeypatch) -> None:
    store, _, _, hw = stores
    await put_wear_history(store, days=3, agent_id="bad")
    await put_wear_history(store, days=3, agent_id="good")
    real = hardware_history.rollup_agent

    async def flaky(store_, hw_, agent_id, **kw):
        if agent_id == "bad":
            raise RuntimeError("boom")
        return await real(store_, hw_, agent_id, **kw)

    monkeypatch.setattr(hardware_history, "rollup_agent", flaky)
    await hardware_history.rollup_all(store, hw, now=NOW)
    assert await hw.series("good", "2000-01-01") != {}
    assert await hw.series("bad", "2000-01-01") == {}


async def test_the_alert_loops_prune_pass_rolls_up_before_it_prunes_snapshots(stores) -> None:
    store, _, _, hw = stores
    store.retention_days = 1
    now = datetime.now(timezone.utc)  # the stores prune against the real clock
    old = now - timedelta(days=10)
    await put(store, wearing(10), old)
    await put(store, wearing(20), now - timedelta(hours=1))
    engine = make_engine(stores, prunables=[(store, None)])
    await engine._maybe_prune(now)
    assert len(await store.history("pc1")) == 1  # the old snapshot is gone ...
    days = [d for d, _ in (await hw.series("pc1", "2000-01-01"))["disk:S1"]["percentage_used"]]
    assert old.date().isoformat() in days  # ... but its day was rolled up first


async def test_without_a_history_store_the_engine_does_not_roll_up_or_forecast(stores) -> None:
    store, _, state, _ = stores
    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=stores[1],
        registry=FakeRegistry(),
        notifiers=[FakeNotifier()],
    )
    await put_wear_history(store)
    assert await engine.rollup_hardware_history(NOW) == 0
    assert forecast_notes(await engine.evaluate_once(NOW)) == []
    assert await engine.hardware_forecasts("pc1", NOW) == []


async def test_component_counts_and_fans_roll_up_from_real_snapshots(stores) -> None:
    store, _, _, hw = stores
    day = "2026-06-30"
    await put(
        store,
        snapshot(
            hardware_errors=hardware_errors(group(WHEA, 17, {day: 3})),
            fans=fans_section(fan(samples=[1000, 1000, 1000, 1000, 1000], duty=40)),
        ),
        datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
    )
    await hardware_history.rollup_agent(store, hw, "pc1", now=NOW)
    got = await hw.series("pc1", "2000-01-01")
    assert got["host:pcie"]["corrected_events"] == [(day, 3.0)]
    assert got["fan:nct6798.fan1"]["rpm_duty_30_50"] == [(day, 1000.0)]


# -- the hardware_forecast alert ------------------------------------------------------


async def test_a_wearing_disk_raises_one_daily_hardware_forecast(stores) -> None:
    store, _, state, hw = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)

    (note,) = forecast_notes(await engine.evaluate_once(NOW))
    assert note.kind == "alert" and note.route == "daily"
    assert note.agent_id == "pc1"
    assert note.sections == {"disk_smart": "warn"}
    assert "WD_BLACK SN850X" in note.body and "write endurance" in note.body
    assert note.title == "pc1: hardware at risk"
    assert notifier.sent == []  # daily route: nothing pushed
    row = await state.get("pc1", "section:hardware_forecast")
    assert row is not None and row["status"] == "warn"


async def test_the_same_forecast_is_not_news_again(stores) -> None:
    store, _, _, _ = stores
    engine = make_engine(stores)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    assert len(forecast_notes(await engine.evaluate_once(NOW))) == 1
    await put(store, wearing(90), NOW + timedelta(hours=1))
    assert forecast_notes(await engine.evaluate_once(NOW + timedelta(hours=2))) == []
    # and not after a restart either: the announced set is persisted
    restarted = make_engine(stores)
    await put(store, wearing(90), NOW + timedelta(hours=3))
    assert forecast_notes(await restarted.evaluate_once(NOW + timedelta(hours=4))) == []


async def test_a_growing_set_notifies_again_with_the_new_item_first(stores) -> None:
    store, _, state, _ = stores
    engine = make_engine(stores)
    for i in range(40):
        at = NOW - timedelta(days=39 - i, minutes=1)
        await put(
            store,
            snapshot(
                disk_smart=disk_smart(
                    disk("S1", "WD_BLACK SN850X", pct=70.0 + i * 0.5, spare=100),
                    disk("S2", "Samsung 990", pct=5, spare=100, media_errors=0),
                )
            ),
            at,
        )
    await engine.rollup_hardware_history(NOW)
    (first,) = forecast_notes(await engine.evaluate_once(NOW))
    assert "Samsung" not in first.body

    later = NOW + timedelta(days=1)
    await put(
        store,
        snapshot(
            disk_smart=disk_smart(
                disk("S1", "WD_BLACK SN850X", pct=90, spare=100),
                disk("S2", "Samsung 990", pct=5, spare=100, media_errors=4),
            )
        ),
        later,
    )
    await engine.rollup_hardware_history(later)
    (second,) = forecast_notes(await engine.evaluate_once(later + timedelta(minutes=1)))
    assert second.body.splitlines()[0].startswith("Samsung 990")  # the new pair leads
    assert "WD_BLACK SN850X" in second.body
    assert second.sections == {"disk_smart": "warn"}
    row = await state.get("pc1", "section:hardware_forecast")
    assert row["status"] == "warn"


async def test_the_scope_clears_when_the_set_empties_and_notifies_again_on_return(stores) -> None:
    store, _, state, _ = stores
    engine = make_engine(stores)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    assert len(forecast_notes(await engine.evaluate_once(NOW))) == 1

    # The disk stops reporting (replaced, removed): its series goes stale and the
    # forecast clears without a notification.
    gone = NOW + timedelta(days=30)
    await put(store, snapshot(disk_smart=disk_smart()), gone)
    await engine.rollup_hardware_history(gone)
    assert forecast_notes(await engine.evaluate_once(gone + timedelta(minutes=1))) == []
    assert (await state.get("pc1", "section:hardware_forecast"))["status"] == "ok"
    assert (await state.get("pc1", "hwforecast:set"))["status"] == ""

    back = gone + timedelta(days=1)
    await put(store, wearing(97), back)
    await engine.rollup_hardware_history(back)
    assert len(forecast_notes(await engine.evaluate_once(back + timedelta(minutes=1)))) == 1
    assert (await state.get("pc1", "section:hardware_forecast"))["status"] == "warn"


async def test_a_shrinking_set_stays_quiet_but_forgets_so_a_return_is_news(stores) -> None:
    store, _, state, _ = stores
    engine = make_engine(stores)
    # two disks wear out; later only one does
    for i in range(40):
        await put(
            store,
            snapshot(
                disk_smart=disk_smart(
                    disk("A", "Disk A", pct=70.0 + i * 0.5, spare=100),
                    disk("B", "Disk B", pct=70.0 + i * 0.5, spare=100),
                )
            ),
            NOW - timedelta(days=39 - i, minutes=1),
        )
    await engine.rollup_hardware_history(NOW)
    (note,) = forecast_notes(await engine.evaluate_once(NOW))
    assert "Disk A" in note.body and "Disk B" in note.body
    set_row = await state.get("pc1", "hwforecast:set")
    assert set_row["status"].count("\n") == 1

    # A is swapped out for a stale series; B keeps wearing
    for i in range(1, 25):
        at = NOW + timedelta(days=i)
        await put(store, snapshot(disk_smart=disk_smart(disk("B", "Disk B", pct=89.0 + i * 0.4, spare=100))), at)
    end = NOW + timedelta(days=24)
    await engine.rollup_hardware_history(end)
    assert forecast_notes(await engine.evaluate_once(end + timedelta(minutes=1))) == []
    assert (await state.get("pc1", "hwforecast:set"))["status"] == "disk:B|wear_out"
    assert (await state.get("pc1", "section:hardware_forecast"))["status"] == "warn"


async def test_forecasts_for_several_device_kinds_name_each_matching_section(stores) -> None:
    store, _, _, _ = stores
    engine = make_engine(stores)
    for i in range(40):
        at = NOW - timedelta(days=39 - i, minutes=1)
        wide = 16 if i < 33 else 8
        await put(
            store,
            snapshot(
                disk_smart=disk_smart(disk("S1", "WD_BLACK SN850X", pct=70.0 + i * 0.5, spare=100)),
                gpu={"status": "ok", "gpus": [_gpu(wide)]},
            ),
            at,
        )
    await engine.rollup_hardware_history(NOW)
    (note,) = forecast_notes(await engine.evaluate_once(NOW))
    assert note.sections == {"disk_smart": "warn", "gpu": "warn"}
    assert len(note.body.splitlines()) == 2


def _gpu(width):
    from support.hardware import gpu

    return gpu(util=80, width=width)


async def test_the_daily_summary_lists_the_hardware_at_risk(stores) -> None:
    store, _, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, daily_hour=8, digest_enabled=False)
    assert await engine.maybe_send_daily(NOW - timedelta(days=6)) is False  # baseline
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    await engine.evaluate_once(NOW)

    assert await engine.maybe_send_daily(datetime(2026, 7, 2, 8, 5, tzinfo=timezone.utc))
    (line,) = notifier.sent[-1].body.splitlines()
    assert line.startswith("pc1 · hardware: ")
    assert "WD_BLACK SN850X" in line and "(for " in line


async def test_the_daily_summary_does_not_double_count_a_forecast_as_an_old_finding(stores) -> None:
    store, _, _, _ = stores
    notifier = FakeNotifier()
    engine = make_engine(stores, notifier, daily_hour=8, digest_enabled=False)
    await engine.maybe_send_daily(NOW - timedelta(days=6))
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    await engine.evaluate_once(NOW)
    await engine.maybe_send_daily(datetime(2026, 7, 2, 8, 5, tzinfo=timezone.utc))
    # nothing new the next morning, but the open forecast is not forgotten
    assert await engine.maybe_send_daily(datetime(2026, 7, 3, 8, 5, tzinfo=timezone.utc)) is False


async def test_a_hardware_forecast_opens_a_ticket_merged_by_section(stores) -> None:
    store, _, _, _ = stores
    opened: list[Notification] = []

    async def open_ticket(note: Notification) -> None:
        opened.append(note)

    engine = make_engine(stores, open_ticket=open_ticket)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    await engine.evaluate_once(NOW)
    (ticketed,) = [n for n in opened if n.event_type == "hardware_forecast"]
    assert ticketed.sections == {"disk_smart": "warn"}


# -- ticket rules vocabulary ----------------------------------------------------------


def test_hardware_forecast_is_a_validated_event_type_that_opens_tickets_by_default() -> None:
    assert "hardware_forecast" in ticket_rules.EVENT_TYPES
    assert ticket_rules.DEFAULT_DECISION["hardware_forecast"] == "open_all"
    assert ticket_rules.KNOWN_SECTIONS["hardware_forecast"] == {
        "disk_smart", "gpu", "fans", "hardware_errors",
    }
    decision = ticket_rules.decide(
        {},
        kind="alert",
        agent_id="pc1",
        event_type="hardware_forecast",
        priority="default",
        sections={"disk_smart": "warn", "gpu": "warn"},
    )
    assert decision.open


def test_every_forecast_section_is_one_a_rule_may_name() -> None:
    assert set(trends.FORECAST_SECTION.values()) <= ticket_rules.KNOWN_SECTIONS["hardware_forecast"]


# -- digest ---------------------------------------------------------------------------


async def test_the_digest_lists_hardware_at_risk(stores) -> None:
    store, events, _, hw = stores
    await put_wear_history(store)
    await hardware_history.rollup_all(store, hw, now=NOW)
    body = (await build_digest(store, events, FakeRegistry(), now=NOW, hw_history=hw)).body
    todo = next(ln for ln in body.splitlines() if ln.startswith("To do:"))
    assert "hardware at risk: pc1 (1)" in todo


async def test_the_digest_has_no_block_without_forecasts_or_a_history_store(stores) -> None:
    store, events, _, hw = stores
    await put(store, wearing(10), NOW - timedelta(hours=1))
    await hardware_history.rollup_all(store, hw, now=NOW)
    body = (await build_digest(store, events, FakeRegistry(), now=NOW, hw_history=hw)).body
    assert "hardware at risk" not in body
    await put_wear_history(store, agent_id="pc2")
    await hardware_history.rollup_all(store, hw, now=NOW)
    body = (await build_digest(store, events, FakeRegistry(), now=NOW)).body
    assert "hardware at risk" not in body


# -- fleet KPI ------------------------------------------------------------------------


def test_the_hardware_at_risk_kpi_counts_hosts_with_a_forecast() -> None:
    agents = [
        {"agent_id": "a", "online": True, "snapshot": {}, "health": {"sections": {}}},
        {"agent_id": "b", "online": True, "snapshot": {}, "health": {"sections": {}}},
    ]
    risk = [{"symptom": "Disk x is wearing", "device_key": "disk:x"}]
    kpis = {
        k["key"]: k
        for k in fleet_stats.aggregate_overview(
            agents, hardware_forecasts={"a": risk, "b": []}
        )["kpis"]
    }
    kpi = kpis["hardware_at_risk"]
    assert kpi["value"] == 1 and kpi["severity"] == "warn"
    assert [m["agent_id"] for m in kpi["members"]] == ["a"]
    assert kpi["members"][0]["detail"] == "Disk x is wearing"
    quiet = {k["key"]: k for k in fleet_stats.aggregate_overview(agents)["kpis"]}
    assert quiet["hardware_at_risk"]["value"] == 0 and quiet["hardware_at_risk"]["severity"] == "ok"
    assert list(kpis)[-2:] == ["disk_forecast", "hardware_at_risk"]


# -- forecast facts -------------------------------------------------------------------


def _risk(label="WD_BLACK SN850X", days=40.0, reason="wear_out", key=None):
    return {
        "device_key": key or f"disk:{label}",
        "kind": "disk",
        "label": label,
        "reason": reason,
        "symptom": f"{label} has used 90% of its rated write endurance"
        + (f" and may reach it in about {int(days)} days" if days is not None else ""),
        "days_until": days,
    }


def test_build_facts_carries_a_capped_hardware_list_soonest_first() -> None:
    risks = [_risk(f"Disk {i}", days=100 - i) for i in range(10)] + [
        _risk("Fan", days=None, reason="fan_drift", key="fan:f")
    ]
    facts = forecast.build_facts({}, [], None, [], hardware=risks)
    assert len(facts["hardware"]) == forecast._MAX_HARDWARE
    assert facts["hardware_total"] == 11
    assert facts["hardware"][0]["label"] == "Disk 9"  # 91 days, the soonest
    assert set(facts["hardware"][0]) == {"label", "kind", "reason", "symptom", "days_until"}
    assert [h["days_until"] for h in facts["hardware"]] == sorted(h["days_until"] for h in facts["hardware"])


def test_build_facts_without_hardware_is_unchanged_in_meaning() -> None:
    facts = forecast.build_facts({}, [], None, [])
    assert facts["hardware"] == [] and facts["hardware_total"] == 0


def test_the_model_sees_the_hardware_rows_and_is_told_to_mention_them() -> None:
    facts = forecast.build_facts({}, [], None, [], hardware=[_risk(days=40.0)])
    content = forecast._facts_message(facts)["content"]
    assert "hardware at risk (1 total):" in content
    assert "WD_BLACK SN850X has used 90%" in content and "(~40 days)" in content
    assert "hardware" in forecast._SYSTEM_TEXT
    quiet = forecast._facts_message(forecast.build_facts({}, [], None, []))["content"]
    assert "hardware at risk" not in quiet


def test_the_deterministic_summary_names_the_hardware_without_a_model() -> None:
    facts = forecast.build_facts({}, [], None, [], hardware=[_risk(days=40.0), _risk("Fan", None, "fan_drift")])
    summary = forecast.deterministic_summary(facts)
    assert summary.startswith("WD_BLACK SN850X has used 90%")
    assert "1 other hardware item is also showing signs of wear or failure." in summary
    assert "Nothing on the horizon" not in summary
    three = forecast.build_facts({}, [], None, [], hardware=[_risk(f"D{i}", 10.0 + i) for i in range(3)])
    assert "2 other hardware items are also" in forecast.deterministic_summary(three)
    assert "Nothing on the horizon" in forecast.deterministic_summary(
        forecast.build_facts({}, [], None, [])
    )


def test_hardware_ranks_after_a_filling_disk_and_before_the_battery() -> None:
    disk_row = [{"mount": "C:", "current_percent": 90, "slope_percent_per_day": 1, "days_until_full": 8}]
    battery = {"current_percent": 70, "percent_per_30d": -5.0}
    facts = forecast.build_facts({}, disk_row, battery, [], hardware=[_risk()])
    summary = forecast.deterministic_summary(facts)
    assert summary.index("Drive C:") < summary.index("WD_BLACK") < summary.index("Battery")


# -- the dashboard API ----------------------------------------------------------------


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _seed_wear(c, app, agent_id="example-pc", days=40):
    store = app.state.store
    now = datetime.now(timezone.utc)
    for i in range(days):
        at = now - timedelta(days=days - 1 - i, minutes=1)
        c.portal.call(partial(store.insert, agent_id, at.isoformat(), wearing(70.0 + i * 0.5)))
    return now


def test_the_trends_api_carries_the_hardware_history_and_forecasts(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api.sqlite"))
    with TestClient(app) as c:
        now = _seed_wear(c, app)
        c.portal.call(partial(app.state.alert_engine.rollup_hardware_history, now))
        body = c.get("/api/agent/example-pc/trends", headers=_bearer(app)).json()

    # the existing keys are untouched
    assert body["agent_id"] == "example-pc" and body["battery"] is None and body["disk"] == []
    hw = body["hardware"]
    assert hw["window_days"] == 180
    (device,) = hw["devices"]
    assert device["device_key"] == "disk:S1" and device["kind"] == "disk"
    assert device["label"] == "WD_BLACK SN850X"
    points = device["series"]["percentage_used"]
    assert len(points) == 40
    assert set(points[0]) == {"day", "value"}
    assert points[0]["value"] == 70.0 and points[-1]["value"] == 89.5
    assert [p["day"] for p in points] == sorted(p["day"] for p in points)
    (forecast_row,) = hw["forecasts"]
    assert set(forecast_row) == {"device_key", "kind", "label", "reason", "symptom", "days_until"}
    assert forecast_row["reason"] == "wear_out" and forecast_row["kind"] == "disk"
    assert forecast_row["device_key"] == "disk:S1"
    assert isinstance(forecast_row["days_until"], float)
    assert "WD_BLACK SN850X" in forecast_row["symptom"]


def test_the_trends_api_hardware_is_empty_not_missing_without_history(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_empty.sqlite"))
    with TestClient(app) as c:
        body = c.get("/api/agent/nobody/trends", headers=_bearer(app)).json()
    assert body["hardware"] == {"window_days": 180, "devices": [], "forecasts": []}


def test_the_trends_api_limits_series_to_the_last_180_days_and_labels_components(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_window.sqlite"))
    now = datetime.now(timezone.utc)
    with TestClient(app) as c:
        hw = app.state.hw_history
        today = now.date()
        old = (today - timedelta(days=200)).isoformat()
        recent = (today - timedelta(days=5)).isoformat()
        c.portal.call(
            partial(
                hw.record,
                "example-pc",
                [
                    ("host:memory", "corrected_events", old, 1.0),
                    ("host:memory", "corrected_events", recent, 2.0),
                    ("fan:gone", "stall_seen", old, 0.0),
                ],
                last_day=recent,
            )
        )
        body = c.get("/api/agent/example-pc/trends", headers=_bearer(app)).json()
    devices = {d["device_key"]: d for d in body["hardware"]["devices"]}
    assert set(devices) == {"host:memory"}  # the fan only has a 200-day-old point
    assert devices["host:memory"]["kind"] == "component"
    assert devices["host:memory"]["label"] == "Memory"
    assert devices["host:memory"]["series"]["corrected_events"] == [{"day": recent, "value": 2.0}]


def test_the_fleet_overview_has_the_hardware_kpi(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_fleet.sqlite"))
    with TestClient(app) as c:
        now = _seed_wear(c, app)
        c.portal.call(partial(app.state.alert_engine.rollup_hardware_history, now))
        body = c.get("/api/fleet/overview", headers=_bearer(app)).json()
    kpi = next(k for k in body["kpis"] if k["key"] == "hardware_at_risk")
    assert kpi["value"] == 1
    assert [m["agent_id"] for m in kpi["members"]] == ["example-pc"]


def test_removing_a_host_deletes_its_hardware_history(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_purge.sqlite"))
    with TestClient(app) as c:
        now = _seed_wear(c, app)
        c.portal.call(partial(app.state.alert_engine.rollup_hardware_history, now))
        hw = app.state.hw_history
        assert c.portal.call(partial(hw.series, "example-pc", "2000-01-01"))
        resp = c.delete("/api/agent/example-pc", headers=_bearer(app))
        assert resp.status_code == 200
        assert resp.json()["purged"]["hardware_history"] == "ok"
        assert c.portal.call(partial(hw.series, "example-pc", "2000-01-01")) == {}


def test_main_wires_the_history_store_into_the_engine_and_the_prune_sweep(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "wiring.sqlite"))
    assert isinstance(app.state.hw_history, HardwareHistoryStore)
    keys = {id(s): key for s, key in app.state.alert_engine._prunables}
    assert keys[id(app.state.hw_history)] == "KENNY_HW_HISTORY_RETENTION_DAYS"
    assert app.state.alert_engine._hw_history is app.state.hw_history
    # the history has its own window, not the snapshots'
    assert keys[id(app.state.hw_history)] != keys[id(app.state.store)]


# -- an unreadable history never takes the rest down ----------------------------------------


async def _broken(*args, **kw):
    raise RuntimeError("history store is down")


def test_the_trends_api_survives_an_unreadable_history(tmp_path, monkeypatch, caplog) -> None:
    app = build_app(db_path=str(tmp_path / "api_broken.sqlite"))
    with TestClient(app) as c:
        now = datetime.now(timezone.utc)
        for i in range(6):  # a filling volume: the disk forecast has something to say
            snap = {"disk": {"volumes": [{"mount": "C:", "percent_used": 50.0 + 5 * i}]}}
            at = now - timedelta(days=5 - i)
            c.portal.call(partial(app.state.store.insert, "example-pc", at.isoformat(), snap))
        monkeypatch.setattr(app.state.hw_history, "series", _broken)
        with caplog.at_level("WARNING", logger="kenny.webui"):
            resp = c.get("/api/agent/example-pc/trends", headers=_bearer(app))
    assert resp.status_code == 200
    body = resp.json()
    assert body["hardware"] == hardware_history.empty_payload()
    assert [v["mount"] for v in body["disk"]] == ["C:"]
    assert body["battery"] is None
    assert any("hardware history unavailable" in r.message for r in caplog.records)


async def test_the_digest_survives_an_unreadable_history(stores, monkeypatch, caplog) -> None:
    store, events, _, hw = stores
    await put_wear_history(store)
    await hardware_history.rollup_all(store, hw, now=NOW)
    healthy = (await build_digest(store, events, FakeRegistry(), now=NOW, hw_history=hw)).body
    assert "hardware at risk:" in healthy

    monkeypatch.setattr(hw, "series", _broken)
    with caplog.at_level("WARNING", logger="kenny.digest"):
        body = (await build_digest(store, events, FakeRegistry(), now=NOW, hw_history=hw)).body
    assert "hardware at risk" not in body
    assert body.splitlines()[0] == healthy.splitlines()[0]  # the rest is intact
    assert any("hardware forecast failed" in r.message for r in caplog.records)


# -- the boot-time rollup runs in the background ------------------------------------------


def test_startup_does_not_wait_for_the_first_rollup(tmp_path, monkeypatch) -> None:
    import threading

    started = threading.Event()

    async def never_finishes(*args, **kw) -> int:
        started.set()
        await asyncio.sleep(3600)
        return 0

    monkeypatch.setattr(hardware_history, "rollup_all", never_finishes)
    app = build_app(db_path=str(tmp_path / "boot.sqlite"))
    # Entering the context returns only once the lifespan has finished starting;
    # an awaited rollup would hang it here.
    with TestClient(app):
        assert started.wait(5), "the boot-time rollup never started"
    # and leaving it cancels the rollup instead of waiting it out


async def test_the_boot_rollup_precedes_the_snapshot_prune_and_the_first_tick_skips_its_own(
    stores, monkeypatch
) -> None:
    store, _, _, _ = stores
    calls: list[str] = []

    async def rollup_all(*args, **kw) -> int:
        calls.append("rollup")
        return 0

    real_prune = store.prune

    async def prune(*args, **kw):
        calls.append("prune")
        return await real_prune(*args, **kw)

    monkeypatch.setattr(hardware_history, "rollup_all", rollup_all)
    monkeypatch.setattr(store, "prune", prune)
    engine = make_engine(stores, prunables=[(store, None)])
    engine.start_startup_maintenance()
    await engine._maybe_prune(NOW)  # the alert loop's first pass waits for it ...
    assert calls == ["rollup", "prune", "prune"]  # ... and does not roll up again
    await engine._maybe_prune(NOW + timedelta(hours=25))
    assert calls.count("rollup") == 2  # later passes roll up as usual


async def test_without_a_boot_task_the_first_pass_rolls_up(stores, monkeypatch) -> None:
    calls: list[str] = []

    async def rollup_all(*args, **kw) -> int:
        calls.append("rollup")
        return 0

    monkeypatch.setattr(hardware_history, "rollup_all", rollup_all)
    engine = make_engine(stores)
    await engine._maybe_prune(NOW)
    assert calls == ["rollup"]


# -- forecasts are cached until the history changes ---------------------------------------


class _Spy:
    """Counts the reads that make a forecast expensive."""

    def __init__(self, monkeypatch, store, hw) -> None:
        self.series = self.latest = 0
        real_series, real_latest = hw.series, store.latest

        async def series(*args, **kw):
            self.series += 1
            return await real_series(*args, **kw)

        async def latest(*args, **kw):
            self.latest += 1
            return await real_latest(*args, **kw)

        monkeypatch.setattr(hw, "series", series)
        monkeypatch.setattr(store, "latest", latest)


async def test_a_second_forecast_without_a_new_rollup_does_not_touch_the_store(
    stores, monkeypatch
) -> None:
    store, _, _, hw = stores
    engine = make_engine(stores)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    snap = (await store.latest("pc1"))["snapshot"]
    spy = _Spy(monkeypatch, store, hw)

    first = await engine.hardware_forecasts("pc1", NOW, snapshot=snap)
    assert first and spy.series == 1
    assert spy.latest == 0  # the caller's snapshot named the devices
    assert await engine.hardware_forecasts("pc1", NOW, snapshot=snap) == first
    assert await engine.hardware_forecasts("pc1", NOW + timedelta(hours=3)) == first
    assert (spy.series, spy.latest) == (1, 0)


async def test_every_snapshot_after_the_first_reuses_the_cached_forecast(stores, monkeypatch) -> None:
    store, _, _, hw = stores
    engine = make_engine(stores)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    await engine.evaluate_once(NOW)
    spy = _Spy(monkeypatch, store, hw)
    await put(store, wearing(90), NOW + timedelta(hours=1))
    await engine.evaluate_once(NOW + timedelta(hours=2))  # a new snapshot, no new rollup
    assert spy.series == 0


async def test_describe_reuses_the_forecast_cache(stores, monkeypatch) -> None:
    store, _, _, hw = stores
    engine = make_engine(stores)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    await engine.evaluate_once(NOW)
    spy = _Spy(monkeypatch, store, hw)
    lines = await engine._describe("pc1", {"hardware_forecast": {"since": NOW.isoformat()}}, NOW)
    assert lines and "write endurance" in lines[0][2]
    assert spy.series == 0


async def test_a_new_rollup_or_a_new_day_refreshes_the_forecast(stores, monkeypatch) -> None:
    store, _, _, hw = stores
    engine = make_engine(stores)
    await put_wear_history(store)
    await engine.rollup_hardware_history(NOW)
    spy = _Spy(monkeypatch, store, hw)
    await engine.hardware_forecasts("pc1", NOW)
    assert spy.series == 1
    await put(store, wearing(95), NOW + timedelta(hours=1))
    await engine.rollup_hardware_history(NOW + timedelta(hours=2))  # record() bumps the version
    await engine.hardware_forecasts("pc1", NOW + timedelta(hours=2))
    assert spy.series == 2
    await engine.hardware_forecasts("pc1", NOW + timedelta(days=1))  # the UTC day turned
    assert spy.series == 3
    await hw.delete_agent("pc1")
    assert await engine.hardware_forecasts("pc1", NOW + timedelta(days=1)) == []
    assert spy.series == 4

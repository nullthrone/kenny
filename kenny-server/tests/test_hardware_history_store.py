"""``HardwareHistoryStore`` (ADR-0070): schema, upsert, series, prune, cascade, backup."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import pytest

from kenny_server import config, inventory
from kenny_server.backup import BackupManager
from kenny_server.store import (
    HW_HISTORY_RETENTION_DAYS,
    BackupTargetStore,
    HardwareHistoryStore,
    TelemetryStore,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def hw(tmp_path):
    store = HardwareHistoryStore(str(tmp_path / "kenny.sqlite"))
    await store.connect()
    yield store
    await store.close()


def row(key, metric, day, value):
    return (key, metric, day, value)


async def test_the_schema_is_a_without_rowid_table_keyed_on_agent_device_metric_day(hw) -> None:
    con = sqlite3.connect(hw.db_path)
    try:
        (sql,) = con.execute("SELECT sql FROM sqlite_master WHERE name = 'hw_metrics'").fetchone()
        cols = [r[1] for r in con.execute("PRAGMA table_info(hw_metrics)")]
        pk = [r[1] for r in sorted(con.execute("PRAGMA table_info(hw_metrics)"), key=lambda r: r[5]) if r[5]]
        state = [r[1] for r in con.execute("PRAGMA table_info(hw_rollup_state)")]
    finally:
        con.close()
    assert "WITHOUT ROWID" in sql
    assert cols == ["agent_id", "device_key", "metric", "day", "value"]
    assert pk == ["agent_id", "device_key", "metric", "day"]
    assert state == ["agent_id", "last_day"]


async def test_connecting_twice_and_reopening_keeps_the_data(tmp_path) -> None:
    path = str(tmp_path / "kenny.sqlite")
    first = HardwareHistoryStore(path)
    await first.connect()
    await first.connect()  # idempotent
    await first.record("pc1", [row("disk:S", "media_errors", "2026-06-30", 1.0)], last_day="2026-06-30")
    await first.close()
    second = HardwareHistoryStore(path)
    await second.connect()
    try:
        assert await second.series("pc1", "2026-01-01") == {
            "disk:S": {"media_errors": [("2026-06-30", 1.0)]}
        }
    finally:
        await second.close()


async def test_using_the_store_before_connect_is_a_clear_error() -> None:
    with pytest.raises(RuntimeError, match="not connected"):
        await HardwareHistoryStore().series("pc1", "2026-01-01")


async def test_record_upserts_and_the_same_rows_are_idempotent(hw) -> None:
    rows = [row("disk:S", "percentage_used", "2026-06-29", 10.0)]
    assert await hw.record("pc1", rows, last_day="2026-06-29") == 1
    assert await hw.record("pc1", rows, last_day="2026-06-29") == 1
    assert await hw.record(
        "pc1", [row("disk:S", "percentage_used", "2026-06-29", 11.0)], last_day="2026-06-29"
    ) == 1
    assert (await hw.series("pc1", "2026-01-01"))["disk:S"]["percentage_used"] == [("2026-06-29", 11.0)]


async def test_series_is_grouped_ascending_and_bounded_by_since_day(hw) -> None:
    await hw.record(
        "pc1",
        [
            row("disk:S", "percentage_used", "2026-06-30", 3.0),
            row("disk:S", "percentage_used", "2026-06-28", 1.0),
            row("disk:S", "percentage_used", "2026-06-29", 2.0),
            row("disk:S", "media_errors", "2026-06-29", 0.0),
            row("fan:f1", "stall_seen", "2026-06-29", 0.0),
        ],
        last_day="2026-06-30",
    )
    got = await hw.series("pc1", "2026-06-01")
    assert got == {
        "disk:S": {
            "media_errors": [("2026-06-29", 0.0)],
            "percentage_used": [("2026-06-28", 1.0), ("2026-06-29", 2.0), ("2026-06-30", 3.0)],
        },
        "fan:f1": {"stall_seen": [("2026-06-29", 0.0)]},
    }
    later = await hw.series("pc1", "2026-06-29")
    assert later["disk:S"]["percentage_used"] == [("2026-06-29", 2.0), ("2026-06-30", 3.0)]
    assert await hw.series("pc1", "2026-07-01") == {}
    assert await hw.series("unknown", "2026-01-01") == {}
    # a full timestamp is accepted for the bound
    assert await hw.series("pc1", "2026-06-30T05:00:00Z") == {
        "disk:S": {"percentage_used": [("2026-06-30", 3.0)]}
    }


async def test_agents_do_not_see_each_others_series(hw) -> None:
    await hw.record("a", [row("disk:S", "m", "2026-06-30", 1.0)], last_day="2026-06-30")
    await hw.record("b", [row("disk:S", "m", "2026-06-30", 2.0)], last_day="2026-06-29")
    assert (await hw.series("a", "2026-01-01"))["disk:S"]["m"] == [("2026-06-30", 1.0)]
    assert (await hw.series("b", "2026-01-01"))["disk:S"]["m"] == [("2026-06-30", 2.0)]
    assert await hw.last_day("a") == "2026-06-30"
    assert await hw.last_day("b") == "2026-06-29"
    assert await hw.last_day("c") is None


async def test_a_failed_record_changes_neither_rows_nor_state(hw) -> None:
    await hw.record("pc1", [row("disk:S", "m", "2026-06-28", 1.0)], last_day="2026-06-28")
    with pytest.raises(Exception):
        # a value the column cannot hold fails mid-transaction
        await hw.record(
            "pc1",
            [row("disk:S", "m", "2026-06-29", 2.0), row("disk:S", "m", "2026-06-30", None)],  # type: ignore[arg-type]
            last_day="2026-06-30",
        )
    assert await hw.last_day("pc1") == "2026-06-28"
    assert (await hw.series("pc1", "2026-01-01"))["disk:S"]["m"] == [("2026-06-28", 1.0)]
    # and the connection is usable afterwards (the transaction was rolled back)
    await hw.record("pc1", [row("disk:S", "m", "2026-06-29", 2.0)], last_day="2026-06-29")
    assert await hw.last_day("pc1") == "2026-06-29"


async def test_prune_drops_rows_past_the_retention_and_keeps_the_state(hw) -> None:
    await hw.record(
        "pc1",
        [
            row("disk:S", "m", "2024-06-30", 1.0),  # 731 days before NOW
            row("disk:S", "m", "2024-07-02", 2.0),  # 729 days
            row("disk:S", "m", "2026-06-30", 3.0),
        ],
        last_day="2026-06-30",
    )
    assert await hw.prune(now=NOW) == 1  # the default is two years
    assert [d for d, _ in (await hw.series("pc1", "2000-01-01"))["disk:S"]["m"]] == [
        "2024-07-02",
        "2026-06-30",
    ]
    assert await hw.prune(now=NOW, retention_days=30) == 1
    assert await hw.prune(now=NOW, retention_days=30) == 0
    assert await hw.last_day("pc1") == "2026-06-30"


async def test_the_default_retention_is_two_years_and_matches_the_setting() -> None:
    assert HW_HISTORY_RETENTION_DAYS == 730
    assert HardwareHistoryStore().retention_days == 730
    spec = config.CATALOG["KENNY_HW_HISTORY_RETENTION_DAYS"]
    assert spec.lifecycle == "live"
    assert spec.min == 30
    assert spec.parse(spec.default_raw) == HW_HISTORY_RETENTION_DAYS
    with pytest.raises(ValueError):
        spec.validate("29")


async def test_delete_agent_removes_rows_and_state_for_that_agent_only(hw) -> None:
    await hw.record("a", [row("disk:S", "m", "2026-06-30", 1.0)], last_day="2026-06-30")
    await hw.record("b", [row("disk:S", "m", "2026-06-30", 2.0)], last_day="2026-06-30")
    assert await hw.delete_agent("a") == 1
    assert await hw.series("a", "2000-01-01") == {}
    assert await hw.last_day("a") is None
    assert await hw.series("b", "2000-01-01") != {}
    assert await hw.last_day("b") == "2026-06-30"
    assert await hw.delete_agent("a") == 0


async def test_removing_a_host_from_inventory_cascades_into_the_history(hw) -> None:
    class _Ok:
        async def delete_agent(self, agent_id):
            return 0

        async def delete(self, agent_id):
            return None

        async def purge_host(self, agent_id):
            return None

        def remove(self, agent_id):
            return False

        def forget(self, agent_id):
            return None

    ok = _Ok()
    await hw.record("pc1", [row("disk:S", "m", "2026-06-30", 1.0)], last_day="2026-06-30")
    result = await inventory.purge_agent(
        "pc1",
        registry=ok,
        store=ok,
        event_store=ok,
        alert_state=ok,
        token_store=ok,
        key_store=ok,
        webfilter_store=ok,
        user_store=ok,
        screenshots=ok,
        hw_history=hw,
    )
    assert result["hardware_history"] == "ok"
    assert await hw.series("pc1", "2000-01-01") == {}
    assert await hw.last_day("pc1") is None


async def test_the_backup_carries_the_hardware_history(tmp_path) -> None:
    db = str(tmp_path / "kenny.sqlite")
    telemetry = TelemetryStore(db)
    hw = HardwareHistoryStore(db)
    await telemetry.connect()
    await hw.connect()
    await hw.record(
        "pc1", [row("disk:S", "percentage_used", "2026-06-30", 42.0)], last_day="2026-06-30"
    )
    targets = BackupTargetStore(db)
    await targets.connect()
    try:
        mgr = BackupManager(db, targets)
        result = await mgr.create("manual")
        assert result["integrity"] == "ok"
        restored = HardwareHistoryStore(os.path.join(mgr.backup_dir, result["name"]))
        await restored.connect()
        try:
            assert await restored.series("pc1", "2000-01-01") == {
                "disk:S": {"percentage_used": [("2026-06-30", 42.0)]}
            }
            assert await restored.last_day("pc1") == "2026-06-30"
        finally:
            await restored.close()
    finally:
        await targets.close()
        await hw.close()
        await telemetry.close()


async def test_snapshots_for_day_returns_only_that_days_requested_sections(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "kenny.sqlite"))
    await store.connect()
    try:
        big = {"disk_smart": {"disks": [{"serial": "S"}]}, "processes": {"list": ["x"] * 50}}
        await store.insert("pc1", "2026-06-29T23:59:59Z", {"disk_smart": {"disks": []}})
        await store.insert("pc1", "2026-06-30T00:00:00+00:00", big)
        await store.insert("pc1", "2026-06-30T12:00:00Z", {"gpu": {"gpus": []}})
        await store.insert("pc1", "2026-07-01T00:00:00Z", {"disk_smart": {"disks": []}})
        await store.insert("pc2", "2026-06-30T10:00:00Z", {"disk_smart": {"disks": []}})
        got = await store.snapshots_for_day("pc1", "2026-06-30", ("disk_smart", "gpu"))
        assert [r["collected_at"] for r in got] == [
            "2026-06-30T00:00:00+00:00",
            "2026-06-30T12:00:00Z",
        ]
        assert got[0]["snapshot"] == {"disk_smart": {"disks": [{"serial": "S"}]}, "gpu": None}
        assert got[1]["snapshot"] == {"disk_smart": None, "gpu": {"gpus": []}}
        assert await store.snapshots_for_day("pc1", "2026-06-15", ("gpu",)) == []
    finally:
        await store.close()

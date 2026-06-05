"""Tests for :class:`kenny_server.store.EventStore`.

Covers ``insert_log``/``insert_audit``, ``query`` filters (agent/level/kind),
and the 30-day ``prune`` retention window. Uses a temp DB path per test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.store import EventStore


@pytest.fixture
async def events(tmp_path) -> EventStore:
    es = EventStore(db_path=str(tmp_path / "events.sqlite"))
    await es.connect()
    yield es
    await es.close()


async def test_insert_log_round_trip(events: EventStore) -> None:
    await events.insert_log(
        source="agent",
        at="2026-06-04T18:00:01Z",
        level="warn",
        target="kenny_agent::tunnel",
        message="tunnel error; backing off",
        agent_id="papa-pc",
        fields={"error": "connection reset", "backoff_secs": 4},
    )
    rows = await events.query(kind="log")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "agent"
    assert row["level"] == "warn"
    assert row["kind"] == "log"
    assert row["target"] == "kenny_agent::tunnel"
    assert row["message"] == "tunnel error; backing off"
    assert row["agent_id"] == "papa-pc"
    assert row["fields"] == {"error": "connection reset", "backoff_secs": 4}
    assert row["tool"] is None
    assert row["ok"] is None


async def test_insert_log_without_fields(events: EventStore) -> None:
    await events.insert_log(
        source="server",
        at="2026-06-04T18:00:02Z",
        level="info",
        message="agent dev connected",
    )
    rows = await events.query(kind="log")
    assert rows[0]["fields"] is None
    assert rows[0]["agent_id"] is None


async def test_insert_audit_round_trip(events: EventStore) -> None:
    await events.insert_audit(agent_id="papa-pc", tool="winget_update", ok=True)
    await events.insert_audit(
        agent_id="papa-pc", tool="powershell_exec", ok=False, error="timeout"
    )
    rows = await events.query(kind="audit")
    assert len(rows) == 2
    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["winget_update"]["ok"] is True
    assert by_tool["winget_update"]["source"] == "server"
    assert by_tool["powershell_exec"]["ok"] is False
    assert by_tool["powershell_exec"]["error"] == "timeout"


async def test_query_filters(events: EventStore) -> None:
    await events.insert_log(
        source="agent", at="2026-06-04T18:00:01Z", level="warn",
        message="w", agent_id="a1",
    )
    await events.insert_log(
        source="agent", at="2026-06-04T18:00:02Z", level="error",
        message="e", agent_id="a1",
    )
    await events.insert_log(
        source="agent", at="2026-06-04T18:00:03Z", level="info",
        message="i", agent_id="a2",
    )
    await events.insert_audit(agent_id="a1", tool="fs_list", ok=True)

    assert len(await events.query(agent_id="a1")) == 3
    assert len(await events.query(agent_id="a2")) == 1
    assert len(await events.query(level="error")) == 1
    assert len(await events.query(kind="log")) == 3
    assert len(await events.query(kind="audit")) == 1
    # Combined filters.
    combined = await events.query(agent_id="a1", level="warn", kind="log")
    assert len(combined) == 1
    assert combined[0]["message"] == "w"


async def test_query_newest_first_and_limit(events: EventStore) -> None:
    for i in range(5):
        await events.insert_log(
            source="server",
            at=f"2026-06-04T18:00:0{i}Z",
            level="info",
            message=f"m{i}",
        )
    rows = await events.query(limit=3)
    assert [r["message"] for r in rows] == ["m4", "m3", "m2"]


async def test_prune_retention(events: EventStore) -> None:
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    recent = (now - timedelta(days=5)).isoformat()
    await events.insert_log(source="server", at=old, level="info", message="old")
    await events.insert_log(source="server", at=recent, level="info", message="recent")

    deleted = await events.prune(now=now)
    assert deleted == 1
    rows = await events.query()
    assert len(rows) == 1
    assert rows[0]["message"] == "recent"

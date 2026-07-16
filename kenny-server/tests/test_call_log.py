"""Tests for :class:`kenny_server.tools.CallLog`.

Covers the in-memory fallback and, most importantly, that a transient
``EventStore`` write failure (e.g. sqlite "database is locked" under
concurrent agent pushes) is swallowed rather than propagated — a failing
audit-log write must never fail the tool call that already succeeded.
"""

from __future__ import annotations

import pytest

from kenny_server.store import EventStore
from kenny_server.tools import CallLog


@pytest.fixture
async def events(tmp_path) -> EventStore:
    es = EventStore(db_path=str(tmp_path / "events.sqlite"))
    await es.connect()
    yield es
    await es.close()


async def test_record_without_event_store_uses_memory() -> None:
    log = CallLog()
    await log.record("example-pc", "screen_capture", {}, ok=True)
    entries = await log.list()
    assert len(entries) == 1
    assert entries[0]["tool"] == "screen_capture"
    assert entries[0]["ok"] is True


async def test_record_persists_to_event_store(events: EventStore) -> None:
    log = CallLog(event_store=events)
    await log.record("example-pc", "screen_capture", {}, ok=True)
    entries = await log.list()
    assert len(entries) == 1
    assert entries[0]["tool"] == "screen_capture"


async def test_record_swallows_event_store_failure(events: EventStore, monkeypatch) -> None:
    log = CallLog(event_store=events)

    async def boom(**kwargs):
        raise Exception("database is locked")

    monkeypatch.setattr(events, "insert_audit", boom)

    # Must not raise: the tool call this logs already succeeded/failed on its
    # own terms, and a broken audit write is not grounds to break the caller.
    await log.record("example-pc", "screen_capture", {}, ok=True)

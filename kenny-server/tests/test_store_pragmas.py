"""Regression tests for the shared SQLite connection settings.

Every store opens its own connection to the *same* file. If any of them omits
``busy_timeout`` (SQLite default: 0), momentary write contention raises
``OperationalError: database is locked`` instead of waiting — which previously
propagated out of the agent tunnel and flapped every agent's WebSocket. These
tests pin WAL + a non-zero busy_timeout on every store, and prove a second
writer waits for a held lock instead of failing immediately.
"""

from __future__ import annotations

import asyncio

import pytest

from kenny_server.store import (
    AlertStateStore,
    BackupTargetStore,
    ChatHistoryStore,
    EventStore,
    PolicyStore,
    TelemetryStore,
    WebFilterStore,
)

ALL_STORES = [
    TelemetryStore,
    EventStore,
    AlertStateStore,
    PolicyStore,
    WebFilterStore,
    ChatHistoryStore,
    BackupTargetStore,
]


@pytest.mark.parametrize("store_cls", ALL_STORES, ids=lambda c: c.__name__)
async def test_store_sets_wal_and_busy_timeout(store_cls, tmp_path) -> None:
    store = store_cls(db_path=str(tmp_path / "kenny.sqlite"))
    await store.connect()
    try:
        async with store._conn.execute("PRAGMA busy_timeout") as cur:
            busy_timeout = (await cur.fetchone())[0]
        async with store._conn.execute("PRAGMA journal_mode") as cur:
            journal_mode = (await cur.fetchone())[0]
    finally:
        await store.close()

    assert busy_timeout > 0, f"{store_cls.__name__} left busy_timeout at 0 (locks fail instantly)"
    assert journal_mode.lower() == "wal", f"{store_cls.__name__} is not in WAL mode"


async def test_insert_waits_out_a_held_write_lock(tmp_path) -> None:
    """A telemetry insert must not raise while another connection holds the lock.

    This is the exact failure that tore down the agent tunnel: with busy_timeout=0
    the insert raised ``database is locked`` immediately. With the configured
    timeout it blocks until the holder commits, then succeeds.
    """

    db = str(tmp_path / "kenny.sqlite")
    writer = TelemetryStore(db_path=db)
    inserter = TelemetryStore(db_path=db)
    await writer.connect()
    await inserter.connect()
    try:
        # Hold an exclusive write transaction open on the first connection.
        await writer._conn.execute("BEGIN IMMEDIATE")
        await writer._conn.execute(
            "INSERT INTO snapshots (agent_id, collected_at, received_at, snapshot) "
            "VALUES ('holder', '2026-07-04T00:00:00Z', '2026-07-04T00:00:00Z', '{}')"
        )

        async def _release_after(delay: float) -> None:
            await asyncio.sleep(delay)
            await writer._conn.commit()

        # The second insert must block on the lock and then succeed once the
        # holder commits — never raise OperationalError. busy_timeout (5s default)
        # comfortably covers the 0.2s hold.
        release = asyncio.create_task(_release_after(0.2))
        # Must not raise OperationalError("database is locked"):
        await inserter.insert("example-pc", "2026-07-04T00:00:01Z", {"cpu": {"load": 1}})
        await release
        assert await inserter.latest("example-pc") is not None
    finally:
        await inserter.close()
        await writer.close()

"""SQLite telemetry store (aiosqlite).

Persists pushed snapshots with ~30-day retention. Provides a ``latest``
accessor (most recent snapshot per agent), a ``history`` accessor (time series
for the drill-down trend), and a ``prune`` retention helper. The DB path is
configurable; the default ``kenny.sqlite`` is gitignored.

See ADR 0007 for the push-model + SQLite rationale.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

DEFAULT_DB_PATH = "kenny.sqlite"
RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    snapshot     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_agent_time
    ON snapshots (agent_id, collected_at DESC);
"""


class TelemetryStore:
    """Async SQLite-backed store for telemetry snapshots."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = RETENTION_DAYS) -> None:
        self.db_path = db_path
        self.retention_days = retention_days
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("TelemetryStore is not connected; call connect() first")
        return self._db

    async def insert(
        self,
        agent_id: str,
        collected_at: str,
        snapshot: dict[str, Any],
        *,
        received_at: str | None = None,
    ) -> None:
        """Store a snapshot for an agent."""

        received_at = received_at or datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO snapshots (agent_id, collected_at, received_at, snapshot) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, collected_at, received_at, json.dumps(snapshot)),
        )
        await self._conn.commit()

    async def latest(self, agent_id: str) -> dict[str, Any] | None:
        """Return the most recent stored snapshot for an agent, or None."""

        async with self._conn.execute(
            "SELECT agent_id, collected_at, received_at, snapshot FROM snapshots "
            "WHERE agent_id = ? ORDER BY collected_at DESC, id DESC LIMIT 1",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_record(row) if row else None

    async def history(self, agent_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return up to ``limit`` recent snapshots for an agent, newest first."""

        async with self._conn.execute(
            "SELECT agent_id, collected_at, received_at, snapshot FROM snapshots "
            "WHERE agent_id = ? ORDER BY collected_at DESC, id DESC LIMIT ?",
            (agent_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def known_agents(self) -> list[str]:
        """Return distinct agent_ids that have stored snapshots."""

        async with self._conn.execute(
            "SELECT DISTINCT agent_id FROM snapshots ORDER BY agent_id"
        ) as cur:
            rows = await cur.fetchall()
        return [r["agent_id"] for r in rows]

    async def prune(self, *, now: datetime | None = None) -> int:
        """Delete snapshots older than the retention window. Returns rows deleted."""

        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=self.retention_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM snapshots WHERE collected_at < ?", (cutoff,)
        )
        await self._conn.commit()
        return cur.rowcount or 0

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "agent_id": row["agent_id"],
            "collected_at": row["collected_at"],
            "received_at": row["received_at"],
            "snapshot": json.loads(row["snapshot"]),
        }

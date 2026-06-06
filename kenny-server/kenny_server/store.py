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


_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    agent_id  TEXT,
    source    TEXT NOT NULL,
    level     TEXT,
    kind      TEXT NOT NULL,
    tool      TEXT,
    ok        INTEGER,
    error     TEXT,
    target    TEXT,
    message   TEXT,
    fields    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time
    ON events (at DESC);
CREATE INDEX IF NOT EXISTS idx_events_agent_time
    ON events (agent_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind_time
    ON events (kind, at DESC);
"""


class EventStore:
    """Async SQLite-backed store for log lines and tool-call audit events.

    Shares the same database file as :class:`TelemetryStore` but owns its own
    connection. ``source`` is ``'server'`` or ``'agent'``; ``kind`` is ``'log'``
    or ``'audit'``. Retention mirrors the snapshot store (~30 days).
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = RETENTION_DAYS) -> None:
        self.db_path = db_path
        self.retention_days = retention_days
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        # Two connections share one file; WAL keeps readers/writers from blocking.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_EVENTS_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("EventStore is not connected; call connect() first")
        return self._db

    async def insert_log(
        self,
        *,
        source: str,
        at: str,
        level: str,
        target: str | None = None,
        message: str,
        agent_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Store a structured log line (kind='log')."""

        await self._conn.execute(
            "INSERT INTO events (at, agent_id, source, level, kind, target, message, fields) "
            "VALUES (?, ?, ?, ?, 'log', ?, ?, ?)",
            (
                at,
                agent_id,
                source,
                level,
                target,
                message,
                json.dumps(fields) if fields is not None else None,
            ),
        )
        await self._conn.commit()

    async def insert_audit(
        self,
        *,
        agent_id: str,
        tool: str,
        ok: bool,
        error: str | None = None,
        at: str | None = None,
    ) -> None:
        """Store a forwarded tool-call audit event (kind='audit', source='server')."""

        at = at or datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO events (at, agent_id, source, kind, tool, ok, error) "
            "VALUES (?, ?, 'server', 'audit', ?, ?, ?)",
            (at, agent_id, tool, 1 if ok else 0, error),
        )
        await self._conn.commit()

    async def query(
        self,
        *,
        agent_id: str | None = None,
        level: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return matching events newest-first as a list of dicts."""

        clauses: list[str] = []
        params: list[Any] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if level is not None:
            clauses.append("level = ?")
            params.append(level)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._conn.execute(
            "SELECT at, agent_id, source, level, kind, tool, ok, error, target, message, fields "
            f"FROM events {where} ORDER BY at DESC, id DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    async def prune(self, *, now: datetime | None = None) -> int:
        """Delete events older than the retention window. Returns rows deleted."""

        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=self.retention_days)).isoformat()
        cur = await self._conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        await self._conn.commit()
        return cur.rowcount or 0

    @staticmethod
    def _row_to_event(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "at": row["at"],
            "agent_id": row["agent_id"],
            "source": row["source"],
            "level": row["level"],
            "kind": row["kind"],
            "tool": row["tool"],
            "ok": None if row["ok"] is None else bool(row["ok"]),
            "error": row["error"],
            "target": row["target"],
            "message": row["message"],
            "fields": json.loads(row["fields"]) if row["fields"] is not None else None,
        }


_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_policy_rules (
    id          TEXT PRIMARY KEY,
    applies_to  TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operator_policy_created
    ON operator_policy_rules (created_at);
"""


class PolicyStore:
    """Async SQLite-backed store for the operator's append-only deny rules.

    Persists ONLY operator additions (ADR-0021); built-in rules live in the
    shared catalog and are never stored here. "Append-only" means operator rules
    can never weaken the built-ins — operators may still add/remove their own
    entries. Shares the same database file as the other stores (own connection).
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_POLICY_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("PolicyStore is not connected; call connect() first")
        return self._db

    async def list(self) -> list[dict[str, Any]]:
        """Return operator rules (id/applies_to/pattern/reason), oldest-first."""

        async with self._conn.execute(
            "SELECT id, applies_to, pattern, reason FROM operator_policy_rules "
            "ORDER BY created_at, id"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "applies_to": r["applies_to"],
                "pattern": r["pattern"],
                "reason": r["reason"],
            }
            for r in rows
        ]

    async def add(
        self, *, id: str, applies_to: str, pattern: str, reason: str
    ) -> None:
        """Insert (or replace) an operator rule, stamping ``created_at``."""

        created_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR REPLACE INTO operator_policy_rules "
            "(id, applies_to, pattern, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (id, applies_to, pattern, reason, created_at),
        )
        await self._conn.commit()

    async def remove(self, id: str) -> bool:
        """Delete one operator rule by id. Returns True if a row was removed."""

        cur = await self._conn.execute(
            "DELETE FROM operator_policy_rules WHERE id = ?", (id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

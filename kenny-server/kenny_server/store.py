"""SQLite telemetry store (aiosqlite).

Persists pushed snapshots with ~30-day retention. Provides a ``latest``
accessor (most recent snapshot per agent), a ``history`` accessor (time series
for the drill-down trend), and a ``prune`` retention helper. The DB path is
configurable; the default ``kenny.sqlite`` is gitignored.

See ADR 0007 for the push-model + SQLite rationale.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

import aiosqlite

DEFAULT_DB_PATH = "kenny.sqlite"
RETENTION_DAYS = 30
# TelemetryStore's own default (ADR-0056), deliberately its own constant and
# not a reuse of RETENTION_DAYS above: KENNY_TELEMETRY_RETENTION_DAYS's catalog
# default is asserted against this one specifically, so changing the shared
# constant EventStore/WebFilterStore still fall back to cannot silently move
# telemetry retention (or vice versa) without the seam test in test_config.py
# catching it.
TELEMETRY_RETENTION_DAYS = 30

# Every store opens its own aiosqlite connection to the *same* file. WAL lets
# readers and writers proceed concurrently; ``busy_timeout`` makes a connection
# wait up to N ms for a contended lock instead of failing immediately with
# "database is locked" (SQLite's default busy_timeout is 0). Without it, any
# momentary write contention on the shared file raised OperationalError out of a
# telemetry INSERT and tore down the agent WebSocket tunnel (connection flapping).
# 20000 (not SQLite's usual "a couple seconds" folk-default): this DB carries
# ~90 KB telemetry blobs across ~16 long-lived connections plus several
# background writers (alert engine, log drain, ticket sweep); under real
# contention a short timeout raises well before a queued writer gets its turn.
_BUSY_TIMEOUT_MS = int(os.environ.get("KENNY_SQLITE_BUSY_TIMEOUT_MS", "20000"))


async def _configure_connection(db: aiosqlite.Connection) -> None:
    """Apply the connection settings every store shares (row factory + pragmas).

    Cursors are explicitly closed (rather than left to the garbage collector)
    so no statement is left "in progress" on the connection — a lingering
    unclosed PRAGMA cursor is otherwise enough to make a later ``VACUUM``/
    ``VACUUM INTO`` on the same connection fail with "SQL statements in
    progress" (see :mod:`kenny_server.backup`).

    ``synchronous=NORMAL`` trades one specific, bounded risk for materially
    shorter write transactions: in WAL mode NORMAL still guarantees the
    database can never be *corrupted*, but a commit no longer fsyncs before
    returning, so the most recent commit(s) can be lost if the **OS/host**
    crashes or loses power outright (a killed/crashed *process* is fine — the
    WAL survives that). Worth it here: every write is competing for the same
    single-writer file, and FULL's fsync-per-commit was directly widening the
    contention window this module exists to avoid. ADR-0043's backups cover
    the durability gap this leaves.
    """

    db.row_factory = aiosqlite.Row
    async with db.execute("PRAGMA journal_mode=WAL"):
        pass
    async with db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}"):
        pass
    async with db.execute("PRAGMA synchronous=NORMAL"):
        pass


# -- process-wide write serialization (ADR-0056) ------------------------------
#
# SQLite already allows only one writer at a time; ``busy_timeout`` bounds how
# long a second writer waits for that single slot before raising. That bound
# was still not enough: a connection that has *no* ``busy_timeout`` (see the
# five stores this module's docstring above used to omit it for) can leave a
# multi-statement write half-finished — the driver never rolls back on error —
# and hold the WAL writer lock indefinitely, so every other writer's full
# timeout elapses and it raises "database is locked" anyway. Fixing that gap
# closes the trigger; this lock removes the blast radius: this process's own
# writers queue on an ``asyncio.Lock`` instead of racing SQLite's single-writer
# slot, so a stuck or merely slow writer delays the next one rather than
# starving it out from under a busy_timeout.
#
# Two properties that are easy to get wrong:
#
# * **Re-entrant per task.** ``ticketstore._insert_event`` writes on its
#   caller's transaction and is called *inside* other write methods
#   (``set_state``, ``set_agent_id``, ``append_event``). A plain
#   ``asyncio.Lock`` taken at both levels self-deadlocks. Re-entrancy is keyed
#   on ``asyncio.current_task()`` identity, not a ``ContextVar`` — a task
#   spawned *while* the lock is held inherits the parent's context, and a
#   ContextVar flag would make that child believe it already holds the lock.
# * **Bound to one event loop.** ``asyncio.Lock`` binds to the loop of its
#   first ``acquire`` and raises if reused from another (pytest-asyncio gives
#   each test its own loop). State lives in a ``WeakKeyDictionary`` keyed by
#   the running loop instead of a single module-level lock.
#
# The rule that makes the ticket-store case the *only* nesting, and therefore
# keeps this deadlock-free: take the lock only inside individual store
# methods, immediately around the statements it protects — never around a
# loop, and never around an ``await`` that reaches the tunnel, an LLM call, or
# any callback the caller supplied (e.g. a ticket gate resumer). A write held
# across I/O like that would stall every other writer in the process for the
# duration of that I/O.


class _WriteLockState:
    __slots__ = ("lock", "owner", "depth")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.owner: asyncio.Task | None = None
        self.depth = 0


_WRITE_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _WriteLockState]" = (
    weakref.WeakKeyDictionary()
)


@asynccontextmanager
async def write_lock() -> AsyncIterator[None]:
    """Serialize this process's SQLite writers. Re-entrant within one task.

    See the module-level comment above for why this exists and the rule that
    keeps it deadlock-free (never hold it across a loop or non-DB I/O).
    """

    loop = asyncio.get_running_loop()
    state = _WRITE_LOCKS.get(loop)
    if state is None:
        state = _WRITE_LOCKS[loop] = _WriteLockState()
    task = asyncio.current_task()
    if state.owner is not None and task is state.owner:
        # Re-entrant hop: the current task already holds the lock (e.g.
        # ticketstore.set_state -> _insert_event). Do not acquire again.
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
        return
    await state.lock.acquire()
    state.owner, state.depth = task, 1
    try:
        yield
    finally:
        state.depth -= 1
        state.owner = None
        state.lock.release()


async def _begin_immediate(db: aiosqlite.Connection) -> None:
    """Take the WAL writer lock as the transaction's first act.

    Insurance, not the fix for the failure this module's writer contention
    caused (see ADR-0056): every read in this codebase runs to completion
    (``async with ... execute(...)``) before any write, under the legacy
    ``isolation_level=""`` that only opens an implicit transaction on the
    first DML statement — so there is no read-then-upgrade-to-write path here
    for ``BEGIN IMMEDIATE`` to rescue. Its value is narrower: a multi-statement
    writer takes the writer lock up front instead of partway through, bounding
    how long it can hold a half-finished transaction, and it also protects
    against a writer outside this process (the ``sqlite3`` CLI, a future
    second worker) that :func:`write_lock` cannot see.
    """

    async with db.execute("BEGIN IMMEDIATE"):
        pass


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

    def __init__(
        self, db_path: str = DEFAULT_DB_PATH, retention_days: int = TELEMETRY_RETENTION_DAYS
    ) -> None:
        self.db_path = db_path
        self.retention_days = retention_days
        self._db: aiosqlite.Connection | None = None
        # Optional read-path annotator, called as ``annotate(agent_id, snapshot)``
        # for every snapshot this store deserializes, mutating it in place.
        # Operator-declared, LLM-free server-side annotations (reliability alarm
        # suppression) must reach *every* health consumer -- alerting, the
        # digest, the fleet list, the dashboard, MCP -- not just the two read
        # paths that already run the ADR-0028 LLM categorization. Hooking in
        # here, at the store boundary, means every caller gets it for free
        # instead of each of the ~8 call sites opting in individually. Never
        # touches the persisted row. ``None`` (the default) is a no-op, so
        # every existing caller and test is unaffected.
        self.annotate: Callable[[str, dict[str, Any]], None] | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
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
        async with write_lock():
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

    async def daily_latest(
        self, agent_id: str, since: str, *, limit: int = 400
    ) -> list[dict[str, Any]]:
        """Return the last snapshot of each calendar day since ``since``, oldest first.

        Used by the fleet health trend: one representative snapshot per UTC day
        keeps the query cheap regardless of push frequency. ``since`` is an ISO
        timestamp (or date) lower bound. Relies on SQLite returning the row of
        the ``MAX(collected_at)`` within each ``GROUP BY`` date bucket.
        """

        async with self._conn.execute(
            "SELECT collected_at, snapshot, MAX(collected_at) AS _m FROM snapshots "
            "WHERE agent_id = ? AND collected_at >= ? "
            "GROUP BY substr(collected_at, 1, 10) ORDER BY collected_at ASC LIMIT ?",
            (agent_id, since, limit),
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            snapshot = json.loads(r["snapshot"])
            if self.annotate is not None:
                self.annotate(agent_id, snapshot)
            out.append({"collected_at": r["collected_at"], "snapshot": snapshot})
        return out

    async def known_agents(self) -> list[str]:
        """Return distinct agent_ids that have stored snapshots."""

        async with self._conn.execute(
            "SELECT DISTINCT agent_id FROM snapshots ORDER BY agent_id"
        ) as cur:
            rows = await cur.fetchall()
        return [r["agent_id"] for r in rows]

    _PRUNE_CHUNK = 500

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Delete snapshots older than the retention window. Returns rows deleted.

        ``retention_days`` overrides ``self.retention_days`` for this call —
        the operator-configurable live setting (ADR-0056) resolves it fresh on
        every scheduled pass instead of freezing it at store construction.

        Snapshots are the largest table in this database by a wide margin
        (~90 KB per row); an unbounded ``DELETE`` here can hold the write lock
        — and so block every other writer in the process, see
        :func:`write_lock` — for as long as it takes to delete everything past
        the cutoff. Deleted in small chunks, each its own transaction, with a
        scheduling yield between them, so a large prune interleaves with
        telemetry inserts instead of stalling them.
        """

        now = now or datetime.now(timezone.utc)
        days = retention_days if retention_days is not None else self.retention_days
        cutoff = (now - timedelta(days=days)).isoformat()
        total = 0
        while True:
            async with write_lock():
                cur = await self._conn.execute(
                    "DELETE FROM snapshots WHERE id IN "
                    "(SELECT id FROM snapshots WHERE collected_at < ? LIMIT ?)",
                    (cutoff, self._PRUNE_CHUNK),
                )
                await self._conn.commit()
            deleted = cur.rowcount or 0
            total += deleted
            if deleted < self._PRUNE_CHUNK:
                break
            await asyncio.sleep(0)
        return total

    async def delete_agent(self, agent_id: str) -> int:
        """Delete all snapshots for ``agent_id`` (host removed from inventory)."""

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM snapshots WHERE agent_id = ?", (agent_id,)
            )
            await self._conn.commit()
        return cur.rowcount or 0

    def _row_to_record(self, row: aiosqlite.Row) -> dict[str, Any]:
        snapshot = json.loads(row["snapshot"])
        if self.annotate is not None:
            self.annotate(row["agent_id"], snapshot)
        return {
            "agent_id": row["agent_id"],
            "collected_at": row["collected_at"],
            "received_at": row["received_at"],
            "snapshot": snapshot,
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
        await _configure_connection(self._db)
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

        async with write_lock():
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
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO events (at, agent_id, source, kind, tool, ok, error) "
                "VALUES (?, ?, 'server', 'audit', ?, ?, ?)",
                (at, agent_id, tool, 1 if ok else 0, error),
            )
            await self._conn.commit()

    async def insert_alert(
        self,
        *,
        agent_id: str | None,
        message: str,
        level: str,
        fields: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> None:
        """Store an emitted operator alert (kind='alert', source='server').

        Alert history reuses the events table (ADR-0029): the Activity view and
        the weekly digest read these back via ``query(kind='alert')``.
        """

        at = at or datetime.now(timezone.utc).isoformat()
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO events (at, agent_id, source, level, kind, target, message, fields) "
                "VALUES (?, ?, 'server', ?, 'alert', 'kenny.alert', ?, ?)",
                (
                    at,
                    agent_id,
                    level,
                    message,
                    json.dumps(fields) if fields is not None else None,
                ),
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

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Delete events older than the retention window. Returns rows deleted.

        ``retention_days`` overrides ``self.retention_days`` for this call —
        see :meth:`TelemetryStore.prune` for why (ADR-0056's live-retention
        setting). No operator-facing key is wired to this store yet; the
        parameter exists so one can be added later without a second signature
        change.
        """

        now = now or datetime.now(timezone.utc)
        days = retention_days if retention_days is not None else self.retention_days
        cutoff = (now - timedelta(days=days)).isoformat()
        async with write_lock():
            cur = await self._conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
            await self._conn.commit()
        return cur.rowcount or 0

    async def delete_agent(self, agent_id: str) -> int:
        """Delete all events for ``agent_id`` (host removed from inventory)."""

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM events WHERE agent_id = ?", (agent_id,)
            )
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


_ALERT_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state (
    agent_id         TEXT NOT NULL,
    scope            TEXT NOT NULL,
    status           TEXT NOT NULL,
    since            TEXT NOT NULL,
    last_notified_at TEXT,
    PRIMARY KEY (agent_id, scope)
);
"""


class AlertStateStore:
    """Async SQLite-backed last-known alert state per (agent, scope).

    ``scope`` is ``'offline'``, ``'overall'``, ``'section:<name>'``,
    ``'change:<section>'`` or ``'digest'``. Persisting the state (rather than
    keeping it in memory) means a server restart does not re-fire alerts for
    conditions that were already notified (ADR-0029). Rows are tiny and pruned
    implicitly by being overwritten, so there is no retention job.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_ALERT_STATE_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AlertStateStore is not connected; call connect() first")
        return self._db

    async def get(self, agent_id: str, scope: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT agent_id, scope, status, since, last_notified_at FROM alert_state "
            "WHERE agent_id = ? AND scope = ?",
            (agent_id, scope),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all(self, agent_id: str) -> dict[str, dict[str, Any]]:
        """Return every scope row for an agent, keyed by scope."""

        async with self._conn.execute(
            "SELECT agent_id, scope, status, since, last_notified_at FROM alert_state "
            "WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["scope"]: dict(r) for r in rows}

    async def upsert(
        self,
        agent_id: str,
        scope: str,
        *,
        status: str,
        since: str,
        last_notified_at: str | None,
    ) -> None:
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO alert_state (agent_id, scope, status, since, last_notified_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (agent_id, scope) DO UPDATE SET "
                "status = excluded.status, since = excluded.since, "
                "last_notified_at = excluded.last_notified_at",
                (agent_id, scope, status, since, last_notified_at),
            )
            await self._conn.commit()

    async def delete_agent(self, agent_id: str) -> int:
        """Delete all alert state for ``agent_id`` (host removed from inventory)."""

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM alert_state WHERE agent_id = ?", (agent_id,)
            )
            await self._conn.commit()
        return cur.rowcount or 0


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
        await _configure_connection(self._db)
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


_RELIABILITY_SUPPRESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reliability_suppressions (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    event_id   INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reliability_suppressions_created
    ON reliability_suppressions (created_at);
"""


class ReliabilitySuppressionStore:
    """Async SQLite-backed store for operator-declared reliability alarm
    suppression rules (issue #166 / see ADR-0045).

    A rule mutes one ``(source, event_id)`` reliability event pattern out of
    health *scoring* — never out of the displayed raw counts. ``agent_id``
    empty means fleet-wide; ``source`` empty means a wildcard on any source
    reporting that ``event_id``. Empty-string sentinels are used instead of
    NULL because SQLite treats NULLs as pairwise-distinct in a would-be
    UNIQUE(agent_id, source, event_id) constraint, which would silently allow
    duplicate rules; the sentinels also match how
    :mod:`kenny_server.event_categories` already normalizes ``source``
    (``str(source or "").strip()``). ``id`` is the deterministic
    ``"<agent_id>|<source>|<event_id>"`` key built by the caller
    (:mod:`kenny_server.reliability_suppression`), which both enforces
    uniqueness of the triple and makes the row directly addressable for
    removal without a lookup.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_RELIABILITY_SUPPRESSION_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ReliabilitySuppressionStore is not connected; call connect() first")
        return self._db

    async def list(self) -> list[dict[str, Any]]:
        """Return all suppression rules, oldest-first."""

        async with self._conn.execute(
            "SELECT id, agent_id, source, event_id, note, created_at, created_by "
            "FROM reliability_suppressions ORDER BY created_at, id"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "source": r["source"],
                "event_id": r["event_id"],
                "note": r["note"],
                "created_at": r["created_at"],
                "created_by": r["created_by"],
            }
            for r in rows
        ]

    async def add(
        self,
        *,
        id: str,
        agent_id: str,
        source: str,
        event_id: int,
        note: str = "",
        created_by: str = "",
    ) -> None:
        """Insert (or replace) a suppression rule, stamping ``created_at``."""

        created_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR REPLACE INTO reliability_suppressions "
            "(id, agent_id, source, event_id, note, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, agent_id, source, event_id, note, created_at, created_by),
        )
        await self._conn.commit()

    async def remove(self, id: str) -> bool:
        """Delete one suppression rule by id. Returns True if a row was removed."""

        cur = await self._conn.execute(
            "DELETE FROM reliability_suppressions WHERE id = ?", (id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def delete_agent(self, agent_id: str) -> int:
        """Delete host-scoped rules for a removed host. Fleet-wide rules
        (``agent_id == ''``) are untouched by design -- they mute a Windows
        quirk, not this specific PC."""

        cur = await self._conn.execute(
            "DELETE FROM reliability_suppressions WHERE agent_id = ?", (agent_id,)
        )
        await self._conn.commit()
        return cur.rowcount or 0


_TICKET_RULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_rules (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    section    TEXT NOT NULL DEFAULT '',
    decision   TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ticket_rules_created ON ticket_rules (created_at);
"""


class TicketRuleStore:
    """Async SQLite-backed store for operator-declared auto-ticket rules
    (ADR-0053).

    A rule decides whether a given ``(agent_id, event_type, section)`` alert
    subject opens a ticket: ``open_all`` always opens it, ``open_crit`` only
    for a ``crit``-severity subject, ``never`` suppresses it. ``agent_id``
    empty means fleet-wide; ``section`` empty means any section. Same
    empty-string-sentinel reasoning as :class:`ReliabilitySuppressionStore`
    (NULL would let SQLite treat wildcard rows as pairwise-distinct). ``id``
    is the deterministic ``"<agent_id>|<event_type>|<section>"`` key built by
    the caller (:mod:`kenny_server.ticket_rules`), which both enforces
    uniqueness of the triple and makes the row directly addressable for
    removal without a lookup. The table records only *deviations* from the
    coded default in ``ticket_rules.DEFAULT_DECISION`` -- an empty table
    reproduces the pre-ADR-0053 behavior exactly.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_TICKET_RULE_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("TicketRuleStore is not connected; call connect() first")
        return self._db

    async def list(self) -> list[dict[str, Any]]:
        """Return all ticket rules, oldest-first."""

        async with self._conn.execute(
            "SELECT id, agent_id, event_type, section, decision, note, "
            "created_at, created_by FROM ticket_rules ORDER BY created_at, id"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "event_type": r["event_type"],
                "section": r["section"],
                "decision": r["decision"],
                "note": r["note"],
                "created_at": r["created_at"],
                "created_by": r["created_by"],
            }
            for r in rows
        ]

    async def add(
        self,
        *,
        id: str,
        agent_id: str,
        event_type: str,
        section: str,
        decision: str,
        note: str = "",
        created_by: str = "",
    ) -> None:
        """Insert (or replace) a ticket rule, stamping ``created_at``."""

        created_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR REPLACE INTO ticket_rules "
            "(id, agent_id, event_type, section, decision, note, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, agent_id, event_type, section, decision, note, created_at, created_by),
        )
        await self._conn.commit()

    async def remove(self, id: str) -> bool:
        """Delete one ticket rule by id. Returns True if a row was removed."""

        cur = await self._conn.execute("DELETE FROM ticket_rules WHERE id = ?", (id,))
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def delete_agent(self, agent_id: str) -> int:
        """Delete host-scoped rules for a removed host. Fleet-wide rules
        (``agent_id == ''``) are untouched -- they are fleet policy, not tied
        to this specific PC."""

        cur = await self._conn.execute(
            "DELETE FROM ticket_rules WHERE agent_id = ?", (agent_id,)
        )
        await self._conn.commit()
        return cur.rowcount or 0


_BACKUP_TARGET_SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_targets (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL,
    config     TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class BackupTargetStore:
    """Async SQLite-backed store for operator-configured remote backup destinations.

    Rows are dispatched through :func:`kenny_server.backup_targets.build_destination`
    by ``kind`` (``http``/``scp``/``ftp``). ``config`` is the destination's
    connection dict (including secrets — SFTP key/password, FTP password, HTTP
    token) json-encoded, consistent with the existing secret storage for
    ``AgentTokenStore``/``KeyStore``/``OAuthStore``. Callers presenting this to
    an operator (Phase B API) are responsible for masking secret fields.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_BACKUP_TARGET_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("BackupTargetStore is not connected; call connect() first")
        return self._db

    def _row_to_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "label": row["label"],
            "config": json.loads(row["config"]),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def list(self) -> list[dict[str, Any]]:
        """Return all targets, config json-decoded, ordered by ``created_at``."""

        async with self._conn.execute(
            "SELECT id, kind, label, config, enabled, created_at, updated_at "
            "FROM backup_targets ORDER BY created_at, id"
        ) as cur:
            rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get(self, id: str) -> dict[str, Any] | None:
        """Return one target by id, or None."""

        async with self._conn.execute(
            "SELECT id, kind, label, config, enabled, created_at, updated_at "
            "FROM backup_targets WHERE id = ?",
            (id,),
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def add(
        self, *, id: str | None = None, kind: str, label: str, config: dict[str, Any]
    ) -> str:
        """Insert a new target. Returns the (possibly generated) id."""

        target_id = id or uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO backup_targets "
            "(id, kind, label, config, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (target_id, kind, label, json.dumps(config), now, now),
        )
        await self._conn.commit()
        return target_id

    async def update(
        self, id: str, *, label: str | None = None, config: dict[str, Any] | None = None
    ) -> bool:
        """Update label and/or config for one target. Returns True if it existed."""

        if label is None and config is None:
            return await self.get(id) is not None
        sets = ["updated_at = ?"]
        params: list[Any] = [datetime.now(timezone.utc).isoformat()]
        if label is not None:
            sets.append("label = ?")
            params.append(label)
        if config is not None:
            sets.append("config = ?")
            params.append(json.dumps(config))
        params.append(id)
        cur = await self._conn.execute(
            f"UPDATE backup_targets SET {', '.join(sets)} WHERE id = ?", params
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def set_enabled(self, id: str, enabled: bool) -> bool:
        """Flip the enabled flag on one target. Returns True if it existed."""

        cur = await self._conn.execute(
            "UPDATE backup_targets SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, datetime.now(timezone.utc).isoformat(), id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def delete(self, id: str) -> bool:
        """Delete one target by id. Returns True if a row was removed."""

        cur = await self._conn.execute("DELETE FROM backup_targets WHERE id = ?", (id,))
        await self._conn.commit()
        return (cur.rowcount or 0) > 0


_UPDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS update_availability (
    component  TEXT PRIMARY KEY,
    version    TEXT NOT NULL,
    url        TEXT,
    sha256     TEXT,
    digest     TEXT,
    ok         INTEGER NOT NULL DEFAULT 1,
    message    TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS update_campaigns (
    id           TEXT PRIMARY KEY,
    component    TEXT NOT NULL DEFAULT 'agent',
    version      TEXT NOT NULL,
    on_connect   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    revoked_at   TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS update_campaign_targets (
    campaign_id TEXT NOT NULL,
    os          TEXT NOT NULL,
    arch        TEXT NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    PRIMARY KEY (campaign_id, os, arch)
);
CREATE TABLE IF NOT EXISTS update_campaign_agents (
    campaign_id     TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    held            INTEGER NOT NULL DEFAULT 0,
    updated_version INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (campaign_id, agent_id)
);
-- Per-agent operator-desired release channel (ADR-0052): the soll side of the
-- soll/ist split, separate from what the connected binary actually reports
-- (``registry.Agent.channel``). Default 'stable'; a row only exists once an
-- operator has explicitly set one (see `UpdateStore.set_desired_channel`).
CREATE TABLE IF NOT EXISTS agent_channel_prefs (
    agent_id   TEXT PRIMARY KEY,
    channel    TEXT NOT NULL DEFAULT 'stable',
    updated_at TEXT NOT NULL
);
"""

# Update-campaign columns added after the table's original release, migrated
# in for existing DB files the same way `KeyStore._migrate` adds its grace
# columns (ADR-0052).
_CAMPAIGN_MIGRATED_COLUMNS: dict[str, str] = {
    "channel": "TEXT NOT NULL DEFAULT 'stable'",
}


def _availability_key(component: str, channel: str = "stable") -> str:
    """Compose the ``update_availability.component`` key for ``(component, channel)``.

    ``channel="stable"`` is byte-identical to the pre-ADR-0052 key (``"agent"``/
    ``"server"``) — existing rows are untouched. ``channel="dev"`` composes
    ``"agent:dev"``/``"server:dev"``, a second, additive row alongside the
    stable one. The ``update_availability`` table's schema/PK is unchanged;
    this only widens what a caller may pass as ``component``.
    """

    return component if channel == "stable" else f"{component}:{channel}"

# Bounded number of update attempts a single agent gets under one campaign
# before it is marked "held" and stops being auto-retried (ADR-0044). Prevents
# a kill-switch-off agent or a crash-looping bad release from retriggering
# forever on every reconnect.
ATTEMPT_BUDGET = 3


class UpdateStore:
    """Async SQLite-backed store for scheduled update detection + rollout (ADR-0044).

    Three concerns, one store (all tiny, all sharing the DB file):

    * ``update_availability`` — the latest known version per component
      (``agent`` | ``server``) from the last detection pass, one row each.
    * ``update_campaigns`` (+ ``update_campaign_targets``) — an
      operator-approved agent rollout. Approving a campaign **pins** an exact
      version, snapshotting per-(os, arch) binary artifacts at approval time
      (copied to a durable per-campaign path by the caller) so a later
      detection pass refreshing the shared agent-binary cache can never change
      what an active campaign pushes — the fix for the "campaign silently
      tracks whatever is newest" trap. Only one campaign is active at a time
      per component; approving a new one supersedes (revokes) the prior one.
    * ``update_campaign_agents`` — per-agent attempt bookkeeping under a
      campaign: a bounded retry budget (:data:`ATTEMPT_BUDGET`) so a refusing
      or crash-looping agent gets marked ``held`` instead of being retried on
      every reconnect forever.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_UPDATE_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add columns to ``update_campaigns`` for DBs created before they existed."""

        async with self._conn.execute("PRAGMA table_info(update_campaigns)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        for col, ddl in _CAMPAIGN_MIGRATED_COLUMNS.items():
            if col not in cols:
                await self._conn.execute(f"ALTER TABLE update_campaigns ADD COLUMN {col} {ddl}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("UpdateStore is not connected; call connect() first")
        return self._db

    # -- availability (last detection pass) --------------------------------

    async def set_availability(
        self,
        component: str,
        *,
        version: str,
        url: str | None = None,
        sha256: str | None = None,
        digest: str | None = None,
        ok: bool = True,
        message: str = "",
    ) -> None:
        checked_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO update_availability "
            "(component, version, url, sha256, digest, ok, message, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(component) DO UPDATE SET "
            "version=excluded.version, url=excluded.url, sha256=excluded.sha256, "
            "digest=excluded.digest, ok=excluded.ok, message=excluded.message, "
            "checked_at=excluded.checked_at",
            (component, version, url, sha256, digest, 1 if ok else 0, message, checked_at),
        )
        await self._conn.commit()

    async def get_availability(self, component: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT component, version, url, sha256, digest, ok, message, checked_at "
            "FROM update_availability WHERE component = ?",
            (component,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["ok"] = bool(d["ok"])
        return d

    async def list_availability(self) -> dict[str, dict[str, Any]]:
        async with self._conn.execute(
            "SELECT component, version, url, sha256, digest, ok, message, checked_at "
            "FROM update_availability"
        ) as cur:
            rows = await cur.fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            d["ok"] = bool(d["ok"])
            out[d["component"]] = d
        return out

    # -- campaigns -----------------------------------------------------------

    def _campaign_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        d["on_connect"] = bool(d["on_connect"])
        return d

    async def get_active_campaign(
        self, component: str = "agent", channel: str = "stable"
    ) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT id, component, version, on_connect, status, created_at, "
            "expires_at, revoked_at, completed_at, channel FROM update_campaigns "
            "WHERE component = ? AND channel = ? AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (component, channel),
        ) as cur:
            row = await cur.fetchone()
        return self._campaign_row(row) if row else None

    async def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT id, component, version, on_connect, status, created_at, "
            "expires_at, revoked_at, completed_at, channel FROM update_campaigns WHERE id = ?",
            (campaign_id,),
        ) as cur:
            row = await cur.fetchone()
        return self._campaign_row(row) if row else None

    async def list_campaigns(
        self, *, component: str = "agent", channel: str = "stable", limit: int = 20
    ) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT id, component, version, on_connect, status, created_at, "
            "expires_at, revoked_at, completed_at, channel FROM update_campaigns "
            "WHERE component = ? AND channel = ? ORDER BY created_at DESC LIMIT ?",
            (component, channel, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [self._campaign_row(r) for r in rows]

    async def create_campaign(
        self,
        *,
        id: str | None = None,
        component: str = "agent",
        channel: str = "stable",
        version: str,
        on_connect: bool,
        expires_at: str | None,
        targets: list[dict[str, str]],
    ) -> str:
        """Persist a new active campaign, superseding any prior active one.

        ``targets`` is a list of ``{"os", "arch", "path", "sha256"}`` — the
        durable, per-campaign artifact copies the caller already staged on
        disk (see ``update_manager.approve_campaign``); this method only
        records their location, it does not touch the filesystem. ``id`` lets
        the caller pick the id up front (``update_manager`` derives the
        per-campaign artifact directory from it before this is called);
        omitting it generates one. Only one campaign is active at a time per
        ``(component, channel)`` (ADR-0052) — approving a dev campaign never
        supersedes a concurrently-active stable one, and vice versa.
        """

        campaign_id = id or uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        prior = await self.get_active_campaign(component, channel)
        if prior is not None:
            await self._conn.execute(
                "UPDATE update_campaigns SET status = 'revoked', revoked_at = ? WHERE id = ?",
                (now, prior["id"]),
            )
        await self._conn.execute(
            "INSERT INTO update_campaigns "
            "(id, component, version, on_connect, status, created_at, expires_at, channel) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            (campaign_id, component, version, 1 if on_connect else 0, now, expires_at, channel),
        )
        for t in targets:
            await self._conn.execute(
                "INSERT INTO update_campaign_targets (campaign_id, os, arch, path, sha256) "
                "VALUES (?, ?, ?, ?, ?)",
                (campaign_id, t["os"], t["arch"], t["path"], t["sha256"]),
            )
        await self._conn.commit()
        return campaign_id

    async def campaign_targets(self, campaign_id: str) -> list[dict[str, str]]:
        async with self._conn.execute(
            "SELECT os, arch, path, sha256 FROM update_campaign_targets WHERE campaign_id = ?",
            (campaign_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def set_campaign_status(
        self, campaign_id: str, status: str, *, at_field: str | None = None
    ) -> bool:
        """Transition a campaign to a terminal status (``revoked``/``expired``/``completed``)."""

        now = datetime.now(timezone.utc).isoformat()
        field = at_field or {
            "revoked": "revoked_at",
            "expired": "revoked_at",
            "completed": "completed_at",
        }.get(status)
        if field is None:
            cur = await self._conn.execute(
                "UPDATE update_campaigns SET status = ? WHERE id = ? AND status = 'active'",
                (status, campaign_id),
            )
        else:
            cur = await self._conn.execute(
                f"UPDATE update_campaigns SET status = ?, {field} = ? "
                "WHERE id = ? AND status = 'active'",
                (status, now, campaign_id),
            )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -- per-agent attempt bookkeeping under a campaign ----------------------

    async def get_agent_state(self, campaign_id: str, agent_id: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT campaign_id, agent_id, attempts, held, updated_version, "
            "last_attempt_at, last_error FROM update_campaign_agents "
            "WHERE campaign_id = ? AND agent_id = ?",
            (campaign_id, agent_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["held"] = bool(d["held"])
        d["updated_version"] = bool(d["updated_version"])
        return d

    async def list_agent_states(self, campaign_id: str) -> dict[str, dict[str, Any]]:
        async with self._conn.execute(
            "SELECT campaign_id, agent_id, attempts, held, updated_version, "
            "last_attempt_at, last_error FROM update_campaign_agents WHERE campaign_id = ?",
            (campaign_id,),
        ) as cur:
            rows = await cur.fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            d["held"] = bool(d["held"])
            d["updated_version"] = bool(d["updated_version"])
            out[d["agent_id"]] = d
        return out

    async def record_attempt(
        self,
        campaign_id: str,
        agent_id: str,
        *,
        ok: bool,
        error: str | None = None,
        count_against_budget: bool = True,
    ) -> dict[str, Any]:
        """Record one rollout attempt for ``agent_id`` under ``campaign_id``.

        A successful attempt marks ``updated_version`` (the on-connect/apply
        loop then leaves this agent alone). A failed attempt increments
        ``attempts`` only when ``count_against_budget`` is true (an anti-cheat
        ``paused`` refusal is expected to clear on its own and is retried
        without spending the budget); once ``attempts >= ATTEMPT_BUDGET`` the
        agent is marked ``held`` and is not auto-retried again under this
        campaign. Returns the resulting row.
        """

        existing = await self.get_agent_state(campaign_id, agent_id)
        attempts = existing["attempts"] if existing else 0
        now = datetime.now(timezone.utc).isoformat()
        if ok:
            attempts = attempts  # unchanged; success ends the retry loop via updated_version
            held = False
            updated_version = True
        else:
            if count_against_budget:
                attempts += 1
            held = attempts >= ATTEMPT_BUDGET
            updated_version = False
        await self._conn.execute(
            "INSERT INTO update_campaign_agents "
            "(campaign_id, agent_id, attempts, held, updated_version, last_attempt_at, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(campaign_id, agent_id) DO UPDATE SET "
            "attempts=excluded.attempts, held=excluded.held, "
            "updated_version=excluded.updated_version, "
            "last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error",
            (campaign_id, agent_id, attempts, 1 if held else 0, 1 if updated_version else 0, now, error),
        )
        await self._conn.commit()
        return await self.get_agent_state(campaign_id, agent_id)  # type: ignore[return-value]

    # -- desired channel (per-agent, operator-editable, ADR-0052) -----------

    async def get_desired_channel(self, agent_id: str) -> str:
        """The operator-desired channel for ``agent_id``, defaulting to ``"stable"``.

        This is the soll side of ADR-0052's soll/ist split — separate from
        ``registry.Agent.channel``, which is what the connected binary reports
        about itself. Campaign eligibility is checked against this, not that.
        """

        async with self._conn.execute(
            "SELECT channel FROM agent_channel_prefs WHERE agent_id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["channel"] if row is not None else "stable"

    async def set_desired_channel(self, agent_id: str, channel: str) -> None:
        """Set the operator-desired channel for ``agent_id`` (upsert)."""

        if channel not in ("stable", "dev"):
            raise ValueError(f"channel must be 'stable' or 'dev', got {channel!r}")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO agent_channel_prefs (agent_id, channel, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET channel=excluded.channel, "
            "updated_at=excluded.updated_at",
            (agent_id, channel, now),
        )
        await self._conn.commit()

    async def list_desired_channels(self) -> dict[str, str]:
        """Every agent with an explicit desired-channel row, ``agent_id -> channel``.

        An agent absent from this dict has never had one set and defaults to
        ``"stable"`` (see :meth:`get_desired_channel`).
        """

        async with self._conn.execute("SELECT agent_id, channel FROM agent_channel_prefs") as cur:
            rows = await cur.fetchall()
        return {r["agent_id"]: r["channel"] for r in rows}


_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SettingsStore:
    """Async SQLite-backed key/value store for operator setting overrides.

    Stores only keys the operator has explicitly overridden; anything absent
    falls back to the environment/default in :class:`~.config.Settings`. Values
    are raw strings (typed by the catalog). Shares the DB file with the other
    stores (own connection), following the :class:`PolicyStore` pattern.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_SETTINGS_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SettingsStore is not connected; call connect() first")
        return self._db

    async def all(self) -> dict[str, str]:
        """Return every stored override as a ``{key: value}`` mapping."""

        async with self._conn.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    async def set(self, key: str, value: str) -> None:
        """Upsert one override, stamping ``updated_at``."""

        updated_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, updated_at),
        )
        await self._conn.commit()

    async def delete(self, key: str) -> bool:
        """Remove one override. Returns True if a row was deleted."""

        cur = await self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self._conn.commit()
        return (cur.rowcount or 0) > 0


_WEBFILTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS webfilter_config (
    agent_id              TEXT PRIMARY KEY,
    enabled               INTEGER NOT NULL DEFAULT 0,
    block_mode            INTEGER NOT NULL DEFAULT 0,
    use_external_adult    INTEGER NOT NULL DEFAULT 1,
    use_bypass_protection INTEGER NOT NULL DEFAULT 0,
    doh_policy            TEXT NOT NULL DEFAULT 'disable',
    updated_at            TEXT,
    applied_hash          TEXT,
    applied_at            TEXT,
    applied_ok            INTEGER
);
CREATE TABLE IF NOT EXISTS webfilter_domains (
    agent_id  TEXT NOT NULL,
    domain    TEXT NOT NULL,
    action    TEXT NOT NULL CHECK (action IN ('watch', 'block', 'allow')),
    note      TEXT,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (agent_id, domain)
);
CREATE TABLE IF NOT EXISTS web_activity_events (
    agent_id   TEXT NOT NULL,
    domain     TEXT NOT NULL,
    first_seen TEXT,
    last_seen  TEXT,
    hits       INTEGER NOT NULL DEFAULT 0,
    sources    TEXT,
    flagged    INTEGER NOT NULL DEFAULT 0,
    category   TEXT,
    PRIMARY KEY (agent_id, domain)
);
CREATE INDEX IF NOT EXISTS idx_web_activity_last_seen
    ON web_activity_events (agent_id, last_seen DESC);
"""

# Config defaults for a host that has never been configured (ADR-0026).
_WEBFILTER_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "block_mode": False,
    "use_external_adult": True,
    "use_bypass_protection": False,
    "doh_policy": "disable",
}
_WEBFILTER_TOGGLES = (
    "enabled",
    "block_mode",
    "use_external_adult",
    "use_bypass_protection",
)


class WebFilterStore:
    """Async SQLite-backed store for parental-controls state (ADR-0026).

    Holds, per host: the feature config, the editable custom domain list
    (``watch``/``block``/``allow``), and the accumulated ``web_activity`` events
    (server-side 24 h+ window). Shares the DB file with the other stores but owns
    its own connection. Retention mirrors telemetry (~30 days).
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, retention_days: int = RETENTION_DAYS) -> None:
        self.db_path = db_path
        self.retention_days = retention_days
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_WEBFILTER_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("WebFilterStore is not connected; call connect() first")
        return self._db

    # -- config ------------------------------------------------------------

    async def get_config(self, agent_id: str) -> dict[str, Any]:
        """Return the host's config (defaults when never configured)."""

        async with self._conn.execute(
            "SELECT enabled, block_mode, use_external_adult, use_bypass_protection, "
            "doh_policy, updated_at, applied_hash, applied_at, applied_ok "
            "FROM webfilter_config WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {
                "agent_id": agent_id,
                **_WEBFILTER_DEFAULTS,
                "updated_at": None,
                "applied_hash": None,
                "applied_at": None,
                "applied_ok": None,
            }
        return {
            "agent_id": agent_id,
            "enabled": bool(row["enabled"]),
            "block_mode": bool(row["block_mode"]),
            "use_external_adult": bool(row["use_external_adult"]),
            "use_bypass_protection": bool(row["use_bypass_protection"]),
            "doh_policy": row["doh_policy"],
            "updated_at": row["updated_at"],
            "applied_hash": row["applied_hash"],
            "applied_at": row["applied_at"],
            "applied_ok": None if row["applied_ok"] is None else bool(row["applied_ok"]),
        }

    async def set_config(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        """Upsert a partial config change (unknown keys ignored). Returns config."""

        current = await self.get_config(agent_id)
        for key in _WEBFILTER_TOGGLES:
            if fields.get(key) is not None:
                current[key] = bool(fields[key])
        if fields.get("doh_policy") is not None:
            current["doh_policy"] = str(fields["doh_policy"])
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO webfilter_config "
            "(agent_id, enabled, block_mode, use_external_adult, use_bypass_protection, "
            "doh_policy, updated_at, applied_hash, applied_at, applied_ok) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "enabled=excluded.enabled, block_mode=excluded.block_mode, "
            "use_external_adult=excluded.use_external_adult, "
            "use_bypass_protection=excluded.use_bypass_protection, "
            "doh_policy=excluded.doh_policy, updated_at=excluded.updated_at",
            (
                agent_id,
                int(current["enabled"]),
                int(current["block_mode"]),
                int(current["use_external_adult"]),
                int(current["use_bypass_protection"]),
                current["doh_policy"],
                now,
                current["applied_hash"],
                current["applied_at"],
                None if current["applied_ok"] is None else int(current["applied_ok"]),
            ),
        )
        await self._conn.commit()
        return await self.get_config(agent_id)

    async def set_applied_state(
        self, agent_id: str, list_hash: str | None, applied_at: str, ok: bool
    ) -> None:
        """Persist the last-applied block hash/time/result for drift display."""

        await self._conn.execute(
            "INSERT INTO webfilter_config (agent_id, applied_hash, applied_at, applied_ok) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "applied_hash=excluded.applied_hash, applied_at=excluded.applied_at, "
            "applied_ok=excluded.applied_ok",
            (agent_id, list_hash, applied_at, 1 if ok else 0),
        )
        await self._conn.commit()

    # -- custom domain list ------------------------------------------------

    async def list_domains(self, agent_id: str) -> list[dict[str, Any]]:
        """Return the host's custom entries, oldest-first."""

        async with self._conn.execute(
            "SELECT domain, action, note, added_at FROM webfilter_domains "
            "WHERE agent_id = ? ORDER BY added_at, domain",
            (agent_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "domain": r["domain"],
                "action": r["action"],
                "note": r["note"],
                "added_at": r["added_at"],
            }
            for r in rows
        ]

    async def add_domain(
        self, agent_id: str, domain: str, action: str, note: str | None = None
    ) -> None:
        """Insert (or replace) one custom entry, stamping ``added_at``."""

        added_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO webfilter_domains (agent_id, domain, action, note, added_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id, domain) DO UPDATE SET "
            "action=excluded.action, note=excluded.note",
            (agent_id, domain, action, note, added_at),
        )
        await self._conn.commit()

    async def remove_domain(self, agent_id: str, domain: str) -> bool:
        """Delete one custom entry. Returns True if a row was removed."""

        cur = await self._conn.execute(
            "DELETE FROM webfilter_domains WHERE agent_id = ? AND domain = ?",
            (agent_id, domain),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -- observed events ---------------------------------------------------

    async def upsert_events(self, agent_id: str, events: list[dict[str, Any]]) -> None:
        """Merge observed domains: min(first_seen), max(last_seen), hits +=, sources ∪.

        A SELECT-then-INSERT pair per event, all one transaction with a single
        commit at the end — the longest-held write transaction in this module,
        so it holds :func:`write_lock` for its whole duration (never released
        mid-loop) and opens with ``BEGIN IMMEDIATE`` to take the writer lock up
        front rather than partway through the loop.
        """

        async with write_lock():
            await _begin_immediate(self._conn)
            for event in events:
                domain = event["domain"]
                async with self._conn.execute(
                    "SELECT first_seen, last_seen, hits, sources FROM web_activity_events "
                    "WHERE agent_id = ? AND domain = ?",
                    (agent_id, domain),
                ) as cur:
                    existing = await cur.fetchone()
                first_seen = event.get("first_seen")
                last_seen = event.get("last_seen")
                hits = int(event.get("hits") or 0)
                sources = set(event.get("sources") or [])
                if existing is not None:
                    firsts = [x for x in (existing["first_seen"], first_seen) if x]
                    lasts = [x for x in (existing["last_seen"], last_seen) if x]
                    first_seen = min(firsts) if firsts else None
                    last_seen = max(lasts) if lasts else None
                    hits += int(existing["hits"] or 0)
                    if existing["sources"]:
                        sources |= set(json.loads(existing["sources"]))
                await self._conn.execute(
                    "INSERT INTO web_activity_events "
                    "(agent_id, domain, first_seen, last_seen, hits, sources, flagged, category) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(agent_id, domain) DO UPDATE SET "
                    "first_seen=excluded.first_seen, last_seen=excluded.last_seen, "
                    "hits=excluded.hits, sources=excluded.sources, "
                    "flagged=excluded.flagged, category=excluded.category",
                    (
                        agent_id,
                        domain,
                        first_seen,
                        last_seen,
                        hits,
                        json.dumps(sorted(sources)),
                        1 if event.get("flagged") else 0,
                        event.get("category"),
                    ),
                )
            await self._conn.commit()

    async def activity(
        self, agent_id: str, since_iso: str, flagged_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return observed domains with ``last_seen >= since``, newest-first."""

        sql = (
            "SELECT domain, first_seen, last_seen, hits, sources, flagged, category "
            "FROM web_activity_events WHERE agent_id = ? AND last_seen >= ?"
        )
        params: list[Any] = [agent_id, since_iso]
        if flagged_only:
            sql += " AND flagged = 1"
        sql += " ORDER BY last_seen DESC"
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            {
                "domain": r["domain"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "hits": r["hits"],
                "sources": json.loads(r["sources"]) if r["sources"] else [],
                "flagged": bool(r["flagged"]),
                "category": r["category"],
            }
            for r in rows
        ]

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Delete events whose ``last_seen`` is older than retention. Returns count.

        ``retention_days`` overrides ``self.retention_days`` for this call —
        see :meth:`TelemetryStore.prune` for why (ADR-0056's live-retention
        setting). No operator-facing key is wired to this store yet.
        """

        now = now or datetime.now(timezone.utc)
        days = retention_days if retention_days is not None else self.retention_days
        cutoff = (now - timedelta(days=days)).isoformat()
        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM web_activity_events WHERE last_seen < ?", (cutoff,)
            )
            await self._conn.commit()
        return cur.rowcount or 0

    async def delete_agent(self, agent_id: str) -> None:
        """Delete all web-filter state for ``agent_id`` (host removed from inventory)."""

        async with write_lock():
            await _begin_immediate(self._conn)
            for table in ("webfilter_config", "webfilter_domains", "web_activity_events"):
                await self._conn.execute(
                    f"DELETE FROM {table} WHERE agent_id = ?", (agent_id,)
                )
            await self._conn.commit()


_CHAT_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    agent_id    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    messages    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conversations_updated
    ON chat_conversations (updated_at DESC);
"""


class ChatHistoryStore:
    """Async SQLite-backed store for persisted copilot chat conversations.

    Shares the DB file with the other stores but owns its own connection.
    Unlike ``TelemetryStore``/``EventStore``/``WebFilterStore`` there is no
    ``prune()`` here: retention is unlimited and operator-curated (manual
    delete only), matching ``PolicyStore``'s append-until-explicitly-removed
    shape rather than the auto-pruned telemetry pattern (ADR-0027).
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_CHAT_HISTORY_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ChatHistoryStore is not connected; call connect() first")
        return self._db

    async def save(
        self,
        *,
        id: str,
        title: str,
        agent_id: str | None,
        messages: list[dict[str, Any]],
    ) -> None:
        """Insert-or-update one conversation.

        ``title`` and ``created_at`` are only honored on first insert (a
        conversation is titled once, at creation — see ``ON CONFLICT``
        below); ``agent_id``/``messages``/``updated_at`` are refreshed on
        every call.
        """

        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO chat_conversations "
            "(id, title, agent_id, created_at, updated_at, messages) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "agent_id=excluded.agent_id, updated_at=excluded.updated_at, "
            "messages=excluded.messages",
            (id, title, agent_id, now, now, json.dumps(messages, default=str)),
        )
        await self._conn.commit()

    async def get(self, id: str) -> dict[str, Any] | None:
        """Return one conversation with its full parsed ``messages``, or None."""

        async with self._conn.execute(
            "SELECT id, title, agent_id, created_at, updated_at, messages "
            "FROM chat_conversations WHERE id = ?",
            (id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "agent_id": row["agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": json.loads(row["messages"]),
        }

    async def list(self) -> list[dict[str, Any]]:
        """Return conversation summaries (no ``messages``), newest-updated first."""

        async with self._conn.execute(
            "SELECT id, title, agent_id, created_at, updated_at "
            "FROM chat_conversations ORDER BY updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "agent_id": r["agent_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    async def delete(self, id: str) -> bool:
        """Delete one conversation by id. Returns True if a row was removed."""

        cur = await self._conn.execute(
            "DELETE FROM chat_conversations WHERE id = ?", (id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

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
# TelemetryStore's own default (ADR-0051), deliberately its own constant and
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
    contention window this module exists to avoid. ADR-0039's backups cover
    the durability gap this leaves.
    """

    db.row_factory = aiosqlite.Row
    async with db.execute("PRAGMA journal_mode=WAL"):
        pass
    async with db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}"):
        pass
    async with db.execute("PRAGMA synchronous=NORMAL"):
        pass


# -- process-wide write serialization (ADR-0051) ------------------------------
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
#   (``set_state``, ``set_blocked``, ``append_event``). A plain
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
    # POSSIBLY DEAD: `depth` is written on every acquire/release but never
    # read back by any caller or assertion — nothing in this module or its
    # tests consults it. Kept rather than removed since it is the natural
    # place to assert `depth == 0` before the outer release, should this
    # re-entrant lock ever need that invariant checked.
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
    caused (see ADR-0051): every read in this codebase runs to completion
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
        # paths that already run the ADR-0026 LLM categorization. Hooking in
        # here, at the store boundary, means every caller gets it for free
        # instead of each of the ~8 call sites opting in individually. Never
        # touches the persisted row. ``None`` (the default) is a no-op, so
        # every existing caller and test is unaffected. Since ADR-0058 more
        # than one annotation rides this seam (suppression, then the persisted
        # LLM classification), so the hook is a list; ``annotate`` remains as a
        # single-callable view of it for callers that only ever set one.
        self.annotators: list[Callable[[str, dict[str, Any]], None]] = []

    @property
    def annotate(self) -> Callable[[str, dict[str, Any]], None] | None:
        """The composed read-path annotator, or ``None`` when none is set."""

        if not self.annotators:
            return None
        return self._apply_annotators

    @annotate.setter
    def annotate(self, fn: Callable[[str, dict[str, Any]], None] | None) -> None:
        self.annotators = [fn] if fn is not None else []

    def _apply_annotators(self, agent_id: str, snapshot: dict[str, Any]) -> None:
        for fn in self.annotators:
            fn(agent_id, snapshot)

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

    async def received_times(self, agent_id: str, since: str, until: str) -> list[str]:
        """``received_at`` of every snapshot of ``agent_id`` in ``[since, until)``, oldest first.

        Selects the timestamp column alone: the availability reconstruction
        (``availability.py``) needs when pushes arrived, never what they said, and
        a snapshot blob is ~90 KB.
        """

        return (await self.received_times_many([agent_id], since, until)).get(agent_id, [])

    async def received_times_many(
        self, agent_ids: list[str], since: str, until: str
    ) -> dict[str, list[str]]:
        """Batch :meth:`received_times`: one query for many agents.

        Agents with no snapshot in the range are absent from the result.
        """

        out: dict[str, list[str]] = {}
        if not agent_ids:
            return out
        marks = ",".join("?" * len(agent_ids))
        async with self._conn.execute(
            f"SELECT agent_id, received_at FROM snapshots WHERE agent_id IN ({marks}) "
            "AND received_at >= ? AND received_at < ? ORDER BY received_at",
            (*agent_ids, since, until),
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            out.setdefault(r["agent_id"], []).append(r["received_at"])
        return out

    async def first_collected_many(self, agent_ids: list[str]) -> dict[str, str]:
        """The oldest stored ``collected_at`` per agent (served from the index).

        Availability treats time before a host's first snapshot as "not enrolled
        yet". ``collected_at`` rather than ``received_at`` because the
        ``(agent_id, collected_at)`` index answers it without touching a row.
        """

        if not agent_ids:
            return {}
        marks = ",".join("?" * len(agent_ids))
        async with self._conn.execute(
            f"SELECT agent_id, MIN(collected_at) AS first FROM snapshots "
            f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
            tuple(agent_ids),
        ) as cur:
            rows = await cur.fetchall()
        return {r["agent_id"]: r["first"] for r in rows if r["first"] is not None}

    _PRUNE_CHUNK = 500

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Delete snapshots older than the retention window. Returns rows deleted.

        ``retention_days`` overrides ``self.retention_days`` for this call —
        the operator-configurable live setting (ADR-0051) resolves it fresh on
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

    async def clear_web_activity_domains(self, agent_id: str) -> int:
        """Empty ``web_activity.domains`` in every stored snapshot of ``agent_id``.

        The storage half of turning a host's web history to ``violations``
        (ADR-0069): the matches stay in the snapshot's ``flagged`` annotation,
        every other observed domain is dropped. Returns the snapshots rewritten.

        Rewrites whole ~90 KB rows, so — like :meth:`prune` — it works in small
        chunks, each its own transaction under :func:`write_lock`, with a
        scheduling yield between them. The predicate excludes rows whose
        ``domains`` is already an empty array, which is what makes the loop end.
        """

        total = 0
        while True:
            async with write_lock():
                cur = await self._conn.execute(
                    "UPDATE snapshots SET snapshot = "
                    "json_set(snapshot, '$.web_activity.domains', json('[]')) "
                    "WHERE id IN (SELECT id FROM snapshots WHERE agent_id = ? "
                    "AND json_type(snapshot, '$.web_activity.domains') IS NOT NULL "
                    "AND NOT (json_type(snapshot, '$.web_activity.domains') = 'array' "
                    "AND json_array_length(snapshot, '$.web_activity.domains') = 0) "
                    "LIMIT ?)",
                    (agent_id, self._PRUNE_CHUNK),
                )
                await self._conn.commit()
            updated = cur.rowcount or 0
            total += updated
            if updated < self._PRUNE_CHUNK:
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


_PRESENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS presence_sessions (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    connected_at    TEXT NOT NULL,
    disconnected_at TEXT,
    end_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_presence_sessions_agent_time
    ON presence_sessions (agent_id, connected_at);
CREATE TABLE IF NOT EXISTS server_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    last_alive_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_boots (
    agent_id TEXT NOT NULL,
    boot_at  TEXT NOT NULL,
    PRIMARY KEY (agent_id, boot_at)
);
CREATE TABLE IF NOT EXISTS presence_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Why a presence session ended. ``disconnect``: the socket closed.
#: ``heartbeat_timeout``: no frame for three heartbeat intervals (docs/protocol.md
#: § ping/pong). ``server_stopped``: the server shut down, or crashed and closed
#: the session at its last sign of life on the next start. ``superseded``: the
#: agent reconnected on a new socket before the old one died.
PRESENCE_END_REASONS: tuple[str, ...] = (
    "disconnect",
    "heartbeat_timeout",
    "server_stopped",
    "superseded",
)

#: Boot instants this close together are one boot (docs/protocol.md § uptime):
#: the agent derives ``boot_time_unix`` from now minus uptime, so successive
#: pushes of one boot jitter by a second or two.
BOOT_SAME_WITHIN_SECS = 120


def _presence_ts(value: datetime | None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _boot_iso(boot_unix: int) -> str:
    # Whole seconds, one fixed format: host_boots.boot_at is range-compared as text.
    return datetime.fromtimestamp(boot_unix, timezone.utc).isoformat()


class PresenceStore:
    """The server's own record of when each host was reachable.

    Three tables. ``presence_sessions`` holds one row per tunnel connection
    (connect to disconnect/heartbeat timeout). ``server_runs`` holds the spans the
    server itself was running, so time it was down reads as *unknown* rather than
    as every host being offline. ``host_boots`` holds the distinct reboot instants
    the ``uptime`` section reported. ``availability.py`` turns them into segments.

    ``presence_meta`` records one-time work (the boot backfill), the same way
    ``shell_allow_meta`` records the shipped-defaults offer.

    Retention follows the telemetry snapshots (``KENNY_TELEMETRY_RETENTION_DAYS``):
    before the ledger, availability is reconstructed from snapshot arrival times,
    so the two have to age together.

    Reads use a second connection (``_rconn``). A read cancelled mid-flight (an
    HTTP request going away, the boot backfill stopped at shutdown) has its cursor
    finalized on the event loop's thread; if that coincides with a write on the
    same connection, the write's busy wait holds the connection mutex the
    finalizer needs, and both sit out the whole ``busy_timeout``. Writes here keep
    the tunnel's sessions and the run heartbeat, so they never share a connection
    with a read that can be cancelled.
    """

    _BOOTS_BACKFILLED = "boots_backfilled"

    def __init__(
        self, db_path: str = DEFAULT_DB_PATH, retention_days: int = TELEMETRY_RETENTION_DAYS
    ) -> None:
        self.db_path = db_path
        self.retention_days = retention_days
        self._db: aiosqlite.Connection | None = None
        self._rdb: aiosqlite.Connection | None = None
        #: The ``server_runs`` row of this process, set by :meth:`start_run`.
        self.run_id: int | None = None
        # Last boot instant noted per agent, so the common case -- the same boot
        # reported on every push -- costs no query.
        self._last_boot: dict[str, int] = {}

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_PRESENCE_SCHEMA)
        await self._db.commit()
        self._rdb = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._rdb)

    async def close(self) -> None:
        for db in (self._rdb, self._db):
            if db is not None:
                await db.close()
        self._db = self._rdb = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        """The write connection."""

        if self._db is None:
            raise RuntimeError("PresenceStore is not connected; call connect() first")
        return self._db

    @property
    def _rconn(self) -> aiosqlite.Connection:
        """The read connection (see the class docstring)."""

        if self._rdb is None:
            raise RuntimeError("PresenceStore is not connected; call connect() first")
        return self._rdb

    # -- server runs -------------------------------------------------------

    async def start_run(self, now: datetime | None = None) -> int:
        """Open this process's run; close whatever the previous one left open.

        Sessions still open belong to a previous process that did not shut down
        cleanly. They end at that run's last sign of life (``last_alive_at``, kept
        fresh by :meth:`touch_run`), so a crash misattributes at most one touch
        interval. The gap until now is covered by no run and reads as unknown.
        """

        stamp = _presence_ts(now)
        async with write_lock():
            async with self._rconn.execute(
                "SELECT last_alive_at FROM server_runs ORDER BY started_at DESC, id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            ended = row["last_alive_at"] if row is not None else stamp
            await self._conn.execute(
                "UPDATE presence_sessions SET disconnected_at = MAX(connected_at, ?), "
                "end_reason = 'server_stopped' WHERE disconnected_at IS NULL",
                (ended,),
            )
            cur = await self._conn.execute(
                "INSERT INTO server_runs (started_at, last_alive_at) VALUES (?, ?)",
                (stamp, stamp),
            )
            await self._conn.commit()
        self.run_id = int(cur.lastrowid)
        return self.run_id

    async def touch_run(self, now: datetime | None = None) -> None:
        """Advance this run's ``last_alive_at`` (called about once a minute)."""

        if self.run_id is None:
            return
        async with write_lock():
            await self._conn.execute(
                "UPDATE server_runs SET last_alive_at = ? WHERE id = ?",
                (_presence_ts(now), self.run_id),
            )
            await self._conn.commit()

    async def stop_run(self, now: datetime | None = None) -> None:
        """Graceful shutdown: end every open session and this run at ``now``."""

        stamp = _presence_ts(now)
        async with write_lock():
            await self._conn.execute(
                "UPDATE presence_sessions SET disconnected_at = MAX(connected_at, ?), "
                "end_reason = 'server_stopped' WHERE disconnected_at IS NULL",
                (stamp,),
            )
            await self._conn.commit()
        if self.run_id is not None:
            async with write_lock():
                await self._conn.execute(
                    "UPDATE server_runs SET last_alive_at = ? WHERE id = ?",
                    (stamp, self.run_id),
                )
                await self._conn.commit()
        self.run_id = None

    # -- sessions ----------------------------------------------------------

    async def open_session(self, agent_id: str, now: datetime | None = None) -> int:
        """Record that ``agent_id`` connected; returns the session id."""

        async with write_lock():
            cur = await self._conn.execute(
                "INSERT INTO presence_sessions (agent_id, connected_at) VALUES (?, ?)",
                (agent_id, _presence_ts(now)),
            )
            await self._conn.commit()
        return int(cur.lastrowid)

    async def close_session(
        self, session_id: int, reason: str, now: datetime | None = None
    ) -> bool:
        """End an open session. A session already ended is left as it is."""

        if reason not in PRESENCE_END_REASONS:
            raise ValueError(f"unknown presence end reason {reason!r}")
        async with write_lock():
            cur = await self._conn.execute(
                "UPDATE presence_sessions SET disconnected_at = MAX(connected_at, ?), "
                "end_reason = ? WHERE id = ? AND disconnected_at IS NULL",
                (_presence_ts(now), reason, session_id),
            )
            await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -- boots -------------------------------------------------------------

    async def note_boot(
        self, agent_id: str, boot_unix: Any, now: datetime | None = None
    ) -> bool:
        """Record a reported boot instant unless one within 120 s is already known.

        Returns True when a new boot was recorded. Values that are not a plausible
        Unix time (not an int, not positive, more than a day in the future) are
        ignored: the value comes from the agent and is not trusted.
        """

        if isinstance(boot_unix, bool) or not isinstance(boot_unix, int):
            return False
        horizon = (now or datetime.now(timezone.utc)).timestamp() + 86400
        if boot_unix <= 0 or boot_unix > horizon:
            return False
        cached = self._last_boot.get(agent_id)
        if cached is not None and abs(cached - boot_unix) <= BOOT_SAME_WITHIN_SECS:
            return False
        recorded = await self._insert_boot(agent_id, boot_unix)
        self._last_boot[agent_id] = boot_unix
        return recorded

    async def _insert_boot(self, agent_id: str, boot_unix: int) -> bool:
        low = _boot_iso(max(0, boot_unix - BOOT_SAME_WITHIN_SECS))
        high = _boot_iso(boot_unix + BOOT_SAME_WITHIN_SECS)
        async with write_lock():
            # Under the write lock, so no other writer in this process can slip a
            # boot in between this check and the insert below.
            async with self._rconn.execute(
                "SELECT 1 FROM host_boots WHERE agent_id = ? AND boot_at BETWEEN ? AND ? LIMIT 1",
                (agent_id, low, high),
            ) as cur:
                if await cur.fetchone() is not None:
                    return False
            await self._conn.execute(
                "INSERT OR IGNORE INTO host_boots (agent_id, boot_at) VALUES (?, ?)",
                (agent_id, _boot_iso(boot_unix)),
            )
            await self._conn.commit()
        return True

    async def backfill_boots_from_snapshots(self) -> int:
        """Once per database: record the boot instants already in stored snapshots.

        Reads ``uptime.boot_time_unix`` out of the ``snapshots`` table in SQL, so
        reboots from before the presence ledger existed still show. Recorded in
        ``presence_meta`` whatever it found, so it never runs twice. Returns how
        many boots were added.
        """

        async with self._rconn.execute(
            "SELECT 1 FROM presence_meta WHERE key = ?", (self._BOOTS_BACKFILLED,)
        ) as cur:
            if await cur.fetchone() is not None:
                return 0
        async with self._rconn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'snapshots'"
        ) as cur:
            has_snapshots = await cur.fetchone() is not None
        by_agent: dict[str, list[int]] = {}
        if has_snapshots:
            # ``instr`` first so only snapshots that carry the field are parsed.
            async with self._rconn.execute(
                "SELECT agent_id, json_extract(snapshot, '$.uptime.boot_time_unix') AS boot "
                "FROM snapshots WHERE instr(snapshot, 'boot_time_unix') > 0"
            ) as cur:
                rows = await cur.fetchall()
            for r in rows:
                boot = r["boot"]
                if isinstance(boot, int) and not isinstance(boot, bool) and boot > 0:
                    by_agent.setdefault(r["agent_id"], []).append(boot)
        added = 0
        for agent_id, boots in by_agent.items():
            kept: list[int] = []
            for boot in sorted(set(boots)):
                if kept and boot - kept[-1] <= BOOT_SAME_WITHIN_SECS:
                    continue
                kept.append(boot)
            for boot in kept:
                if await self._insert_boot(agent_id, boot):
                    added += 1
        async with write_lock():
            await self._conn.execute(
                "INSERT OR REPLACE INTO presence_meta (key, value) VALUES (?, ?)",
                (self._BOOTS_BACKFILLED, _presence_ts(None)),
            )
            await self._conn.commit()
        return added

    # -- reads -------------------------------------------------------------

    async def ledger_epoch(self) -> str | None:
        """When the presence record begins: the earliest run's start, or None."""

        async with self._rconn.execute("SELECT MIN(started_at) AS epoch FROM server_runs") as cur:
            row = await cur.fetchone()
        return row["epoch"] if row is not None else None

    async def runs(self, since: str, until: str) -> list[dict[str, Any]]:
        """Server runs overlapping ``[since, until)``, oldest first.

        ``current`` marks this process's run; its ``last_alive_at`` trails the
        truth by up to one touch interval, so readers extend it to now.
        """

        async with self._rconn.execute(
            "SELECT id, started_at, last_alive_at FROM server_runs "
            "WHERE (started_at < ? AND last_alive_at >= ?) OR id = ? ORDER BY started_at",
            (until, since, self.run_id),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "started_at": r["started_at"],
                "last_alive_at": r["last_alive_at"],
                "current": r["id"] == self.run_id,
            }
            for r in rows
        ]

    async def sessions(self, agent_id: str, since: str, until: str) -> list[dict[str, Any]]:
        """Sessions of ``agent_id`` overlapping ``[since, until)``, oldest first."""

        return (await self.sessions_many([agent_id], since, until)).get(agent_id, [])

    async def sessions_many(
        self, agent_ids: list[str], since: str, until: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Batch :meth:`sessions`. An open session has ``disconnected_at`` None."""

        out: dict[str, list[dict[str, Any]]] = {}
        if not agent_ids:
            return out
        marks = ",".join("?" * len(agent_ids))
        async with self._rconn.execute(
            "SELECT agent_id, connected_at, disconnected_at, end_reason FROM presence_sessions "
            f"WHERE agent_id IN ({marks}) AND connected_at < ? "
            "AND (disconnected_at IS NULL OR disconnected_at > ?) ORDER BY connected_at",
            (*agent_ids, until, since),
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            out.setdefault(r["agent_id"], []).append(
                {
                    "connected_at": r["connected_at"],
                    "disconnected_at": r["disconnected_at"],
                    "end_reason": r["end_reason"],
                }
            )
        return out

    async def first_session_many(self, agent_ids: list[str]) -> dict[str, str]:
        """The earliest recorded ``connected_at`` per agent."""

        if not agent_ids:
            return {}
        marks = ",".join("?" * len(agent_ids))
        async with self._rconn.execute(
            "SELECT agent_id, MIN(connected_at) AS first FROM presence_sessions "
            f"WHERE agent_id IN ({marks}) GROUP BY agent_id",
            tuple(agent_ids),
        ) as cur:
            rows = await cur.fetchall()
        return {r["agent_id"]: r["first"] for r in rows if r["first"] is not None}

    async def boots(self, agent_id: str, since: str, until: str) -> list[str]:
        """Boot instants of ``agent_id`` in ``[since, until]``, oldest first."""

        return (await self.boots_many([agent_id], since, until)).get(agent_id, [])

    async def boots_many(
        self, agent_ids: list[str], since: str, until: str
    ) -> dict[str, list[str]]:
        """Batch :meth:`boots`."""

        out: dict[str, list[str]] = {}
        if not agent_ids:
            return out
        marks = ",".join("?" * len(agent_ids))
        async with self._rconn.execute(
            f"SELECT agent_id, boot_at FROM host_boots WHERE agent_id IN ({marks}) "
            "AND boot_at >= ? AND boot_at <= ? ORDER BY boot_at",
            (*agent_ids, since, until),
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            out.setdefault(r["agent_id"], []).append(r["boot_at"])
        return out

    # -- retention ---------------------------------------------------------

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Age the record out past the retention window. Returns rows deleted.

        What straddles the cutoff is clipped to it rather than kept whole: the
        ledger then begins exactly where the retained snapshots do, and time
        before it reads as unknown instead of as a run with its sessions pruned
        away (which would read as offline). This process's own run is never
        deleted.
        """

        now = now or datetime.now(timezone.utc)
        days = retention_days if retention_days is not None else self.retention_days
        cutoff = (now - timedelta(days=days)).isoformat()
        total = 0
        statements: list[tuple[str, tuple[Any, ...], bool]] = [
            (
                "DELETE FROM presence_sessions "
                "WHERE disconnected_at IS NOT NULL AND disconnected_at < ?",
                (cutoff,),
                True,
            ),
            (
                "UPDATE presence_sessions SET connected_at = ? WHERE connected_at < ?",
                (cutoff, cutoff),
                False,
            ),
            (
                "DELETE FROM server_runs WHERE last_alive_at < ? AND id IS NOT ?",
                (cutoff, self.run_id),
                True,
            ),
            (
                "UPDATE server_runs SET started_at = ? WHERE started_at < ?",
                (cutoff, cutoff),
                False,
            ),
            ("DELETE FROM host_boots WHERE boot_at < ?", (cutoff,), True),
        ]
        for sql, params, counts in statements:
            async with write_lock():
                cur = await self._conn.execute(sql, params)
                await self._conn.commit()
            if counts:
                total += cur.rowcount or 0
        return total

    async def delete_agent(self, agent_id: str) -> int:
        """Delete every session and boot of ``agent_id`` (host removed from inventory)."""

        total = 0
        for table in ("presence_sessions", "host_boots"):
            async with write_lock():
                cur = await self._conn.execute(
                    f"DELETE FROM {table} WHERE agent_id = ?", (agent_id,)
                )
                await self._conn.commit()
            total += cur.rowcount or 0
        self._last_boot.pop(agent_id, None)
        return total


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
    fields    TEXT,
    ticket_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time
    ON events (at DESC);
CREATE INDEX IF NOT EXISTS idx_events_agent_time
    ON events (agent_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind_time
    ON events (kind, at DESC);
"""


def _alert_row(row: Any) -> dict[str, Any]:
    """Shape one ``kind='alert'`` row for a reader that wants title and body apart.

    ``message`` is the notification's title and body joined by a newline (see
    ``AlertEngine._dispatch``). The title is also carried verbatim in ``fields``
    so a title that itself contains a newline survives the round trip; splitting
    ``message`` is the fallback for rows written before that field existed.
    """

    fields: dict[str, Any] = {}
    raw = row["fields"]
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            fields = parsed
    message = row["message"] or ""
    title = fields.get("title")
    if isinstance(title, str) and title:
        body = message[len(title) :].lstrip("\n") if message.startswith(title) else message
    else:
        title, _, body = message.partition("\n")
    return {
        "id": row["id"],
        "at": row["at"],
        "agent_id": row["agent_id"],
        "level": row["level"],
        "title": title,
        "body": body,
        "priority": fields.get("priority", ""),
        "event_type": fields.get("event_type", ""),
    }


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
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add ``ticket_id`` to ``events`` for DBs created before it existed."""

        async with self._conn.execute("PRAGMA table_info(events)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "ticket_id" not in cols:
            await self._conn.execute("ALTER TABLE events ADD COLUMN ticket_id TEXT")
        # An index over a migrated column belongs here, not in ``_EVENTS_SCHEMA``:
        # the schema script runs first, so on a database whose ``events`` table
        # predates the column a CREATE INDEX there fails with "no such column"
        # and takes the whole boot with it. Fresh databases reach this line too,
        # so the index is created exactly once either way.
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_ticket ON events (ticket_id, at)"
        )

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
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Store a forwarded tool-call audit event (kind='audit', source='server').

        ``fields`` (actor, run_id, redacted args) is stored as JSON in the
        ``fields`` column; callers pass it already redacted.
        """

        at = at or datetime.now(timezone.utc).isoformat()
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO events (at, agent_id, source, kind, tool, ok, error, fields) "
                "VALUES (?, ?, 'server', 'audit', ?, ?, ?, ?)",
                (
                    at,
                    agent_id,
                    tool,
                    1 if ok else 0,
                    error,
                    json.dumps(fields) if fields is not None else None,
                ),
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
    ) -> int:
        """Store an emitted operator alert (kind='alert', source='server').

        Alert history reuses the events table (ADR-0027): the Activity view and
        the weekly digest read these back via ``query(kind='alert')``.

        Returns the row's id so the caller can link it to the ticket it opened
        (:meth:`link_alert_to_ticket`). The alert is recorded before that
        decision is made and is never delayed by it, so the back-reference is
        filled in afterwards rather than written with the row.
        """

        at = at or datetime.now(timezone.utc).isoformat()
        async with write_lock():
            cur = await self._conn.execute(
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
            event_id = cur.lastrowid
            await self._conn.commit()
        return int(event_id or 0)

    async def link_alert_to_ticket(self, event_id: int, ticket_id: str) -> None:
        """Record that ``event_id`` is one of ``ticket_id``'s alerts.

        Scoped to ``kind='alert'``: every other event kind reaches this table
        through a different writer and has no ticket to belong to.
        """

        async with write_lock():
            await self._conn.execute(
                "UPDATE events SET ticket_id = ? WHERE id = ? AND kind = 'alert'",
                (ticket_id, event_id),
            )
            await self._conn.commit()

    async def alerts_for_ticket(self, ticket_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return this ticket's alerts oldest-first.

        Chronological, unlike :meth:`query` — the opening transition first and
        every recurrence deduplication attached to the ticket after it, which
        is the order the story happened in.

        The list is bounded by event retention (~30 days), not by the ticket's
        lifetime: an old ticket's opening alert can already be pruned away, and
        a caller must render that as an empty history rather than an error.
        """

        if not ticket_id:
            return []
        async with self._conn.execute(
            "SELECT id, at, agent_id, level, message, fields FROM events "
            "WHERE ticket_id = ? AND kind = 'alert' ORDER BY at ASC, id ASC LIMIT ?",
            (ticket_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_alert_row(row) for row in rows]

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

    async def query_log(
        self,
        *,
        kind: str | None = None,
        q: str | None = None,
        agent_ids: list[str] | None = None,
        before: tuple[str, int] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search/paginate events for ``/api/log`` (tools/alerts/events, merged).

        ``agent_ids`` is the caller's visible-host allowlist (``None`` means
        unrestricted; an *empty* list means "nothing visible" and short-circuits
        without a query) — the scoping happens in SQL, not by filtering the page
        after the fact, so a scoped caller's page is never short (see
        ``api_events``'s post-filter, which this deliberately does not repeat).
        ``before`` is an exclusive keyset cursor over ``(at, id)`` (both already
        indexed by ``idx_events_time``'s ``at DESC`` and the primary key) — the
        caller reads the last returned row's ``(at, id)`` back in for the next
        page. ``q`` is a plain ``LIKE`` scan over message/tool/target; there is
        no FTS table, consistent with this codebase's scale (see module ADR-0017).
        """

        if agent_ids is not None and not agent_ids:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if agent_ids is not None:
            clauses.append(f"agent_id IN ({', '.join('?' for _ in agent_ids)})")
            params.extend(agent_ids)
        if q:
            like = f"%{q}%"
            clauses.append("(message LIKE ? OR tool LIKE ? OR target LIKE ?)")
            params.extend([like, like, like])
        if before is not None:
            clauses.append("(at, id) < (?, ?)")
            params.extend([before[0], before[1]])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._conn.execute(
            "SELECT id, at, agent_id, source, level, kind, tool, ok, error, target, message, fields "
            f"FROM events {where} ORDER BY at DESC, id DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": int(row["id"]),
                **self._row_to_event(row),
            }
            for row in rows
        ]

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Delete events older than the retention window. Returns rows deleted.

        ``retention_days`` overrides ``self.retention_days`` for this call —
        see :meth:`TelemetryStore.prune` for why (ADR-0051's live-retention
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
CREATE TABLE IF NOT EXISTS notification_messages (
    channel     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    doc         TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_notification_messages_open
    ON notification_messages (channel, agent_id, resolved_at);
"""


class AlertStateStore:
    """Async SQLite-backed last-known alert state per (agent, scope).

    ``scope`` is ``'offline'``, ``'missing'``, ``'overall'``,
    ``'section:<name>'``, ``'pushed:section:<name>'``, ``'change:<section>'``,
    ``'digest'`` or ``'daily'``. Persisting the state (rather than keeping it in
    memory) means a server restart does not re-fire alerts for conditions that
    were already notified (ADR-0027). State rows are tiny and pruned implicitly
    by being overwritten.

    The same database also holds ``notification_messages``: the channel message
    each pushed alert became, so a recovery can edit that message in place
    instead of posting a new one (ADR-0067). Those rows do accumulate and are
    pruned by :meth:`prune`.
    """

    retention_days = 30

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
        last_notified_at: str | None = None,
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

    async def remove(self, agent_id: str, scope: str) -> bool:
        """Drop one ``(agent_id, scope)`` row; ``True`` if it existed.

        Used for the transient ``pending:`` scopes the alert loop keeps while
        an escalation waits for confirmation or for its cooldown -- those are
        candidates, not state, and must not outlive the episode that produced
        them.
        """

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM alert_state WHERE agent_id = ? AND scope = ?", (agent_id, scope)
            )
            await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def all_rows(self) -> list[dict[str, Any]]:
        """Every state row across the fleet (the daily summary's input)."""

        async with self._conn.execute(
            "SELECT agent_id, scope, status, since, last_notified_at FROM alert_state "
            "ORDER BY agent_id, scope"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_agent(self, agent_id: str) -> int:
        """Delete all alert state for ``agent_id`` (host removed from inventory)."""

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM alert_state WHERE agent_id = ?", (agent_id,)
            )
            await self._conn.execute(
                "DELETE FROM notification_messages WHERE agent_id = ?", (agent_id,)
            )
            await self._conn.commit()
        return cur.rowcount or 0

    # -- channel messages a recovery can edit (ADR-0067) -----------------------

    async def save_message(
        self, channel: str, message_id: str, agent_id: str, doc: dict[str, Any], *, at: str
    ) -> None:
        async with write_lock():
            await self._conn.execute(
                "INSERT OR REPLACE INTO notification_messages "
                "(channel, message_id, agent_id, created_at, resolved_at, doc) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (channel, message_id, agent_id, at, json.dumps(doc)),
            )
            await self._conn.commit()

    async def open_messages(self, channel: str, agent_id: str) -> list[dict[str, Any]]:
        """Unresolved messages for one host on one channel, oldest first."""

        async with self._conn.execute(
            "SELECT channel, message_id, agent_id, created_at, doc FROM notification_messages "
            "WHERE channel = ? AND agent_id = ? AND resolved_at IS NULL ORDER BY created_at",
            (channel, agent_id),
        ) as cur:
            rows = await cur.fetchall()
        return [{**dict(r), "doc": json.loads(r["doc"])} for r in rows]

    async def update_message(
        self, channel: str, message_id: str, doc: dict[str, Any], *, resolved_at: str | None
    ) -> None:
        async with write_lock():
            await self._conn.execute(
                "UPDATE notification_messages SET doc = ?, resolved_at = ? "
                "WHERE channel = ? AND message_id = ?",
                (json.dumps(doc), resolved_at, channel, message_id),
            )
            await self._conn.commit()

    async def drop_message(self, channel: str, message_id: str) -> None:
        async with write_lock():
            await self._conn.execute(
                "DELETE FROM notification_messages WHERE channel = ? AND message_id = ?",
                (channel, message_id),
            )
            await self._conn.commit()

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Forget channel messages older than the retention window.

        A message this old is scrolled out of any channel a person reads; an
        episode still open after it simply resolves without an edit.
        """

        now = now or datetime.now(timezone.utc)
        days = retention_days if retention_days is not None else self.retention_days
        cutoff = (now - timedelta(days=days)).isoformat()
        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM notification_messages WHERE created_at < ?", (cutoff,)
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

    Persists ONLY operator additions (ADR-0020); built-in rules live in the
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


_SHELL_ALLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS shell_allow_rules (
    id          TEXT NOT NULL,
    agent_id    TEXT NOT NULL DEFAULT '',
    applies_to  TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (agent_id, id)
);
CREATE INDEX IF NOT EXISTS idx_shell_allow_created
    ON shell_allow_rules (agent_id, created_at);
CREATE TABLE IF NOT EXISTS shell_allow_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


class ShellAllowStore:
    """Async SQLite-backed store for the fleet's shell allow rules (ADR-0064).

    The rules that say what ``powershell_exec`` and ``shell_exec`` may run while the
    fleet is in ``allowlist`` mode. The mode itself is a setting
    (``KENNY_SHELL_POLICY_MODE``); this holds only the rules.

    ``agent_id`` is **reserved**: ADR-0064 keeps a per-agent axis open but does not
    build it, so every row written today carries the fleet-wide sentinel ``''`` and
    every read filters on it. An empty string rather than NULL, because SQLite treats
    NULLs as pairwise-distinct in a uniqueness check and the primary key here spans it
    (the convention ADR-0041 established for ``reliability_suppressions``). One column
    now costs less than a migration later.
    """

    #: The fleet-wide scope. The only value written until a per-agent axis exists.
    FLEET = ""

    #: ``shell_allow_meta`` key recording that the shipped defaults were offered once.
    _SEEDED = "defaults_seeded"

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        #: True when this connect created the rule table: a new database, not one an
        #: operator has already curated (or deliberately emptied).
        self.created = False

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shell_allow_rules'"
        ) as cur:
            self.created = await cur.fetchone() is None
        await self._db.executescript(_SHELL_ALLOW_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ShellAllowStore is not connected; call connect() first")
        return self._db

    async def list(self) -> list[dict[str, Any]]:
        """Return the fleet's allow rules, oldest-first."""

        async with self._conn.execute(
            "SELECT id, applies_to, pattern, reason FROM shell_allow_rules "
            "WHERE agent_id = ? ORDER BY created_at, id",
            (self.FLEET,),
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
        self, *, id: str, applies_to: str, pattern: str, reason: str, created_by: str = ""
    ) -> None:
        """Insert (or replace) a fleet allow rule, stamping ``created_at``."""

        created_at = datetime.now(timezone.utc).isoformat()
        async with write_lock():
            await self._conn.execute(
                "INSERT OR REPLACE INTO shell_allow_rules "
                "(id, agent_id, applies_to, pattern, reason, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (id, self.FLEET, applies_to, pattern, reason, created_at, created_by),
            )
            await self._conn.commit()

    async def remove(self, id: str) -> bool:
        """Delete one fleet allow rule by id. Returns True if a row was removed."""

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM shell_allow_rules WHERE agent_id = ? AND id = ?",
                (self.FLEET, id),
            )
            await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def _insert_all(self, rules: list[dict[str, Any]], created_by: str) -> None:
        """Insert ``rules`` keeping their order: ``list`` sorts by ``created_at``."""

        base = datetime.now(timezone.utc)
        await self._conn.executemany(
            "INSERT OR REPLACE INTO shell_allow_rules "
            "(id, agent_id, applies_to, pattern, reason, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r["id"], self.FLEET, r["applies_to"], r["pattern"], r["reason"],
                    (base + timedelta(microseconds=i)).isoformat(), created_by,
                )
                for i, r in enumerate(rules)
            ],
        )

    async def _mark_seeded(self) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO shell_allow_meta (key, value) VALUES (?, ?)",
            (self._SEEDED, datetime.now(timezone.utc).isoformat()),
        )

    async def seed_defaults(self, rules: list[dict[str, Any]], *, seed: bool) -> bool:
        """Offer the shipped defaults once per database. Returns True if it seeded.

        The first call records that the offer was made, whatever it decides, so a
        later start never re-adds rules an operator removed. It seeds only when
        ``seed`` is true and the fleet list is empty: rules an operator wrote are
        never mixed with the defaults behind their back.
        """

        async with write_lock():
            async with self._conn.execute(
                "SELECT 1 FROM shell_allow_meta WHERE key = ?", (self._SEEDED,)
            ) as cur:
                if await cur.fetchone() is not None:
                    return False
            async with self._conn.execute(
                "SELECT COUNT(*) FROM shell_allow_rules WHERE agent_id = ?", (self.FLEET,)
            ) as cur:
                empty = (await cur.fetchone())[0] == 0
            seeded = seed and empty and bool(rules)
            if seeded:
                await self._insert_all(rules, created_by="kenny")
            await self._mark_seeded()
            await self._conn.commit()
        return seeded

    async def reset_to_defaults(
        self, rules: list[dict[str, Any]], *, created_by: str = ""
    ) -> None:
        """Replace the whole fleet list with ``rules`` in one transaction."""

        async with write_lock():
            await self._conn.execute(
                "DELETE FROM shell_allow_rules WHERE agent_id = ?", (self.FLEET,)
            )
            await self._insert_all(rules, created_by)
            await self._mark_seeded()
            await self._conn.commit()


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
    suppression rules (issue #166 / see ADR-0041).

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


_EVENT_CLASSIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_classifications (
    source        TEXT    NOT NULL DEFAULT '',
    event_id      INTEGER NOT NULL,
    category      TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    cause         TEXT    NOT NULL DEFAULT '',
    user_impact   TEXT    NOT NULL DEFAULT 'unknown',
    symptom       TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    classified_at TEXT    NOT NULL,
    PRIMARY KEY (source, event_id)
);
"""

# Columns added after the table first shipped. A DB created before them keeps
# its rows; they read as ``user_impact='unknown'`` until the classifier reaches
# them again, which the model-string bump that introduced them forces anyway.
_CLASSIFICATION_MIGRATED_COLUMNS = {
    "user_impact": "TEXT NOT NULL DEFAULT 'unknown'",
    "symptom": "TEXT NOT NULL DEFAULT ''",
}


class EventClassificationStore:
    """Async SQLite-backed store for the server's LLM verdicts on reliability
    event patterns (ADR-0026 categorization, made durable by ADR-0058).

    One row per ``(source, event_id)`` pattern -- the same empty-string
    ``source`` sentinel and int ``event_id`` normalization
    :mod:`kenny_server.event_categories` keys its cache on. A classification
    is a fact about the pattern, not about a host, so there is no
    ``agent_id`` column and removing a host from inventory does not touch
    this table. ``model`` records which classifier produced the row so a
    model upgrade can re-classify instead of trusting stale verdicts
    (:meth:`delete_model_except`).
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_EVENT_CLASSIFICATION_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add the impact columns to DBs created before they existed."""

        async with self._conn.execute("PRAGMA table_info(event_classifications)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        for col, ddl in _CLASSIFICATION_MIGRATED_COLUMNS.items():
            if col not in cols:
                await self._conn.execute(
                    f"ALTER TABLE event_classifications ADD COLUMN {col} {ddl}"
                )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("EventClassificationStore is not connected; call connect() first")
        return self._db

    async def list(self) -> list[dict[str, Any]]:
        """Return every persisted classification."""

        async with self._conn.execute(
            "SELECT source, event_id, category, severity, cause, user_impact, symptom, "
            "model, classified_at FROM event_classifications ORDER BY source, event_id"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def upsert_many(self, rows: list[dict[str, Any]]) -> None:
        """Insert or replace classifications. Each row carries ``source``,
        ``event_id``, ``category``, ``severity``, ``cause``, ``user_impact``,
        ``symptom`` and ``model``; ``classified_at`` is stamped here."""

        if not rows:
            return
        classified_at = datetime.now(timezone.utc).isoformat()
        async with write_lock():
            await self._conn.executemany(
                "INSERT OR REPLACE INTO event_classifications "
                "(source, event_id, category, severity, cause, user_impact, symptom, "
                "model, classified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(r.get("source") or ""),
                        int(r["event_id"]),
                        str(r["category"]),
                        str(r["severity"]),
                        str(r.get("cause") or ""),
                        str(r.get("user_impact") or "unknown"),
                        str(r.get("symptom") or ""),
                        str(r.get("model") or ""),
                        classified_at,
                    )
                    for r in rows
                ],
            )
            await self._conn.commit()

    async def delete_model_except(self, model: str) -> int:
        """Drop rows produced by any classifier other than ``model``, so a
        model upgrade re-classifies rather than serving stale verdicts.
        Returns the number of rows deleted."""

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM event_classifications WHERE model != ?", (model,)
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
    (see ticket_rules.py).

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
    reproduces the coded default exactly.
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

    # POSSIBLY DEAD: no webui route or MCP tool flips a backup target's
    # enabled flag today — only tests call this directly.
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
-- Per-agent operator-desired release channel (ADR-0048): the soll side of the
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
# columns (ADR-0048).
_CAMPAIGN_MIGRATED_COLUMNS: dict[str, str] = {
    "channel": "TEXT NOT NULL DEFAULT 'stable'",
}


def _availability_key(component: str, channel: str = "stable") -> str:
    """Compose the ``update_availability.component`` key for ``(component, channel)``.

    ``channel="stable"`` is byte-identical to the pre-ADR-0048 key (``"agent"``/
    ``"server"``) — existing rows are untouched. ``channel="dev"`` composes
    ``"agent:dev"``/``"server:dev"``, a second, additive row alongside the
    stable one. The ``update_availability`` table's schema/PK is unchanged;
    this only widens what a caller may pass as ``component``.
    """

    return component if channel == "stable" else f"{component}:{channel}"

# Bounded number of update attempts a single agent gets under one campaign
# before it is marked "held" and stops being auto-retried (ADR-0040). Prevents
# a kill-switch-off agent or a crash-looping bad release from retriggering
# forever on every reconnect.
ATTEMPT_BUDGET = 3


class UpdateStore:
    """Async SQLite-backed store for scheduled update detection + rollout (ADR-0040).

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
        ``(component, channel)`` (ADR-0048) — approving a dev campaign never
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
        self,
        campaign_id: str,
        status: str,
        *,
        from_status: str = "active",
        at_field: str | None = None,
    ) -> bool:
        """Transition a campaign out of ``from_status`` and into ``status``.

        Guarded the same way regardless of which transition this is: the
        ``UPDATE`` only touches a row currently in ``from_status``, so a stale
        or duplicate call against a campaign that already moved on (including
        one that raced it) is a silent no-op (returns ``False``) rather than
        clobbering whatever status a concurrent transition already set.

        ``from_status`` defaults to ``"active"`` — every terminal transition
        (``revoked``/``expired``/``completed``) and ``suspended`` itself leave
        ``active``. Resume is the one caller that passes
        ``from_status="suspended"`` to go back the other way. Terminal statuses
        also stamp a timestamp column (``at_field``, defaulting to the
        ``revoked_at``/``completed_at`` mapping below); ``suspended`` and
        ``active`` (resume) have no such column and leave it alone.
        """

        now = datetime.now(timezone.utc).isoformat()
        field = at_field or {
            "revoked": "revoked_at",
            "expired": "revoked_at",
            "completed": "completed_at",
        }.get(status)
        if field is None:
            cur = await self._conn.execute(
                "UPDATE update_campaigns SET status = ? WHERE id = ? AND status = ?",
                (status, campaign_id, from_status),
            )
        else:
            cur = await self._conn.execute(
                f"UPDATE update_campaigns SET status = ?, {field} = ? "
                "WHERE id = ? AND status = ?",
                (status, now, campaign_id, from_status),
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

    # -- desired channel (per-agent, operator-editable, ADR-0048) -----------

    async def get_desired_channel(self, agent_id: str) -> str:
        """The operator-desired channel for ``agent_id``, defaulting to ``"stable"``.

        This is the soll side of ADR-0048's soll/ist split — separate from
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

    # POSSIBLY DEAD: nothing reads the whole map today — callers look up one
    # agent's desired channel at a time via `get_desired_channel`. Only tests
    # call this directly.
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
    applied_ok            INTEGER,
    history               TEXT NOT NULL DEFAULT 'violations'
);
CREATE TABLE IF NOT EXISTS webfilter_domains (
    agent_id  TEXT NOT NULL,
    domain    TEXT NOT NULL,
    action    TEXT NOT NULL CHECK (action IN ('watch', 'block', 'allow')),
    note      TEXT,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (agent_id, domain)
);
CREATE TABLE IF NOT EXISTS webfilter_windows (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    days       TEXT NOT NULL,
    start_min  INTEGER NOT NULL,
    end_min    INTEGER NOT NULL,
    categories TEXT NOT NULL,
    tz         TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webfilter_windows_agent
    ON webfilter_windows (agent_id);
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

#: What a host's web filter does (ADR-0069): match nothing, record and alarm on
#: matches, or also block. Stored in the ``enabled``/``block_mode`` columns:
#: ``off`` is ``enabled=0``, ``log_only`` is ``enabled=1, block_mode=0``,
#: ``protect`` is ``enabled=1, block_mode=1``.
WEBFILTER_ENFORCEMENT_LEVELS: tuple[str, ...] = ("off", "log_only", "protect")
#: What the server keeps of a host's web activity (ADR-0069): only the matches,
#: or every observed domain.
WEBFILTER_HISTORY_MODES: tuple[str, ...] = ("violations", "full")

# Config defaults for a host that has never been configured (ADR-0024). The
# default ``history`` is not here: it is the live setting
# ``KENNY_WEBFILTER_DEFAULT_HISTORY``, passed in by the caller.
_WEBFILTER_DEFAULTS: dict[str, Any] = {
    "enforcement": "off",
    "enabled": False,
    "block_mode": False,
    "use_external_adult": True,
    "use_bypass_protection": False,
    "doh_policy": "disable",
}
_WEBFILTER_TOGGLES = (
    "use_external_adult",
    "use_bypass_protection",
)

# Columns added after the tables first shipped, backfilled into existing DB
# files the same way ``UpdateStore._migrate`` does. The category columns default
# to the pre-category behaviour: no extra categories on a config, and an
# untagged (always-in-force) custom entry. ``history`` defaults to ``full`` for
# rows that predate it, because every host was recording its full web activity
# then — an upgrade must not change what is recorded (ADR-0069).
_WEBFILTER_MIGRATED_COLUMNS: dict[str, dict[str, str]] = {
    "webfilter_config": {
        "categories": "TEXT",
        "history": "TEXT NOT NULL DEFAULT 'full'",
    },
    "webfilter_domains": {"category": "TEXT"},
}


def _enforcement_from_columns(enabled: Any, block_mode: Any) -> str:
    """Map the stored booleans onto an enforcement level.

    A ``block_mode`` without ``enabled`` is ``off``: nothing was ever pushed or
    flagged for such a host, so reading it as anything stricter would change
    behaviour on upgrade.
    """

    if not enabled:
        return "off"
    return "protect" if block_mode else "log_only"


def _check_choice(name: str, value: Any, choices: tuple[str, ...]) -> str:
    text = str(value)
    if text not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}; got {value!r}")
    return text


class WebFilterStore:
    """Async SQLite-backed store for parental-controls state (ADR-0024).

    Holds, per host: the feature config (including the enabled category set),
    the editable custom domain list (``watch``/``block``/``allow``, optionally
    tagged with a category), the schedule windows that add categories for a
    weekday/time range, and the accumulated ``web_activity`` events (server-side
    24 h+ window). Shares the DB file with the other stores but owns its own
    connection. Retention mirrors telemetry (~30 days) and applies to events
    only — config, list and schedule are operator-curated and never auto-pruned.
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
        added = await self._migrate()
        if "webfilter_config.history" in added:
            await self._materialise_observed_hosts()
        await self._db.commit()

    async def _migrate(self) -> set[str]:
        """Add later columns to DB files created before they existed.

        Returns the ``table.column`` names this call added, so a migration step
        that must run exactly once — when its column appears — can key off it.
        """

        added: set[str] = set()
        for table, columns in _WEBFILTER_MIGRATED_COLUMNS.items():
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row["name"] for row in await cur.fetchall()}
            for col, ddl in columns.items():
                if col not in existing:
                    await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    added.add(f"{table}.{col}")
        return added

    async def _materialise_observed_hosts(self) -> None:
        """Give every host with recorded web activity a config row keeping ``full``.

        Runs once, when ``history`` is added to an existing database. A host
        that was recording but never configured would otherwise fall to the
        live default for unconfigured hosts, which may be ``violations`` — and
        an upgrade must not change what is recorded (ADR-0069). Every other
        column takes its default, so the enforcement level stays ``off``.
        """

        await self._conn.execute(
            "INSERT OR IGNORE INTO webfilter_config (agent_id, history) "
            "SELECT DISTINCT e.agent_id, 'full' FROM web_activity_events e "
            "WHERE NOT EXISTS "
            "(SELECT 1 FROM webfilter_config c WHERE c.agent_id = e.agent_id)"
        )

    async def ensure_config(self, agent_id: str, *, default_history: str) -> None:
        """Fix a host's history at first contact, if it has no config row yet.

        The default for unconfigured hosts is a live setting; resolving it on
        every read would let a later change of that setting silently change what
        an already-known host records. Writing it down the first time the host
        is heard from makes the default apply to new hosts only (ADR-0069).
        """

        history = (
            default_history if default_history in WEBFILTER_HISTORY_MODES else "violations"
        )
        async with write_lock():
            await self._conn.execute(
                "INSERT OR IGNORE INTO webfilter_config (agent_id, history) VALUES (?, ?)",
                (agent_id, history),
            )
            await self._conn.commit()

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

    @staticmethod
    def _merge_categories(
        raw: Any, use_adult: bool, use_bypass: bool
    ) -> list[str]:
        """Canonical enabled-category list from the extras column + the booleans.

        ``adult`` and ``bypass`` predate the category system and keep their own
        boolean columns, which stay the source of truth for them so the existing
        API, dashboard and stored rows keep working unchanged. The ``categories``
        column holds only the categories added later. Readers see one merged
        list and never have to know about the split.
        """

        keys: set[str] = set()
        if isinstance(raw, str) and raw:
            try:
                loaded = json.loads(raw)
            except ValueError:
                loaded = []
            if isinstance(loaded, list):
                keys.update(str(k) for k in loaded)
        keys.discard("adult")
        keys.discard("bypass")
        if use_adult:
            keys.add("adult")
        if use_bypass:
            keys.add("bypass")
        return sorted(keys)

    async def get_config(
        self, agent_id: str, *, default_history: str = "violations"
    ) -> dict[str, Any]:
        """Return the host's config (defaults when never configured).

        ``enforcement`` is derived from the stored ``enabled``/``block_mode``
        columns, and those two are reported back *normalised* from it —
        ``enabled`` is "not ``off``", ``block_mode`` is "``protect``" — so a
        stored ``block_mode`` without ``enabled`` reads as ``off`` everywhere.
        ``default_history`` is what a host with no row reports (the live
        ``KENNY_WEBFILTER_DEFAULT_HISTORY``).
        """

        async with self._conn.execute(
            "SELECT enabled, block_mode, use_external_adult, use_bypass_protection, "
            "doh_policy, updated_at, applied_hash, applied_at, applied_ok, categories, "
            "history FROM webfilter_config WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            defaults = dict(_WEBFILTER_DEFAULTS)
            return {
                "agent_id": agent_id,
                **defaults,
                "history": (
                    default_history
                    if default_history in WEBFILTER_HISTORY_MODES
                    else "violations"
                ),
                "categories": self._merge_categories(
                    None,
                    bool(defaults["use_external_adult"]),
                    bool(defaults["use_bypass_protection"]),
                ),
                "updated_at": None,
                "applied_hash": None,
                "applied_at": None,
                "applied_ok": None,
            }
        enforcement = _enforcement_from_columns(row["enabled"], row["block_mode"])
        history = row["history"]
        return {
            "agent_id": agent_id,
            "enforcement": enforcement,
            # An unreadable stored value keeps the less revealing mode.
            "history": history if history in WEBFILTER_HISTORY_MODES else "violations",
            "enabled": enforcement != "off",
            "block_mode": enforcement == "protect",
            "use_external_adult": bool(row["use_external_adult"]),
            "use_bypass_protection": bool(row["use_bypass_protection"]),
            "categories": self._merge_categories(
                row["categories"],
                bool(row["use_external_adult"]),
                bool(row["use_bypass_protection"]),
            ),
            "doh_policy": row["doh_policy"],
            "updated_at": row["updated_at"],
            "applied_hash": row["applied_hash"],
            "applied_at": row["applied_at"],
            "applied_ok": None if row["applied_ok"] is None else bool(row["applied_ok"]),
        }

    async def _stored_toggles(self, agent_id: str) -> tuple[bool, bool] | None:
        """The raw ``(enabled, block_mode)`` columns, or ``None`` without a row."""

        async with self._conn.execute(
            "SELECT enabled, block_mode FROM webfilter_config WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return bool(row["enabled"]), bool(row["block_mode"])

    async def set_config(
        self, agent_id: str, *, default_history: str = "violations", **fields: Any
    ) -> dict[str, Any]:
        """Upsert a partial config change (unknown keys ignored). Returns config.

        ``enforcement`` sets both ``enabled`` and ``block_mode``; without it the
        legacy ``enabled``/``block_mode`` toggles still move their own column,
        applied to what is stored, so a caller toggling them one at a time
        gets the same result it always did. ``history`` is written as given; a
        host with no row materialises ``default_history``. Raises
        :class:`ValueError` for an unknown enforcement level or history mode,
        before anything is written.

        ``categories`` is the whole enabled set, not a delta: passing it also
        settles ``use_external_adult``/``use_bypass_protection``, so the two
        representations can never disagree. Passing one of those booleans
        *instead* still works and only moves its own category.
        """

        enforcement = fields.get("enforcement")
        if enforcement is not None:
            enforcement = _check_choice(
                "enforcement", enforcement, WEBFILTER_ENFORCEMENT_LEVELS
            )
        history = fields.get("history")
        if history is not None:
            history = _check_choice("history", history, WEBFILTER_HISTORY_MODES)

        current = await self.get_config(agent_id, default_history=default_history)
        stored = await self._stored_toggles(agent_id)
        enabled, block_mode = stored if stored is not None else (False, False)
        if enforcement is not None:
            enabled = enforcement != "off"
            block_mode = enforcement == "protect"
        else:
            if fields.get("enabled") is not None:
                enabled = bool(fields["enabled"])
            if fields.get("block_mode") is not None:
                block_mode = bool(fields["block_mode"])
        if history is not None:
            current["history"] = history
        categories = fields.get("categories")
        if categories is not None:
            wanted = {str(c) for c in categories}
            current["use_external_adult"] = "adult" in wanted
            current["use_bypass_protection"] = "bypass" in wanted
            current["categories"] = sorted(wanted)
        for key in _WEBFILTER_TOGGLES:
            if fields.get(key) is not None:
                current[key] = bool(fields[key])
        if fields.get("doh_policy") is not None:
            current["doh_policy"] = str(fields["doh_policy"])
        extras = sorted(
            set(current["categories"]) - {"adult", "bypass"}
        )
        now = datetime.now(timezone.utc).isoformat()
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO webfilter_config "
                "(agent_id, enabled, block_mode, use_external_adult, use_bypass_protection, "
                "doh_policy, updated_at, applied_hash, applied_at, applied_ok, categories, "
                "history) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET "
                "enabled=excluded.enabled, block_mode=excluded.block_mode, "
                "use_external_adult=excluded.use_external_adult, "
                "use_bypass_protection=excluded.use_bypass_protection, "
                "doh_policy=excluded.doh_policy, updated_at=excluded.updated_at, "
                "categories=excluded.categories, history=excluded.history",
                (
                    agent_id,
                    int(enabled),
                    int(block_mode),
                    int(current["use_external_adult"]),
                    int(current["use_bypass_protection"]),
                    current["doh_policy"],
                    now,
                    current["applied_hash"],
                    current["applied_at"],
                    None if current["applied_ok"] is None else int(current["applied_ok"]),
                    json.dumps(extras),
                    current["history"],
                ),
            )
            await self._conn.commit()
        return await self.get_config(agent_id, default_history=default_history)

    async def set_applied_state(
        self,
        agent_id: str,
        list_hash: str | None,
        applied_at: str,
        ok: bool,
        *,
        default_history: str = "violations",
    ) -> None:
        """Persist the last-applied block hash/time/result for drift display.

        A host with no row yet gets one here; it materialises ``default_history``
        so that a push never silently changes what the host records.
        """

        history = (
            default_history if default_history in WEBFILTER_HISTORY_MODES else "violations"
        )
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO webfilter_config "
                "(agent_id, applied_hash, applied_at, applied_ok, history) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET "
                "applied_hash=excluded.applied_hash, applied_at=excluded.applied_at, "
                "applied_ok=excluded.applied_ok",
                (agent_id, list_hash, applied_at, 1 if ok else 0, history),
            )
            await self._conn.commit()

    # -- custom domain list ------------------------------------------------

    async def list_domains(self, agent_id: str) -> list[dict[str, Any]]:
        """Return the host's custom entries, oldest-first.

        ``category`` is ``None`` for an entry that is always in force (the
        pre-category shape); otherwise the entry only applies while that
        category is on for the host or added by a schedule window.
        """

        async with self._conn.execute(
            "SELECT domain, action, note, added_at, category FROM webfilter_domains "
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
                "category": r["category"],
            }
            for r in rows
        ]

    async def add_domain(
        self,
        agent_id: str,
        domain: str,
        action: str,
        note: str | None = None,
        category: str | None = None,
    ) -> None:
        """Insert (or replace) one custom entry, stamping ``added_at``."""

        added_at = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO webfilter_domains "
            "(agent_id, domain, action, note, added_at, category) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id, domain) DO UPDATE SET "
            "action=excluded.action, note=excluded.note, category=excluded.category",
            (agent_id, domain, action, note, added_at, category),
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

    # -- schedule windows --------------------------------------------------

    @staticmethod
    def _window_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "label": row["label"],
            "days": json.loads(row["days"]),
            "start_min": row["start_min"],
            "end_min": row["end_min"],
            "categories": json.loads(row["categories"]),
            "tz": row["tz"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
        }

    async def list_windows(self, agent_id: str) -> list[dict[str, Any]]:
        """Return the host's schedule windows, oldest-first."""

        async with self._conn.execute(
            "SELECT * FROM webfilter_windows WHERE agent_id = ? "
            "ORDER BY created_at, id",
            (agent_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._window_row(r) for r in rows]

    async def add_window(self, window: dict[str, Any]) -> None:
        """Insert (or replace by id) one schedule window."""

        await self._conn.execute(
            "INSERT INTO webfilter_windows "
            "(id, agent_id, label, days, start_min, end_min, categories, tz, "
            "enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "label=excluded.label, days=excluded.days, start_min=excluded.start_min, "
            "end_min=excluded.end_min, categories=excluded.categories, "
            "tz=excluded.tz, enabled=excluded.enabled",
            (
                window["id"],
                window["agent_id"],
                window["label"],
                json.dumps(list(window["days"])),
                int(window["start_min"]),
                int(window["end_min"]),
                json.dumps(list(window["categories"])),
                window["tz"],
                1 if window["enabled"] else 0,
                window["created_at"],
            ),
        )
        await self._conn.commit()

    async def set_window_enabled(
        self, agent_id: str, window_id: str, enabled: bool
    ) -> bool:
        """Enable/disable one window. Returns True if a row changed."""

        cur = await self._conn.execute(
            "UPDATE webfilter_windows SET enabled = ? WHERE id = ? AND agent_id = ?",
            (1 if enabled else 0, window_id, agent_id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def remove_window(self, agent_id: str, window_id: str) -> bool:
        """Delete one window. Returns True if a row was removed."""

        cur = await self._conn.execute(
            "DELETE FROM webfilter_windows WHERE id = ? AND agent_id = ?",
            (window_id, agent_id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def agents_with_windows(self) -> list[str]:
        """Hosts that have at least one *enabled* window (the schedule loop's fleet).

        A host with no enabled window is never touched by the schedule loop, so
        this is also the opt-in: authoring a window is what consents to the
        server pushing to that host unattended (ADR-0055).
        """

        async with self._conn.execute(
            "SELECT DISTINCT agent_id FROM webfilter_windows WHERE enabled = 1 "
            "ORDER BY agent_id"
        ) as cur:
            rows = await cur.fetchall()
        return [r["agent_id"] for r in rows]

    # -- observed events ---------------------------------------------------

    async def upsert_events(self, agent_id: str, events: list[dict[str, Any]]) -> None:
        """Merge observed domains: min(first_seen), max(last_seen), hits +=, sources ∪.

        A SELECT-then-INSERT pair per event, all one transaction with a single
        commit at the end — the longest-held write transaction in this module,
        so it holds :func:`write_lock` for its whole duration (never released
        mid-loop) and opens with ``BEGIN IMMEDIATE`` to take the writer lock up
        front rather than partway through the loop.

        ``events`` ultimately comes from an agent's ``web_activity`` telemetry
        (an unvalidated wire extra), so a field of a type SQLite can't bind
        (e.g. a ``first_seen`` that arrived as a dict) can raise partway
        through the loop. Without a rollback here, the still-open ``BEGIN
        IMMEDIATE`` transaction would poison this connection: every later
        call would fail with "cannot start a transaction within a
        transaction" until the process restarts, well past the one bad
        event. Roll back and re-raise so a caller's existing try/except
        (``tunnel.py`` already wraps this call) only ever loses the one
        malformed batch, never every batch after it.
        """

        async with write_lock():
            await _begin_immediate(self._conn)
            try:
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
            except BaseException:
                await self._conn.rollback()
                raise

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
        see :meth:`TelemetryStore.prune` for why (ADR-0051's live-retention
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

    async def purge_unflagged_events(self, agent_id: str) -> int:
        """Delete ``agent_id``'s observed domains that matched nothing. Returns count.

        The events half of turning a host's history to ``violations``
        (ADR-0069); the matches (``flagged = 1``) are kept.
        """

        async with write_lock():
            cur = await self._conn.execute(
                "DELETE FROM web_activity_events WHERE agent_id = ? AND flagged = 0",
                (agent_id,),
            )
            await self._conn.commit()
        return cur.rowcount or 0

    async def delete_agent(self, agent_id: str) -> None:
        """Delete all web-filter state for ``agent_id`` (host removed from inventory)."""

        async with write_lock():
            await _begin_immediate(self._conn)
            for table in (
                "webfilter_config",
                "webfilter_domains",
                "webfilter_windows",
                "web_activity_events",
            ):
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
    shape rather than the auto-pruned telemetry pattern (ADR-0025).
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

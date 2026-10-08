"""SQLite storage for specialized agents (ADR-0071): per-agent mode and run history.

Two tables on the shared DB file, one connection of its own, same shape as the
stores in :mod:`kenny_server.store` (own
:func:`~kenny_server.store._configure_connection` for WAL + busy-timeout,
``CREATE TABLE IF NOT EXISTS`` only, ISO-8601 UTC text timestamps, every write
under :func:`~kenny_server.store.write_lock`).

* ``agent_settings`` — per agent id: the operator's chosen mode, the effective
  hash ``act`` was chosen at (``act_hash``, ADR-0072), and the agent's
  parameters. A missing row, or an empty ``mode`` (a row that exists only for
  its parameters), means "mode never chosen"; the caller falls back to the
  spec's default mode.
* ``agent_runs`` — one row per started run: what it was bound to (the spec
  hash, and the effective hash and frozen parameters it ran with), what started
  it, the mode it ran in, how it ended and what it cost.

Pure storage. Whether a run may start, and what it may do, is decided elsewhere.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from ..store import DEFAULT_DB_PATH, _begin_immediate, _configure_connection, write_lock
from .spec import MODES

__all__ = [
    "DEFAULT_DB_PATH",
    "FINISHED_STATUSES",
    "INTERRUPTED_ERROR",
    "RUN_STATUSES",
    "USAGE_KEYS",
    "AgentRun",
    "AgentStore",
]

#: ``running`` while a run is in flight; the rest are the ways it can end.
RUN_STATUSES: tuple[str, ...] = ("running", "completed", "failed", "skipped")

#: The statuses a run can be finished with.
FINISHED_STATUSES: tuple[str, ...] = ("completed", "failed", "skipped")

#: The token counters a run's ``usage`` mapping may carry. Missing keys count 0.
USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)

#: What a run that was in flight when the server stopped is marked with.
INTERRUPTED_ERROR = "interrupted by a server restart"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_settings (
    agent_id   TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    params     TEXT NOT NULL DEFAULT '{}',
    act_hash   TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id                    TEXT PRIMARY KEY,
    agent_id              TEXT NOT NULL,
    spec_hash             TEXT NOT NULL,
    trigger               TEXT NOT NULL,
    subject               TEXT,
    host_id               TEXT,
    mode                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    verdict               TEXT,
    summary               TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    ticket_id             TEXT,
    error                 TEXT,
    actions               TEXT NOT NULL DEFAULT '[]',
    recommendations       TEXT NOT NULL DEFAULT '[]',
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    params                TEXT,
    effective_hash        TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs (agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs (started_at DESC);
"""

_RUN_COLUMNS = (
    "id, agent_id, spec_hash, trigger, subject, host_id, mode, status, verdict, summary, "
    "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
    "ticket_id, error, actions, recommendations, started_at, finished_at, "
    "params, effective_hash"
)

#: Columns added after a table was first shipped, with their DDL; added by
#: :meth:`AgentStore._migrate` to a database that predates them.
_MIGRATED_COLUMNS: dict[str, dict[str, str]] = {
    "agent_settings": {"params": "TEXT NOT NULL DEFAULT '{}'", "act_hash": "TEXT"},
    "agent_runs": {"params": "TEXT", "effective_hash": "TEXT"},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_iso(value: str) -> str:
    """Render an ISO-8601 instant as UTC text so it compares with stored stamps.

    Stored stamps are ``datetime.isoformat()`` in UTC; a caller's cutoff may
    carry another offset or a trailing ``Z``, which would sort wrongly as text.
    """

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown agent mode {mode!r}; expected one of {', '.join(MODES)}")


@dataclass(frozen=True)
class AgentRun:
    """One row of ``agent_runs``."""

    id: str
    agent_id: str
    spec_hash: str
    trigger: str
    subject: str | None
    host_id: str | None
    mode: str
    status: str
    verdict: str | None
    summary: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    ticket_id: str | None
    error: str | None
    #: What the run did, one entry per tool call (``tool``, ``args``, ``agent_id``,
    #: ``tool_class``, and ``ok``/``code`` once it ran). Stored as the caller
    #: gave it; the caller redacts.
    actions: list[dict[str, Any]]
    #: What the run proposed but was not allowed to do, same entry shape.
    recommendations: list[dict[str, Any]]
    started_at: str
    finished_at: str | None
    #: The parameters the run's gate enforced, frozen at its start (ADR-0072);
    #: ``None`` for a run that takes none recorded (triage, older rows).
    params: dict[str, Any] | None = None
    #: The effective hash the run was bound to; ``None`` as for ``params``.
    effective_hash: str | None = None

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> AgentRun:
        values = {key: row[key] for key in row.keys()}
        values["actions"] = json.loads(values["actions"])
        values["recommendations"] = json.loads(values["recommendations"])
        if values.get("params") is not None:
            values["params"] = json.loads(values["params"])
        return cls(**values)

    def to_public(self) -> dict[str, Any]:
        """The run as the dashboard and the API show it."""

        return asdict(self)


class AgentStore:
    """Async SQLite-backed store for agent modes and run history.

    Shares the DB file with the stores in :mod:`kenny_server.store` but owns its
    own connection. The schema is ``CREATE ... IF NOT EXISTS`` only, so
    ``connect()`` is idempotent. Run history is pruned by age
    (:meth:`prune`), never while a run is still ``running``.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add the columns a database created before them lacks; idempotent."""

        for table, columns in _MIGRATED_COLUMNS.items():
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                present = {row["name"] for row in await cur.fetchall()}
            for column, ddl in columns.items():
                if column not in present:
                    await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AgentStore is not connected; call connect() first")
        return self._db

    # -- mode ----------------------------------------------------------------

    async def get_mode(self, agent_id: str) -> str | None:
        """The mode an operator chose for ``agent_id``, or ``None`` if never chosen."""

        async with self._conn.execute(
            "SELECT mode FROM agent_settings WHERE agent_id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None or not row["mode"] else str(row["mode"])

    async def get_act_hash(self, agent_id: str) -> str | None:
        """The effective hash ``act`` was chosen at, or ``None``."""

        async with self._conn.execute(
            "SELECT act_hash FROM agent_settings WHERE agent_id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None or not row["act_hash"] else str(row["act_hash"])

    async def set_mode(
        self, agent_id: str, mode: str, *, actor: str, act_hash: str | None = None
    ) -> None:
        """Record ``mode`` as the operator's choice for ``agent_id``.

        ``act_hash`` is the effective hash ``act`` binds to (ADR-0072 rule 6);
        it is stored for ``act`` only and cleared for every other mode. An
        ``act`` stored without one is bound to nothing, and the runner treats it
        as ``shadow``.
        """

        _check_mode(mode)
        bound = act_hash if mode == "act" else None
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO agent_settings (agent_id, mode, updated_at, updated_by, act_hash) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET "
                "mode=excluded.mode, updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by, act_hash=excluded.act_hash",
                (agent_id, mode, _now_iso(), actor, bound),
            )
            await self._conn.commit()

    async def demote_unbound(self, agent_id: str, effective_hash: str, *, actor: str) -> bool:
        """Drop ``agent_id`` from ``act`` to ``shadow`` unless ``act`` is bound to ``effective_hash``.

        Returns whether this call demoted it, so exactly one caller records the
        drop when several notice it at once.
        """

        async with write_lock():
            cur = await self._conn.execute(
                "UPDATE agent_settings SET mode = 'shadow', act_hash = NULL, "
                "updated_at = ?, updated_by = ? "
                "WHERE agent_id = ? AND mode = 'act' "
                "AND (act_hash IS NULL OR act_hash != ?)",
                (_now_iso(), actor, agent_id, effective_hash),
            )
            changed = cur.rowcount or 0
            await self._conn.commit()
        return bool(changed)

    # -- params --------------------------------------------------------------

    async def get_params(self, agent_id: str) -> dict[str, Any]:
        """``agent_id``'s parameters; ``{}`` while none were ever set."""

        async with self._conn.execute(
            "SELECT params FROM agent_settings WHERE agent_id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or not row["params"]:
            return {}
        value = json.loads(row["params"])
        return value if isinstance(value, dict) else {}

    async def set_params(
        self, agent_id: str, params: Mapping[str, Any], *, actor: str, demote: bool = False
    ) -> str | None:
        """Store ``params`` for ``agent_id``; returns the mode stored before.

        With ``demote``, an agent in ``act`` is dropped to ``shadow`` (and its
        ``act`` binding cleared) in the same transaction as the write, so no
        call can see the new parameters while still in ``act``. Validating the
        values is the caller's job. A row created here carries an empty mode
        ("never chosen").
        """

        payload = json.dumps(dict(params), sort_keys=True, default=str)
        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                async with self._conn.execute(
                    "SELECT mode FROM agent_settings WHERE agent_id = ?", (agent_id,)
                ) as cur:
                    row = await cur.fetchone()
                before = None if row is None or not row["mode"] else str(row["mode"])
                await self._conn.execute(
                    "INSERT INTO agent_settings (agent_id, mode, updated_at, updated_by, params) "
                    "VALUES (?, '', ?, ?, ?) "
                    "ON CONFLICT(agent_id) DO UPDATE SET params=excluded.params, "
                    "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (agent_id, _now_iso(), actor, payload),
                )
                if demote and before == "act":
                    await self._conn.execute(
                        "UPDATE agent_settings SET mode = 'shadow', act_hash = NULL "
                        "WHERE agent_id = ?",
                        (agent_id,),
                    )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return before

    # -- runs ----------------------------------------------------------------

    async def start_run(
        self,
        *,
        agent_id: str,
        spec_hash: str,
        trigger: str,
        mode: str,
        subject: str | None = None,
        host_id: str | None = None,
        ticket_id: str | None = None,
        params: Mapping[str, Any] | None = None,
        effective_hash: str | None = None,
    ) -> AgentRun:
        """Open a run in the ``running`` state and return it.

        ``params`` and ``effective_hash`` are what the run is bound to for its
        whole life (ADR-0072), stored as given.
        """

        _check_mode(mode)
        run_id = uuid.uuid4().hex
        params_json = (
            None if params is None else json.dumps(dict(params), sort_keys=True, default=str)
        )
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO agent_runs "
                "(id, agent_id, spec_hash, trigger, subject, host_id, mode, status, "
                "ticket_id, started_at, params, effective_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (
                    run_id,
                    agent_id,
                    spec_hash,
                    trigger,
                    subject,
                    host_id,
                    mode,
                    ticket_id,
                    _now_iso(),
                    params_json,
                    effective_hash,
                ),
            )
            await self._conn.commit()
        run = await self.get_run(run_id)
        if run is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"agent run {run_id} vanished after insert")
        return run

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        verdict: str | None = None,
        summary: str | None = None,
        usage: Mapping[str, int] | None = None,
        error: str | None = None,
        ticket_id: str | None = None,
        actions: Sequence[Mapping[str, Any]] | None = None,
        recommendations: Sequence[Mapping[str, Any]] | None = None,
    ) -> AgentRun:
        """Close a ``running`` run with its outcome, token usage and what it did.

        Raises :class:`ValueError` for an unknown or non-final ``status``, for an
        unknown ``run_id`` and for a run that is not ``running`` — a finished run
        is a record and is never rewritten. ``ticket_id`` left ``None`` keeps
        whatever the run was started with. ``actions`` and ``recommendations``
        are stored verbatim as JSON; redacting them is the caller's job.
        """

        if status not in FINISHED_STATUSES:
            raise ValueError(
                f"cannot finish a run with status {status!r}; "
                f"expected one of {', '.join(FINISHED_STATUSES)}"
            )
        counts = {key: int((usage or {}).get(key, 0) or 0) for key in USAGE_KEYS}
        actions_json = json.dumps([dict(a) for a in actions or ()], default=str)
        recommendations_json = json.dumps([dict(r) for r in recommendations or ()], default=str)
        async with write_lock():
            cur = await self._conn.execute(
                "UPDATE agent_runs SET status = ?, verdict = ?, summary = ?, "
                "input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, "
                "cache_creation_tokens = ?, error = ?, actions = ?, recommendations = ?, "
                "ticket_id = COALESCE(?, ticket_id), finished_at = ? "
                "WHERE id = ? AND status = 'running'",
                (
                    status,
                    verdict,
                    summary,
                    counts["input_tokens"],
                    counts["output_tokens"],
                    counts["cache_read_tokens"],
                    counts["cache_creation_tokens"],
                    error,
                    actions_json,
                    recommendations_json,
                    ticket_id,
                    _now_iso(),
                    run_id,
                ),
            )
            changed = cur.rowcount or 0
            await self._conn.commit()
        run = await self.get_run(run_id)
        if run is None:
            raise ValueError(f"no agent run {run_id}")
        if not changed:
            raise ValueError(f"agent run {run_id} is already {run.status}, not running")
        return run

    async def get_run(self, run_id: str) -> AgentRun | None:
        async with self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else AgentRun._from_row(row)

    async def list_runs(self, *, agent_id: str | None = None, limit: int = 50) -> list[AgentRun]:
        """Runs newest first, optionally for one agent."""

        sql = f"SELECT {_RUN_COLUMNS} FROM agent_runs"
        params: list[Any] = []
        if agent_id is not None:
            sql += " WHERE agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(max(0, int(limit)))
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [AgentRun._from_row(row) for row in rows]

    async def tokens_since(self, since_iso: str, *, agent_id: str | None = None) -> int:
        """Every token of runs started at or after ``since_iso``.

        Input + output + cache creation + cache read: the API's
        ``input_tokens`` excludes the cached part of the prompt, so leaving the
        cache counters out would undercount what a run read by most of its
        prompt.
        """

        sql = (
            "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens "
            "+ cache_read_tokens), 0) AS total "
            "FROM agent_runs WHERE started_at >= ?"
        )
        params: list[Any] = [_normalize_iso(since_iso)]
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row["total"]) if row is not None else 0

    async def count_running(self, *, agent_id: str | None = None) -> int:
        """How many runs are in flight, optionally for one agent."""

        sql = "SELECT COUNT(*) AS n FROM agent_runs WHERE status = 'running'"
        params: list[Any] = []
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row is not None else 0

    async def fail_interrupted(self) -> int:
        """Mark every ``running`` run ``failed``; called once at startup.

        Nothing is running when the server has just started, so any row still
        ``running`` belongs to a process that died. Returns rows changed.
        """

        async with write_lock():
            cur = await self._conn.execute(
                "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ? "
                "WHERE status = 'running'",
                (INTERRUPTED_ERROR, _now_iso()),
            )
            changed = cur.rowcount or 0
            await self._conn.commit()
        return changed

    async def prune(self, before_iso: str) -> int:
        """Delete finished runs started before ``before_iso``; returns rows deleted.

        A ``running`` run is never deleted, however old: it is still in flight,
        or :meth:`fail_interrupted` has yet to account for it.
        """

        cutoff = _normalize_iso(before_iso)
        async with write_lock():
            await _begin_immediate(self._conn)
            cur = await self._conn.execute(
                "DELETE FROM agent_runs WHERE status != 'running' AND started_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount or 0
            await self._conn.commit()
        return deleted

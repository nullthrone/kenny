"""SQLite storage for stewards (ADR-0071): per-steward mode and run history.

Two tables on the shared DB file, one connection of its own, same shape as the
stores in :mod:`kenny_server.store` (own
:func:`~kenny_server.store._configure_connection` for WAL + busy-timeout,
``CREATE TABLE IF NOT EXISTS`` only, ISO-8601 UTC text timestamps, every write
under :func:`~kenny_server.store.write_lock`).

* ``steward_settings`` — per steward id: the operator's chosen mode, the effective
  hash ``act`` was chosen at (``act_hash``, ADR-0072), the steward's
  parameters and its trigger configuration (``triggers``, ``NULL`` while none
  was ever stored: the spec's baseline). A missing row, or an empty ``mode`` (a
  row that exists only for its configuration), means "mode never chosen"; the
  caller falls back to the spec's default mode.
* ``steward_runs`` — one row per started run: what it was bound to (the spec
  hash, the effective hash and frozen parameters it ran with, and the evidence
  its constraints were fed at start), what started it and the person who asked
  for it, if one did (``initiated_by``), the mode it ran in, how it ended and
  what it cost.
* ``steward_specs`` — the stewards a superuser built in the dashboard (ADR-0074):
  per id, its data form (``spec``, as ``stewards.builder.spec_to_data`` writes
  it), the ``version`` the server gave its latest save, where it came from
  (``source``: ``blank``, ``clone:<catalog id>`` or ``copilot``) and who created
  and last saved it. Deleting one keeps the row as a tombstone (``deleted_at``)
  so the version counter never restarts: a steward re-created under the same id
  has a version, and so a spec hash, it never had before.

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
from .builder import BUILT_ID_RE
from .catalog import CATALOG
from .spec import MODES

__all__ = [
    "DEFAULT_DB_PATH",
    "FINISHED_STATUSES",
    "INTERRUPTED_ERROR",
    "RUN_STATUSES",
    "USAGE_KEYS",
    "BuiltSpecRow",
    "SpecConflict",
    "StewardRun",
    "StewardStore",
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

#: What a run that did not finish is closed with: cancelled by a graceful
#: shutdown, or still ``running`` when a dead process was accounted for at the
#: next start. One constant for both, so whoever reads a run (the scheduler's
#: circuit breaker) can tell an interruption from a fault.
INTERRUPTED_ERROR = "the run was interrupted before it finished (cancelled or a server restart)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS steward_settings (
    steward_id TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    params     TEXT NOT NULL DEFAULT '{}',
    act_hash   TEXT,
    triggers   TEXT
);

CREATE TABLE IF NOT EXISTS steward_runs (
    id                    TEXT PRIMARY KEY,
    steward_id            TEXT NOT NULL,
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
    effective_hash        TEXT,
    evidence              TEXT,
    initiated_by          TEXT
);
CREATE INDEX IF NOT EXISTS idx_steward_runs_steward ON steward_runs (steward_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_steward_runs_started ON steward_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS steward_specs (
    id         TEXT PRIMARY KEY,
    spec       TEXT NOT NULL,
    version    INTEGER NOT NULL CHECK (version >= 1),
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    deleted_at TEXT,
    deleted_by TEXT
);
"""

_SPEC_COLUMNS = (
    "id, spec, version, source, created_at, created_by, updated_at, updated_by, "
    "deleted_at, deleted_by"
)

_RUN_COLUMNS = (
    "id, steward_id, spec_hash, trigger, subject, host_id, mode, status, verdict, summary, "
    "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
    "ticket_id, error, actions, recommendations, started_at, finished_at, "
    "params, effective_hash, evidence, initiated_by"
)

#: Columns added after a table was first shipped, with their DDL; added by
#: :meth:`StewardStore._migrate` to a database that predates them.
_MIGRATED_COLUMNS: dict[str, dict[str, str]] = {
    "steward_settings": {
        "params": "TEXT NOT NULL DEFAULT '{}'",
        "act_hash": "TEXT",
        "triggers": "TEXT",
    },
    "steward_runs": {
        "params": "TEXT",
        "effective_hash": "TEXT",
        "evidence": "TEXT",
        "initiated_by": "TEXT",
    },
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
        raise ValueError(f"unknown steward mode {mode!r}; expected one of {', '.join(MODES)}")


@dataclass(frozen=True)
class StewardRun:
    """One row of ``steward_runs``."""

    id: str
    steward_id: str
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
    #: The values server code computed at run start for the run's
    #: evidence-fed constraints (ADR-0072 rule 6), by evidence name, frozen
    #: for the run; ``None`` for a run whose steward takes none.
    evidence: dict[str, list[str]] | None = None
    #: The person who started the run (a command or a preview), as the audit
    #: trail names them; ``None`` for a run the server started on its own.
    initiated_by: str | None = None

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> StewardRun:
        values = {key: row[key] for key in row.keys()}
        values["actions"] = json.loads(values["actions"])
        values["recommendations"] = json.loads(values["recommendations"])
        for key in ("params", "evidence"):
            if values.get(key) is not None:
                values[key] = json.loads(values[key])
        return cls(**values)

    def to_public(self) -> dict[str, Any]:
        """The run as the dashboard and the API show it."""

        return asdict(self)


@dataclass(frozen=True)
class BuiltSpecRow:
    """One row of ``steward_specs``: a steward a superuser built (ADR-0074)."""

    id: str
    #: The data form as stored; ``None`` when the stored text is not a JSON
    #: object (it then cannot be loaded, and is shown as invalid).
    spec: dict[str, Any] | None
    version: int
    source: str
    created_at: str
    created_by: str
    updated_at: str
    updated_by: str
    deleted_at: str | None = None
    deleted_by: str | None = None

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> BuiltSpecRow:
        values = {key: row[key] for key in row.keys()}
        try:
            data = json.loads(values["spec"])
        except ValueError:
            data = None
        values["spec"] = data if isinstance(data, dict) else None
        values["version"] = int(values["version"])
        return cls(**values)

    def meta(self) -> dict[str, Any]:
        """Who made it and when, as the API shows it beside the steward."""

        return {
            "source": self.source,
            "version": self.version,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class SpecConflict(ValueError):
    """A built-steward write that lost to another: the id is taken, or the row moved on."""


class StewardStore:
    """Async SQLite-backed store for steward modes and run history.

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
            raise RuntimeError("StewardStore is not connected; call connect() first")
        return self._db

    # -- mode ----------------------------------------------------------------

    async def get_mode(self, steward_id: str) -> str | None:
        """The mode an operator chose for ``steward_id``, or ``None`` if never chosen."""

        async with self._conn.execute(
            "SELECT mode FROM steward_settings WHERE steward_id = ?", (steward_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None or not row["mode"] else str(row["mode"])

    async def get_act_hash(self, steward_id: str) -> str | None:
        """The effective hash ``act`` was chosen at, or ``None``."""

        async with self._conn.execute(
            "SELECT act_hash FROM steward_settings WHERE steward_id = ?", (steward_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None or not row["act_hash"] else str(row["act_hash"])

    async def set_mode(
        self, steward_id: str, mode: str, *, actor: str, act_hash: str | None = None
    ) -> None:
        """Record ``mode`` as the operator's choice for ``steward_id``.

        ``act_hash`` is the effective hash ``act`` binds to (ADR-0072 rule 6);
        it is stored for ``act`` only and cleared for every other mode. An
        ``act`` stored without one is bound to nothing, and the runner treats it
        as ``shadow``.
        """

        _check_mode(mode)
        bound = act_hash if mode == "act" else None
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO steward_settings (steward_id, mode, updated_at, updated_by, act_hash) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(steward_id) DO UPDATE SET "
                "mode=excluded.mode, updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by, act_hash=excluded.act_hash",
                (steward_id, mode, _now_iso(), actor, bound),
            )
            await self._conn.commit()

    async def demote_unbound(self, steward_id: str, effective_hash: str, *, actor: str) -> bool:
        """Drop ``steward_id`` from ``act`` to ``shadow`` unless ``act`` is bound to ``effective_hash``.

        Returns whether this call demoted it, so exactly one caller records the
        drop when several notice it at once.
        """

        async with write_lock():
            cur = await self._conn.execute(
                "UPDATE steward_settings SET mode = 'shadow', act_hash = NULL, "
                "updated_at = ?, updated_by = ? "
                "WHERE steward_id = ? AND mode = 'act' "
                "AND (act_hash IS NULL OR act_hash != ?)",
                (_now_iso(), actor, steward_id, effective_hash),
            )
            changed = cur.rowcount or 0
            await self._conn.commit()
        return bool(changed)

    # -- params --------------------------------------------------------------

    async def get_params(self, steward_id: str) -> dict[str, Any]:
        """``steward_id``'s parameters; ``{}`` while none were ever set."""

        async with self._conn.execute(
            "SELECT params FROM steward_settings WHERE steward_id = ?", (steward_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or not row["params"]:
            return {}
        value = json.loads(row["params"])
        return value if isinstance(value, dict) else {}

    async def set_params(
        self, steward_id: str, params: Mapping[str, Any], *, actor: str, demote: bool = False
    ) -> str | None:
        """Store ``params`` for ``steward_id``; returns the mode stored before (:meth:`set_config`)."""

        return await self.set_config(steward_id, params=params, actor=actor, demote=demote)

    # -- triggers ------------------------------------------------------------

    async def get_triggers(self, steward_id: str) -> Any:
        """``steward_id``'s stored trigger configuration; ``None`` while none was ever stored.

        Returned as stored, unvalidated: ``spec.effective_triggers`` reads it
        against the spec, and a value it cannot read switches nothing on. A
        value that is not JSON is returned as ``{}``, which it cannot read.
        """

        async with self._conn.execute(
            "SELECT triggers FROM steward_settings WHERE steward_id = ?", (steward_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["triggers"] is None:
            return None
        try:
            return json.loads(row["triggers"])
        except ValueError:
            return {}

    async def set_config(
        self,
        steward_id: str,
        *,
        actor: str,
        params: Mapping[str, Any] | None = None,
        triggers: Mapping[str, Any] | None = None,
        demote: bool = False,
    ) -> str | None:
        """Store ``params`` and/or ``triggers`` for ``steward_id``; returns the mode stored before.

        Either left ``None`` is left as it is. With ``demote``, a steward in
        ``act`` is dropped to ``shadow`` (and its ``act`` binding cleared) in
        the same transaction as the write, so no call can see the new
        configuration while still in ``act``. Validating the values is the
        caller's job. A row created here carries an empty mode ("never
        chosen").
        """

        columns: dict[str, str] = {}
        if params is not None:
            columns["params"] = json.dumps(dict(params), sort_keys=True, default=str)
        if triggers is not None:
            columns["triggers"] = json.dumps(dict(triggers), sort_keys=True, default=str)
        if not columns:
            raise ValueError("set_config needs params or triggers")
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{name}=excluded.{name}" for name in columns)
        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                async with self._conn.execute(
                    "SELECT mode FROM steward_settings WHERE steward_id = ?", (steward_id,)
                ) as cur:
                    row = await cur.fetchone()
                before = None if row is None or not row["mode"] else str(row["mode"])
                # Column names are the literals above, never the caller's.
                await self._conn.execute(
                    "INSERT INTO steward_settings (steward_id, mode, updated_at, updated_by, "
                    f"{names}) VALUES (?, '', ?, ?, {marks}) "
                    f"ON CONFLICT(steward_id) DO UPDATE SET {updates}, "
                    "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (steward_id, _now_iso(), actor, *columns.values()),
                )
                if demote and before == "act":
                    await self._conn.execute(
                        "UPDATE steward_settings SET mode = 'shadow', act_hash = NULL "
                        "WHERE steward_id = ?",
                        (steward_id,),
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
        steward_id: str,
        spec_hash: str,
        trigger: str,
        mode: str,
        subject: str | None = None,
        host_id: str | None = None,
        ticket_id: str | None = None,
        params: Mapping[str, Any] | None = None,
        effective_hash: str | None = None,
        evidence: Mapping[str, Sequence[str]] | None = None,
        initiated_by: str | None = None,
    ) -> StewardRun:
        """Open a run in the ``running`` state and return it.

        ``params``, ``effective_hash`` and ``evidence`` are what the run is
        bound to for its whole life (ADR-0072), stored as given;
        ``initiated_by`` is the person who asked for the run, if one did.
        """

        _check_mode(mode)
        run_id = uuid.uuid4().hex
        params_json = (
            None if params is None else json.dumps(dict(params), sort_keys=True, default=str)
        )
        evidence_json = (
            None
            if evidence is None
            else json.dumps({k: sorted(v) for k, v in evidence.items()}, sort_keys=True)
        )
        async with write_lock():
            await self._conn.execute(
                "INSERT INTO steward_runs "
                "(id, steward_id, spec_hash, trigger, subject, host_id, mode, status, "
                "ticket_id, started_at, params, effective_hash, evidence, initiated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    steward_id,
                    spec_hash,
                    trigger,
                    subject,
                    host_id,
                    mode,
                    ticket_id,
                    _now_iso(),
                    params_json,
                    effective_hash,
                    evidence_json,
                    initiated_by,
                ),
            )
            await self._conn.commit()
        run = await self.get_run(run_id)
        if run is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"steward run {run_id} vanished after insert")
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
    ) -> StewardRun:
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
                "UPDATE steward_runs SET status = ?, verdict = ?, summary = ?, "
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
            raise ValueError(f"no steward run {run_id}")
        if not changed:
            raise ValueError(f"steward run {run_id} is already {run.status}, not running")
        return run

    async def append_action(self, run_id: str, action: Mapping[str, Any]) -> None:
        """Add one change to a ``running`` run's ``actions`` as it is made.

        Written when the gate allows the change, before the call is forwarded,
        so a run that is still going — or that a crash or a restart cut short —
        already shows what it changed (ADR-0071 rule 6 reads it). The run's
        close rewrites the list with each change's outcome. Raises
        :class:`ValueError` for a run that is unknown or no longer ``running``:
        a change that cannot be put on the record must not be made.
        """

        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                async with self._conn.execute(
                    "SELECT actions FROM steward_runs WHERE id = ? AND status = 'running'",
                    (run_id,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    raise ValueError(f"steward run {run_id} is not running")
                actions = json.loads(row["actions"] or "[]")
                actions.append(dict(action))
                await self._conn.execute(
                    "UPDATE steward_runs SET actions = ? WHERE id = ? AND status = 'running'",
                    (json.dumps(actions, default=str), run_id),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def get_run(self, run_id: str) -> StewardRun | None:
        async with self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM steward_runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else StewardRun._from_row(row)

    async def list_runs(self, *, steward_id: str | None = None, limit: int = 50) -> list[StewardRun]:
        """Runs newest first, optionally for one steward."""

        sql = f"SELECT {_RUN_COLUMNS} FROM steward_runs"
        params: list[Any] = []
        if steward_id is not None:
            sql += " WHERE steward_id = ?"
            params.append(steward_id)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(max(0, int(limit)))
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [StewardRun._from_row(row) for row in rows]

    async def scheduled_runs(self, steward_id: str, *, prefix: str) -> list[StewardRun]:
        """Every run of ``steward_id`` whose ``trigger`` starts with ``prefix``, newest first.

        The scheduler's whole history (``scheduler.SCHEDULE_TRIGGER_PREFIX``),
        selected in SQL, so no number of runs of another kind -- previews a
        person can start at will -- can push a scheduled run out of it. Bounded
        by run retention, not by a count.
        """

        if not prefix or any(ch in prefix for ch in "%_\\"):
            raise ValueError(f"not a plain trigger prefix: {prefix!r}")
        async with self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM steward_runs "
            "WHERE steward_id = ? AND trigger LIKE ? AND substr(trigger, 1, ?) = ? "
            "ORDER BY started_at DESC, rowid DESC",
            (steward_id, f"{prefix}%", len(prefix), prefix),
        ) as cur:
            rows = await cur.fetchall()
        return [StewardRun._from_row(row) for row in rows]

    async def runs_with_trigger_prefixes(
        self, steward_id: str, prefixes: Sequence[str]
    ) -> list[StewardRun]:
        """Every run of ``steward_id`` whose ``trigger`` starts with any of ``prefixes``, newest first.

        Selected in SQL, like :meth:`scheduled_runs`, so no number of runs of
        another kind can push one out. Bounded by run retention.
        """

        clean = sorted(set(prefixes))
        if not clean or any(not p or any(ch in p for ch in "%_\\") for p in clean):
            raise ValueError(f"not plain trigger prefixes: {prefixes!r}")
        where = " OR ".join("substr(trigger, 1, ?) = ?" for _ in clean)
        args: list[Any] = [steward_id]
        for prefix in clean:
            args += [len(prefix), prefix]
        async with self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM steward_runs WHERE steward_id = ? AND ({where}) "
            "ORDER BY started_at DESC, rowid DESC",
            args,
        ) as cur:
            rows = await cur.fetchall()
        return [StewardRun._from_row(row) for row in rows]

    async def latest_run_started(
        self,
        steward_id: str,
        host_id: str | None,
        trigger: str,
        *,
        include_skipped: bool = False,
    ) -> datetime | None:
        """When the newest run of ``steward_id`` on ``host_id`` with exactly ``trigger`` started.

        ``host_id`` ``None`` matches runs on no host. A ``skipped`` run never
        ran and is left out unless ``include_skipped``. ``None`` when there is
        no such run.
        """

        sql = "SELECT MAX(started_at) AS at FROM steward_runs WHERE steward_id = ? AND trigger = ?"
        args: list[Any] = [steward_id, trigger]
        if host_id is None:
            sql += " AND host_id IS NULL"
        else:
            sql += " AND host_id = ?"
            args.append(host_id)
        if not include_skipped:
            sql += " AND status != 'skipped'"
        async with self._conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        if row is None or row["at"] is None:
            return None
        return datetime.fromisoformat(_normalize_iso(str(row["at"])))

    async def count_runs_since(self, steward_id: str, *, prefix: str, since_iso: str) -> int:
        """How many runs of ``steward_id`` with a ``trigger`` starting ``prefix`` began at or after ``since_iso``."""

        if not prefix or any(ch in prefix for ch in "%_\\"):
            raise ValueError(f"not a plain trigger prefix: {prefix!r}")
        async with self._conn.execute(
            "SELECT COUNT(*) FROM steward_runs WHERE steward_id = ? AND trigger LIKE ? "
            "AND substr(trigger, 1, ?) = ? AND started_at >= ?",
            (steward_id, f"{prefix}%", len(prefix), prefix, _normalize_iso(since_iso)),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def runs_on_host_since(self, host_id: str | None, since_iso: str) -> list[StewardRun]:
        """Every run of any steward on ``host_id`` that may have acted at or after ``since_iso``.

        Newest first: a run started or finished at or after ``since_iso``, and
        every run still ``running`` however long ago it started. ``host_id``
        ``None`` reads the server-only runs (on no host).
        """

        since = _normalize_iso(since_iso)
        where = "host_id IS NULL" if host_id is None else "host_id = ?"
        args: list[Any] = [] if host_id is None else [host_id]
        async with self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM steward_runs WHERE {where} "
            "AND (started_at >= ? OR finished_at >= ? OR status = 'running') "
            "ORDER BY started_at DESC, rowid DESC",
            (*args, since, since),
        ) as cur:
            rows = await cur.fetchall()
        return [StewardRun._from_row(row) for row in rows]

    async def demote_absent(self, steward_ids: Sequence[str], *, actor: str) -> list[str]:
        """Drop every stored ``act`` whose steward is not in ``steward_ids`` to ``shadow``.

        Returns the ids demoted. Called at startup with the catalog's ids, so an
        steward a release removed keeps no ``act`` for an identical spec to come
        back to (ADR-0072 rule 6).
        """

        keep = sorted(set(steward_ids))
        absent = f"AND steward_id NOT IN ({', '.join('?' for _ in keep)})" if keep else ""
        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                async with self._conn.execute(
                    f"SELECT steward_id FROM steward_settings WHERE mode = 'act' {absent}",
                    keep,
                ) as cur:
                    demoted = sorted(str(row["steward_id"]) for row in await cur.fetchall())
                if demoted:
                    await self._conn.execute(
                        "UPDATE steward_settings SET mode = 'shadow', act_hash = NULL, "
                        "updated_at = ?, updated_by = ? "
                        f"WHERE steward_id IN ({', '.join('?' for _ in demoted)})",
                        (_now_iso(), actor, *demoted),
                    )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return demoted

    async def tokens_since(self, since_iso: str, *, steward_id: str | None = None) -> int:
        """Every token of runs started at or after ``since_iso``.

        Input + output + cache creation + cache read: the API's
        ``input_tokens`` excludes the cached part of the prompt, so leaving the
        cache counters out would undercount what a run read by most of its
        prompt.
        """

        sql = (
            "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_creation_tokens "
            "+ cache_read_tokens), 0) AS total "
            "FROM steward_runs WHERE started_at >= ?"
        )
        params: list[Any] = [_normalize_iso(since_iso)]
        if steward_id is not None:
            sql += " AND steward_id = ?"
            params.append(steward_id)
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row["total"]) if row is not None else 0

    async def count_running(self, *, steward_id: str | None = None) -> int:
        """How many runs are in flight, optionally for one steward."""

        sql = "SELECT COUNT(*) AS n FROM steward_runs WHERE status = 'running'"
        params: list[Any] = []
        if steward_id is not None:
            sql += " AND steward_id = ?"
            params.append(steward_id)
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
                "UPDATE steward_runs SET status = 'failed', error = ?, finished_at = ? "
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
                "DELETE FROM steward_runs WHERE status != 'running' AND started_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount or 0
            await self._conn.commit()
        return deleted

    # -- built stewards (ADR-0074) -------------------------------------------

    async def list_built_specs(self, *, include_deleted: bool = False) -> list[BuiltSpecRow]:
        """Every built steward, by id; deleted ones (tombstones) only when asked."""

        where = "" if include_deleted else " WHERE deleted_at IS NULL"
        async with self._conn.execute(
            f"SELECT {_SPEC_COLUMNS} FROM steward_specs{where} ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        return [BuiltSpecRow._from_row(row) for row in rows]

    async def get_built_spec(
        self, steward_id: str, *, include_deleted: bool = False
    ) -> BuiltSpecRow | None:
        """The built steward ``steward_id``, or ``None``; a tombstone only when asked."""

        async with self._conn.execute(
            f"SELECT {_SPEC_COLUMNS} FROM steward_specs WHERE id = ?", (steward_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        found = BuiltSpecRow._from_row(row)
        if found.deleted_at is not None and not include_deleted:
            return None
        return found

    async def create_built_spec(
        self,
        steward_id: str,
        spec: Mapping[str, Any],
        *,
        version: int,
        source: str,
        actor: str,
    ) -> BuiltSpecRow:
        """Store a new built steward at ``version``; :class:`SpecConflict` if the id is live.

        Over a tombstone of the same id, ``version`` must be above the
        tombstone's: the counter never restarts. In the same transaction every
        setting stored for the id (a mode, parameters, triggers left behind) is
        dropped, so a re-created steward starts from nothing it had before.
        """

        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("version must be a whole number of at least 1")
        if not BUILT_ID_RE.fullmatch(steward_id) or steward_id in CATALOG:
            raise ValueError(f"{steward_id!r} is not a built steward's id")
        now = _now_iso()
        text = json.dumps(dict(spec), sort_keys=True)
        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                async with self._conn.execute(
                    "SELECT version, deleted_at FROM steward_specs WHERE id = ?", (steward_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row is not None and row["deleted_at"] is None:
                    raise SpecConflict(f"a steward {steward_id} already exists")
                if row is not None and version <= int(row["version"]):
                    raise SpecConflict(
                        f"steward {steward_id} was version {row['version']} before it was deleted; "
                        "a new one takes a later version"
                    )
                await self._conn.execute(
                    f"INSERT INTO steward_specs ({_SPEC_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL) "
                    "ON CONFLICT(id) DO UPDATE SET spec=excluded.spec, version=excluded.version, "
                    "source=excluded.source, created_at=excluded.created_at, "
                    "created_by=excluded.created_by, updated_at=excluded.updated_at, "
                    "updated_by=excluded.updated_by, deleted_at=NULL, deleted_by=NULL",
                    (steward_id, text, version, source, now, actor, now, actor),
                )
                await self._conn.execute(
                    "DELETE FROM steward_settings WHERE steward_id = ?", (steward_id,)
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        created = await self.get_built_spec(steward_id)
        if created is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"built steward {steward_id} vanished after insert")
        return created

    async def update_built_spec(
        self,
        steward_id: str,
        spec: Mapping[str, Any],
        *,
        version: int,
        expected_version: int,
        actor: str,
        params: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Save a new ``version`` of a built steward; returns the mode stored before.

        Refused with :class:`SpecConflict` unless the live row is still at
        ``expected_version``. In the same transaction a steward in ``act`` is
        dropped to ``shadow`` (its binding cleared), so nothing sees the new
        spec while still in ``act``; ``params``, when given, replaces the
        stored parameters (the caller passes the ones the new spec still
        declares).
        """

        if version <= expected_version:
            raise ValueError("a save takes a later version")
        text = json.dumps(dict(spec), sort_keys=True)
        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                cur = await self._conn.execute(
                    "UPDATE steward_specs SET spec = ?, version = ?, updated_at = ?, updated_by = ? "
                    "WHERE id = ? AND deleted_at IS NULL AND version = ?",
                    (text, version, _now_iso(), actor, steward_id, expected_version),
                )
                if not (cur.rowcount or 0):
                    raise SpecConflict(f"steward {steward_id} changed or was deleted since it was read")
                async with self._conn.execute(
                    "SELECT mode FROM steward_settings WHERE steward_id = ?", (steward_id,)
                ) as cur:
                    row = await cur.fetchone()
                before = None if row is None or not row["mode"] else str(row["mode"])
                if before == "act":
                    await self._conn.execute(
                        "UPDATE steward_settings SET mode = 'shadow', act_hash = NULL, "
                        "updated_at = ?, updated_by = ? WHERE steward_id = ?",
                        (_now_iso(), actor, steward_id),
                    )
                if params is not None and row is not None:
                    await self._conn.execute(
                        "UPDATE steward_settings SET params = ? WHERE steward_id = ?",
                        (json.dumps(dict(params), sort_keys=True, default=str), steward_id),
                    )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return before

    async def delete_built_spec(self, steward_id: str, *, actor: str) -> bool:
        """Delete a built steward: keep its row as a tombstone, drop its settings.

        Returns whether a live row was deleted. The tombstone keeps the version
        (:meth:`create_built_spec`); the settings row (mode, parameters,
        triggers) goes in the same transaction.
        """

        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                cur = await self._conn.execute(
                    "UPDATE steward_specs SET deleted_at = ?, deleted_by = ? "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (_now_iso(), actor, steward_id),
                )
                deleted = bool(cur.rowcount or 0)
                if deleted:
                    await self._conn.execute(
                        "DELETE FROM steward_settings WHERE steward_id = ?", (steward_id,)
                    )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return deleted

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
        return [
            {"collected_at": r["collected_at"], "snapshot": json.loads(r["snapshot"])}
            for r in rows
        ]

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
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
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
        await self._conn.execute(
            "INSERT INTO alert_state (agent_id, scope, status, since, last_notified_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (agent_id, scope) DO UPDATE SET "
            "status = excluded.status, since = excluded.since, "
            "last_notified_at = excluded.last_notified_at",
            (agent_id, scope, status, since, last_notified_at),
        )
        await self._conn.commit()


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
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
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
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
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
        """Merge observed domains: min(first_seen), max(last_seen), hits +=, sources ∪."""

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

    async def prune(self, *, now: datetime | None = None) -> int:
        """Delete events whose ``last_seen`` is older than retention. Returns count."""

        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=self.retention_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM web_activity_events WHERE last_seen < ?", (cutoff,)
        )
        await self._conn.commit()
        return cur.rowcount or 0


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
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
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

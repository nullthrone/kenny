"""Standing authorizations: consent a superuser gives ahead (ADR-0072).

An authorization says that one agent, bound to what that agent *is* on this
install (its effective hash, :func:`~kenny_server.agents.spec.effective_hash`),
may make one kind of ``normal_change`` on named hosts, a bounded number of
times per host per rolling 24 hours, until it expires. This module stores them
and answers the one question the agent gate asks at step 8: *is there one that
covers this call, and if so, spend an attempt on it.*

What it enforces, and where:

* **Never authorizable** (:data:`NEVER_AUTHORIZED`): refused at
  :meth:`AuthorizationStore.grant` *and* at :meth:`AuthorizationStore.consume`,
  so a row written past :meth:`grant` (a restored backup, a hand edit) still
  matches nothing.
* **Grant refusals**: a tool outside the agent's spec, a tool that is not
  ``normal_change``, an empty scope (an empty list is never "all"), the server
  sentinel for a tool that always runs on a host, an attempt budget below one,
  an expiry in the past or more than :data:`MAX_EXPIRY_DAYS` out. Who may grant (a
  superuser) is the API's to enforce; this module has no principal.
* **Consume, then execute**: :meth:`consume` finds a live match and records the
  attempt in one transaction under :func:`~kenny_server.store.write_lock`, so
  two runs reaching for the last attempt cannot both get it. The caller
  executes only after it returns; a call that then fails still counted.
* **Voiding is permanent**: :meth:`void_other_hashes` stamps ``voided_at`` on
  every authorization bound to a hash other than the agent's current one.
  Nothing clears it, so rolling the code or the parameters back does not
  revive a grant made against what the agent used to be.

Storage follows :mod:`kenny_server.agents.store`: its own connection on the
shared DB file, ``CREATE TABLE IF NOT EXISTS`` only, UTC ISO-8601 text stamps
(always with microseconds, so they compare correctly as text).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from ..store import DEFAULT_DB_PATH, _begin_immediate, _configure_connection, write_lock
from ..tool_classes import NORMAL_CHANGE, TOOL_CLASSES
from ..tools import CAPABILITY_TOOLS
from .spec import AgentSpec

__all__ = [
    "BUDGET_WINDOW",
    "MAX_EXPIRY_DAYS",
    "NEVER_AUTHORIZED",
    "SERVER_SCOPE",
    "Authorization",
    "AuthorizationError",
    "AuthorizationStore",
]

#: Tools no standing authorization may ever cover (ADR-0072 rule 1). A
#: free-text script cannot be bound to values, and an agent binary's consent
#: model is the pinned campaign (ADR-0040). A literal, on purpose: nothing
#: derived from a tier map or a setting can quietly shrink it.
NEVER_AUTHORIZED: frozenset[str] = frozenset({"shell_exec", "powershell_exec", "agent_update"})

#: The longest an authorization may run before it lapses (ADR-0072 rule 2).
MAX_EXPIRY_DAYS = 180

#: The scope of an authorization for a server-side change: the call names no host.
SERVER_SCOPE = "server"

#: The rolling window ``max_attempts_per_day`` counts over, per host.
BUDGET_WINDOW = timedelta(hours=24)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_authorizations (
    id                   TEXT PRIMARY KEY,
    agent_id             TEXT NOT NULL,
    effective_hash       TEXT NOT NULL,
    tool                 TEXT NOT NULL,
    scope                TEXT NOT NULL,
    max_attempts_per_day INTEGER NOT NULL CHECK (max_attempts_per_day >= 1),
    expires_at           TEXT NOT NULL,
    granted_by           TEXT NOT NULL,
    granted_at           TEXT NOT NULL,
    revoked_at           TEXT,
    revoked_by           TEXT,
    voided_at            TEXT,
    voided_by            TEXT,
    note                 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_authorizations_agent
    ON agent_authorizations (agent_id, tool);

CREATE TABLE IF NOT EXISTS agent_authorization_uses (
    authorization_id TEXT NOT NULL,
    host_id          TEXT,
    at               TEXT NOT NULL,
    run_id           TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_authorization_uses
    ON agent_authorization_uses (authorization_id, at);
"""

_COLUMNS = (
    "id, agent_id, effective_hash, tool, scope, max_attempts_per_day, expires_at, "
    "granted_by, granted_at, revoked_at, revoked_by, voided_at, voided_by, note"
)

_MAX_NOTE = 500


class AuthorizationError(ValueError):
    """A grant this module refuses; the message says why."""


def _iso(moment: datetime) -> str:
    """``moment`` as UTC text that sorts correctly against every other stamp."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_instant(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise AuthorizationError(f"expires_at {value!r} is not an ISO-8601 instant") from exc
    else:
        raise AuthorizationError("expires_at is required")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _normalize_scope(scope: Any) -> list[str] | str:
    """A list of distinct host ids, or :data:`SERVER_SCOPE`; anything else refused."""

    if scope == SERVER_SCOPE:
        return SERVER_SCOPE
    if not isinstance(scope, (list, tuple)):
        raise AuthorizationError(f'scope must be a list of host ids or "{SERVER_SCOPE}"')
    hosts: list[str] = []
    for host in scope:
        if not isinstance(host, str) or not host.strip():
            raise AuthorizationError("every host in a scope must be a non-empty id")
        if host.strip() not in hosts:
            hosts.append(host.strip())
    if not hosts:
        # An empty list would read as "everywhere" to anyone who forgot the rule.
        raise AuthorizationError("scope names no host; an empty scope is never all hosts")
    return sorted(hosts)


@dataclass(frozen=True)
class Authorization:
    """One row of ``agent_authorizations``."""

    id: str
    agent_id: str
    effective_hash: str
    tool: str
    #: Host ids, or :data:`SERVER_SCOPE` for a change that names no host.
    scope: list[str] | str
    max_attempts_per_day: int
    expires_at: str
    granted_by: str
    granted_at: str
    revoked_at: str | None
    revoked_by: str | None
    voided_at: str | None
    voided_by: str | None
    note: str

    @classmethod
    def _from_row(cls, row: aiosqlite.Row) -> Authorization:
        values = {key: row[key] for key in row.keys()}
        values["scope"] = json.loads(values["scope"])
        return cls(**values)

    def covers(self, host_id: str | None) -> bool:
        """Whether this authorization's scope names ``host_id``.

        ``None`` (a call that touches no host) matches only the server sentinel;
        an empty list matches nothing.
        """

        if host_id is None:
            return self.scope == SERVER_SCOPE
        return isinstance(self.scope, list) and host_id in self.scope

    def status(self, now: datetime) -> str:
        """``revoked``, ``voided``, ``expired`` or ``live``, in that precedence."""

        if self.revoked_at is not None:
            return "revoked"
        if self.voided_at is not None:
            return "voided"
        if self.expires_at <= _iso(now):
            return "expired"
        return "live"

    def to_public(self, now: datetime | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "agent_id": self.agent_id,
            "effective_hash": self.effective_hash,
            "tool": self.tool,
            "scope": self.scope,
            "max_attempts_per_day": self.max_attempts_per_day,
            "expires_at": self.expires_at,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "revoked_at": self.revoked_at,
            "revoked_by": self.revoked_by,
            "voided_at": self.voided_at,
            "voided_by": self.voided_by,
            "note": self.note,
        }
        out["status"] = self.status(now or datetime.now(timezone.utc))
        return out


class AuthorizationStore:
    """Async SQLite store of standing authorizations and the attempts spent on them."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

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
            raise RuntimeError("AuthorizationStore is not connected; call connect() first")
        return self._db

    # -- grant / revoke / void ---------------------------------------------

    async def grant(
        self,
        *,
        spec: AgentSpec,
        effective_hash: str,
        tool: str,
        scope: Sequence[str] | str,
        max_attempts_per_day: int,
        expires_at: datetime | str,
        granted_by: str,
        note: str = "",
        now: datetime | None = None,
    ) -> Authorization:
        """Record a grant for ``spec``'s agent at ``effective_hash``; refuse a bad one.

        Raises :class:`AuthorizationError` naming the first rule the grant
        breaks. Only a superuser may call this; the API enforces that.
        """

        moment = now or datetime.now(timezone.utc)
        if tool in NEVER_AUTHORIZED:
            raise AuthorizationError(f"{tool} can never be authorized ahead")
        if tool not in spec.tools:
            raise AuthorizationError(f"{tool} is not one of agent {spec.id}'s tools")
        if TOOL_CLASSES.get(tool) != NORMAL_CHANGE:
            raise AuthorizationError(
                f"{tool} is not a normal_change; only a normal_change is authorized ahead"
            )
        if not isinstance(effective_hash, str) or not effective_hash:
            raise AuthorizationError("an authorization must bind to an effective hash")
        normalized = _normalize_scope(scope)
        if normalized == SERVER_SCOPE and tool in CAPABILITY_TOOLS:
            # A capability always runs on a host, so this grant could never match.
            raise AuthorizationError(f"{tool} runs on a host; its scope must name hosts")
        if (
            isinstance(max_attempts_per_day, bool)
            or not isinstance(max_attempts_per_day, int)
            or max_attempts_per_day < 1
        ):
            raise AuthorizationError("max_attempts_per_day must be a whole number of at least 1")
        expiry = _parse_instant(expires_at)
        if expiry <= moment:
            raise AuthorizationError("expires_at is in the past")
        if expiry > moment + timedelta(days=MAX_EXPIRY_DAYS):
            raise AuthorizationError(f"expires_at is more than {MAX_EXPIRY_DAYS} days out")
        if not isinstance(granted_by, str) or not granted_by.strip():
            raise AuthorizationError("granted_by is required")
        auth_id = uuid.uuid4().hex
        async with write_lock():
            await self._conn.execute(
                f"INSERT INTO agent_authorizations ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                (
                    auth_id,
                    spec.id,
                    effective_hash,
                    tool,
                    json.dumps(normalized),
                    max_attempts_per_day,
                    _iso(expiry),
                    granted_by.strip(),
                    _iso(moment),
                    str(note or "").strip()[:_MAX_NOTE],
                ),
            )
            await self._conn.commit()
        granted = await self.get(auth_id)
        if granted is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"authorization {auth_id} vanished after insert")
        return granted

    async def revoke(
        self, authorization_id: str, actor: str, *, now: datetime | None = None
    ) -> Authorization:
        """Revoke one authorization; the next call it would have covered is refused.

        A call already forwarded is not recalled. Revoking twice keeps the first
        revocation. Raises :class:`KeyError` for an unknown id.
        """

        async with write_lock():
            await self._conn.execute(
                "UPDATE agent_authorizations SET revoked_at = ?, revoked_by = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (_iso(now or datetime.now(timezone.utc)), actor, authorization_id),
            )
            await self._conn.commit()
        found = await self.get(authorization_id)
        if found is None:
            raise KeyError(authorization_id)
        return found

    async def void_other_hashes(
        self,
        agent_id: str,
        effective_hash: str,
        actor: str = "system",
        *,
        now: datetime | None = None,
    ) -> int:
        """Void, for good, every live authorization of ``agent_id`` not bound to ``effective_hash``.

        Returns how many were voided. Revoked and already-voided rows are left
        as they are. Nothing un-voids a row (ADR-0072 rule 6).
        """

        async with write_lock():
            cur = await self._conn.execute(
                "UPDATE agent_authorizations SET voided_at = ?, voided_by = ? "
                "WHERE agent_id = ? AND effective_hash != ? "
                "AND voided_at IS NULL AND revoked_at IS NULL",
                (_iso(now or datetime.now(timezone.utc)), actor, agent_id, effective_hash),
            )
            changed = cur.rowcount or 0
            await self._conn.commit()
        return changed

    # -- the gate's question -----------------------------------------------

    async def consume(
        self,
        *,
        agent_id: str,
        effective_hash: str,
        tool: str,
        host_id: str | None,
        run_id: str | None,
        now: datetime | None = None,
    ) -> Authorization | None:
        """Spend one attempt of a live authorization covering this call, or ``None``.

        A match is: same agent, same effective hash, same tool, not revoked,
        voided or expired, its scope naming ``host_id`` (or the server sentinel
        for ``host_id=None``), and fewer than ``max_attempts_per_day`` attempts
        on that host in the last 24 hours. The attempt is recorded before this
        returns, in the same transaction that counted the earlier ones, so two
        concurrent callers cannot both spend the last attempt.
        """

        if tool in NEVER_AUTHORIZED:
            return None
        moment = now or datetime.now(timezone.utc)
        stamp = _iso(moment)
        since = _iso(moment - BUDGET_WINDOW)
        async with write_lock():
            await _begin_immediate(self._conn)
            try:
                async with self._conn.execute(
                    f"SELECT {_COLUMNS} FROM agent_authorizations "
                    "WHERE agent_id = ? AND effective_hash = ? AND tool = ? "
                    "AND revoked_at IS NULL AND voided_at IS NULL AND expires_at > ? "
                    "ORDER BY granted_at, id",
                    (agent_id, effective_hash, tool, stamp),
                ) as cur:
                    rows = await cur.fetchall()
                chosen: Authorization | None = None
                for row in rows:
                    candidate = Authorization._from_row(row)
                    if not candidate.covers(host_id):
                        continue
                    async with self._conn.execute(
                        "SELECT COUNT(*) AS n FROM agent_authorization_uses "
                        "WHERE authorization_id = ? AND host_id IS ? AND at > ?",
                        (candidate.id, host_id, since),
                    ) as cur:
                        used = await cur.fetchone()
                    if int(used["n"]) < candidate.max_attempts_per_day:
                        chosen = candidate
                        break
                if chosen is None:
                    await self._conn.rollback()
                    return None
                await self._conn.execute(
                    "INSERT INTO agent_authorization_uses (authorization_id, host_id, at, run_id) "
                    "VALUES (?, ?, ?, ?)",
                    (chosen.id, host_id, stamp, run_id),
                )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        return chosen

    # -- reads -------------------------------------------------------------

    async def get(self, authorization_id: str) -> Authorization | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM agent_authorizations WHERE id = ?", (authorization_id,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else Authorization._from_row(row)

    async def list(self, agent_id: str) -> list[Authorization]:
        """Every authorization of ``agent_id``, newest grant first, dead ones included."""

        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM agent_authorizations WHERE agent_id = ? "
            "ORDER BY granted_at DESC, id",
            (agent_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [Authorization._from_row(row) for row in rows]

    async def uses_since(self, authorization_id: str, since: datetime) -> dict[str, int]:
        """Attempts spent on ``authorization_id`` since ``since``, per host (``server`` for none)."""

        async with self._conn.execute(
            "SELECT host_id, COUNT(*) AS n FROM agent_authorization_uses "
            "WHERE authorization_id = ? AND at > ? GROUP BY host_id",
            (authorization_id, _iso(since)),
        ) as cur:
            rows = await cur.fetchall()
        return {(row["host_id"] or SERVER_SCOPE): int(row["n"]) for row in rows}

"""SQLite storage for ITSM tickets (aiosqlite).

A *ticket* is one support case: a requester has a problem with their PC, an
assistant works it, an operator can gate risky steps and read the record
afterwards. This module is pure storage — it knows nothing about chat
transports, LLMs or how a ticket is worked. Lifecycle rules (which state may
follow which, and who may drive the change) live in
:mod:`kenny_server.tickets`.

Five tables, one connection, shared DB file — same shape as the stores in
:mod:`kenny_server.store` (own :func:`~kenny_server.store._configure_connection`
for WAL + busy-timeout, ``CREATE TABLE IF NOT EXISTS`` only, ISO-8601 UTC text
timestamps).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from .store import DEFAULT_DB_PATH, _configure_connection

__all__ = [
    "DEFAULT_DB_PATH",
    "RUN_RETENTION_DAYS",
    "Ticket",
    "TicketApproval",
    "TicketChannel",
    "TicketEvent",
    "TicketRun",
    "TicketStore",
    "now_iso",
    "to_iso",
]

# How long a closed ticket keeps its (potentially large) working transcript.
# Only ``ticket_runs`` is subject to this; see ``TicketStore.prune``.
RUN_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                TEXT PRIMARY KEY,
    number            INTEGER NOT NULL,          -- monotonic display number
    title             TEXT NOT NULL,
    state             TEXT NOT NULL,
    origin            TEXT NOT NULL,             -- discord | dashboard | alert
    priority          TEXT NOT NULL DEFAULT 'normal',
    category          TEXT,
    requester_user_id INTEGER,                   -- NULL for alert-origin
    agent_id          TEXT,                      -- FROZEN at creation
    role_snapshot     TEXT,
    profile_snapshot  TEXT,
    summary           TEXT NOT NULL DEFAULT '',
    resolution        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    closed_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_number ON tickets (number);
CREATE INDEX IF NOT EXISTS idx_tickets_state ON tickets (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_req   ON tickets (requester_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS ticket_runs (
    ticket_id      TEXT PRIMARY KEY,
    messages       TEXT NOT NULL DEFAULT '[]',
    staged_results TEXT NOT NULL DEFAULT '[]',
    queue          TEXT NOT NULL DEFAULT '[]',
    turns          INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT NOT NULL,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    actor      TEXT NOT NULL,
    tool       TEXT,
    tool_class TEXT,
    ok         INTEGER,
    from_state TEXT,
    to_state   TEXT,
    summary    TEXT NOT NULL DEFAULT '',
    fields     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticket_events ON ticket_events (ticket_id, at);

CREATE TABLE IF NOT EXISTS ticket_approvals (
    id                 TEXT PRIMARY KEY,
    ticket_id          TEXT NOT NULL,
    tool_use_id        TEXT NOT NULL,
    tool               TEXT NOT NULL,
    tool_class         TEXT NOT NULL,
    args               TEXT NOT NULL,
    agent_id           TEXT,
    kind               TEXT NOT NULL,        -- operator_approval | user_consent
    status             TEXT NOT NULL DEFAULT 'pending',
    requested_at       TEXT NOT NULL,
    expires_at         TEXT,
    decided_at         TEXT,
    decided_by         INTEGER,
    decided_via        TEXT,
    discord_channel_id TEXT,
    discord_message_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_approvals_open
    ON ticket_approvals (ticket_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS ticket_channels (
    ticket_id   TEXT PRIMARY KEY,
    guild_id    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    private     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    archived_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_channels_thread ON ticket_channels (thread_id);
"""


def to_iso(value: datetime) -> str:
    """Render a datetime as ISO-8601 UTC text (naive input is assumed UTC)."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """Current time as ISO-8601 UTC text."""

    return to_iso(datetime.now(timezone.utc))


def _stamp(now: datetime | str | None) -> str:
    if now is None:
        return now_iso()
    if isinstance(now, datetime):
        return to_iso(now)
    return now


@dataclass(slots=True)
class Ticket:
    """One support case."""

    id: str
    number: int
    title: str
    state: str
    origin: str
    priority: str
    category: str | None
    requester_user_id: int | None
    agent_id: str | None
    role_snapshot: str | None
    profile_snapshot: str | None
    summary: str
    resolution: str | None
    created_at: str
    updated_at: str
    closed_at: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Ticket:
        return cls(
            id=row["id"],
            number=int(row["number"]),
            title=row["title"],
            state=row["state"],
            origin=row["origin"],
            priority=row["priority"],
            category=row["category"],
            requester_user_id=(
                None if row["requester_user_id"] is None else int(row["requester_user_id"])
            ),
            agent_id=row["agent_id"],
            role_snapshot=row["role_snapshot"],
            profile_snapshot=row["profile_snapshot"],
            summary=row["summary"],
            resolution=row["resolution"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TicketEvent:
    """One entry of a ticket's audit trail."""

    id: int
    ticket_id: str
    at: str
    kind: str
    actor: str
    tool: str | None = None
    tool_class: str | None = None
    ok: bool | None = None
    from_state: str | None = None
    to_state: str | None = None
    summary: str = ""
    fields: dict[str, Any] | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> TicketEvent:
        return cls(
            id=int(row["id"]),
            ticket_id=row["ticket_id"],
            at=row["at"],
            kind=row["kind"],
            actor=row["actor"],
            tool=row["tool"],
            tool_class=row["tool_class"],
            ok=None if row["ok"] is None else bool(row["ok"]),
            from_state=row["from_state"],
            to_state=row["to_state"],
            summary=row["summary"],
            fields=json.loads(row["fields"]) if row["fields"] is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TicketApproval:
    """A durable gate: one pending tool call waiting on a human decision."""

    id: str
    ticket_id: str
    tool_use_id: str
    tool: str
    tool_class: str
    args: dict[str, Any]
    agent_id: str | None
    kind: str
    status: str
    requested_at: str
    expires_at: str | None = None
    decided_at: str | None = None
    decided_by: int | None = None
    decided_via: str | None = None
    discord_channel_id: str | None = None
    discord_message_id: str | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> TicketApproval:
        return cls(
            id=row["id"],
            ticket_id=row["ticket_id"],
            tool_use_id=row["tool_use_id"],
            tool=row["tool"],
            tool_class=row["tool_class"],
            args=json.loads(row["args"]),
            agent_id=row["agent_id"],
            kind=row["kind"],
            status=row["status"],
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            decided_by=None if row["decided_by"] is None else int(row["decided_by"]),
            decided_via=row["decided_via"],
            discord_channel_id=row["discord_channel_id"],
            discord_message_id=row["discord_message_id"],
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TicketChannel:
    """Where a ticket's conversation lives, when it has one."""

    ticket_id: str
    guild_id: str
    channel_id: str
    thread_id: str
    private: bool
    created_at: str
    archived_at: str | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> TicketChannel:
        return cls(
            ticket_id=row["ticket_id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            thread_id=row["thread_id"],
            private=bool(row["private"]),
            created_at=row["created_at"],
            archived_at=row["archived_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TicketRun:
    """The assistant's working state for a ticket (transcript + staging)."""

    ticket_id: str
    messages: list[Any] = field(default_factory=list)
    staged_results: list[Any] = field(default_factory=list)
    queue: list[Any] = field(default_factory=list)
    turns: int = 0
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> TicketRun:
        return cls(
            ticket_id=row["ticket_id"],
            messages=json.loads(row["messages"]),
            staged_results=json.loads(row["staged_results"]),
            queue=json.loads(row["queue"]),
            turns=int(row["turns"]),
            updated_at=row["updated_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_TICKET_COLUMNS = (
    "id, number, title, state, origin, priority, category, requester_user_id, "
    "agent_id, role_snapshot, profile_snapshot, summary, resolution, "
    "created_at, updated_at, closed_at"
)
_EVENT_COLUMNS = (
    "id, ticket_id, at, kind, actor, tool, tool_class, ok, from_state, to_state, "
    "summary, fields"
)
_APPROVAL_COLUMNS = (
    "id, ticket_id, tool_use_id, tool, tool_class, args, agent_id, kind, status, "
    "requested_at, expires_at, decided_at, decided_by, decided_via, "
    "discord_channel_id, discord_message_id"
)
_CHANNEL_COLUMNS = "ticket_id, guild_id, channel_id, thread_id, private, created_at, archived_at"

# States whose entry stamps ``closed_at`` (and thus starts the transcript
# retention clock). Kept here — not imported from ``tickets`` — so the store
# stays free of the lifecycle module.
_CLOSING_STATES = frozenset({"closed", "cancelled"})


class TicketStore:
    """Async SQLite-backed store for tickets, their run state, trail and gates.

    Shares the DB file with the stores in :mod:`kenny_server.store` but owns its
    own connection. There is no migration framework: the schema is
    ``CREATE ... IF NOT EXISTS`` only, so ``connect()`` is idempotent.
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        run_retention_days: int = RUN_RETENTION_DAYS,
    ) -> None:
        self.db_path = db_path
        self.run_retention_days = run_retention_days
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
            raise RuntimeError("TicketStore is not connected; call connect() first")
        return self._db

    # -- tickets -----------------------------------------------------------

    async def create(
        self,
        *,
        title: str,
        origin: str,
        id: str | None = None,
        state: str = "new",
        priority: str = "normal",
        category: str | None = None,
        requester_user_id: int | None = None,
        agent_id: str | None = None,
        role_snapshot: str | None = None,
        profile_snapshot: str | None = None,
        summary: str = "",
        now: datetime | str | None = None,
    ) -> Ticket:
        """Insert a ticket and return it, display ``number`` included.

        ``number`` is derived inside the INSERT itself
        (``COALESCE(MAX(number), 0) + 1`` over ``tickets``) so it is assigned
        under the same write lock as the row: concurrent creates get distinct,
        increasing numbers instead of racing on a read-then-write. Numbers are
        monotonic but gap-tolerant — a rolled-back insert burns one.
        """

        ticket_id = id or uuid.uuid4().hex
        stamp = _stamp(now)
        await self._conn.execute(
            f"INSERT INTO tickets ({_TICKET_COLUMNS}) "
            "SELECT ?, COALESCE(MAX(number), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "NULL, ?, ?, NULL FROM tickets",
            (
                ticket_id,
                title,
                state,
                origin,
                priority,
                category,
                requester_user_id,
                agent_id,
                role_snapshot,
                profile_snapshot,
                summary,
                stamp,
                stamp,
            ),
        )
        await self._conn.commit()
        ticket = await self.get(ticket_id)
        if ticket is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"ticket {ticket_id} vanished after insert")
        return ticket

    async def get(self, ticket_id: str) -> Ticket | None:
        """Return one ticket by id, or None."""

        async with self._conn.execute(
            f"SELECT {_TICKET_COLUMNS} FROM tickets WHERE id = ?", (ticket_id,)
        ) as cur:
            row = await cur.fetchone()
        return Ticket.from_row(row) if row else None

    async def get_by_number(self, number: int) -> Ticket | None:
        """Return one ticket by its display number, or None."""

        async with self._conn.execute(
            f"SELECT {_TICKET_COLUMNS} FROM tickets WHERE number = ?", (number,)
        ) as cur:
            row = await cur.fetchone()
        return Ticket.from_row(row) if row else None

    async def list(
        self,
        *,
        state: str | None = None,
        states: Sequence[str] | None = None,
        requester_user_id: int | None = None,
        agent_id: str | None = None,
        updated_before: str | None = None,
        limit: int = 50,
    ) -> list[Ticket]:
        """Return tickets newest-updated first, filtered and capped by ``limit``."""

        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if states:
            clauses.append(f"state IN ({', '.join('?' for _ in states)})")
            params.extend(states)
        if requester_user_id is not None:
            clauses.append("requester_user_id = ?")
            params.append(requester_user_id)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if updated_before is not None:
            clauses.append("updated_at < ?")
            params.append(updated_before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._conn.execute(
            f"SELECT {_TICKET_COLUMNS} FROM tickets {where} "
            "ORDER BY updated_at DESC, number DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [Ticket.from_row(r) for r in rows]

    async def update(
        self,
        ticket_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        resolution: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        now: datetime | str | None = None,
    ) -> Ticket | None:
        """Patch the editable fields of a ticket. ``state`` is NOT one of them."""

        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_stamp(now)]
        for column, value in (
            ("title", title),
            ("summary", summary),
            ("resolution", resolution),
            ("priority", priority),
            ("category", category),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        params.append(ticket_id)
        await self._conn.execute(
            f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", params
        )
        await self._conn.commit()
        return await self.get(ticket_id)

    async def set_state(
        self,
        ticket_id: str,
        to_state: str,
        *,
        actor: str,
        reason: str = "",
        now: datetime | str | None = None,
    ) -> Ticket | None:
        """Low-level state write. Do not call this directly.

        The only sanctioned caller is
        :meth:`kenny_server.tickets.TicketService.transition`, which owns the
        legality and authorization rules. The UPDATE and the ``kind='state'``
        ``ticket_events`` row are written on the same connection and committed
        together, so a state change that left no trace is not representable.
        Returns None if the ticket does not exist.
        """

        stamp = _stamp(now)
        current = await self.get(ticket_id)
        if current is None:
            return None
        closed_at = stamp if to_state in _CLOSING_STATES else None
        await self._conn.execute(
            "UPDATE tickets SET state = ?, updated_at = ?, closed_at = ? WHERE id = ?",
            (to_state, stamp, closed_at, ticket_id),
        )
        await self._insert_event(
            ticket_id=ticket_id,
            at=stamp,
            kind="state",
            actor=actor,
            from_state=current.state,
            to_state=to_state,
            summary=reason,
        )
        await self._conn.commit()
        return await self.get(ticket_id)

    async def set_agent_id(
        self,
        ticket_id: str,
        agent_id: str | None,
        *,
        actor: str,
        reason: str = "",
        now: datetime | str | None = None,
    ) -> Ticket | None:
        """Low-level retarget of the frozen routing target. Do not call directly.

        The only sanctioned caller is
        :meth:`kenny_server.tickets.TicketService.reassign`. Writes the
        ``kind='handoff'`` event in the same transaction as the column change.
        """

        stamp = _stamp(now)
        current = await self.get(ticket_id)
        if current is None:
            return None
        await self._conn.execute(
            "UPDATE tickets SET agent_id = ?, updated_at = ? WHERE id = ?",
            (agent_id, stamp, ticket_id),
        )
        await self._insert_event(
            ticket_id=ticket_id,
            at=stamp,
            kind="handoff",
            actor=actor,
            summary=reason,
            fields={"from_agent_id": current.agent_id, "to_agent_id": agent_id},
        )
        await self._conn.commit()
        return await self.get(ticket_id)

    async def delete(self, ticket_id: str) -> bool:
        """Delete a ticket and everything hanging off it. Operator action only."""

        cur = await self._conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        for table in ("ticket_runs", "ticket_events", "ticket_approvals", "ticket_channels"):
            await self._conn.execute(f"DELETE FROM {table} WHERE ticket_id = ?", (ticket_id,))
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -- run state ---------------------------------------------------------
    #
    # Deliberately its own table rather than columns on ``tickets``: a state
    # change must never rewrite a transcript blob that can grow to megabytes,
    # and the transcript is pruned on its own clock (see ``prune``) while the
    # ticket and its trail are kept.

    async def load_run(self, ticket_id: str) -> TicketRun:
        """Return the ticket's run state (an empty one if never saved)."""

        async with self._conn.execute(
            "SELECT ticket_id, messages, staged_results, queue, turns, updated_at "
            "FROM ticket_runs WHERE ticket_id = ?",
            (ticket_id,),
        ) as cur:
            row = await cur.fetchone()
        return TicketRun.from_row(row) if row else TicketRun(ticket_id=ticket_id)

    async def save_run(
        self,
        ticket_id: str,
        *,
        messages: list[Any] | None = None,
        staged_results: list[Any] | None = None,
        queue: list[Any] | None = None,
        turns: int | None = None,
        now: datetime | str | None = None,
    ) -> TicketRun:
        """Upsert the ticket's run state; omitted parts keep their stored value."""

        current = await self.load_run(ticket_id)
        merged = TicketRun(
            ticket_id=ticket_id,
            messages=current.messages if messages is None else messages,
            staged_results=(
                current.staged_results if staged_results is None else staged_results
            ),
            queue=current.queue if queue is None else queue,
            turns=current.turns if turns is None else turns,
            updated_at=_stamp(now),
        )
        await self._conn.execute(
            "INSERT INTO ticket_runs "
            "(ticket_id, messages, staged_results, queue, turns, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticket_id) DO UPDATE SET "
            "messages=excluded.messages, staged_results=excluded.staged_results, "
            "queue=excluded.queue, turns=excluded.turns, updated_at=excluded.updated_at",
            (
                ticket_id,
                json.dumps(merged.messages, default=str),
                json.dumps(merged.staged_results, default=str),
                json.dumps(merged.queue, default=str),
                merged.turns,
                merged.updated_at,
            ),
        )
        await self._conn.commit()
        return merged

    async def delete_run(self, ticket_id: str) -> bool:
        """Drop one ticket's run state. Returns True if a row was removed."""

        cur = await self._conn.execute(
            "DELETE FROM ticket_runs WHERE ticket_id = ?", (ticket_id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -- events ------------------------------------------------------------

    async def _insert_event(
        self,
        *,
        ticket_id: str,
        at: str,
        kind: str,
        actor: str,
        tool: str | None = None,
        tool_class: str | None = None,
        ok: bool | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        summary: str = "",
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Write one trail row on the current transaction (no commit)."""

        await self._conn.execute(
            "INSERT INTO ticket_events "
            "(ticket_id, at, kind, actor, tool, tool_class, ok, from_state, to_state, "
            "summary, fields) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                at,
                kind,
                actor,
                tool,
                tool_class,
                None if ok is None else (1 if ok else 0),
                from_state,
                to_state,
                summary,
                json.dumps(fields, default=str) if fields is not None else None,
            ),
        )

    async def append_event(
        self,
        *,
        ticket_id: str,
        kind: str,
        actor: str,
        tool: str | None = None,
        tool_class: str | None = None,
        ok: bool | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        summary: str = "",
        fields: dict[str, Any] | None = None,
        now: datetime | str | None = None,
    ) -> None:
        """Append one row to a ticket's audit trail and commit."""

        await self._insert_event(
            ticket_id=ticket_id,
            at=_stamp(now),
            kind=kind,
            actor=actor,
            tool=tool,
            tool_class=tool_class,
            ok=ok,
            from_state=from_state,
            to_state=to_state,
            summary=summary,
            fields=fields,
        )
        await self._conn.commit()

    async def list_events(
        self, ticket_id: str, *, kind: str | None = None, limit: int = 500
    ) -> list[TicketEvent]:
        """Return a ticket's trail oldest-first (the order it reads in)."""

        clauses = ["ticket_id = ?"]
        params: list[Any] = [ticket_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        params.append(limit)
        async with self._conn.execute(
            f"SELECT {_EVENT_COLUMNS} FROM ticket_events "
            f"WHERE {' AND '.join(clauses)} ORDER BY at, id LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [TicketEvent.from_row(r) for r in rows]

    # -- approvals ---------------------------------------------------------
    #
    # ``idx_ticket_approvals_open`` is a partial UNIQUE index over
    # ``ticket_id WHERE status = 'pending'``: at most one open gate per ticket,
    # enforced by SQLite rather than by application logic. It is the durable
    # counterpart of the single in-memory pending slot the dashboard chat keeps.
    # A second pending row therefore raises ``sqlite3.IntegrityError``; callers
    # translate that into a conflict.

    async def create_approval(
        self,
        *,
        ticket_id: str,
        tool_use_id: str,
        tool: str,
        tool_class: str,
        args: dict[str, Any],
        kind: str,
        id: str | None = None,
        agent_id: str | None = None,
        expires_at: datetime | str | None = None,
        discord_channel_id: str | None = None,
        discord_message_id: str | None = None,
        now: datetime | str | None = None,
    ) -> TicketApproval:
        """Open a gate. Raises ``sqlite3.IntegrityError`` if one is already open.

        ``args`` is stored verbatim — this row is the pending call's payload,
        not the human-readable record. Anything rendering it to a person should
        run it through :func:`kenny_server.tickets.redact_args` first.
        """

        approval_id = id or uuid.uuid4().hex
        await self._conn.execute(
            f"INSERT INTO ticket_approvals ({_APPROVAL_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, ?, ?)",
            (
                approval_id,
                ticket_id,
                tool_use_id,
                tool,
                tool_class,
                json.dumps(args, default=str),
                agent_id,
                kind,
                _stamp(now),
                None if expires_at is None else _stamp(expires_at),
                discord_channel_id,
                discord_message_id,
            ),
        )
        await self._conn.commit()
        approval = await self.get_approval(approval_id)
        if approval is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"approval {approval_id} vanished after insert")
        return approval

    async def get_approval(self, approval_id: str) -> TicketApproval | None:
        """Return one approval by id, or None."""

        async with self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM ticket_approvals WHERE id = ?",
            (approval_id,),
        ) as cur:
            row = await cur.fetchone()
        return TicketApproval.from_row(row) if row else None

    async def get_open_approval(self, ticket_id: str) -> TicketApproval | None:
        """Return the ticket's one open gate, or None."""

        async with self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM ticket_approvals "
            "WHERE ticket_id = ? AND status = 'pending'",
            (ticket_id,),
        ) as cur:
            row = await cur.fetchone()
        return TicketApproval.from_row(row) if row else None

    async def list_open_approvals(
        self, *, ticket_id: str | None = None, due_at: datetime | str | None = None
    ) -> list[TicketApproval]:
        """Return pending approvals, oldest request first.

        ``due_at`` narrows the result to gates whose ``expires_at`` has passed —
        what the sweeper needs.
        """

        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if ticket_id is not None:
            clauses.append("ticket_id = ?")
            params.append(ticket_id)
        if due_at is not None:
            clauses.append("expires_at IS NOT NULL AND expires_at <= ?")
            params.append(_stamp(due_at))
        async with self._conn.execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM ticket_approvals "
            f"WHERE {' AND '.join(clauses)} ORDER BY requested_at, id",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [TicketApproval.from_row(r) for r in rows]

    async def decide_approval(
        self,
        approval_id: str,
        *,
        status: str,
        decided_by: int | None = None,
        decided_via: str | None = None,
        now: datetime | str | None = None,
    ) -> TicketApproval | None:
        """Close a pending gate with ``status`` (``approved``/``denied``).

        Only a still-pending row is written, so two racing decisions cannot both
        land; returns None when nothing was pending.
        """

        cur = await self._conn.execute(
            "UPDATE ticket_approvals SET status = ?, decided_at = ?, decided_by = ?, "
            "decided_via = ? WHERE id = ? AND status = 'pending'",
            (status, _stamp(now), decided_by, decided_via, approval_id),
        )
        await self._conn.commit()
        if (cur.rowcount or 0) == 0:
            return None
        return await self.get_approval(approval_id)

    async def expire_approval(
        self, approval_id: str, *, now: datetime | str | None = None
    ) -> TicketApproval | None:
        """Mark one pending gate ``expired``. Returns None if it was not pending."""

        return await self.decide_approval(
            approval_id, status="expired", decided_via="timeout", now=now
        )

    async def set_approval_message(
        self,
        approval_id: str,
        *,
        channel_id: str | None,
        message_id: str | None,
    ) -> bool:
        """Record where the gate was posted. Returns True if the row existed."""

        cur = await self._conn.execute(
            "UPDATE ticket_approvals SET discord_channel_id = ?, discord_message_id = ? "
            "WHERE id = ?",
            (channel_id, message_id, approval_id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -- channels ----------------------------------------------------------

    async def bind_channel(
        self,
        *,
        ticket_id: str,
        guild_id: str,
        channel_id: str,
        thread_id: str,
        private: bool = True,
        now: datetime | str | None = None,
    ) -> TicketChannel:
        """Bind (or rebind) a ticket to its conversation thread."""

        await self._conn.execute(
            f"INSERT INTO ticket_channels ({_CHANNEL_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(ticket_id) DO UPDATE SET "
            "guild_id=excluded.guild_id, channel_id=excluded.channel_id, "
            "thread_id=excluded.thread_id, private=excluded.private",
            (ticket_id, guild_id, channel_id, thread_id, 1 if private else 0, _stamp(now)),
        )
        await self._conn.commit()
        channel = await self.get_channel(ticket_id)
        if channel is None:  # pragma: no cover - upsert just succeeded
            raise RuntimeError(f"channel binding for {ticket_id} vanished after insert")
        return channel

    async def get_channel(self, ticket_id: str) -> TicketChannel | None:
        """Return a ticket's channel binding, or None."""

        async with self._conn.execute(
            f"SELECT {_CHANNEL_COLUMNS} FROM ticket_channels WHERE ticket_id = ?",
            (ticket_id,),
        ) as cur:
            row = await cur.fetchone()
        return TicketChannel.from_row(row) if row else None

    async def channel_by_thread(self, thread_id: str) -> TicketChannel | None:
        """Return the binding owning ``thread_id`` (inbound message routing)."""

        async with self._conn.execute(
            f"SELECT {_CHANNEL_COLUMNS} FROM ticket_channels WHERE thread_id = ?",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
        return TicketChannel.from_row(row) if row else None

    async def archive_channel(
        self, ticket_id: str, *, now: datetime | str | None = None
    ) -> bool:
        """Stamp ``archived_at`` on a binding. Returns True if it existed."""

        cur = await self._conn.execute(
            "UPDATE ticket_channels SET archived_at = ? WHERE ticket_id = ?",
            (_stamp(now), ticket_id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # -- retention ---------------------------------------------------------

    async def prune(self, *, now: datetime | None = None) -> int:
        """Delete run state of tickets closed longer than retention ago.

        Only ``ticket_runs`` rows go: the ticket and its event trail are the
        operator-curated record and are never pruned, the stance
        ``ChatHistoryStore`` takes about conversations. Returns rows deleted.
        """

        now = now or datetime.now(timezone.utc)
        cutoff = to_iso(now - timedelta(days=self.run_retention_days))
        cur = await self._conn.execute(
            "DELETE FROM ticket_runs WHERE ticket_id IN ("
            "SELECT id FROM tickets WHERE closed_at IS NOT NULL AND closed_at < ?)",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

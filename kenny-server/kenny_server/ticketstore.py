"""SQLite storage for ITSM tickets (aiosqlite).

A *ticket* is one support case: a requester has a problem with their PC, an
assistant works it, an operator can gate risky steps and read the record
afterwards. This module is pure storage — it knows nothing about chat
transports, LLMs or how a ticket is worked. Lifecycle rules (which state may
follow which, and who may drive the change) live in
:mod:`kenny_server.tickets`.

Six tables, one connection, shared DB file — same shape as the stores in
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

from .store import DEFAULT_DB_PATH, _begin_immediate, _configure_connection, write_lock

__all__ = [
    "DEFAULT_DB_PATH",
    "PENDING_REQUEST_TTL_SECS",
    "PendingRequest",
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

# How long an unanswered "which PC?" picker stays clickable. Matched to the
# link-claim window in ``discord_identity``: both are a card a human is looking
# at right now, and a stale one should expire rather than open a ticket about a
# problem from last week.
PENDING_REQUEST_TTL_SECS = 900

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
    closed_at         TEXT,
    blocked_on        TEXT NOT NULL DEFAULT '',  -- '' | user | approval | operator
    blocked_since     TEXT,
    blocked_ref       TEXT NOT NULL DEFAULT '',  -- opaque pointer (e.g. an approval id)
    blocked_nudged_at TEXT,
    assignee_user_id  INTEGER,                   -- the operator working it, if claimed
    -- What a ticket is about, for suppressing a duplicate while one is open.
    -- '' means "never deduplicated" and is what every human-opened ticket has.
    -- Comment above, not trailing: SQLite rewrites this CREATE statement on
    -- ALTER TABLE ... DROP COLUMN and cannot parse a comment after the last
    -- column definition.
    dedup_key         TEXT NOT NULL DEFAULT '',
    -- Who put the ticket in the state it is in now, when that was not a person:
    -- 'triage' for an unprompted investigation, '' for everyone else. Rewritten
    -- by every state change, so it describes the current state and not some
    -- earlier one -- a reopened ticket no longer claims kenny resolved it.
    resolved_by       TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_number ON tickets (number);
CREATE INDEX IF NOT EXISTS idx_tickets_state ON tickets (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_req   ON tickets (requester_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_blocked ON tickets (blocked_on, blocked_since);

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

CREATE TABLE IF NOT EXISTS discord_pending_requests (
    id              TEXT PRIMARY KEY,
    discord_user_id TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    guild_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    message_id      TEXT,
    content         TEXT NOT NULL,
    candidates      TEXT NOT NULL,      -- JSON array; what was offered, display only
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_discord_pending_open
    ON discord_pending_requests (consumed_at, expires_at);
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
    blocked_on: str
    blocked_since: str | None
    blocked_ref: str
    blocked_nudged_at: str | None
    assignee_user_id: int | None
    #: Stable identity of *what this ticket is about*, for suppressing a second
    #: ticket while one is already open for the same thing. Empty for every
    #: ticket a human opened -- two people asking the same question are two
    #: cases, and only a machine repeats itself verbatim.
    dedup_key: str = ""
    #: ``"triage"`` when an unprompted investigation put the ticket in its
    #: current state, empty otherwise. The trail knows this too (a ``state`` row
    #: with actor ``system`` and a ``triage:`` reason); the column exists so
    #: "what did kenny decide" is a query rather than a read-through of every
    #: ticket's history.
    resolved_by: str = ""

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
            blocked_on=row["blocked_on"] or "",
            blocked_since=row["blocked_since"],
            blocked_ref=row["blocked_ref"] or "",
            blocked_nudged_at=row["blocked_nudged_at"],
            assignee_user_id=(
                None if row["assignee_user_id"] is None else int(row["assignee_user_id"])
            ),
            dedup_key=row["dedup_key"] or "",
            resolved_by=row["resolved_by"] or "",
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: The ``actor`` value recorded for turns the assistant itself drove (as
#: opposed to ``"user"``, ``"operator"`` or ``"system"``). A named constant
#: because it is a stored column value, not just a UI label — see
#: ``TicketStore._migrate`` for the one-time rename from the old ``"kenny"``
#: value.
ASSISTANT_ACTOR = "assistant"

#: The principal name an unprompted triage session runs under (``triage.py``).
#: Not an account and never resolvable to one — it exists so a trail row, a log
#: line and a denied call can all say *which* assistant did this: the one a
#: person is talking to, or the one that let itself in.
TRIAGE_ACTOR = "triage"


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


@dataclass(slots=True)
class PendingRequest:
    """A support request waiting for its author to say which PC it is about.

    This is the pre-ticket phase, and it exists because there is deliberately no
    conversational state before a ticket: the target host is frozen when the
    ticket is opened and nothing said afterwards may move it, so the question
    "which PC?" cannot be answered by a later message. It is answered by a
    button click instead, and this row is what the click resolves back to.

    ``candidates`` records what was offered, for the trail. It is **not** the
    authorization list — a click re-checks the chosen host against the clicker's
    scope as it is at click time, so a scope narrowed in between still bites.
    """

    id: str
    discord_user_id: str
    user_id: int
    guild_id: str
    channel_id: str
    message_id: str | None
    content: str
    candidates: list[str]
    created_at: str
    expires_at: str
    consumed_at: str | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> PendingRequest:
        return cls(
            id=row["id"],
            discord_user_id=row["discord_user_id"],
            user_id=int(row["user_id"]),
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            content=row["content"],
            candidates=json.loads(row["candidates"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_TICKET_COLUMNS = (
    "id, number, title, state, origin, priority, category, requester_user_id, "
    "agent_id, role_snapshot, profile_snapshot, summary, resolution, "
    "created_at, updated_at, closed_at, "
    "blocked_on, blocked_since, blocked_ref, blocked_nudged_at, assignee_user_id, "
    "dedup_key, resolved_by"
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
_PENDING_COLUMNS = (
    "id, discord_user_id, user_id, guild_id, channel_id, message_id, content, "
    "candidates, created_at, expires_at, consumed_at"
)

# States whose entry stamps ``closed_at`` (and thus starts the transcript
# retention clock). Kept here — not imported from ``tickets`` — so the store
# stays free of the lifecycle module.
_CLOSING_STATES = frozenset({"closed", "cancelled"})

# Columns added after the table's original release, migrated in for existing
# DB files the same way ``UpdateStore._migrate`` adds its channel column
# (ADR-0048) — ``PRAGMA table_info`` + ``ALTER TABLE ADD COLUMN`` for whatever
# is missing.
_TICKET_MIGRATED_COLUMNS: dict[str, str] = {
    "blocked_on": "TEXT NOT NULL DEFAULT ''",
    "blocked_since": "TEXT",
    "blocked_ref": "TEXT NOT NULL DEFAULT ''",
    "blocked_nudged_at": "TEXT",
    "assignee_user_id": "INTEGER",
    "dedup_key": "TEXT NOT NULL DEFAULT ''",
    "resolved_by": "TEXT NOT NULL DEFAULT ''",
}


class TicketStore:
    """Async SQLite-backed store for tickets, their run state, trail and gates.

    Shares the DB file with the stores in :mod:`kenny_server.store` but owns its
    own connection. There is no migration framework beyond
    ``PRAGMA table_info`` + ``ALTER TABLE ADD COLUMN`` for columns added after
    a table's original release (see :meth:`_migrate`) — otherwise the schema is
    ``CREATE ... IF NOT EXISTS`` only, so ``connect()`` stays idempotent.
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
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add the blocked-on axis to a ``tickets`` table created before it
        existed, and fold the retired ``triage``/``awaiting_*`` states into the
        new five-state model plus that axis.

        No commit here — the caller (:meth:`connect`) commits once, after the
        schema script and this. The backfill ``UPDATE``s are idempotent by
        *content*, not by a migration-ran flag: once a row's ``state`` has been
        folded it no longer matches ``state = 'awaiting_user'`` etc., so a
        second ``connect()`` finds nothing left to touch and this is a no-op.
        """

        async with self._conn.execute("PRAGMA table_info(tickets)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        for col, ddl in _TICKET_MIGRATED_COLUMNS.items():
            if col not in cols:
                await self._conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")
        # An index over a migrated column belongs *here*, not in ``_SCHEMA``:
        # the schema script runs before this method, so on an existing database
        # -- one whose ``tickets`` table predates the column -- a CREATE INDEX
        # there fails outright with "no such column" and takes the whole boot
        # with it. Fresh databases reach this line too, so the index is created
        # exactly once either way.
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_dedup ON tickets (dedup_key, state)"
        )
        # blocked_since best-approximates "since when" as the row's last
        # updated_at (the moment the old awaiting_* state was entered) — the
        # exact original transition timestamp is not retrievable without
        # rewriting ticket_events, which ADR-0046 makes the authority and this
        # migration must not touch (see module docstring).
        await self._conn.execute(
            "UPDATE tickets SET state = 'in_progress', blocked_on = 'user', "
            "blocked_since = updated_at WHERE state = 'awaiting_user'"
        )
        await self._conn.execute(
            "UPDATE tickets SET state = 'in_progress', blocked_on = 'approval', "
            "blocked_since = updated_at WHERE state = 'awaiting_approval'"
        )
        await self._conn.execute(
            "UPDATE tickets SET state = 'in_progress', blocked_on = 'operator', "
            "blocked_since = updated_at WHERE state = 'awaiting_agent'"
        )
        await self._conn.execute(
            "UPDATE tickets SET state = 'in_progress' WHERE state = 'triage'"
        )
        # Rename the assistant's actor label from the old "kenny" value to
        # "assistant" (see ASSISTANT_ACTOR above). This is a label rename for
        # the same acting entity, not a change to who acted, so it is not the
        # sort of ``ticket_events`` rewrite the note above warns against.
        await self._conn.execute(
            "UPDATE ticket_events SET actor = 'assistant' WHERE actor = 'kenny'"
        )

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
        dedup_key: str = "",
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
        # A new ticket is never created already blocked or claimed — the
        # trailing five columns (blocked_on, blocked_since, blocked_ref,
        # blocked_nudged_at, assignee_user_id) are their unblocked/unclaimed
        # defaults, spelled out because this is an INSERT...SELECT enumerating
        # every column rather than a bare INSERT that would fall back to the
        # schema's own DEFAULTs.
        await self._conn.execute(
            f"INSERT INTO tickets ({_TICKET_COLUMNS}) "
            "SELECT ?, COALESCE(MAX(number), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "NULL, ?, ?, NULL, '', NULL, '', NULL, NULL, ?, '' FROM tickets",
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
                dedup_key,
            ),
        )
        await self._conn.commit()
        ticket = await self.get(ticket_id)
        if ticket is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"ticket {ticket_id} vanished after insert")
        return ticket

    async def find_open_by_dedup_key(self, dedup_key: str) -> Ticket | None:
        """Return the oldest still-open ticket carrying ``dedup_key``, or None.

        "Open" is ``new``/``in_progress`` — the states in which a case is still
        somebody's to answer. A ``resolved`` ticket is deliberately *not* a
        match: the condition was dealt with, so a fresh occurrence deserves a
        fresh ticket rather than reopening a case someone already closed out.

        Oldest-first, so a recurrence attaches to the ticket that has been
        waiting longest rather than to whichever one sorted first. An empty
        ``dedup_key`` never matches — it is the "not deduplicated" sentinel
        that every human-opened ticket carries.
        """

        if not dedup_key:
            return None
        async with self._conn.execute(
            f"SELECT {_TICKET_COLUMNS} FROM tickets "
            "WHERE dedup_key = ? AND state IN ('new', 'in_progress') "
            "ORDER BY created_at ASC, number ASC LIMIT 1",
            (dedup_key,),
        ) as cur:
            row = await cur.fetchone()
        return Ticket.from_row(row) if row else None

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
        assignee_user_id: int | None = None,
        blocked_on: str | None = None,
        blocked_on_in: Sequence[str] | None = None,
        blocked_before: str | None = None,
        nudged: bool | None = None,
        updated_before: str | None = None,
        limit: int = 50,
    ) -> list[Ticket]:
        """Return tickets newest-updated first, filtered and capped by ``limit``.

        ``blocked_on``/``blocked_on_in`` filter the blocked-on axis;
        ``blocked_before`` narrows to tickets blocked since before a cutoff
        (paired with ``blocked_on_in`` this is what the stall sweep queries);
        ``nudged`` narrows to whether ``blocked_nudged_at`` has been stamped.
        """

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
        if assignee_user_id is not None:
            clauses.append("assignee_user_id = ?")
            params.append(assignee_user_id)
        if blocked_on is not None:
            clauses.append("blocked_on = ?")
            params.append(blocked_on)
        if blocked_on_in:
            clauses.append(f"blocked_on IN ({', '.join('?' for _ in blocked_on_in)})")
            params.extend(blocked_on_in)
        if blocked_before is not None:
            clauses.append("blocked_since IS NOT NULL AND blocked_since < ?")
            params.append(blocked_before)
        if nudged is not None:
            clauses.append("blocked_nudged_at IS " + ("NOT NULL" if nudged else "NULL"))
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

    async def counts(self, *, requester_user_id: int | None = None) -> dict[str, int]:
        """Bucket counts for the dashboard's grouped ticket list.

        Buckets: ``needs_you`` (blocked on ``approval``/``operator``, or a
        ``new`` alert-origin ticket — nobody but an operator can act on
        either), ``waiting`` (blocked on ``user``), ``working`` (``in_progress``
        and unblocked), ``new`` (not yet started, has a requester), ``done``
        (``resolved``/``closed``/``cancelled`` collapsed — this tile only needs
        "no longer open"). Narrowed to one requester's tickets when given,
        mirroring :meth:`list`'s own scoping rule for a non-operator caller.

        Computed by one full scan in Python rather than a ``CASE``-heavy SQL
        aggregate: a household fleet's ticket count is small, and the bucket
        rule is easier to keep in sync with :meth:`list`'s filters this way.
        """

        where = "WHERE requester_user_id = ?" if requester_user_id is not None else ""
        params = [requester_user_id] if requester_user_id is not None else []
        async with self._conn.execute(
            f"SELECT state, blocked_on, requester_user_id FROM tickets {where}", params
        ) as cur:
            rows = await cur.fetchall()
        counts = {"needs_you": 0, "waiting": 0, "working": 0, "new": 0, "done": 0}
        for row in rows:
            state, blocked_on, req = row["state"], row["blocked_on"], row["requester_user_id"]
            if state in ("resolved", "closed", "cancelled"):
                counts["done"] += 1
            elif state == "new":
                counts["needs_you" if req is None else "new"] += 1
            elif state == "in_progress":
                if blocked_on in ("operator", "approval"):
                    counts["needs_you"] += 1
                elif blocked_on == "user":
                    counts["waiting"] += 1
                else:
                    counts["working"] += 1
        return counts

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
        resolved_by: str = "",
        now: datetime | str | None = None,
    ) -> Ticket | None:
        """Low-level state write. Do not call this directly.

        The only sanctioned caller is
        :meth:`kenny_server.tickets.TicketService.transition`, which owns the
        legality and authorization rules. The UPDATE and the ``kind='state'``
        ``ticket_events`` row are written on the same connection and committed
        together, so a state change that left no trace is not representable.
        Returns None if the ticket does not exist.

        Leaving ``to_state != "in_progress"`` also clears the blocked-on axis
        in the same UPDATE: ``blocked_on`` is only meaningful while a ticket is
        being worked, so "resolved but still blocked" must not be representable.

        ``resolved_by`` is written on **every** state change, defaulting to
        empty. It describes the state the ticket is in *now*, so a human
        resolving a ticket clears whatever was there and reopening it clears it
        too — a reopened ticket that no longer claims kenny resolved it is the
        whole point of rewriting rather than only setting.
        """

        stamp = _stamp(now)
        current = await self.get(ticket_id)
        if current is None:
            return None
        closed_at = stamp if to_state in _CLOSING_STATES else None
        async with write_lock():
            if to_state == "in_progress":
                await self._conn.execute(
                    "UPDATE tickets SET state = ?, updated_at = ?, closed_at = ?, "
                    "resolved_by = ? WHERE id = ?",
                    (to_state, stamp, closed_at, resolved_by, ticket_id),
                )
            else:
                await self._conn.execute(
                    "UPDATE tickets SET state = ?, updated_at = ?, closed_at = ?, "
                    "blocked_on = '', blocked_since = NULL, blocked_ref = '', "
                    "blocked_nudged_at = NULL, resolved_by = ? WHERE id = ?",
                    (to_state, stamp, closed_at, resolved_by, ticket_id),
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
        async with write_lock():
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

    async def set_blocked(
        self,
        ticket_id: str,
        blocked_on: str,
        *,
        actor: str,
        ref: str = "",
        reason: str = "",
        now: datetime | str | None = None,
    ) -> Ticket | None:
        """Low-level block/unblock write. Do not call this directly.

        The only sanctioned caller is
        :meth:`kenny_server.tickets.TicketService.block`/:meth:`~kenny_server.tickets.TicketService.unblock`,
        which owns legality and authorization. Empty ``blocked_on`` clears the
        axis (unblock). Writes the UPDATE and the ``kind='block'``
        ``ticket_events`` row on the same connection and commits together,
        mirroring :meth:`set_state`/:meth:`set_agent_id` — a block that left no
        trace must not be representable either. Re-blocking an already-blocked
        ticket resets ``blocked_since`` and clears any prior nudge stamp — this
        is how the stall sweep's escalation (a stale ``user`` block becoming an
        ``operator`` one) restarts the clock.
        """

        stamp = _stamp(now)
        current = await self.get(ticket_id)
        if current is None:
            return None
        blocked_since = stamp if blocked_on else None
        async with write_lock():
            await self._conn.execute(
                "UPDATE tickets SET blocked_on = ?, blocked_since = ?, blocked_ref = ?, "
                "blocked_nudged_at = NULL, updated_at = ? WHERE id = ?",
                (blocked_on, blocked_since, ref, stamp, ticket_id),
            )
            await self._insert_event(
                ticket_id=ticket_id,
                at=stamp,
                kind="block",
                actor=actor,
                summary=reason,
                fields={
                    "from_blocked_on": current.blocked_on,
                    "to_blocked_on": blocked_on,
                    "ref": ref,
                },
            )
            await self._conn.commit()
        return await self.get(ticket_id)

    async def set_assignee(
        self,
        ticket_id: str,
        assignee_user_id: int | None,
        *,
        actor: str,
        reason: str = "",
        now: datetime | str | None = None,
    ) -> Ticket | None:
        """Low-level operator-assignment write. Do not call this directly.

        The only sanctioned caller is
        :meth:`kenny_server.tickets.TicketService.assign`. Writes the
        ``kind='assign'`` event in the same transaction as the column change.
        """

        stamp = _stamp(now)
        current = await self.get(ticket_id)
        if current is None:
            return None
        async with write_lock():
            await self._conn.execute(
                "UPDATE tickets SET assignee_user_id = ?, updated_at = ? WHERE id = ?",
                (assignee_user_id, stamp, ticket_id),
            )
            await self._insert_event(
                ticket_id=ticket_id,
                at=stamp,
                kind="assign",
                actor=actor,
                summary=reason,
                fields={
                    "from_assignee_user_id": current.assignee_user_id,
                    "to_assignee_user_id": assignee_user_id,
                },
            )
            await self._conn.commit()
        return await self.get(ticket_id)

    async def mark_nudged(
        self,
        ticket_id: str,
        *,
        reason: str = "",
        actor: str = "system",
        now: datetime | str | None = None,
    ) -> Ticket | None:
        """Stamp ``blocked_nudged_at`` and record the reminder, same commit.

        Only sanctioned caller: :meth:`kenny_server.tickets.TicketService.nudge_stalled`.
        A ``note`` event, not a dedicated kind: the reminder itself changes
        nothing about the ticket's state or block, it is purely informational.
        """

        stamp = _stamp(now)
        current = await self.get(ticket_id)
        if current is None:
            return None
        async with write_lock():
            await self._conn.execute(
                "UPDATE tickets SET blocked_nudged_at = ? WHERE id = ?", (stamp, ticket_id)
            )
            await self._insert_event(
                ticket_id=ticket_id,
                at=stamp,
                kind="note",
                actor=actor,
                summary=reason or f"stall reminder sent (blocked on {current.blocked_on})",
            )
            await self._conn.commit()
        return await self.get(ticket_id)

    async def delete(self, ticket_id: str) -> bool:
        """Delete a ticket and everything hanging off it. Operator action only."""

        async with write_lock():
            await _begin_immediate(self._conn)
            cur = await self._conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            for table in ("ticket_runs", "ticket_events", "ticket_approvals", "ticket_channels"):
                await self._conn.execute(
                    f"DELETE FROM {table} WHERE ticket_id = ?", (ticket_id,)
                )
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
        """Write one trail row on the current transaction (no commit).

        Every caller already holds :func:`write_lock` around its own
        UPDATE + this call + commit; taking it here too is the re-entrant
        hop (same task, same lock, depth+1 — see :func:`write_lock`), kept
        so a future direct caller cannot bypass serialization by accident.
        """

        async with write_lock():
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

        async with write_lock():
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

    # -- pending requests (the pre-ticket "which PC?" phase) ------------------

    async def open_pending_request(
        self,
        *,
        discord_user_id: str,
        user_id: int,
        guild_id: str,
        channel_id: str,
        content: str,
        candidates: Sequence[str],
        message_id: str | None = None,
        ttl_secs: int = PENDING_REQUEST_TTL_SECS,
        now: datetime | str | None = None,
    ) -> PendingRequest:
        """Record a request whose target host is still unanswered."""

        created = _stamp(now)
        # Derive the expiry from the creation stamp rather than the wall clock,
        # so an injected clock governs both ends of the window.
        expires = to_iso(
            datetime.fromisoformat(created) + timedelta(seconds=max(0, ttl_secs))
        )
        request_id = uuid.uuid4().hex
        await self._conn.execute(
            f"INSERT INTO discord_pending_requests ({_PENDING_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                request_id,
                discord_user_id,
                user_id,
                guild_id,
                channel_id,
                message_id,
                content,
                json.dumps(list(candidates)),
                created,
                expires,
            ),
        )
        await self._conn.commit()
        pending = await self.get_pending_request(request_id)
        if pending is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"pending request {request_id} vanished after insert")
        return pending

    async def get_pending_request(self, request_id: str) -> PendingRequest | None:
        """Return one pending request by id, consumed or not."""

        async with self._conn.execute(
            f"SELECT {_PENDING_COLUMNS} FROM discord_pending_requests WHERE id = ?",
            (request_id,),
        ) as cur:
            row = await cur.fetchone()
        return PendingRequest.from_row(row) if row else None

    async def consume_pending_request(
        self, request_id: str, *, now: datetime | str | None = None
    ) -> PendingRequest | None:
        """Claim a pending request exactly once; None if unknown, used or stale.

        Single-use is what stops a picker card from opening a second ticket when
        two buttons are clicked in quick succession — the losing click finds the
        row already consumed rather than racing the winner.
        """

        stamp = _stamp(now)
        cur = await self._conn.execute(
            "UPDATE discord_pending_requests SET consumed_at = ? "
            "WHERE id = ? AND consumed_at IS NULL AND expires_at > ?",
            (stamp, request_id, stamp),
        )
        if (cur.rowcount or 0) == 0:
            await self._conn.rollback()
            return None
        await self._conn.commit()
        return await self.get_pending_request(request_id)

    async def prune(
        self, *, now: datetime | None = None, retention_days: int | None = None
    ) -> int:
        """Delete run state of tickets closed longer than retention ago.

        Only ``ticket_runs`` rows go: the ticket and its event trail are the
        operator-curated record and are never pruned, the stance
        ``ChatHistoryStore`` takes about conversations. Dead pending requests
        (consumed or expired) go too — an unanswered picker leaves nothing worth
        keeping, and a consumed one's outcome is already the ticket it opened.
        Returns rows deleted.

        ``retention_days`` overrides ``self.run_retention_days`` for this call
        — accepted for conformance with the ``AlertEngine`` prunable protocol
        (ADR-0051); no operator-facing settings key is wired to this store yet.
        """

        now = now or datetime.now(timezone.utc)
        days = retention_days if retention_days is not None else self.run_retention_days
        cutoff = to_iso(now - timedelta(days=days))
        async with write_lock():
            await _begin_immediate(self._conn)
            cur = await self._conn.execute(
                "DELETE FROM ticket_runs WHERE ticket_id IN ("
                "SELECT id FROM tickets WHERE closed_at IS NOT NULL AND closed_at < ?)",
                (cutoff,),
            )
            deleted = cur.rowcount or 0
            cur = await self._conn.execute(
                "DELETE FROM discord_pending_requests "
                "WHERE consumed_at IS NOT NULL OR expires_at <= ?",
                (to_iso(now),),
            )
            deleted += cur.rowcount or 0
            await self._conn.commit()
        return deleted

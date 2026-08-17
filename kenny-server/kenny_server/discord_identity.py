"""SQLite storage for the Discord snowflake -> kenny account mapping (aiosqlite).

This table is the whole basis of the Discord surface's authorization: a Discord
message is only ever acted on because a row here says which kenny account the
author *is*. Everything downstream — role, host scope, capability profile — is
then read from that account, so this module is the single point where an
external platform identity crosses into kenny's own principal model.

Two rules are structural rather than conventional:

* **Snowflakes only.** ``discord_user_id`` is Discord's immutable numeric id.
  A display name is stored exactly once, in ``discord_link_claims.display_hint``,
  purely so an operator can recognise a pending claim in the dashboard — it is
  never an input to :meth:`DiscordIdentityStore.resolve`.
* **Guild-scoped.** ``resolve`` matches on ``(discord_user_id, guild_id)``: a
  mapping made in one guild does not carry into another, and the caller's guild
  allowlist check is therefore not the only thing standing between a foreign
  guild and a principal.

Own connection, shared DB file, ``CREATE TABLE IF NOT EXISTS`` only, ISO-8601
UTC text timestamps — the same shape as :mod:`kenny_server.ticketstore`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from . import security
from .store import DEFAULT_DB_PATH, _configure_connection

__all__ = [
    "CLAIM_CODE_LEN",
    "DEFAULT_CLAIM_TTL_SECS",
    "DiscordIdentity",
    "DiscordIdentityStore",
    "DiscordLinkClaim",
    "IdentityConflict",
    "LINK_VIA",
    "now_iso",
    "to_iso",
]

# How long a ``/link`` claim stays confirmable in the dashboard.
DEFAULT_CLAIM_TTL_SECS = 900

# Claim codes are typed/read by a human out of an ephemeral Discord reply, so
# they are truncated from a full token. 12 url-safe characters is ~71 bits —
# far more than a 15-minute, single-use, operator-confirmed code needs.
CLAIM_CODE_LEN = 12

#: The two enrollment paths. Both land in the same ``discord_identities`` row
#: and both record who did the linking (``linked_by``) and how (``linked_via``).
LINK_VIA: frozenset[str] = frozenset({"claim", "member_list"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_identities (
    discord_user_id TEXT PRIMARY KEY,   -- snowflake ONLY, never a username
    user_id         INTEGER NOT NULL,
    guild_id        TEXT NOT NULL,
    linked_at       TEXT NOT NULL,
    linked_by       INTEGER,
    linked_via      TEXT NOT NULL,      -- claim | member_list
    disabled        INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_identities_user
    ON discord_identities (user_id, guild_id);

CREATE TABLE IF NOT EXISTS discord_link_claims (
    code            TEXT PRIMARY KEY,
    discord_user_id TEXT NOT NULL,
    display_hint    TEXT NOT NULL,      -- display only, never used to resolve anyone
    guild_id        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT,
    consumed_by     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_discord_claims_pending
    ON discord_link_claims (consumed_at, expires_at);
"""

_IDENTITY_COLUMNS = (
    "discord_user_id, user_id, guild_id, linked_at, linked_by, linked_via, disabled"
)
_CLAIM_COLUMNS = (
    "code, discord_user_id, display_hint, guild_id, created_at, expires_at, "
    "consumed_at, consumed_by"
)


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


class IdentityConflict(Exception):
    """The account already has an identity in this guild.

    The unique index over ``(user_id, guild_id)`` is what raises this: one kenny
    account maps to at most one snowflake per guild, so two Discord users can
    never both speak as the same person.
    """

    status_code = 409


@dataclass(slots=True)
class DiscordIdentity:
    """One snowflake -> kenny account binding."""

    discord_user_id: str
    user_id: int
    guild_id: str
    linked_at: str
    linked_by: int | None
    linked_via: str
    disabled: bool

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> DiscordIdentity:
        return cls(
            discord_user_id=row["discord_user_id"],
            user_id=int(row["user_id"]),
            guild_id=row["guild_id"],
            linked_at=row["linked_at"],
            linked_by=None if row["linked_by"] is None else int(row["linked_by"]),
            linked_via=row["linked_via"],
            disabled=bool(row["disabled"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DiscordLinkClaim:
    """A pending ``/link`` request, waiting for an operator to confirm it."""

    code: str
    discord_user_id: str
    display_hint: str
    guild_id: str
    created_at: str
    expires_at: str
    consumed_at: str | None = None
    consumed_by: int | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> DiscordLinkClaim:
        return cls(
            code=row["code"],
            discord_user_id=row["discord_user_id"],
            display_hint=row["display_hint"],
            guild_id=row["guild_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            consumed_by=(
                None if row["consumed_by"] is None else int(row["consumed_by"])
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_open(self, now: datetime | str | None = None) -> bool:
        """True while the claim can still be consumed."""

        return self.consumed_at is None and _stamp(now) < self.expires_at


class DiscordIdentityStore:
    """Async SQLite store for Discord identities and pending link claims.

    Shares the DB file with the other stores but owns its connection. There is
    no migration framework: the schema is ``CREATE ... IF NOT EXISTS`` only, so
    :meth:`connect` is idempotent.
    """

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        *,
        claim_ttl_secs: int = DEFAULT_CLAIM_TTL_SECS,
    ) -> None:
        self.db_path = db_path
        self.claim_ttl_secs = claim_ttl_secs
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
            raise RuntimeError("DiscordIdentityStore is not connected; call connect() first")
        return self._db

    # -- identities --------------------------------------------------------

    async def resolve(self, discord_user_id: str, guild_id: str) -> DiscordIdentity | None:
        """The account behind ``discord_user_id`` in ``guild_id``, or None.

        **A ``disabled`` row resolves to None.** Disabling is the revocation
        switch: it must have exactly the effect of never having been linked, so
        no caller has to remember to check the flag. Callers get an unmapped
        snowflake, which the Discord surface treats as completely inert.
        """

        async with self._conn.execute(
            f"SELECT {_IDENTITY_COLUMNS} FROM discord_identities "
            "WHERE discord_user_id = ? AND guild_id = ? AND disabled = 0",
            (discord_user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
        return DiscordIdentity.from_row(row) if row else None

    async def get(self, discord_user_id: str) -> DiscordIdentity | None:
        """The row for ``discord_user_id`` regardless of guild or disabled flag.

        Administrative read (the dashboard's identity list). Never use it to
        decide whether someone may act — that is :meth:`resolve`.
        """

        async with self._conn.execute(
            f"SELECT {_IDENTITY_COLUMNS} FROM discord_identities WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cur:
            row = await cur.fetchone()
        return DiscordIdentity.from_row(row) if row else None

    async def link(
        self,
        *,
        discord_user_id: str,
        user_id: int,
        guild_id: str,
        linked_via: str,
        linked_by: int | None = None,
        now: datetime | str | None = None,
    ) -> DiscordIdentity:
        """Bind (or rebind) ``discord_user_id`` to a kenny account.

        Both enrollment paths end here: the user-initiated claim
        (``linked_via='claim'``) and the operator's pick from the guild member
        list (``linked_via='member_list'``). Rebinding a snowflake to another
        account is allowed (an operator fixing a misassignment); binding a
        *second* snowflake to an account that already has one in this guild is
        not, and raises :class:`IdentityConflict`.
        """

        if linked_via not in LINK_VIA:
            raise ValueError(f"linked_via {linked_via!r} must be one of {sorted(LINK_VIA)}")
        try:
            await self._conn.execute(
                f"INSERT INTO discord_identities ({_IDENTITY_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(discord_user_id) DO UPDATE SET "
                "user_id=excluded.user_id, guild_id=excluded.guild_id, "
                "linked_at=excluded.linked_at, linked_by=excluded.linked_by, "
                "linked_via=excluded.linked_via, disabled=0",
                (
                    discord_user_id,
                    user_id,
                    guild_id,
                    _stamp(now),
                    linked_by,
                    linked_via,
                ),
            )
        except sqlite3.IntegrityError as exc:
            await self._conn.rollback()
            raise IdentityConflict(
                f"user {user_id} already has a Discord identity in guild {guild_id}"
            ) from exc
        await self._conn.commit()
        identity = await self.get(discord_user_id)
        if identity is None:  # pragma: no cover - upsert just succeeded
            raise RuntimeError(f"identity {discord_user_id} vanished after insert")
        return identity

    async def unlink(self, discord_user_id: str) -> bool:
        """Delete a binding. Returns True if one existed."""

        cur = await self._conn.execute(
            "DELETE FROM discord_identities WHERE discord_user_id = ?", (discord_user_id,)
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def unlink_user(self, user_id: int) -> int:
        """Delete every *active* binding a kenny account holds. Returns the count removed.

        The self-service counterpart to :meth:`unlink`, which deletes by
        snowflake (an operator's tool) — this deletes by ``user_id`` (the
        account's own tool), so it is what backs ``DELETE /api/me/discord``.
        Deliberately scoped to ``disabled = 0``: a disabled row was already
        revoked by an operator, carries no privilege (see :meth:`resolve`),
        and stays an operator-owned record rather than something self-service
        can make disappear.

        A user may hold a binding per guild (the ``(user_id, guild_id)``
        unique index allows one row per guild), and this removes all of them
        in one call rather than taking a guild — there is no guild the caller
        can legitimately ask to keep, since a binding grants the same kind of
        privilege in every guild it exists in.
        """

        cur = await self._conn.execute(
            "DELETE FROM discord_identities WHERE user_id = ? AND disabled = 0",
            (user_id,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def set_disabled(self, discord_user_id: str, *, disabled: bool) -> bool:
        """Flip a binding's ``disabled`` flag. Returns True if the row existed.

        A disabled row keeps its audit trail (who linked it, when, how) while
        resolving to nothing — the reason revocation is a flag and not a delete.
        """

        cur = await self._conn.execute(
            "UPDATE discord_identities SET disabled = ? WHERE discord_user_id = ?",
            (1 if disabled else 0, discord_user_id),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def list_identities(
        self,
        *,
        user_id: int | None = None,
        guild_id: str | None = None,
        include_disabled: bool = True,
    ) -> list[DiscordIdentity]:
        """Identities, newest link first, optionally filtered."""

        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if not include_disabled:
            clauses.append("disabled = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._conn.execute(
            f"SELECT {_IDENTITY_COLUMNS} FROM discord_identities {where} "
            "ORDER BY linked_at DESC, discord_user_id",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [DiscordIdentity.from_row(r) for r in rows]

    # -- link claims -------------------------------------------------------

    async def open_claim(
        self,
        *,
        discord_user_id: str,
        display_hint: str,
        guild_id: str,
        ttl_secs: int | None = None,
        now: datetime | str | None = None,
    ) -> DiscordLinkClaim:
        """Open a pending claim for ``discord_user_id`` and return it.

        ``display_hint`` is carried so the operator confirming in the dashboard
        can tell *which* guild member this is. It is written, shown, and never
        read back by any resolution path.
        """

        created = _stamp(now)
        ttl = self.claim_ttl_secs if ttl_secs is None else ttl_secs
        # Derive the expiry from the creation stamp, not from the wall clock, so
        # an injected clock governs both ends of the window.
        expires = to_iso(datetime.fromisoformat(created) + timedelta(seconds=max(0, ttl)))
        code = security.generate_token()[:CLAIM_CODE_LEN]
        await self._conn.execute(
            f"INSERT INTO discord_link_claims ({_CLAIM_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
            (code, discord_user_id, display_hint, guild_id, created, expires),
        )
        await self._conn.commit()
        claim = await self.get_claim(code)
        if claim is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError(f"claim {code} vanished after insert")
        return claim

    async def get_claim(self, code: str) -> DiscordLinkClaim | None:
        """Return one claim by its code, consumed or not."""

        async with self._conn.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM discord_link_claims WHERE code = ?", (code,)
        ) as cur:
            row = await cur.fetchone()
        return DiscordLinkClaim.from_row(row) if row else None

    async def consume_claim(
        self,
        code: str,
        *,
        user_id: int,
        linked_by: int | None = None,
        now: datetime | str | None = None,
    ) -> DiscordIdentity | None:
        """Confirm a claim and create the identity it asked for.

        Returns None — and changes nothing — when the code is unknown, already
        consumed, or past ``expires_at``. The consume and the link are one
        transaction, so a confirmed claim can never leave a consumed row without
        a binding (or the reverse). Raises :class:`IdentityConflict` if the
        account already has an identity in that guild.
        """

        stamp = _stamp(now)
        cur = await self._conn.execute(
            "UPDATE discord_link_claims SET consumed_at = ?, consumed_by = ? "
            "WHERE code = ? AND consumed_at IS NULL AND expires_at > ?",
            (stamp, user_id, code, stamp),
        )
        if (cur.rowcount or 0) == 0:
            await self._conn.rollback()
            return None
        claim = await self.get_claim(code)
        if claim is None:  # pragma: no cover - updated above
            await self._conn.rollback()
            return None
        try:
            await self._conn.execute(
                f"INSERT INTO discord_identities ({_IDENTITY_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, 'claim', 0) "
                "ON CONFLICT(discord_user_id) DO UPDATE SET "
                "user_id=excluded.user_id, guild_id=excluded.guild_id, "
                "linked_at=excluded.linked_at, linked_by=excluded.linked_by, "
                "linked_via='claim', disabled=0",
                (
                    claim.discord_user_id,
                    user_id,
                    claim.guild_id,
                    stamp,
                    linked_by,
                ),
            )
        except sqlite3.IntegrityError as exc:
            await self._conn.rollback()
            raise IdentityConflict(
                f"user {user_id} already has a Discord identity in guild {claim.guild_id}"
            ) from exc
        await self._conn.commit()
        return await self.get(claim.discord_user_id)

    async def list_pending_claims(
        self, *, guild_id: str | None = None, now: datetime | str | None = None
    ) -> list[DiscordLinkClaim]:
        """Unconsumed, unexpired claims — what the dashboard offers to confirm."""

        clauses = ["consumed_at IS NULL", "expires_at > ?"]
        params: list[Any] = [_stamp(now)]
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        async with self._conn.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM discord_link_claims "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, code",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [DiscordLinkClaim.from_row(r) for r in rows]

    # -- retention ---------------------------------------------------------

    async def prune(
        self, *, now: datetime | str | None = None, retention_days: int | None = None
    ) -> int:
        """Delete dead claims (expired or consumed). Identities are never pruned.

        A consumed claim's outcome already lives on the identity row
        (``linked_at``/``linked_by``/``linked_via``), so nothing auditable is
        lost; an expired one never produced anything.

        ``retention_days`` is accepted and ignored: this store has no
        duration-based retention window to override (a claim is deleted the
        moment it is dead, not after N days) — the parameter exists only for
        conformance with the ``AlertEngine`` prunable protocol (ADR-0051).
        """

        cur = await self._conn.execute(
            "DELETE FROM discord_link_claims WHERE consumed_at IS NOT NULL OR expires_at <= ?",
            (_stamp(now),),
        )
        await self._conn.commit()
        return cur.rowcount or 0

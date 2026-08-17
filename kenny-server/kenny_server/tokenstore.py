"""SQLite-backed per-agent token store (aiosqlite), hashed at rest.

Agent tokens are stored as ``sha256`` hex digests in an ``agent_tokens`` table
(never the plaintext). Verification is constant-time (``hmac.compare_digest``)
over the hex digest. New tokens are minted with :func:`secrets.token_urlsafe`
and returned to the caller **once**; only their hash is persisted.

On first connect the store **seeds** the historic dev token map and any
``KENNY_AGENT_TOKENS`` env pairs so existing agents/tests keep authenticating
without a manual rotation step. Seeding never overwrites an already-stored
agent (so a rotated token survives a restart).

Shares the same DB file as :class:`~kenny_server.store.TelemetryStore`
(``KENNY_DB_PATH``); it opens its own aiosqlite connection to keep the two
stores independent and simple. See ADR-0014.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite

from .registry import load_tokens
from .store import _configure_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tokens (
    agent_id          TEXT PRIMARY KEY,
    token_sha256      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    rotated_at        TEXT,
    prev_token_sha256 TEXT,
    prev_expires_at   TEXT
);
"""

# Columns added after the initial release; older DBs need an ALTER on connect.
_GRACE_COLUMNS = ("prev_token_sha256", "prev_expires_at")

_DEFAULT_GRACE_SECS = 7 * 24 * 3600  # 7 days


def _grace_secs() -> int:
    """Seconds a rotated-away token keeps verifying (until the new one is used).

    Bounds how long the previous token stays valid after a rotation so that
    generating an installer never instantly bricks a still-connected agent. See
    ADR-0014. ``0`` restores the historic instant-invalidation behaviour.
    """

    raw = os.environ.get("KENNY_TOKEN_GRACE_SECS")
    if raw is None or raw == "":
        return _DEFAULT_GRACE_SECS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_GRACE_SECS


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AgentTokenStore:
    """Async SQLite-backed store for hashed per-agent tokens."""

    def __init__(self, db_path: str) -> None:
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
        await self._seed()

    async def _migrate(self) -> None:
        """Add grace-window columns to DBs created before they existed."""

        async with self._conn.execute("PRAGMA table_info(agent_tokens)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        for col in _GRACE_COLUMNS:
            if col not in cols:
                await self._conn.execute(
                    f"ALTER TABLE agent_tokens ADD COLUMN {col} TEXT"
                )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AgentTokenStore is not connected; call connect() first")
        return self._db

    async def _seed(self) -> None:
        """Bootstrap historic dev/env tokens without clobbering rotated ones."""

        now = datetime.now(timezone.utc).isoformat()
        for agent_id, token in load_tokens().items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO agent_tokens "
                "(agent_id, token_sha256, created_at, rotated_at) "
                "VALUES (?, ?, ?, NULL)",
                (agent_id, _sha256_hex(token), now),
            )
        await self._conn.commit()

    async def verify(self, agent_id: str, token: str) -> bool:
        """Return True iff ``token`` matches the current or grace-period token.

        The current token always verifies. The previous token (set by the most
        recent :meth:`create_or_rotate`) also verifies until either the current
        token is first seen here — which retires the previous one — or its grace
        window (``prev_expires_at``) lapses. This keeps a still-connected agent
        authenticating across a rotation it hasn't picked up yet (ADR-0014).
        """

        if not token:
            return False
        async with self._conn.execute(
            "SELECT token_sha256, prev_token_sha256, prev_expires_at "
            "FROM agent_tokens WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False

        digest = _sha256_hex(token)
        if hmac.compare_digest(row["token_sha256"], digest):
            # New token first seen: retire the previous one so it stops verifying.
            if row["prev_token_sha256"] is not None:
                await self._conn.execute(
                    "UPDATE agent_tokens SET prev_token_sha256 = NULL, "
                    "prev_expires_at = NULL WHERE agent_id = ?",
                    (agent_id,),
                )
                await self._conn.commit()
            return True

        prev = row["prev_token_sha256"]
        if prev is not None and not self._grace_expired(row["prev_expires_at"]):
            return hmac.compare_digest(prev, digest)
        return False

    @staticmethod
    def _grace_expired(expires_at: str | None) -> bool:
        if not expires_at:
            return True
        return datetime.now(timezone.utc) >= datetime.fromisoformat(expires_at)

    async def create_or_rotate(self, agent_id: str) -> str:
        """Mint a fresh token for ``agent_id``, persist its hash, return plaintext.

        The plaintext is returned **once** and never stored; callers must capture
        it. Any previously issued token is demoted to a grace-period token that
        keeps verifying until the new one is first used or ``KENNY_TOKEN_GRACE_SECS``
        elapses, so generating an installer never instantly bricks a live agent
        (ADR-0014).
        """

        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=_grace_secs())).isoformat()
        await self._conn.execute(
            "INSERT INTO agent_tokens "
            "(agent_id, token_sha256, created_at, rotated_at) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "prev_token_sha256 = agent_tokens.token_sha256, "
            "prev_expires_at = ?, "
            "token_sha256 = excluded.token_sha256, "
            "rotated_at = ?",
            (agent_id, _sha256_hex(token), now_iso, expires_iso, now_iso),
        )
        await self._conn.commit()
        return token

    async def list_agents(self) -> list[dict[str, str | None]]:
        """Return stored agents with timestamps (no token material)."""

        async with self._conn.execute(
            "SELECT agent_id, created_at, rotated_at FROM agent_tokens "
            "ORDER BY agent_id"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "agent_id": r["agent_id"],
                "created_at": r["created_at"],
                "rotated_at": r["rotated_at"],
            }
            for r in rows
        ]

    async def has_token(self, agent_id: str) -> bool:
        """True iff a credential exists for ``agent_id`` (no token material read).

        The question "did anything actually get minted?" — which is what makes a
        share link's lazy mint checkable: a link that was never redeemed must
        leave this False (ADR-0053). Distinct from :meth:`verify`, which asks
        whether a *given* token is the right one.
        """

        async with self._conn.execute(
            "SELECT 1 FROM agent_tokens WHERE agent_id = ? LIMIT 1", (agent_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def delete(self, agent_id: str) -> None:
        """Forget an agent's token (host removed from inventory, ADR-0033)."""

        await self._conn.execute(
            "DELETE FROM agent_tokens WHERE agent_id = ?", (agent_id,)
        )
        await self._conn.commit()

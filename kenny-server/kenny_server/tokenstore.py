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
stores independent and simple. See ADR-0013.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

import aiosqlite

from .registry import load_tokens

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tokens (
    agent_id     TEXT PRIMARY KEY,
    token_sha256 TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    rotated_at   TEXT
);
"""


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
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self._seed()

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
        """Return True iff ``token`` matches the stored hash for ``agent_id``."""

        if not token:
            return False
        async with self._conn.execute(
            "SELECT token_sha256 FROM agent_tokens WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        return hmac.compare_digest(row["token_sha256"], _sha256_hex(token))

    async def create_or_rotate(self, agent_id: str) -> str:
        """Mint a fresh token for ``agent_id``, persist its hash, return plaintext.

        The plaintext is returned **once** and never stored; callers must capture
        it. Any previously issued token for the agent stops verifying immediately.
        """

        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO agent_tokens (agent_id, token_sha256, created_at, rotated_at) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "token_sha256 = excluded.token_sha256, rotated_at = ?",
            (agent_id, _sha256_hex(token), now, now),
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

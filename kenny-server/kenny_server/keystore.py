"""SQLite-backed per-agent Ed25519 public-key store (aiosqlite) + server identity.

Mutual agent⇄server authentication (ADR-0022). Each agent holds its own Ed25519
keypair; the server stores that agent's **public** key (base64, standard padding)
in an ``agent_keys`` table and verifies the agent's ``auth`` signature against it.
The server itself holds one server-wide Ed25519 keypair: the private seed comes
from ``KENNY_SERVER_PRIVATE_KEY`` / ``KENNY_SERVER_PRIVATE_KEY_FILE`` (base64 raw
32-byte seed); when neither is set the store generates one, persists the seed in a
``server_identity`` table, and logs the public key at WARNING so the operator can
pin it in the installer.

Key rotation mirrors :class:`~kenny_server.tokenstore.AgentTokenStore`: a re-key
demotes the old public key to a grace-period key that keeps verifying until the
new one is first seen or ``KENNY_KEY_GRACE_SECS`` elapses, so re-keying never
instantly bricks a still-connected agent.

Shares the same DB file as the other stores (``KENNY_DB_PATH``); it opens its own
aiosqlite connection to keep the stores independent and simple. See ADR-0022.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .store import _configure_connection

logger = logging.getLogger("kenny.keystore")

# Domain-separation label for the mutual-auth transcript (ADR-0022). Must stay
# byte-identical to the Rust agent and the golden vectors.
_TRANSCRIPT_LABEL = b"kenny-mutual-auth-v1"


def build_transcript(
    agent_id: str, client_nonce_raw: bytes, server_nonce_raw: bytes
) -> bytes:
    """Build the byte-exact transcript both sides sign (see ``docs/protocol.md``).

    ``transcript = label || 0x00 || agent_id || 0x00 || client_nonce || 0x00 ||
    server_nonce`` where the nonces are the **raw** 32 bytes (base64-decoded) and
    ``0x00`` is a single NUL separator.
    """

    return (
        _TRANSCRIPT_LABEL
        + b"\x00"
        + agent_id.encode("utf-8")
        + b"\x00"
        + client_nonce_raw
        + b"\x00"
        + server_nonce_raw
    )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_keys (
    agent_id        TEXT PRIMARY KEY,
    public_key      TEXT NOT NULL,
    enrolled_at     TEXT,
    prev_public_key TEXT,
    prev_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS server_identity (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    seed_b64  TEXT NOT NULL
);
"""

# Columns added after the initial release; older DBs need an ALTER on connect.
_GRACE_COLUMNS = ("prev_public_key", "prev_expires_at")

_DEFAULT_GRACE_SECS = 7 * 24 * 3600  # 7 days


def _grace_secs() -> int:
    """Seconds a rotated-away public key keeps verifying (until the new one is used).

    Bounds how long the previous key stays valid after a re-key so that an
    operator-initiated rotation never instantly locks out a still-connected
    agent. ``0`` restores instant invalidation.
    """

    raw = os.environ.get("KENNY_KEY_GRACE_SECS")
    if raw is None or raw == "":
        return _DEFAULT_GRACE_SECS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_GRACE_SECS


class KeyStore:
    """Async SQLite-backed store for per-agent public keys + the server keypair."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._server_key: Ed25519PrivateKey | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self.db_path)
        await _configure_connection(self._db)
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()
        await self._load_server_key()

    async def _migrate(self) -> None:
        """Add grace-window columns to DBs created before they existed."""

        async with self._conn.execute("PRAGMA table_info(agent_keys)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        for col in _GRACE_COLUMNS:
            if col not in cols:
                await self._conn.execute(
                    f"ALTER TABLE agent_keys ADD COLUMN {col} TEXT"
                )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("KeyStore is not connected; call connect() first")
        return self._db

    # -- per-agent public keys ---------------------------------------------

    async def enroll(self, agent_id: str, public_key_b64: str) -> None:
        """Bind ``public_key_b64`` to ``agent_id`` on first enrollment.

        Enrollment binds **once**: if a current key already exists for the agent
        this raises :class:`ValueError`. Re-keying is an explicit operator action
        (see :meth:`rotate`), not a silent overwrite via the enrollment endpoint.
        """

        # Validate the key material up front so a bad enrollment fails loudly.
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        except Exception as exc:  # noqa: BLE001 - normalize to ValueError
            raise ValueError(f"invalid Ed25519 public key: {exc}") from exc

        async with self._conn.execute(
            "SELECT 1 FROM agent_keys WHERE agent_id = ?", (agent_id,)
        ) as cur:
            if await cur.fetchone() is not None:
                raise ValueError(f"agent {agent_id!r} is already enrolled")

        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO agent_keys (agent_id, public_key, enrolled_at) "
            "VALUES (?, ?, ?)",
            (agent_id, public_key_b64, now),
        )
        await self._conn.commit()

    async def rotate(self, agent_id: str, public_key_b64: str) -> None:
        """Replace an agent's public key, demoting the old one to a grace key.

        Explicit operator-initiated re-key. The previous key keeps verifying
        until the new one is first seen by :meth:`verify_signature` or the grace
        window (``KENNY_KEY_GRACE_SECS``) lapses.
        """

        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid Ed25519 public key: {exc}") from exc

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=_grace_secs())).isoformat()
        await self._conn.execute(
            "INSERT INTO agent_keys (agent_id, public_key, enrolled_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "prev_public_key = agent_keys.public_key, "
            "prev_expires_at = ?, "
            "public_key = excluded.public_key, "
            "enrolled_at = ?",
            (agent_id, public_key_b64, now_iso, expires_iso, now_iso),
        )
        await self._conn.commit()

    async def verify_signature(
        self, agent_id: str, transcript: bytes, sig_b64: str
    ) -> bool:
        """Return True iff ``sig_b64`` over ``transcript`` verifies for ``agent_id``.

        The current key always verifies. A still-valid grace key (from a recent
        :meth:`rotate`) also verifies until the current key is first seen here —
        which retires the grace key — or its grace window lapses. Ed25519 verify
        is constant-time; ``InvalidSignature`` is caught and returns False.
        """

        if not sig_b64:
            return False
        async with self._conn.execute(
            "SELECT public_key, prev_public_key, prev_expires_at "
            "FROM agent_keys WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False

        try:
            sig = base64.b64decode(sig_b64)
        except Exception:  # noqa: BLE001 - malformed base64
            return False

        if self._verify_one(row["public_key"], transcript, sig):
            # New key first seen: retire any grace key so it stops verifying.
            if row["prev_public_key"] is not None:
                await self._conn.execute(
                    "UPDATE agent_keys SET prev_public_key = NULL, "
                    "prev_expires_at = NULL WHERE agent_id = ?",
                    (agent_id,),
                )
                await self._conn.commit()
            return True

        prev = row["prev_public_key"]
        if prev is not None and not self._grace_expired(row["prev_expires_at"]):
            return self._verify_one(prev, transcript, sig)
        return False

    @staticmethod
    def _verify_one(public_key_b64: str, transcript: bytes, sig: bytes) -> bool:
        try:
            pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
            pub.verify(sig, transcript)
            return True
        except InvalidSignature:
            return False
        except Exception:  # noqa: BLE001 - malformed key material
            return False

    @staticmethod
    def _grace_expired(expires_at: str | None) -> bool:
        if not expires_at:
            return True
        return datetime.now(timezone.utc) >= datetime.fromisoformat(expires_at)

    async def delete(self, agent_id: str) -> None:
        """Forget an agent's public key (host removed from inventory, ADR-0033).

        Only the per-agent ``agent_keys`` row is removed; the server identity is
        left untouched.
        """

        await self._conn.execute(
            "DELETE FROM agent_keys WHERE agent_id = ?", (agent_id,)
        )
        await self._conn.commit()

    # -- server identity ---------------------------------------------------

    async def _load_server_key(self) -> None:
        """Load (or generate-and-persist) the server-wide Ed25519 private key.

        Precedence: ``KENNY_SERVER_PRIVATE_KEY`` (base64 seed) >
        ``KENNY_SERVER_PRIVATE_KEY_FILE`` (path to base64 seed) > a seed persisted
        in the ``server_identity`` table. When none exists, generate one, persist
        it, and log the public key at WARNING so the operator can pin it.
        """

        seed = self._seed_from_env()
        if seed is not None:
            self._server_key = Ed25519PrivateKey.from_private_bytes(seed)
            return

        async with self._conn.execute(
            "SELECT seed_b64 FROM server_identity WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            seed = base64.b64decode(row["seed_b64"])
            self._server_key = Ed25519PrivateKey.from_private_bytes(seed)
            return

        # Generate, persist, and warn so the operator can pin the public key.
        key = Ed25519PrivateKey.generate()
        seed = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        await self._conn.execute(
            "INSERT OR REPLACE INTO server_identity (id, seed_b64) VALUES (1, ?)",
            (base64.b64encode(seed).decode(),),
        )
        await self._conn.commit()
        self._server_key = key
        logger.warning(
            "no KENNY_SERVER_PRIVATE_KEY set; generated a server identity. "
            "Pin this public key in the agent installer: %s",
            self.server_public_key_b64(),
        )

    @staticmethod
    def _seed_from_env() -> bytes | None:
        raw = os.environ.get("KENNY_SERVER_PRIVATE_KEY", "").strip()
        if not raw:
            path = os.environ.get("KENNY_SERVER_PRIVATE_KEY_FILE", "").strip()
            if path:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = fh.read().strip()
        if not raw:
            return None
        seed = base64.b64decode(raw)
        if len(seed) != 32:
            raise ValueError(
                "KENNY_SERVER_PRIVATE_KEY must be a base64 raw 32-byte Ed25519 seed"
            )
        return seed

    @property
    def _server_private_key(self) -> Ed25519PrivateKey:
        if self._server_key is None:
            raise RuntimeError("KeyStore is not connected; call connect() first")
        return self._server_key

    def sign_transcript(self, transcript: bytes) -> str:
        """Sign ``transcript`` with the server private key; return base64 signature."""

        sig = self._server_private_key.sign(transcript)
        return base64.b64encode(sig).decode()

    def server_public_key_b64(self) -> str:
        """Return the server's Ed25519 public key as standard base64 (with padding)."""

        pub = self._server_private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(pub).decode()

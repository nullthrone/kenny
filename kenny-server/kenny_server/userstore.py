"""SQLite-backed user store: accounts, PATs, sessions, and host scope.

Backs the multi-user auth model (ADR-0033). Four tables in the shared
``KENNY_DB_PATH`` database, following the same conventions as the other stores
(own aiosqlite connection, ``CREATE TABLE IF NOT EXISTS`` at connect, timestamps
as ISO-8601 UTC):

* ``users`` — one row per account. Passwords are scrypt hashes (never plaintext);
  ``totp_secret`` and ``email`` are optional. ``role`` is ``superuser`` /
  ``operator`` / ``user``.
* ``user_tokens`` — personal access tokens (PATs) for Claude/MCP, stored as
  sha256 digests; the plaintext is returned once at creation and never stored.
* ``sessions`` — browser sessions; the cookie carries the opaque ``id`` only, so
  a stolen cookie is a session handle, not a reusable credential.
* ``user_hosts`` — which agents a ``user``-role account may see and operate on.

``users.theme`` is the one *preference* the store carries: ``light``/``dark``, or
``NULL`` for an account that never chose. It is not an authorization axis and
nothing branches on it — it exists so a choice made in one browser is still there
in the next one.

``users.capability_profile`` is a third, optional authorization axis alongside
role and host scope: a named tool-allowlist (see ``tool_classes.PROFILES``)
that narrows what the account may do. ``NULL`` means "no profile set" —
unrestricted, subject to role and host scope, exactly as before this column
existed.

All lookups that resolve a credential to an account exclude disabled users and
expired/revoked rows, so authorization upstream can trust a returned row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite

from . import security
from .store import _configure_connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    username           TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    totp_secret        TEXT,
    email              TEXT,
    role               TEXT NOT NULL,
    avatar             TEXT,
    disabled           INTEGER NOT NULL DEFAULT 0,
    capability_profile TEXT,
    theme              TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    label        TEXT,
    created_at   TEXT NOT NULL,
    last_used    TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip         TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS user_hosts (
    user_id  INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    PRIMARY KEY (user_id, agent_id)
);
"""

_DEFAULT_SESSION_TTL_SECS = 7 * 24 * 3600  # 7 days

# Columns added after the initial release; older DBs need an ALTER on connect.
# All are nullable TEXT (see ``_migrate``): NULL is the "never set" value and
# every reader has to treat it as such, which is what makes the ALTER safe to
# run against a populated table.
_MIGRATE_COLUMNS = ("capability_profile", "theme")

#: The console's two themes. NULL means "the account never chose one", which is
#: not the same as either — the client then keeps whatever it decided locally.
THEMES: frozenset[str] = frozenset({"light", "dark"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_user(row: aiosqlite.Row) -> dict:
    """Row → API-safe dict (no password hash, TOTP presence not secret)."""

    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "avatar": row["avatar"],
        "disabled": bool(row["disabled"]),
        "totp_enabled": row["totp_secret"] is not None,
        "capability_profile": row["capability_profile"],
        "theme": row["theme"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class UserExists(Exception):
    """Raised when creating/renaming to an already-taken username."""


class UserStore:
    """Async SQLite store for accounts, PATs, sessions, and host scope."""

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

    async def _migrate(self) -> None:
        """Add columns to DBs created before they existed."""

        async with self._conn.execute("PRAGMA table_info(users)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        for col in _MIGRATE_COLUMNS:
            if col not in cols:
                await self._conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("UserStore is not connected; call connect() first")
        return self._db

    # -- accounts -------------------------------------------------------------

    async def count_users(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) AS n FROM users") as cur:
            row = await cur.fetchone()
        return int(row["n"])

    async def create_user(
        self,
        username: str,
        password: str,
        role: str,
        *,
        email: str | None = None,
        avatar: str | None = None,
        totp_secret: str | None = None,
    ) -> dict:
        """Create an account (password hashed here). Raises :class:`UserExists`."""

        username = username.strip()
        if not username:
            raise ValueError("username must not be empty")
        if not security.is_valid_role(role):
            raise ValueError(f"invalid role {role!r}")
        now = _now_iso()
        try:
            cur = await self._conn.execute(
                "INSERT INTO users "
                "(username, password_hash, totp_secret, email, role, avatar, "
                " disabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    username,
                    security.hash_password(password),
                    totp_secret,
                    (email.strip() or None) if email else None,
                    role,
                    avatar,
                    now,
                    now,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise UserExists(username) from exc
        await self._conn.commit()
        created = await self.get_user(cur.lastrowid)
        assert created is not None
        return created

    async def get_user(self, user_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return _public_user(row) if row else None

    async def _get_row(self, user_id: int) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_by_username(self, username: str) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ) as cur:
            return await cur.fetchone()

    async def get_enabled_row(self, user_id: int) -> aiosqlite.Row | None:
        """The account row for ``user_id`` iff it exists and is enabled.

        Used to resolve an OAuth access token (which stores only ``user_id``) back
        to the account, mirroring the enabled-user guard in :meth:`resolve_pat`.
        """

        row = await self._get_row(user_id)
        if row is None or row["disabled"]:
            return None
        return row

    async def list_users(self) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM users ORDER BY username"
        ) as cur:
            rows = await cur.fetchall()
        return [_public_user(r) for r in rows]

    async def list_directory(self) -> list[dict]:
        """A minimal, auth-safe projection for resolving ids to usernames.

        Unlike :meth:`list_users` (superuser-only, full account row), this is
        exposed to any operator+ account so it can render another account's
        id as a name: just ``{id, username, role}``, no email, avatar,
        capability profile, or any other auth-sensitive field.
        """

        async with self._conn.execute(
            "SELECT id, username, role FROM users ORDER BY username"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r["id"], "username": r["username"], "role": r["role"]}
            for r in rows
        ]

    async def verify_login(self, username: str, password: str) -> aiosqlite.Row | None:
        """Return the account row iff username+password match and it's enabled.

        TOTP (when the row has a ``totp_secret``) is verified by the caller so
        this stays a pure password check.
        """

        row = await self.get_by_username(username)
        if row is None or row["disabled"]:
            return None
        if not security.verify_password(password, row["password_hash"]):
            return None
        return row

    async def update_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        email: str | None = None,
        role: str | None = None,
        avatar: str | None = None,
        disabled: bool | None = None,
    ) -> dict | None:
        """Patch mutable account fields (not password/TOTP — see dedicated methods)."""

        sets: list[str] = []
        args: list = []
        if username is not None:
            sets.append("username = ?")
            args.append(username.strip())
        if email is not None:
            sets.append("email = ?")
            args.append(email.strip() or None)
        if role is not None:
            if not security.is_valid_role(role):
                raise ValueError(f"invalid role {role!r}")
            sets.append("role = ?")
            args.append(role)
        if avatar is not None:
            sets.append("avatar = ?")
            args.append(avatar)
        if disabled is not None:
            sets.append("disabled = ?")
            args.append(1 if disabled else 0)
        if not sets:
            return await self.get_user(user_id)
        sets.append("updated_at = ?")
        args.append(_now_iso())
        args.append(user_id)
        try:
            await self._conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ?", args
            )
        except aiosqlite.IntegrityError as exc:
            raise UserExists(username or "") from exc
        await self._conn.commit()
        return await self.get_user(user_id)

    async def set_password(self, user_id: int, password: str) -> None:
        await self._conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (security.hash_password(password), _now_iso(), user_id),
        )
        await self._conn.commit()

    async def set_totp_secret(self, user_id: int, secret: str | None) -> None:
        """Enable (secret) or disable (``None``) TOTP for an account."""

        await self._conn.execute(
            "UPDATE users SET totp_secret = ?, updated_at = ? WHERE id = ?",
            (secret, _now_iso(), user_id),
        )
        await self._conn.commit()

    async def get_totp_secret(self, user_id: int) -> str | None:
        row = await self._get_row(user_id)
        return row["totp_secret"] if row else None

    async def set_capability_profile(self, user_id: int, profile: str | None) -> None:
        """Set (or clear, with ``None``) the account's capability profile.

        ``profile`` must be ``None`` or a name known to ``tool_classes.PROFILES``.
        """

        if profile is not None:
            try:
                # Import here, not at module load: tool_classes lands in a parallel
                # workstream and may not exist yet. Remove this try/except once it
                # has landed and hoist the import to the top of the module.
                from .tool_classes import PROFILES
            except ImportError:
                PROFILES = {}  # noqa: N806 - mirrors the imported name
            if PROFILES and profile not in PROFILES:
                raise ValueError(f"unknown capability profile {profile!r}")
            if not PROFILES and not profile:
                raise ValueError("capability profile must not be empty")
        await self._conn.execute(
            "UPDATE users SET capability_profile = ?, updated_at = ? WHERE id = ?",
            (profile, _now_iso(), user_id),
        )
        await self._conn.commit()

    async def set_theme(self, user_id: int, theme: str | None) -> None:
        """Persist (or clear, with ``None``) the account's console theme.

        Validated here rather than only at the API boundary, following
        :meth:`set_capability_profile`: a closed vocabulary that a second caller
        could otherwise write past. ``theme`` must be ``None`` or a member of
        :data:`THEMES`.

        One statement plus its commit, like every other setter in this store —
        no ``write_lock`` is involved, which ADR-0051 reserves for a write whose
        several statements must land together.
        """

        if theme is not None and theme not in THEMES:
            raise ValueError(f"unknown theme {theme!r}")
        await self._conn.execute(
            "UPDATE users SET theme = ?, updated_at = ? WHERE id = ?",
            (theme, _now_iso(), user_id),
        )
        await self._conn.commit()

    async def get_capability_profile(self, user_id: int) -> str | None:
        row = await self._get_row(user_id)
        return row["capability_profile"] if row else None

    async def count_superusers(self, *, exclude: int | None = None) -> int:
        """Enabled superusers, optionally excluding one id (last-admin guard)."""

        sql = "SELECT COUNT(*) AS n FROM users WHERE role = 'superuser' AND disabled = 0"
        args: list = []
        if exclude is not None:
            sql += " AND id != ?"
            args.append(exclude)
        async with self._conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return int(row["n"])

    async def delete_user(self, user_id: int) -> None:
        """Delete an account and its tokens, sessions, and host scope."""

        await self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await self._conn.execute(
            "DELETE FROM user_tokens WHERE user_id = ?", (user_id,)
        )
        await self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await self._conn.execute("DELETE FROM user_hosts WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    # -- host scope -----------------------------------------------------------

    async def get_user_hosts(self, user_id: int) -> set[str]:
        async with self._conn.execute(
            "SELECT agent_id FROM user_hosts WHERE user_id = ?", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
        return {r["agent_id"] for r in rows}

    async def set_user_hosts(self, user_id: int, agent_ids: list[str]) -> None:
        await self._conn.execute(
            "DELETE FROM user_hosts WHERE user_id = ?", (user_id,)
        )
        for agent_id in dict.fromkeys(agent_ids):  # dedupe, keep order
            await self._conn.execute(
                "INSERT OR IGNORE INTO user_hosts (user_id, agent_id) VALUES (?, ?)",
                (user_id, agent_id),
            )
        await self._conn.commit()

    async def purge_host(self, agent_id: str) -> None:
        """Drop scope rows for a host removed from inventory."""

        await self._conn.execute(
            "DELETE FROM user_hosts WHERE agent_id = ?", (agent_id,)
        )
        await self._conn.commit()

    # -- personal access tokens (PATs) ---------------------------------------

    async def create_pat(self, user_id: int, label: str | None = None) -> str:
        """Mint a PAT for ``user_id``; return the plaintext **once**."""

        token = security.generate_token()
        await self._conn.execute(
            "INSERT INTO user_tokens (user_id, token_sha256, label, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, security.sha256_hex(token), label, _now_iso()),
        )
        await self._conn.commit()
        return token

    async def list_pats(self, user_id: int) -> list[dict]:
        async with self._conn.execute(
            "SELECT id, label, created_at, last_used, revoked FROM user_tokens "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "label": r["label"],
                "created_at": r["created_at"],
                "last_used": r["last_used"],
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ]

    async def revoke_pat(self, user_id: int, pat_id: int) -> bool:
        cur = await self._conn.execute(
            "UPDATE user_tokens SET revoked = 1 WHERE id = ? AND user_id = ?",
            (pat_id, user_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def resolve_pat(self, token: str) -> aiosqlite.Row | None:
        """Resolve a raw PAT to its (enabled) account row; bumps ``last_used``."""

        if not token:
            return None
        digest = security.sha256_hex(token)
        async with self._conn.execute(
            "SELECT u.* FROM user_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_sha256 = ? AND t.revoked = 0 AND u.disabled = 0",
            (digest,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await self._conn.execute(
            "UPDATE user_tokens SET last_used = ? WHERE token_sha256 = ?",
            (_now_iso(), digest),
        )
        await self._conn.commit()
        return row

    # -- sessions -------------------------------------------------------------

    async def create_session(
        self,
        user_id: int,
        *,
        ttl_secs: int = _DEFAULT_SESSION_TTL_SECS,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """Create a session row; return the opaque id for the cookie."""

        sid = security.generate_token()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_secs)
        await self._conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, user_id, now.isoformat(), expires.isoformat(), ip, user_agent),
        )
        await self._conn.commit()
        return sid

    async def list_sessions(self, user_id: int) -> list[dict]:
        """All sessions for ``user_id``, newest first.

        Each dict's ``id`` is the raw session token — the same string the
        cookie carries, so it doubles as a bearer credential. Callers must
        never serialize it back to a client; it exists here only so a caller
        can compare it against the current request's session id (to flag
        "current") or use it to evict a per-session registry slot when the
        session is revoked.
        """

        async with self._conn.execute(
            "SELECT id, created_at, expires_at, ip, user_agent FROM sessions "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "ip": r["ip"],
                "user_agent": r["user_agent"],
            }
            for r in rows
        ]

    async def resolve_session(self, session_id: str) -> aiosqlite.Row | None:
        """Resolve a session id to its (enabled) account row, or ``None``.

        Expired sessions are deleted lazily on lookup.
        """

        if not session_id:
            return None
        async with self._conn.execute(
            "SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.id = ? AND u.disabled = 0",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        if datetime.now(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
            await self.delete_session(session_id)
            return None
        return row

    async def delete_session(self, session_id: str) -> None:
        await self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._conn.commit()

    async def delete_user_sessions(self, user_id: int) -> None:
        """Invalidate all of a user's sessions (e.g. after a password reset)."""

        await self._conn.execute(
            "DELETE FROM sessions WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

"""SQLite-backed OAuth 2.1 store: clients, authorization codes, and tokens.

Backs the co-hosted OAuth Authorization Server that lets Claude Desktop connect
to ``/mcp`` via an OAuth handshake instead of a pasted PAT (ADR-0041). Four
tables in the shared ``KENNY_DB_PATH`` database, following the same conventions
as :mod:`userstore` (own aiosqlite connection, ``CREATE TABLE IF NOT EXISTS`` at
connect, timestamps as ISO-8601 UTC, opaque tokens stored as sha256 digests):

* ``oauth_clients`` — dynamically registered clients (RFC 7591). Public PKCE
  clients only, so there is no secret column; ``redirect_uris`` is a JSON array
  matched exactly at ``/authorize``.
* ``oauth_auth_codes`` — short-lived, single-use authorization codes carrying the
  PKCE challenge, the bound ``redirect_uri``/``resource``, and the resolved user.
* ``oauth_access_tokens`` — opaque bearer tokens bound to a user and an audience
  (``resource``); resolved like a PAT but with expiry + audience checks.
* ``oauth_refresh_tokens`` — rotating refresh tokens. Each token belongs to a
  ``family_id``; presenting an already-rotated/revoked refresh token revokes the
  whole family (rotating-refresh theft response, OAuth 2.1 §4.3.1).

Every token binds to an existing account (``user_id``); the account's role/host
scope is resolved upstream so the same RBAC applies as for PATs and sessions.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aiosqlite

from . import security

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id                TEXT PRIMARY KEY,
    client_name              TEXT,
    redirect_uris            TEXT NOT NULL,
    token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
    grant_types              TEXT NOT NULL DEFAULT 'authorization_code,refresh_token',
    created_at               TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code_sha256    TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    user_id        INTEGER NOT NULL,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource       TEXT NOT NULL,
    scope          TEXT,
    expires_at     TEXT NOT NULL,
    consumed       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token_sha256 TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    resource     TEXT NOT NULL,
    scope        TEXT,
    family_id    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    last_used    TEXT
);
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token_sha256 TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    resource     TEXT NOT NULL,
    scope        TEXT,
    family_id    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    rotated_to   TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
"""

_DEFAULT_ACCESS_TTL_SECS = 3600  # 1 hour
_DEFAULT_REFRESH_TTL_SECS = 30 * 24 * 3600  # 30 days
_AUTH_CODE_TTL_SECS = 60  # single-use codes are redeemed immediately


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


class OAuthStore:
    """Async SQLite store for OAuth clients, auth codes, and tokens."""

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

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("OAuthStore is not connected; call connect() first")
        return self._db

    # -- clients (RFC 7591 dynamic client registration) ----------------------

    async def register_client(
        self,
        redirect_uris: list[str],
        *,
        client_name: str | None = None,
        token_endpoint_auth_method: str = "none",
        grant_types: list[str] | None = None,
    ) -> dict:
        """Register a public client; returns the RFC 7591 client-info fields."""

        if not redirect_uris:
            raise ValueError("redirect_uris must not be empty")
        client_id = security.generate_token()
        grants = grant_types or ["authorization_code", "refresh_token"]
        now = _now_iso()
        await self._conn.execute(
            "INSERT INTO oauth_clients "
            "(client_id, client_name, redirect_uris, token_endpoint_auth_method, "
            " grant_types, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                client_id,
                client_name,
                json.dumps(list(redirect_uris)),
                token_endpoint_auth_method,
                ",".join(grants),
                now,
            ),
        )
        await self._conn.commit()
        return {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "grant_types": grants,
            "response_types": ["code"],
            "created_at": now,
        }

    async def get_client(self, client_id: str) -> aiosqlite.Row | None:
        if not client_id:
            return None
        async with self._conn.execute(
            "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
        ) as cur:
            return await cur.fetchone()

    @staticmethod
    def client_redirect_uris(row: aiosqlite.Row) -> list[str]:
        """Decode the stored ``redirect_uris`` JSON array for a client row."""

        try:
            uris = json.loads(row["redirect_uris"])
        except (ValueError, TypeError):
            return []
        return [str(u) for u in uris] if isinstance(uris, list) else []

    # -- authorization codes --------------------------------------------------

    async def create_auth_code(
        self,
        *,
        client_id: str,
        user_id: int,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
        scope: str | None = None,
        ttl_secs: int = _AUTH_CODE_TTL_SECS,
    ) -> str:
        """Mint a single-use authorization code; return the plaintext once."""

        code = security.generate_token()
        now = _now()
        expires = now + timedelta(seconds=ttl_secs)
        await self._conn.execute(
            "INSERT INTO oauth_auth_codes "
            "(code_sha256, client_id, user_id, redirect_uri, code_challenge, "
            " resource, scope, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                security.sha256_hex(code),
                client_id,
                user_id,
                redirect_uri,
                code_challenge,
                resource,
                scope,
                expires.isoformat(),
                now.isoformat(),
            ),
        )
        await self._conn.commit()
        return code

    async def consume_auth_code(self, code: str) -> aiosqlite.Row | None:
        """Atomically redeem an auth code exactly once.

        Returns the code row if it was unconsumed and unexpired, marking it
        consumed in the same step; returns ``None`` otherwise. A replayed code
        (already consumed) yields ``None`` so the token endpoint can reject it.
        """

        if not code:
            return None
        digest = security.sha256_hex(code)
        # Single-statement guard: only the first caller flips consumed 0->1.
        cur = await self._conn.execute(
            "UPDATE oauth_auth_codes SET consumed = 1 "
            "WHERE code_sha256 = ? AND consumed = 0 AND expires_at > ?",
            (digest, _now_iso()),
        )
        await self._conn.commit()
        if cur.rowcount == 0:
            return None
        async with self._conn.execute(
            "SELECT * FROM oauth_auth_codes WHERE code_sha256 = ?", (digest,)
        ) as sel:
            return await sel.fetchone()

    # -- tokens ---------------------------------------------------------------

    async def issue_token_pair(
        self,
        *,
        client_id: str,
        user_id: int,
        resource: str,
        scope: str | None = None,
        family_id: str | None = None,
        access_ttl_secs: int = _DEFAULT_ACCESS_TTL_SECS,
        refresh_ttl_secs: int = _DEFAULT_REFRESH_TTL_SECS,
    ) -> tuple[str, str, str]:
        """Mint an access + refresh token pair; return ``(access, refresh, family_id)``.

        Both tokens share a ``family_id`` (new when omitted) so a refresh-reuse can
        revoke the entire lineage.
        """

        fam = family_id or security.generate_token()
        access = security.generate_token()
        refresh = security.generate_token()
        now = _now()
        access_exp = (now + timedelta(seconds=access_ttl_secs)).isoformat()
        refresh_exp = (now + timedelta(seconds=refresh_ttl_secs)).isoformat()
        now_iso = now.isoformat()
        await self._conn.execute(
            "INSERT INTO oauth_access_tokens "
            "(token_sha256, client_id, user_id, resource, scope, family_id, "
            " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                security.sha256_hex(access),
                client_id,
                user_id,
                resource,
                scope,
                fam,
                access_exp,
                now_iso,
            ),
        )
        await self._conn.execute(
            "INSERT INTO oauth_refresh_tokens "
            "(token_sha256, client_id, user_id, resource, scope, family_id, "
            " expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                security.sha256_hex(refresh),
                client_id,
                user_id,
                resource,
                scope,
                fam,
                refresh_exp,
                now_iso,
            ),
        )
        await self._conn.commit()
        return access, refresh, fam

    async def resolve_access_token(self, token: str) -> aiosqlite.Row | None:
        """Resolve a raw access token to its row if valid; bumps ``last_used``.

        Valid means: known, not revoked, and unexpired. The caller additionally
        checks the ``resource`` (audience) before trusting the row.
        """

        if not token:
            return None
        digest = security.sha256_hex(token)
        async with self._conn.execute(
            "SELECT * FROM oauth_access_tokens WHERE token_sha256 = ?", (digest,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["revoked"]:
            return None
        if _now() >= datetime.fromisoformat(row["expires_at"]):
            return None
        await self._conn.execute(
            "UPDATE oauth_access_tokens SET last_used = ? WHERE token_sha256 = ?",
            (_now_iso(), digest),
        )
        await self._conn.commit()
        return row

    async def rotate_refresh_token(
        self,
        token: str,
        *,
        access_ttl_secs: int = _DEFAULT_ACCESS_TTL_SECS,
        refresh_ttl_secs: int = _DEFAULT_REFRESH_TTL_SECS,
    ) -> tuple[aiosqlite.Row, str, str] | None:
        """Rotate a refresh token, returning ``(old_row, access, refresh)`` or ``None``.

        Reuse detection: presenting a refresh token that is already rotated or
        revoked revokes the whole ``family_id`` (all access + refresh tokens) and
        returns ``None``. An unknown or expired token also returns ``None``.
        """

        if not token:
            return None
        digest = security.sha256_hex(token)
        async with self._conn.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token_sha256 = ?", (digest,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        # Theft response: a rotated-away or revoked token being replayed kills the
        # entire family so a leaked token cannot outlive its rotation.
        if row["revoked"] or row["rotated_to"] is not None:
            await self.revoke_family(row["family_id"])
            return None
        if _now() >= datetime.fromisoformat(row["expires_at"]):
            return None
        access, refresh, _fam = await self.issue_token_pair(
            client_id=row["client_id"],
            user_id=row["user_id"],
            resource=row["resource"],
            scope=row["scope"],
            family_id=row["family_id"],
            access_ttl_secs=access_ttl_secs,
            refresh_ttl_secs=refresh_ttl_secs,
        )
        await self._conn.execute(
            "UPDATE oauth_refresh_tokens SET rotated_to = ?, revoked = 1 "
            "WHERE token_sha256 = ?",
            (security.sha256_hex(refresh), digest),
        )
        await self._conn.commit()
        return row, access, refresh

    async def revoke_access_token(self, token: str) -> bool:
        """Revoke a single access token by its plaintext value."""

        if not token:
            return False
        cur = await self._conn.execute(
            "UPDATE oauth_access_tokens SET revoked = 1 WHERE token_sha256 = ?",
            (security.sha256_hex(token),),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def revoke_refresh_token(self, token: str) -> bool:
        """Revoke a single refresh token (and its family) by plaintext value."""

        if not token:
            return False
        digest = security.sha256_hex(token)
        async with self._conn.execute(
            "SELECT family_id FROM oauth_refresh_tokens WHERE token_sha256 = ?",
            (digest,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        await self.revoke_family(row["family_id"])
        return True

    async def revoke_token(self, token: str) -> bool:
        """Revoke a token given as either an access or a refresh token (RFC 7009).

        Revocation endpoints are not told which type a token is, so try both.
        """

        revoked = await self.revoke_access_token(token)
        revoked = await self.revoke_refresh_token(token) or revoked
        return revoked

    async def revoke_family(self, family_id: str) -> None:
        """Revoke every access and refresh token in a family."""

        await self._conn.execute(
            "UPDATE oauth_access_tokens SET revoked = 1 WHERE family_id = ?",
            (family_id,),
        )
        await self._conn.execute(
            "UPDATE oauth_refresh_tokens SET revoked = 1 WHERE family_id = ?",
            (family_id,),
        )
        await self._conn.commit()

    async def revoke_for_user(self, user_id: int) -> None:
        """Revoke all of a user's OAuth tokens (account disable/delete/pw reset)."""

        await self._conn.execute(
            "UPDATE oauth_access_tokens SET revoked = 1 WHERE user_id = ?",
            (user_id,),
        )
        await self._conn.execute(
            "UPDATE oauth_refresh_tokens SET revoked = 1 WHERE user_id = ?",
            (user_id,),
        )
        await self._conn.commit()

    async def prune_expired(self) -> int:
        """Delete expired auth codes and expired tokens; return the count.

        Only *expired* rows are removed. Revoked-but-unexpired refresh tokens are
        kept deliberately so a rotated token that is later replayed is still found
        and triggers the family-revoke theft response instead of looking unknown.
        """

        now = _now_iso()
        total = 0
        for table in ("oauth_auth_codes", "oauth_access_tokens", "oauth_refresh_tokens"):
            cur = await self._conn.execute(
                f"DELETE FROM {table} WHERE expires_at <= ?", (now,)
            )
            total += cur.rowcount
        await self._conn.commit()
        return total

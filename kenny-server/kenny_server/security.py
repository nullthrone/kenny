"""Password hashing, TOTP, and role ranking primitives for multi-user auth.

Deliberately dependency-free beyond ``cryptography`` (already a project dep):

* **Passwords** are hashed with scrypt (``cryptography``'s ``Scrypt`` KDF) and
  stored as a single self-describing string ``scrypt$n$r$p$salt$hash`` (salt and
  hash base64). Verification recomputes and compares constant-time.
* **TOTP** is a small RFC-6238 (HMAC-SHA1, 6 digits, 30 s step) implementation on
  top of ``hmac``/``hashlib``/``struct`` — no ``pyotp``. It is validated against
  the RFC test vectors in the tests.
* **Roles** are ranked ``superuser > operator > user`` for ``>=`` style checks.

Keeping these here (separate from ``auth.py``) lets them be unit-tested in
isolation and reused by the user store and the dashboard routes. See ADR-0037.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# -- roles --------------------------------------------------------------------

#: Roles in ascending privilege order; index is the rank.
ROLES: tuple[str, ...] = ("user", "operator", "superuser")


def role_rank(role: str) -> int:
    """Numeric privilege rank; unknown roles rank below every real role (-1)."""

    try:
        return ROLES.index(role)
    except ValueError:
        return -1


def role_at_least(role: str, minimum: str) -> bool:
    """True iff ``role`` is at least as privileged as ``minimum``."""

    return role_rank(role) >= role_rank(minimum)


def is_valid_role(role: str) -> bool:
    return role in ROLES


# -- passwords (scrypt) -------------------------------------------------------

# Cost parameters. n must be a power of two; these bound memory to a few MB and
# keep a single verify well under ~100 ms on a family-scale host.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32
_SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _scrypt(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=_SCRYPT_LEN, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def hash_password(password: str) -> str:
    """Return a self-describing ``scrypt$n$r$p$salt$hash`` string.

    A fresh random salt is generated per call, so identical passwords hash to
    different strings.
    """

    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a stored ``encoded`` hash.

    Returns ``False`` (never raises) on any malformed hash so a corrupt row can
    never authenticate.
    """

    if not password or not encoded:
        return False
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _unb64(salt_s)
        expected = _unb64(hash_s)
        candidate = _scrypt(password, salt, n=n, r=r, p=p)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


# -- TOTP (RFC 6238 / RFC 4226) ----------------------------------------------

_TOTP_STEP = 30
_TOTP_DIGITS = 6
_TOTP_SECRET_BYTES = 20  # 160-bit, RFC-recommended for HMAC-SHA1


def generate_totp_secret() -> str:
    """A fresh base32 (unpadded, uppercase) TOTP secret for authenticator apps."""

    raw = secrets.token_bytes(_TOTP_SECRET_BYTES)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _b32decode(secret: str) -> bytes:
    """Decode a possibly-unpadded, mixed-case base32 secret."""

    cleaned = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned + padding)


def _hotp(key: bytes, counter: int, digits: int) -> str:
    """RFC 4226 HOTP value for ``counter`` (the primitive under TOTP)."""

    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def totp_at(
    secret: str, timestamp: float, *, step: int = _TOTP_STEP, digits: int = _TOTP_DIGITS
) -> str:
    """The TOTP code for ``secret`` at ``timestamp`` (seconds since epoch)."""

    counter = int(timestamp // step)
    return _hotp(_b32decode(secret), counter, digits)


def verify_totp(
    secret: str,
    code: str,
    *,
    timestamp: float | None = None,
    window: int = 1,
    step: int = _TOTP_STEP,
    digits: int = _TOTP_DIGITS,
) -> bool:
    """Constant-time TOTP check allowing ±``window`` steps for clock drift."""

    if not secret or not code:
        return False
    code = code.strip()
    if not code.isdigit() or len(code) != digits:
        return False
    now = time.time() if timestamp is None else timestamp
    try:
        key = _b32decode(secret)
    except (ValueError, TypeError):
        return False
    counter = int(now // step)
    matched = False
    for delta in range(-window, window + 1):
        expected = _hotp(key, counter + delta, digits)
        if hmac.compare_digest(expected, code):
            matched = True
    return matched


def totp_uri(secret: str, username: str, *, issuer: str = "kenny") -> str:
    """``otpauth://`` provisioning URI for authenticator enrollment / QR."""

    label = urllib.parse.quote(f"{issuer}:{username}", safe=":")
    params = urllib.parse.urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": _TOTP_DIGITS,
            "period": _TOTP_STEP,
        }
    )
    return f"otpauth://totp/{label}?{params}"


# -- opaque tokens ------------------------------------------------------------


def generate_token() -> str:
    """A URL-safe random token (personal access tokens, session ids)."""

    return secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    """Hex sha256 of ``value`` (PAT hashing at rest, mirrors tokenstore)."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()

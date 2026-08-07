"""Password hashing, TOTP, and role primitives (ADR-0033).

The TOTP/HOTP paths are checked against the published RFC 4226 / RFC 6238 test
vectors so a regression in the hand-rolled implementation is caught immediately.
"""

from __future__ import annotations

import base64

from kenny_server import security


def test_hotp_rfc4226_vectors() -> None:
    # RFC 4226 Appendix D, ASCII secret "12345678901234567890", 6 digits.
    key = b"12345678901234567890"
    expected = [
        "755224", "287082", "359152", "969429", "338314",
        "254676", "287922", "162583", "399871", "520489",
    ]
    assert [security._hotp(key, i, 6) for i in range(10)] == expected


def test_totp_rfc6238_vectors() -> None:
    # RFC 6238 (SHA-1, 8 digits), same seed encoded as base32 for our API.
    b32 = base64.b32encode(b"12345678901234567890").decode()
    cases = [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
    ]
    for ts, code in cases:
        assert security.totp_at(b32, ts, digits=8) == code


def test_verify_totp_window_and_reject() -> None:
    secret = security.generate_totp_secret()
    now = 1_700_000_000.0
    assert security.verify_totp(secret, security.totp_at(secret, now), timestamp=now)
    # One step of drift in either direction is tolerated (window=1).
    assert security.verify_totp(secret, security.totp_at(secret, now - 30), timestamp=now)
    assert security.verify_totp(secret, security.totp_at(secret, now + 30), timestamp=now)
    # Two steps away is rejected.
    assert not security.verify_totp(secret, security.totp_at(secret, now - 90), timestamp=now)
    assert not security.verify_totp(secret, "000000", timestamp=now)
    assert not security.verify_totp(secret, "", timestamp=now)


def test_password_hash_roundtrip_and_salting() -> None:
    h1 = security.hash_password("correct horse")
    h2 = security.hash_password("correct horse")
    assert h1 != h2  # per-call random salt
    assert h1.startswith("scrypt$")
    assert security.verify_password("correct horse", h1)
    assert not security.verify_password("wrong", h1)
    # A malformed/corrupt hash never authenticates and never raises.
    assert not security.verify_password("anything", "garbage")
    assert not security.verify_password("", h1)


def test_role_ranking() -> None:
    assert security.role_at_least("superuser", "user")
    assert security.role_at_least("operator", "operator")
    assert not security.role_at_least("user", "operator")
    assert security.role_rank("nonsense") == -1
    assert security.is_valid_role("operator")
    assert not security.is_valid_role("root")


def test_totp_uri_shape() -> None:
    uri = security.totp_uri("ABC234", "alice", issuer="kenny")
    assert uri.startswith("otpauth://totp/kenny:alice?")
    assert "secret=ABC234" in uri
    assert "issuer=kenny" in uri

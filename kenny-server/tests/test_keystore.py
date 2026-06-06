"""Per-agent public-key store: enroll, verify, rotation grace, server identity."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kenny_server.keystore import KeyStore, build_transcript

VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "vectors" / "mutual_auth.json").read_text()
)


def _agent_signer() -> tuple[str, Ed25519PrivateKey]:
    """Return (public_key_b64, private_key) for the golden agent keypair."""

    seed = base64.b64decode(VECTORS["agent_seed_b64"])
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    return VECTORS["agent_public_key_b64"], sk


def _sign(sk: Ed25519PrivateKey, transcript: bytes) -> str:
    return base64.b64encode(sk.sign(transcript)).decode()


def _new_pub() -> str:
    """A fresh, unrelated public key (base64)."""

    pub = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(pub).decode()


async def _store(tmp_path, monkeypatch) -> KeyStore:
    # Pin the server seed so server identity is deterministic in tests.
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", VECTORS["server_seed_b64"])
    store = KeyStore(str(tmp_path / "keys.sqlite"))
    await store.connect()
    return store


@pytest.mark.asyncio
async def test_enroll_and_verify_good_signature(tmp_path, monkeypatch) -> None:
    store = await _store(tmp_path, monkeypatch)
    try:
        pub, sk = _agent_signer()
        await store.enroll("example-pc", pub)
        transcript = build_transcript(
            "example-pc",
            base64.b64decode(VECTORS["client_nonce_b64"]),
            base64.b64decode(VECTORS["server_nonce_b64"]),
        )
        # This is the byte-exact golden transcript; reuse the golden signature.
        assert transcript.hex() == VECTORS["transcript_hex"]
        assert await store.verify_signature("example-pc", transcript, VECTORS["agent_sig_b64"]) is True
        # A freshly computed signature over the same transcript also verifies.
        assert await store.verify_signature("example-pc", transcript, _sign(sk, transcript)) is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_verify_bad_signature(tmp_path, monkeypatch) -> None:
    store = await _store(tmp_path, monkeypatch)
    try:
        pub, _ = _agent_signer()
        await store.enroll("example-pc", pub)
        transcript = build_transcript("example-pc", b"\x01" * 32, b"\x02" * 32)
        # A signature made by a different key must not verify.
        other = Ed25519PrivateKey.generate()
        assert await store.verify_signature("example-pc", transcript, _sign(other, transcript)) is False
        # Garbage / empty signatures return False, never raise.
        assert await store.verify_signature("example-pc", transcript, "not-base64!!") is False
        assert await store.verify_signature("example-pc", transcript, "") is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unknown_agent_returns_false(tmp_path, monkeypatch) -> None:
    store = await _store(tmp_path, monkeypatch)
    try:
        _, sk = _agent_signer()
        transcript = build_transcript("ghost", b"\x00" * 32, b"\x00" * 32)
        assert await store.verify_signature("ghost", transcript, _sign(sk, transcript)) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reenroll_refused(tmp_path, monkeypatch) -> None:
    store = await _store(tmp_path, monkeypatch)
    try:
        pub, _ = _agent_signer()
        await store.enroll("example-pc", pub)
        with pytest.raises(ValueError, match="already enrolled"):
            await store.enroll("example-pc", _new_pub())
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_enroll_rejects_bad_key(tmp_path, monkeypatch) -> None:
    store = await _store(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="invalid Ed25519 public key"):
            await store.enroll("bad", "not-a-key")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rotation_grace_window_keeps_previous_key(tmp_path, monkeypatch) -> None:
    store = await _store(tmp_path, monkeypatch)
    try:
        old_sk = Ed25519PrivateKey.generate()
        old_pub = base64.b64encode(
            old_sk.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
        await store.enroll("agent-x", old_pub)

        new_pub, new_sk = _agent_signer()
        await store.rotate("agent-x", new_pub)

        t = build_transcript("agent-x", b"\x03" * 32, b"\x04" * 32)
        # The previous key still verifies during the grace window...
        assert await store.verify_signature("agent-x", t, _sign(old_sk, t)) is True
        # ...and the new key verifies, which retires the grace key.
        assert await store.verify_signature("agent-x", t, _sign(new_sk, t)) is True
        # After the new key is first seen, the old key no longer verifies.
        assert await store.verify_signature("agent-x", t, _sign(old_sk, t)) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rotation_grace_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_KEY_GRACE_SECS", "0")
    store = await _store(tmp_path, monkeypatch)
    try:
        old_sk = Ed25519PrivateKey.generate()
        old_pub = base64.b64encode(
            old_sk.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
        await store.enroll("agent-z", old_pub)
        await store.rotate("agent-z", _new_pub())
        t = build_transcript("agent-z", b"\x05" * 32, b"\x06" * 32)
        assert await store.verify_signature("agent-z", t, _sign(old_sk, t)) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_server_identity_from_env_matches_vectors(tmp_path, monkeypatch) -> None:
    store = await _store(tmp_path, monkeypatch)
    try:
        assert store.server_public_key_b64() == VECTORS["server_public_key_b64"]
        transcript = bytes.fromhex(VECTORS["transcript_hex"])
        assert store.sign_transcript(transcript) == VECTORS["server_sig_b64"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_server_identity_generated_and_persisted(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KENNY_SERVER_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("KENNY_SERVER_PRIVATE_KEY_FILE", raising=False)
    path = str(tmp_path / "gen.sqlite")
    store = KeyStore(path)
    await store.connect()
    pub1 = store.server_public_key_b64()
    await store.close()

    store2 = KeyStore(path)
    await store2.connect()
    try:
        # The generated identity is persisted and reloaded unchanged.
        assert store2.server_public_key_b64() == pub1
    finally:
        await store2.close()

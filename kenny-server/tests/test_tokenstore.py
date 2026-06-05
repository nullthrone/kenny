"""Per-agent token store: hashed-at-rest tokens, rotation, and dev seeding."""

from __future__ import annotations

import pytest

from kenny_server.tokenstore import AgentTokenStore


async def _store(tmp_path) -> AgentTokenStore:
    store = AgentTokenStore(str(tmp_path / "tokens.sqlite"))
    await store.connect()
    return store


@pytest.mark.asyncio
async def test_seeds_dev_tokens(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        assert await store.verify("dev", "dev-token") is True
        assert await store.verify("papa-pc", "dev-token-papa") is True
        assert await store.verify("dev", "wrong") is False
        assert await store.verify("unknown", "whatever") is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_seeds_env_tokens(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_AGENT_TOKENS", "lab=lab-secret")
    store = await _store(tmp_path)
    try:
        assert await store.verify("lab", "lab-secret") is True
        assert await store.verify("lab", "nope") is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_or_rotate_returns_working_token(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        token = await store.create_or_rotate("new-agent")
        assert token
        assert await store.verify("new-agent", token) is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rotation_grace_window_keeps_old_token(tmp_path) -> None:
    """The old token survives a rotation (grace window) until the new one is used."""

    store = await _store(tmp_path)
    try:
        first = await store.create_or_rotate("agent-x")
        assert await store.verify("agent-x", first) is True

        second = await store.create_or_rotate("agent-x")
        assert second != first
        # The old token still verifies during the grace window...
        assert await store.verify("agent-x", first) is True
        # ...until the new token is first seen, which retires the old one.
        assert await store.verify("agent-x", second) is True
        assert await store.verify("agent-x", first) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_grace_window_can_be_disabled(tmp_path, monkeypatch) -> None:
    """KENNY_TOKEN_GRACE_SECS=0 restores instant invalidation of the old token."""

    monkeypatch.setenv("KENNY_TOKEN_GRACE_SECS", "0")
    store = await _store(tmp_path)
    try:
        first = await store.create_or_rotate("agent-z")
        second = await store.create_or_rotate("agent-z")
        assert await store.verify("agent-z", second) is True
        # No grace: the old token is rejected immediately after rotation.
        assert await store.verify("agent-z", first) is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_token_stored_hashed_not_plaintext(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        token = await store.create_or_rotate("hashed-agent")
        rows = await store._conn.execute_fetchall(
            "SELECT token_sha256 FROM agent_tokens WHERE agent_id = ?",
            ("hashed-agent",),
        )
        stored = rows[0][0]
        assert token not in stored
        assert len(stored) == 64  # sha256 hex digest
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_agents(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        await store.create_or_rotate("z-agent")
        agents = {a["agent_id"] for a in await store.list_agents()}
        assert {"dev", "papa-pc", "z-agent"} <= agents
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_seed_does_not_clobber_rotated_token(tmp_path, monkeypatch) -> None:
    """A rotated dev token survives a reconnect (seeding uses INSERT OR IGNORE)."""

    # Disable the grace window so the original seed token is retired immediately.
    monkeypatch.setenv("KENNY_TOKEN_GRACE_SECS", "0")
    path = str(tmp_path / "persist.sqlite")
    store = AgentTokenStore(path)
    await store.connect()
    rotated = await store.create_or_rotate("dev")
    await store.close()

    store2 = AgentTokenStore(path)
    await store2.connect()
    try:
        assert await store2.verify("dev", rotated) is True
        # The original seed token no longer works.
        assert await store2.verify("dev", "dev-token") is False
    finally:
        await store2.close()

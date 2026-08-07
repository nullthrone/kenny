"""UserStore: accounts, PATs, sessions, and host scope (ADR-0033)."""

from __future__ import annotations

import pytest

from kenny_server import security
from kenny_server.userstore import UserExists, UserStore


@pytest.fixture()
async def store(tmp_path):
    us = UserStore(str(tmp_path / "users.sqlite"))
    await us.connect()
    yield us
    await us.close()


@pytest.mark.asyncio
async def test_create_and_login(store) -> None:
    assert await store.count_users() == 0
    user = await store.create_user("thomas", "pw-123456", "superuser", email="t@x.de")
    assert user["role"] == "superuser"
    assert user["totp_enabled"] is False
    assert await store.count_users() == 1
    assert await store.verify_login("thomas", "pw-123456") is not None
    assert await store.verify_login("thomas", "nope") is None
    with pytest.raises(UserExists):
        await store.create_user("thomas", "x", "user")


@pytest.mark.asyncio
async def test_disabled_user_cannot_login(store) -> None:
    u = await store.create_user("kid", "pw-123456", "user")
    await store.update_user(u["id"], disabled=True)
    assert await store.verify_login("kid", "pw-123456") is None


@pytest.mark.asyncio
async def test_pats(store) -> None:
    u = await store.create_user("op", "pw-123456", "operator")
    token = await store.create_pat(u["id"], "claude")
    row = await store.resolve_pat(token)
    assert row is not None and row["username"] == "op"
    pats = await store.list_pats(u["id"])
    assert pats[0]["last_used"] is not None  # resolve bumped it
    assert await store.revoke_pat(u["id"], pats[0]["id"]) is True
    assert await store.resolve_pat(token) is None  # revoked


@pytest.mark.asyncio
async def test_sessions_expire(store) -> None:
    u = await store.create_user("op", "pw-123456", "operator")
    sid = await store.create_session(u["id"], ttl_secs=60)
    assert (await store.resolve_session(sid))["username"] == "op"
    # An already-expired session resolves to None and is pruned.
    expired = await store.create_session(u["id"], ttl_secs=-1)
    assert await store.resolve_session(expired) is None
    await store.delete_session(sid)
    assert await store.resolve_session(sid) is None


@pytest.mark.asyncio
async def test_host_scope(store) -> None:
    u = await store.create_user("kid", "pw-123456", "user")
    await store.set_user_hosts(u["id"], ["PC-A", "PC-B", "PC-A"])
    assert await store.get_user_hosts(u["id"]) == {"PC-A", "PC-B"}
    await store.purge_host("PC-A")
    assert await store.get_user_hosts(u["id"]) == {"PC-B"}


@pytest.mark.asyncio
async def test_totp_and_superuser_count(store) -> None:
    su = await store.create_user("admin", "pw-123456", "superuser")
    secret = security.generate_totp_secret()
    await store.set_totp_secret(su["id"], secret)
    assert await store.get_totp_secret(su["id"]) == secret
    assert (await store.get_user(su["id"]))["totp_enabled"] is True
    assert await store.count_superusers() == 1
    assert await store.count_superusers(exclude=su["id"]) == 0


@pytest.mark.asyncio
async def test_list_directory_minimal_projection(store) -> None:
    su = await store.create_user("admin", "pw-123456", "superuser", email="a@x.de")
    op = await store.create_user("op", "pw-123456", "operator", avatar="dog-pug")
    directory = await store.list_directory()
    # Ordered by username, like list_users(): "admin" sorts before "op".
    assert directory == [
        {"id": su["id"], "username": "admin", "role": "superuser"},
        {"id": op["id"], "username": "op", "role": "operator"},
    ]
    # No auth-sensitive fields leak through this projection.
    for entry in directory:
        assert set(entry) == {"id", "username", "role"}


@pytest.mark.asyncio
async def test_delete_cascades(store) -> None:
    u = await store.create_user("kid", "pw-123456", "user")
    await store.set_user_hosts(u["id"], ["PC-A"])
    token = await store.create_pat(u["id"])
    sid = await store.create_session(u["id"])
    await store.delete_user(u["id"])
    assert await store.get_user(u["id"]) is None
    assert await store.resolve_pat(token) is None
    assert await store.resolve_session(sid) is None
    assert await store.get_user_hosts(u["id"]) == set()

"""OAuth store unit tests: codes single-use, token expiry/audience, rotation."""

from __future__ import annotations

import pytest

from kenny_server.oauthstore import OAuthStore


async def _store(tmp_path) -> OAuthStore:
    store = OAuthStore(str(tmp_path / "oauth.sqlite"))
    await store.connect()
    return store


@pytest.mark.asyncio
async def test_register_and_get_client(tmp_path) -> None:
    store = await _store(tmp_path)
    client = await store.register_client(
        ["https://app.example/cb"], client_name="Claude"
    )
    assert client["client_id"]
    row = await store.get_client(client["client_id"])
    assert row is not None
    assert OAuthStore.client_redirect_uris(row) == ["https://app.example/cb"]
    await store.close()


@pytest.mark.asyncio
async def test_auth_code_is_single_use(tmp_path) -> None:
    store = await _store(tmp_path)
    code = await store.create_auth_code(
        client_id="c1",
        user_id=1,
        redirect_uri="https://app/cb",
        code_challenge="chal",
        resource="https://k/mcp",
    )
    first = await store.consume_auth_code(code)
    assert first is not None and first["user_id"] == 1
    # A replay must not resolve again.
    assert await store.consume_auth_code(code) is None
    await store.close()


@pytest.mark.asyncio
async def test_expired_auth_code_rejected(tmp_path) -> None:
    store = await _store(tmp_path)
    code = await store.create_auth_code(
        client_id="c1",
        user_id=1,
        redirect_uri="https://app/cb",
        code_challenge="chal",
        resource="https://k/mcp",
        ttl_secs=-1,  # already expired
    )
    assert await store.consume_auth_code(code) is None
    await store.close()


@pytest.mark.asyncio
async def test_access_token_resolve_expiry_and_revocation(tmp_path) -> None:
    store = await _store(tmp_path)
    access, _refresh, _fam = await store.issue_token_pair(
        client_id="c1", user_id=7, resource="https://k/mcp", scope="kenny:mcp"
    )
    row = await store.resolve_access_token(access)
    assert row is not None and row["user_id"] == 7 and row["resource"] == "https://k/mcp"

    # Expired access token does not resolve.
    expired, _r, _f = await store.issue_token_pair(
        client_id="c1", user_id=7, resource="https://k/mcp", access_ttl_secs=-1
    )
    assert await store.resolve_access_token(expired) is None

    # Revoked access token does not resolve.
    await store.revoke_access_token(access)
    assert await store.resolve_access_token(access) is None
    await store.close()


@pytest.mark.asyncio
async def test_refresh_rotation_and_reuse_revokes_family(tmp_path) -> None:
    store = await _store(tmp_path)
    access, refresh, fam = await store.issue_token_pair(
        client_id="c1", user_id=3, resource="https://k/mcp"
    )
    result = await store.rotate_refresh_token(refresh)
    assert result is not None
    _old, new_access, new_refresh = result
    # Old refresh is now invalid; new one works.
    assert await store.resolve_access_token(new_access) is not None

    # Reusing the rotated-away refresh triggers the family-revoke theft response.
    assert await store.rotate_refresh_token(refresh) is None
    # The whole family is dead now: the freshly-minted refresh no longer rotates,
    # and the first access token is revoked too.
    assert await store.rotate_refresh_token(new_refresh) is None
    assert await store.resolve_access_token(access) is None
    assert await store.resolve_access_token(new_access) is None
    await store.close()


@pytest.mark.asyncio
async def test_revoke_for_user_and_prune(tmp_path) -> None:
    store = await _store(tmp_path)
    a1, _r1, _f1 = await store.issue_token_pair(
        client_id="c1", user_id=5, resource="https://k/mcp"
    )
    a2, _r2, _f2 = await store.issue_token_pair(
        client_id="c1", user_id=6, resource="https://k/mcp"
    )
    await store.revoke_for_user(5)
    assert await store.resolve_access_token(a1) is None
    assert await store.resolve_access_token(a2) is not None

    # Prune removes only expired rows.
    expired, _r, _f = await store.issue_token_pair(
        client_id="c1", user_id=6, resource="https://k/mcp", access_ttl_secs=-1
    )
    removed = await store.prune_expired()
    assert removed >= 1
    await store.close()


@pytest.mark.asyncio
async def test_revoke_token_handles_access_or_refresh(tmp_path) -> None:
    store = await _store(tmp_path)
    access, refresh, _fam = await store.issue_token_pair(
        client_id="c1", user_id=9, resource="https://k/mcp"
    )
    # RFC 7009: the endpoint isn't told the token type; revoke_token tries both.
    assert await store.revoke_token(refresh) is True
    # Revoking the refresh kills the family, so the access token is gone too.
    assert await store.resolve_access_token(access) is None
    await store.close()

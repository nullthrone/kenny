"""Operator authentication: the MCP endpoint, dashboard API, and UI are gated;
the agent WebSocket path is not (it has its own per-agent token)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kenny_server.main import build_app


def _app(tmp_path):
    return build_app(db_path=str(tmp_path / "auth.sqlite"))


def test_api_requires_operator_token(tmp_path) -> None:
    app = _app(tmp_path)
    token = app.state.operator_token
    with TestClient(app) as c:
        assert c.get("/api/fleet").status_code == 401
        ok = c.get("/api/fleet", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert "agents" in ok.json()


def test_bad_bearer_rejected(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/fleet", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


def _create_first_user(c, username="admin", password="pw-123456", **data):
    """Bootstrap the first (superuser) account via the first-run setup flow."""

    return c.post(
        "/setup",
        data={"username": username, "password": password, **data},
        follow_redirects=False,
    )


def test_ui_redirects_to_login_then_setup_then_works(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        # Unauthenticated UI request redirects to the login page.
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"
        # With no accounts yet, /login sends the browser to first-run setup.
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/setup"
        assert c.get("/setup").status_code == 200
        # Creating the first account signs in (cookie) and reaches the API.
        r = _create_first_user(c)
        assert r.status_code == 303
        assert c.get("/api/fleet").status_code == 200
        # Setup is closed once an account exists.
        r = _create_first_user(c, username="second")
        assert r.status_code == 409


def test_login_with_username_password(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _create_first_user(c)
        c.get("/logout")
        c.cookies.clear()
        assert c.get("/api/fleet", follow_redirects=False).status_code == 401
        r = c.post(
            "/login",
            data={"username": "admin", "password": "pw-123456"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert c.get("/api/fleet").status_code == 200


def test_login_rejects_wrong_credentials(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _create_first_user(c)
        c.get("/logout")
        c.cookies.clear()
        r = c.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 401
        # Still locked out.
        assert c.get("/api/fleet", follow_redirects=False).status_code == 401


def test_login_rate_limited_after_repeated_failures(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_LOGIN_MAX_ATTEMPTS", "3")
    app = _app(tmp_path)
    with TestClient(app) as c:
        _create_first_user(c)
        c.get("/logout")
        c.cookies.clear()
        wrong = {"username": "admin", "password": "wrong"}
        good = {"username": "admin", "password": "pw-123456"}
        # First three wrong attempts are rejected with 401 (not yet locked).
        for _ in range(3):
            r = c.post("/login", data=wrong, follow_redirects=False)
            assert r.status_code == 401
        # The next attempt is locked out with 429 + Retry-After.
        r = c.post("/login", data=wrong, follow_redirects=False)
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        # Even correct credentials are refused while locked out.
        r = c.post("/login", data=good, follow_redirects=False)
        assert r.status_code == 429


def test_login_lockout_is_per_username(tmp_path, monkeypatch) -> None:
    """One account's failures must not lock out another on the same IP (#126)."""

    monkeypatch.setenv("KENNY_LOGIN_MAX_ATTEMPTS", "3")
    app = _app(tmp_path)
    with TestClient(app) as c:
        _create_first_user(c, username="admin")
        # A second account (created by the superuser) to share the client IP.
        assert c.post("/api/users", json={
            "username": "kid", "password": "pw-123456", "role": "user",
        }).status_code == 201
        c.get("/logout")
        c.cookies.clear()
        # Trip the limiter for admin.
        for _ in range(3):
            assert c.post("/login", data={
                "username": "admin", "password": "wrong"}, follow_redirects=False).status_code == 401
        assert c.post("/login", data={
            "username": "admin", "password": "wrong"}, follow_redirects=False).status_code == 429
        # kid, from the same client IP, is in a separate bucket and not locked out.
        assert c.post("/login", data={
            "username": "kid", "password": "wrong"}, follow_redirects=False).status_code == 401


def test_mcp_endpoint_requires_token(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        # Gated before reaching the MCP app.
        r = c.get("/mcp/mcp", follow_redirects=False)
        assert r.status_code == 401


def test_rotate_token_requires_operator(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        # No operator credential -> 401, no token minted.
        r = c.post("/api/agents/example-pc/token", follow_redirects=False)
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_rotate_token_then_agent_authenticates(tmp_path) -> None:
    """The rotation endpoint mints a token that then authenticates an agent."""

    app = build_app(db_path=str(tmp_path / "rotate.sqlite"))
    token = app.state.operator_token
    with TestClient(app) as c:
        r = c.post(
            "/api/agents/example-pc/token",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        minted = r.json()["token"]
        assert minted

        # The minted token verifies through the store-backed agent auth path.
        token_store = app.state.token_store
        assert await token_store.verify("example-pc", minted) is True
        # The old seeded token no longer works after rotation.
        assert await token_store.verify("example-pc", "dev-token-1") is False


@pytest.mark.asyncio
async def test_agent_auth_via_store(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "agentauth.sqlite"))
    with TestClient(app):  # triggers lifespan -> token_store.connect()
        registry = app.state.registry
        # Seeded dev token verifies through the registry's async path.
        await registry.authenticate_async("dev", "dev-token")
        with pytest.raises(Exception):
            await registry.authenticate_async("dev", "wrong")
        with pytest.raises(Exception):
            await registry.authenticate_async("ghost", "anything")


def test_secure_cookie_under_tls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_TLS", "1")
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = _create_first_user(c)
        assert r.status_code == 303
        set_cookie = r.headers["set-cookie"].lower()
        assert "secure" in set_cookie
        assert "httponly" in set_cookie


def test_cookie_not_secure_without_tls(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KENNY_TLS", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = _create_first_user(c)
        assert r.status_code == 303
        assert "secure" not in r.headers["set-cookie"].lower()


def test_env_token_still_authorizes_as_back_compat(tmp_path) -> None:
    """The legacy shared token keeps working after accounts exist (ADR-0037)."""

    app = _app(tmp_path)
    token = app.state.operator_token
    with TestClient(app) as c:
        _create_first_user(c)
        c.cookies.clear()
        # Bearer with the shared operator token is accepted (Claude/MCP path).
        r = c.get("/api/fleet", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


def test_totp_required_when_enabled(tmp_path) -> None:
    import sqlite3
    import time

    from kenny_server import security

    db_path = str(tmp_path / "auth.sqlite")
    app = build_app(db_path=db_path)
    secret = security.generate_totp_secret()
    with TestClient(app) as c:
        _create_first_user(c)
        # Enable TOTP on the admin account by writing the secret directly.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE users SET totp_secret = ? WHERE username = 'admin'", (secret,)
        )
        conn.commit()
        conn.close()
        c.get("/logout")
        c.cookies.clear()
        # Password alone is refused now.
        r = c.post(
            "/login",
            data={"username": "admin", "password": "pw-123456"},
            follow_redirects=False,
        )
        assert r.status_code == 401
        # Password + a valid current code succeeds.
        code = security.totp_at(secret, time.time())
        r = c.post(
            "/login",
            data={"username": "admin", "password": "pw-123456", "totp": code},
            follow_redirects=False,
        )
        assert r.status_code == 303

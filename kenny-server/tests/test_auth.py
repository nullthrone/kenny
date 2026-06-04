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


def test_ui_redirects_to_login_then_works_after_login(tmp_path) -> None:
    app = _app(tmp_path)
    token = app.state.operator_token
    with TestClient(app) as c:
        # Unauthenticated UI request redirects to the login page.
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"
        # The login page itself is public.
        assert c.get("/login").status_code == 200
        # Logging in sets the cookie; the client then reaches the UI and API.
        r = c.post("/login", data={"token": token})  # follows 303 -> /
        assert r.status_code == 200
        assert c.get("/api/fleet").status_code == 200  # cookie now sent


def test_login_rejects_wrong_token(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert r.status_code == 401
        # Still locked out.
        assert c.get("/api/fleet", follow_redirects=False).status_code == 401


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
        r = c.post("/api/agents/papa-pc/token", follow_redirects=False)
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_rotate_token_then_agent_authenticates(tmp_path) -> None:
    """The rotation endpoint mints a token that then authenticates an agent."""

    app = build_app(db_path=str(tmp_path / "rotate.sqlite"))
    token = app.state.operator_token
    with TestClient(app) as c:
        r = c.post(
            "/api/agents/papa-pc/token",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        minted = r.json()["token"]
        assert minted

        # The minted token verifies through the store-backed agent auth path.
        token_store = app.state.token_store
        assert await token_store.verify("papa-pc", minted) is True
        # The old seeded token no longer works after rotation.
        assert await token_store.verify("papa-pc", "dev-token-papa") is False


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
    token = app.state.operator_token
    with TestClient(app) as c:
        r = c.post("/login", data={"token": token}, follow_redirects=False)
        assert r.status_code == 303
        set_cookie = r.headers["set-cookie"].lower()
        assert "secure" in set_cookie
        assert "httponly" in set_cookie


def test_cookie_not_secure_without_tls(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KENNY_TLS", raising=False)
    app = _app(tmp_path)
    token = app.state.operator_token
    with TestClient(app) as c:
        r = c.post("/login", data={"token": token}, follow_redirects=False)
        assert r.status_code == 303
        assert "secure" not in r.headers["set-cookie"].lower()

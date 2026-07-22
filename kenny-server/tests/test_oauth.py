"""OAuth 2.1 connector flow (ADR-0041): discovery, DCR, auth-code+PKCE, refresh.

Exercises kenny acting as its own Authorization Server + Resource Server over
HTTP, and asserts the existing PAT / legacy-token bearer paths still work.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

import pytest
from starlette.testclient import TestClient

from kenny_server.main import build_app

PUBLIC_URL = "https://kenny.example.com"
REDIRECT_URI = "http://localhost:7777/callback"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_PUBLIC_URL", PUBLIC_URL)
    return build_app(db_path=str(tmp_path / "oauth.sqlite"))


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _first_user(c, username="admin", password="pw-123456"):
    return c.post(
        "/setup",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _register(c, redirect_uri=REDIRECT_URI):
    r = c.post(
        "/register",
        json={"redirect_uris": [redirect_uri], "client_name": "Claude Desktop"},
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _authorize_params(client_id, challenge, redirect_uri=REDIRECT_URI, state="st-123"):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": f"{PUBLIC_URL}/mcp",
    }


def _run_authcode_flow(c, client_id):
    """Drive authorize→consent→token; return the token JSON. Requires a session."""

    verifier, challenge = _pkce()
    az = c.get(
        "/authorize",
        params=_authorize_params(client_id, challenge),
        follow_redirects=False,
    )
    assert az.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', az.text).group(1)
    cons = c.post(
        "/authorize/consent",
        data={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-123",
            "resource": f"{PUBLIC_URL}/mcp",
            "csrf": csrf,
            "action": "allow",
        },
        follow_redirects=False,
    )
    assert cons.status_code == 302
    loc = cons.headers["location"]
    assert "state=st-123" in loc
    code = re.search(r"[?&]code=([^&]+)", loc).group(1)
    tok = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    return tok.json()


# -- discovery ----------------------------------------------------------------


def test_protected_resource_metadata_public(app) -> None:
    with TestClient(app) as c:
        r = c.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        body = r.json()
        assert body["resource"] == f"{PUBLIC_URL}/mcp"
        assert body["authorization_servers"] == [PUBLIC_URL]
        # The /mcp-suffixed form some clients probe is also served.
        assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


def test_authorization_server_metadata_public(app) -> None:
    with TestClient(app) as c:
        r = c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        body = r.json()
        assert body["issuer"] == PUBLIC_URL
        assert body["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
        assert body["token_endpoint"] == f"{PUBLIC_URL}/token"
        assert body["registration_endpoint"] == f"{PUBLIC_URL}/register"
        assert body["code_challenge_methods_supported"] == ["S256"]


def test_mcp_401_carries_resource_metadata(app) -> None:
    with TestClient(app) as c:
        r = c.get("/mcp")
        assert r.status_code == 401
        www = r.headers["WWW-Authenticate"]
        assert "resource_metadata=" in www
        assert f"{PUBLIC_URL}/.well-known/oauth-protected-resource" in www


# -- dynamic client registration ----------------------------------------------


def test_register_accepts_loopback_and_https(app) -> None:
    with TestClient(app) as c:
        for uri in ("http://localhost:9/cb", "http://127.0.0.1:9/cb", "https://x/cb"):
            r = c.post("/register", json={"redirect_uris": [uri]})
            assert r.status_code == 201, uri


def test_register_rejects_non_loopback_http(app) -> None:
    with TestClient(app) as c:
        r = c.post("/register", json={"redirect_uris": ["http://evil.example.com/cb"]})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_redirect_uri"


# -- full authorization-code + PKCE flow --------------------------------------


def test_full_authcode_flow_and_token_reaches_api(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        tokens = _run_authcode_flow(c, client_id)
        assert tokens["token_type"] == "Bearer"
        assert tokens["expires_in"] > 0
        assert tokens["refresh_token"]
        # The OAuth access token authenticates against the operator-gated API,
        # resolving to the consenting account (superuser here).
        c.cookies.clear()
        api = c.get(
            "/api/fleet", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert api.status_code == 200


def test_oauth_token_reaches_mcp_endpoint(app) -> None:
    """The issued OAuth token must actually reach the ``/mcp`` session handler.

    Regression guard: the MCP app was mounted so the real Streamable-HTTP endpoint
    lived at ``/mcp/mcp`` while OAuth discovery, the audience-bound resource URL,
    the 401 challenge, and the docs all advertise ``/mcp`` — so an authorized client
    failed to connect right after consent. The rest of the suite only ever exercised
    the token against ``/api``; this drives an actual MCP ``initialize`` at ``/mcp``.
    """

    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        tokens = _run_authcode_flow(c, client_id)
        c.cookies.clear()
        # Without a bearer, the operator gate answers /mcp with a 401 challenge.
        assert c.post("/mcp", json={}).status_code == 401
        # With the OAuth bearer, a proper MCP initialize reaches the FastMCP handler
        # at /mcp (not 401 from auth, not 404 from a mis-mounted path).
        init = c.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "kenny-test", "version": "1"},
                },
            },
        )
        assert init.status_code == 200, init.text
        assert "mcp-session-id" in init.headers


def test_authorize_redirects_to_login_when_signed_out(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        c.cookies.clear()  # signed out
        _verifier, challenge = _pkce()
        r = c.get(
            "/authorize",
            params=_authorize_params(client_id, challenge),
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["location"].startswith("/login?next=")
        assert "%2Fauthorize" in r.headers["location"]


def test_login_next_resumes_authorize(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        c.cookies.clear()
        _v, challenge = _pkce()
        params = _authorize_params(client_id, challenge)
        # Log in with a next pointing back at the authorize request.
        from urllib.parse import urlencode

        nxt = "/authorize?" + urlencode(params)
        r = c.post(
            "/login",
            data={"username": "admin", "password": "pw-123456", "next": nxt},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == nxt


def test_login_next_rejects_open_redirect(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        c.get("/logout")
        c.cookies.clear()
        r = c.post(
            "/login",
            data={
                "username": "admin",
                "password": "pw-123456",
                "next": "https://evil.example.com",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/"  # unsafe next ignored


# -- negative / security cases ------------------------------------------------


def test_bad_pkce_verifier_rejected(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        _verifier, challenge = _pkce()
        az = c.get(
            "/authorize",
            params=_authorize_params(client_id, challenge),
            follow_redirects=False,
        )
        csrf = re.search(r'name="csrf" value="([^"]+)"', az.text).group(1)
        cons = c.post(
            "/authorize/consent",
            data={
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "st-123",
                "resource": f"{PUBLIC_URL}/mcp",
                "csrf": csrf,
                "action": "allow",
            },
            follow_redirects=False,
        )
        code = re.search(r"[?&]code=([^&]+)", cons.headers["location"]).group(1)
        tok = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": "not-the-verifier",
            },
        )
        assert tok.status_code == 400
        assert tok.json()["error"] == "invalid_grant"


def test_authorize_rejects_unregistered_redirect_uri(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        _v, challenge = _pkce()
        params = _authorize_params(
            client_id, challenge, redirect_uri="http://localhost:7777/evil"
        )
        r = c.get("/authorize", params=params, follow_redirects=False)
        # Mismatched redirect_uri never redirects; it renders an error page.
        assert r.status_code == 400
        assert "redirect URI" in r.text


def test_refresh_rotation_and_reuse_detection(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        tokens = _run_authcode_flow(c, client_id)
        refresh = tokens["refresh_token"]
        rr = c.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
        )
        assert rr.status_code == 200
        new_refresh = rr.json()["refresh_token"]
        # Reusing the old refresh token is rejected and revokes the family.
        reuse = c.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh},
        )
        assert reuse.status_code == 400
        after = c.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": new_refresh},
        )
        assert after.status_code == 400  # family revoked


def test_revoke_endpoint_kills_token(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        client_id = _register(c)
        tokens = _run_authcode_flow(c, client_id)
        access = tokens["access_token"]
        c.cookies.clear()
        assert (
            c.get("/api/fleet", headers={"Authorization": f"Bearer {access}"}).status_code
            == 200
        )
        assert c.post("/revoke", data={"token": access}).status_code == 200
        assert (
            c.get("/api/fleet", headers={"Authorization": f"Bearer {access}"}).status_code
            == 401
        )


def test_disabling_account_revokes_oauth_tokens(app) -> None:
    with TestClient(app) as c:
        _first_user(c, username="admin")
        # A second account whose OAuth token we will kill by disabling it.
        c.post(
            "/api/users",
            json={"username": "kid", "password": "pw-123456", "role": "operator"},
        )
        client_id = _register(c)
        # Log in as the kid to consent, then disable that account as admin.
        c.get("/logout")
        c.cookies.clear()
        c.post("/login", data={"username": "kid", "password": "pw-123456"})
        tokens = _run_authcode_flow(c, client_id)
        access = tokens["access_token"]
        # Back to admin; disable the kid.
        c.get("/logout")
        c.cookies.clear()
        c.post("/login", data={"username": "admin", "password": "pw-123456"})
        users = c.get("/api/users").json()["users"]
        kid_id = next(u["id"] for u in users if u["username"] == "kid")
        c.patch(f"/api/users/{kid_id}", json={"disabled": True})
        c.cookies.clear()
        r = c.get("/api/fleet", headers={"Authorization": f"Bearer {access}"})
        assert r.status_code == 401


# -- backward compatibility ---------------------------------------------------


def test_pat_and_legacy_token_still_work(app) -> None:
    with TestClient(app) as c:
        _first_user(c)
        # Legacy shared operator token still authenticates /mcp + /api.
        legacy = app.state.operator_token
        assert (
            c.get("/api/fleet", headers={"Authorization": f"Bearer {legacy}"}).status_code
            == 200
        )
        # Mint a PAT and confirm the bearer-PAT path is undisturbed by OAuth.
        pat = c.post("/api/me/pats", json={"label": "cli"}).json()["token"]
        c.cookies.clear()
        assert (
            c.get("/api/fleet", headers={"Authorization": f"Bearer {pat}"}).status_code
            == 200
        )

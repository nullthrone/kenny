"""Role/scope enforcement across the dashboard API and PAT bearer auth (ADR-0037).

Exercises the whole matrix through the real app: first-run setup, self-service
(/api/me), superuser user management, per-user PATs used as bearer tokens, host
scoping for the ``user`` role, and operator-only host removal.
"""

from __future__ import annotations

import time

from starlette.testclient import TestClient

from kenny_server import security
from kenny_server.main import build_app


def _app(tmp_path):
    return build_app(db_path=str(tmp_path / "rbac.sqlite"))


def _setup_admin(c) -> None:
    r = c.post(
        "/setup", data={"username": "admin", "password": "pw-123456"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _pat_for(c, username: str) -> str:
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    uid = users[username]["id"]
    return c.post(f"/api/users/{uid}/pats", json={"label": "t"}).json()["token"]


def test_role_matrix_via_pats(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        assert c.get("/api/me").json()["role"] == "superuser"
        assert c.post("/api/users", json={
            "username": "op", "password": "pw-123456", "role": "operator"}).status_code == 201
        kid = c.post("/api/users", json={
            "username": "kid", "password": "pw-123456", "role": "user",
            "avatar": "dog-corgi"}).json()
        assert kid["avatar"] == "dog-corgi"
        assert c.put(f"/api/users/{kid['id']}/hosts",
                     json={"hosts": ["PC-KID"]}).json()["hosts"] == ["PC-KID"]
        op_pat = _pat_for(c, "op")
        kid_pat = _pat_for(c, "kid")

    # Operator: full fleet, but no settings/users.
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {op_pat}"}
        assert c.get("/api/fleet", headers=h).status_code == 200
        assert c.get("/api/settings", headers=h).status_code == 403
        assert c.get("/api/users", headers=h).status_code == 403
        assert c.get("/api/me", headers=h).json()["role"] == "operator"

    # User: only assigned hosts; no settings/users; cannot remove hosts.
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {kid_pat}"}
        assert c.get("/api/fleet", headers=h).status_code == 200
        assert c.get("/api/settings", headers=h).status_code == 403
        assert c.get("/api/users", headers=h).status_code == 403
        assert c.get("/api/agent/PC-KID", headers=h).status_code == 200
        assert c.get("/api/agent/PC-OTHER", headers=h).status_code == 403
        assert c.delete("/api/agent/PC-KID", headers=h).status_code == 403
        # A scoped operation (refresh) is allowed on an assigned host but the
        # agent is offline, so it fails at the tunnel (502), not the guard (403).
        assert c.post("/api/agent/PC-KID/refresh", headers=h).status_code != 403
        assert c.post("/api/agent/PC-OTHER/refresh", headers=h).status_code == 403


def test_operator_can_remove_host_user_cannot(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        c.post("/api/users", json={
            "username": "op", "password": "pw-123456", "role": "operator"})
        op_pat = _pat_for(c, "op")
        h = {"Authorization": f"Bearer {op_pat}"}
        r = c.delete("/api/agent/GHOST-PC", headers=h)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "registry" in r.json()["purged"]


def test_last_superuser_protected(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        uid = c.get("/api/me").json()["id"]
        # Cannot demote or delete the only superuser.
        assert c.request("PATCH", f"/api/users/{uid}",
                         json={"role": "operator"}).status_code == 409
        assert c.delete(f"/api/users/{uid}").status_code == 409


def test_self_service_password_and_pats(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        # Change own password (wrong current is rejected).
        assert c.post("/api/me/password", json={
            "current_password": "nope", "new_password": "new-123456"}).status_code == 403
        assert c.post("/api/me/password", json={
            "current_password": "pw-123456", "new_password": "new-123456"}).status_code == 200
        # Mint and revoke a personal token.
        assert c.post("/api/me/pats", json={"label": "mine"}).json()["token"]
        pats = c.get("/api/me/pats").json()["pats"]
        assert any(p["label"] == "mine" for p in pats)
        pid = pats[0]["id"]
        assert c.delete(f"/api/me/pats/{pid}").json()["ok"] is True


def test_self_service_totp_enable_disable(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        setup = c.post("/api/me/totp").json()
        secret = setup["secret"]
        assert setup["uri"].startswith("otpauth://")
        # Wrong code is rejected; a valid current code enables 2FA.
        assert c.request("PUT", "/api/me/totp", json={
            "secret": secret, "code": "000000"}).status_code == 400
        code = security.totp_at(secret, time.time())
        assert c.request("PUT", "/api/me/totp", json={
            "secret": secret, "code": code}).json()["totp_enabled"] is True
        assert c.get("/api/me").json()["totp_enabled"] is True
        # Disable requires the account password.
        assert c.request("DELETE", "/api/me/totp", json={
            "password": "pw-123456"}).json()["totp_enabled"] is False


def test_avatars_endpoint(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        avatars = c.get("/api/avatars").json()["avatars"]
        assert "dog-border-collie" in avatars
        # The rasterized PNG is actually served.
        assert c.get("/assets/dog-border-collie.png").status_code == 200

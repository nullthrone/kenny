"""Role/scope enforcement across the dashboard API and PAT bearer auth (ADR-0033).

Exercises the whole matrix through the real app: first-run setup, self-service
(/api/me), superuser user management, per-user PATs used as bearer tokens, host
scoping for the ``user`` role, and operator-only host removal.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

from starlette.testclient import TestClient

from kenny_server import security
from kenny_server.main import build_app
from kenny_server.webui import users


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


def test_users_directory_operator_readable_user_forbidden(tmp_path) -> None:
    """``/api/users/directory`` is the operator-floor id->username projection
    that lets an operator (who cannot reach superuser-only ``/api/users``)
    resolve another account's id to a name (§6: requester/assignee/actor
    labels on the tickets UI)."""

    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        c.post("/api/users", json={
            "username": "op", "password": "pw-123456", "role": "operator"})
        c.post("/api/users", json={
            "username": "kid", "password": "pw-123456", "role": "user",
            "email": "kid@example.com", "avatar": "dog-corgi"})
        op_pat = _pat_for(c, "op")
        kid_pat = _pat_for(c, "kid")

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {op_pat}"}
        r = c.get("/api/users/directory", headers=h)
        assert r.status_code == 200
        users = {u["username"]: u for u in r.json()["users"]}
        assert set(users) == {"admin", "op", "kid"}
        # Minimal, auth-safe projection only — no email/avatar/capability
        # profile/PAT state, unlike the superuser-only /api/users.
        assert set(users["kid"]) == {"id", "username", "role"}
        assert users["kid"]["role"] == "user"

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {kid_pat}"}
        assert c.get("/api/users/directory", headers=h).status_code == 403


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


def test_password_change_revokes_other_sessions(tmp_path) -> None:
    """A password change invalidates sibling sessions and keeps this one (#125)."""

    from kenny_server.auth import COOKIE_NAME

    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        old_sid = c.cookies.get(COOKIE_NAME)
        assert old_sid
        # Change the password on this device.
        assert c.post("/api/me/password", json={
            "current_password": "pw-123456", "new_password": "new-123456"}).status_code == 200
        new_sid = c.cookies.get(COOKIE_NAME)
        # This device got a fresh session and stays authorized.
        assert new_sid and new_sid != old_sid
        assert c.get("/api/fleet").status_code == 200
        # The pre-change session (as held by any other device) is now rejected.
        c.cookies.set(COOKIE_NAME, old_sid)
        assert c.get("/api/fleet", follow_redirects=False).status_code == 401


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


def test_every_credential_change_rotates_sessions(tmp_path) -> None:
    """Password change, 2FA enable, and 2FA disable all evict other sessions
    the same way (CWE-613), pinned together so they cannot drift apart again.

    Enabling 2FA is exactly when an already-compromised session should be
    evicted -- it is the one credential-change path that used to return
    without rotating anything.
    """

    from kenny_server.auth import COOKIE_NAME

    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)

        def _assert_rotates_and_keeps_this_device(act) -> None:
            old_sid = c.cookies.get(COOKIE_NAME)
            assert old_sid
            act()
            new_sid = c.cookies.get(COOKIE_NAME)
            # This device got a fresh session and stays authorized.
            assert new_sid and new_sid != old_sid
            assert c.get("/api/fleet").status_code == 200
            # The pre-change session (as held by any other device) is now
            # rejected. Passed as a one-off request cookie rather than
            # written into ``c``'s jar, so it can't collide with the fresh
            # one already stored there.
            r = c.get(
                "/api/fleet", cookies={COOKIE_NAME: old_sid}, follow_redirects=False
            )
            assert r.status_code == 401

        # 1. Password change.
        _assert_rotates_and_keeps_this_device(lambda: c.post(
            "/api/me/password",
            json={"current_password": "pw-123456", "new_password": "pw2-123456"},
        ))

        # 2. Enable 2FA.
        setup = c.post("/api/me/totp").json()
        secret = setup["secret"]

        def _enable() -> None:
            code = security.totp_at(secret, time.time())
            r = c.request(
                "PUT", "/api/me/totp", json={"secret": secret, "code": code}
            )
            assert r.json()["totp_enabled"] is True

        _assert_rotates_and_keeps_this_device(_enable)

        # 3. Disable 2FA (password changed in step 1, so use the new one).
        _assert_rotates_and_keeps_this_device(lambda: c.request(
            "DELETE", "/api/me/totp", json={"password": "pw2-123456"}
        ))


def test_avatars_endpoint(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        avatars = c.get("/api/avatars").json()["avatars"]
        assert "dog-border-collie" in avatars
        # The rasterized PNG is actually served.
        assert c.get("/assets/dog-border-collie.png").status_code == 200


def test_avatar_sources_and_rasters_match(tmp_path) -> None:
    """Every offered avatar is an SVG source, a 128x128 PNG, and nothing else.

    The PNG is derived from the SVG by hand (see webui/assets/README.md), so the
    three can drift: an id added to AVATARS with no artwork offers a broken image
    in the picker, and an SVG edited without re-rasterizing ships the old picture.
    """
    assets = Path(users.__file__).parent / "assets"
    svgs = {p.stem for p in (assets / "avatars").glob("dog-*.svg")}
    pngs = {p.stem for p in assets.glob("dog-*.png")}
    assert svgs == set(users.AVATARS) == pngs

    app = _app(tmp_path)
    with TestClient(app) as c:
        for avatar in users.AVATARS:
            r = c.get(f"/assets/{avatar}.png")  # public: the login screen needs it
            assert r.status_code == 200, avatar
            # PNG signature, then IHDR's big-endian width/height.
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n", avatar
            assert struct.unpack(">II", r.content[16:24]) == (128, 128), avatar

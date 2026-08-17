"""Session visibility and revocation (ADR-0033).

``GET /api/me/sessions`` and ``POST /api/me/sessions/revoke-others``: a user
sees only their own sessions, never someone else's, and revocation reuses the
same "kill all, re-issue one for this device" mechanism a password/2FA change
already uses -- including its side effect of clearing the per-session
active-agent slot in the registry (``Principal.active_key``, ADR-0033), so a
revoked session doesn't leave a stale entry behind it.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from kenny_server.auth import COOKIE_NAME
from kenny_server.main import build_app


def _app(tmp_path):
    return build_app(db_path=str(tmp_path / "sessions.sqlite"))


def _setup_admin(c) -> None:
    r = c.post(
        "/setup",
        data={"username": "admin", "password": "pw-123456"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _login(c, username: str, password: str = "pw-123456") -> None:
    r = c.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_list_sessions_reports_current_and_other_devices(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c1:
        _setup_admin(c1)
        with TestClient(app) as c2:
            _login(c2, "admin")  # a second device, same account

            rows1 = c1.get("/api/me/sessions").json()["sessions"]
            rows2 = c2.get("/api/me/sessions").json()["sessions"]
            assert len(rows1) == 2
            assert len(rows2) == 2
            # Each device sees exactly one row marked current, and it's its own.
            assert sum(r["current"] for r in rows1) == 1
            assert sum(r["current"] for r in rows2) == 1
            for row in rows1 + rows2:
                assert set(row) == {
                    "created_at", "expires_at", "ip", "user_agent", "current",
                }
                # The raw session id (a bearer credential) never leaves the server.
                assert "id" not in row


def test_revoke_others_signs_out_every_other_device_but_this_one(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c1:
        _setup_admin(c1)
        with TestClient(app) as c2:
            _login(c2, "admin")
            assert c2.get("/api/fleet").status_code == 200

            old_sid_1 = c1.cookies.get(COOKIE_NAME)
            r = c1.post("/api/me/sessions/revoke-others")
            assert r.status_code == 200
            assert r.json() == {"ok": True, "revoked": 1}

            # c1 kept working, on a fresh cookie for this device.
            new_sid_1 = c1.cookies.get(COOKIE_NAME)
            assert new_sid_1 and new_sid_1 != old_sid_1
            assert c1.get("/api/fleet").status_code == 200

            # c2's session is dead.
            assert c2.get("/api/fleet", follow_redirects=False).status_code == 401

            # Only the one live session remains.
            rows = c1.get("/api/me/sessions").json()["sessions"]
            assert len(rows) == 1
            assert rows[0]["current"] is True


def test_revoke_others_clears_the_registry_active_agent_slot(tmp_path) -> None:
    """A session's active-agent selection is keyed ``s:<session_id>`` in the
    registry (ADR-0033, ``Principal.active_key``). Revoking sessions must not
    leave a stale slot for a session id the database no longer has a row for
    -- that includes the caller's own pre-rotation session id, since
    ``revoke-others`` kills and re-issues it too, not just the siblings'.
    """

    app = _app(tmp_path)
    with TestClient(app) as c1:
        _setup_admin(c1)
        with TestClient(app) as c2:
            _login(c2, "admin")
            old_sid_1 = c1.cookies.get(COOKIE_NAME)
            old_sid_2 = c2.cookies.get(COOKIE_NAME)

            registry = app.state.registry
            key1, key2 = f"s:{old_sid_1}", f"s:{old_sid_2}"
            registry._active_by_key[key1] = "some-host"  # noqa: SLF001 - test setup
            registry._active_by_key[key2] = "some-host"  # noqa: SLF001 - test setup

            c1.post("/api/me/sessions/revoke-others")

            assert key1 not in registry._active_by_key  # noqa: SLF001
            assert key2 not in registry._active_by_key  # noqa: SLF001


def test_a_user_can_only_see_and_revoke_their_own_sessions(tmp_path) -> None:
    """Two real seeded users, not an empty fixture: user A's self-service
    session endpoints must never see or touch user B's sessions. There is no
    ``uid`` path parameter on either route for A to even name B's account —
    the isolation is structural, and this confirms the effect matches.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        assert c.post(
            "/api/users",
            json={"username": "alice", "password": "pw-123456", "role": "user"},
        ).status_code == 201
        assert c.post(
            "/api/users",
            json={"username": "bob", "password": "pw-123456", "role": "user"},
        ).status_code == 201

    with TestClient(app) as alice, TestClient(app) as bob:
        _login(alice, "alice")
        _login(bob, "bob")
        bob_sid = bob.cookies.get(COOKIE_NAME)

        alice_rows = alice.get("/api/me/sessions").json()["sessions"]
        assert len(alice_rows) == 1
        assert alice_rows[0]["current"] is True

        r = alice.post("/api/me/sessions/revoke-others")
        assert r.json() == {"ok": True, "revoked": 0}
        assert bob.cookies.get(COOKIE_NAME) == bob_sid
        assert bob.get("/api/fleet").status_code == 200


def test_shared_token_identity_has_no_sessions_to_list_or_revoke(tmp_path) -> None:
    """The legacy shared/env operator token has no backing user row, so it has
    no session row either — a clean empty list, and revoke is a 400, not a
    crash reaching for a ``user_id`` that doesn't exist.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {app.state.operator_token}"}
        assert c.get("/api/me/sessions", headers=h).json() == {"sessions": []}
        assert c.post("/api/me/sessions/revoke-others", headers=h).status_code == 400


async def test_logout_releases_the_active_agent_slot(tmp_path) -> None:
    """Signing out drops the caller's active-agent selection.

    ``AgentRegistry.clear``'s docstring names logout as its reason to exist, but
    ``/logout`` only ever deleted the session row — leaving the registry holding a
    slot keyed on a session that no longer resolves. The rotation paths close the
    same leak; this covers the one that is not a rotation.
    """

    from starlette.testclient import TestClient

    from kenny_server.main import build_app

    app = build_app(db_path=str(tmp_path / "logout.sqlite"))
    with TestClient(app) as c:
        c.post("/setup", data={"username": "op", "password": "correct-horse-battery"})
        sid = c.cookies.get("kenny_op")
        assert sid, "setup should have signed the browser in"

        app.state.registry._active_by_key[f"s:{sid}"] = "example-pc"
        c.get("/logout", follow_redirects=False)

        assert f"s:{sid}" not in app.state.registry._active_by_key

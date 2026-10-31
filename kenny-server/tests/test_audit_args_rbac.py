"""Audit-entry arguments are readable by operator and above, never by a ``user``.

Joined through the real app: an operator's forwarded call is recorded by
``CallLog`` and read back through every route that returns audit rows
(``/api/events``, ``/api/log``, the host drill-down), as a host-scoped ``user`` and
as an operator.
"""

from __future__ import annotations

from functools import partial

from starlette.testclient import TestClient

from kenny_server.main import build_app

_SCRIPT = "net user bob Hunter2 /add"


def _setup_admin(c: TestClient) -> None:
    r = c.post(
        "/setup", data={"username": "admin", "password": "pw-123456"}, follow_redirects=False
    )
    assert r.status_code == 303


def _pat_for(c: TestClient, username: str) -> str:
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    return c.post(f"/api/users/{users[username]['id']}/pats", json={"label": "t"}).json()["token"]


def test_user_role_sees_audit_rows_without_args(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "audit-rbac.sqlite"))
    with TestClient(app) as c:
        _setup_admin(c)
        assert c.post("/api/users", json={
            "username": "op", "password": "pw-123456", "role": "operator"}).status_code == 201
        kid = c.post("/api/users", json={
            "username": "alice", "password": "pw-123456", "role": "user"}).json()
        c.put(f"/api/users/{kid['id']}/hosts", json={"hosts": ["alice-pc"]})
        op_pat = _pat_for(c, "op")
        alice_pat = _pat_for(c, "alice")
        c.portal.call(partial(
            app.state.call_log.record, "alice-pc", "powershell_exec",
            {"script": _SCRIPT, "timeout_s": 30}, ok=True, actor="op", run_id="run-1",
        ))

    def check(token: str, *, sees_args: bool) -> None:
        h = {"Authorization": f"Bearer {token}"}
        with TestClient(app) as c:
            # /api/events
            events = c.get("/api/events?kind=audit&agent=alice-pc", headers=h).json()["entries"]
            [event] = events
            fields = event["fields"]
            assert fields["actor"] == "op" and fields["run_id"] == "run-1"
            assert ("args" in fields) is sees_args

            # /api/log
            [row] = c.get("/api/log?kind=tools", headers=h).json()["rows"]
            assert row["meta"]["actor"] == "op" and row["meta"]["run_id"] == "run-1"
            assert ("args" in row["meta"]) is sees_args

            # Host drill-down
            detail = c.get("/api/agent/alice-pc", headers=h).json()
            [entry] = detail["call_log"]
            assert entry["actor"] == "op" and entry["run_id"] == "run-1"
            assert ("args" in entry) is sees_args

            # The script text is never stored; its digest is shown to operators only.
            bodies = [
                c.get("/api/events?kind=audit", headers=h).text,
                c.get("/api/log", headers=h).text,
                c.get("/api/agent/alice-pc", headers=h).text,
            ]
            for text in bodies:
                assert "Hunter2" not in text
                assert ("sha256" in text) is sees_args

    check(alice_pat, sees_args=False)
    check(op_pat, sees_args=True)
    # The legacy shared token is a superuser and sees them too.
    check(app.state.operator_token, sees_args=True)

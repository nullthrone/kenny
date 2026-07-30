"""Seamless upgrade from a pre-ADR-0037 single-token install.

Simulates an existing database (a host with telemetry, no user accounts) and
asserts that booting the new server: creates the new tables idempotently, keeps
the existing host in inventory, keeps the shared ``KENNY_OPERATOR_TOKEN`` working
as a superuser machine credential, and offers first-run setup because no account
exists yet.
"""

from __future__ import annotations

import asyncio
import sqlite3

from starlette.testclient import TestClient

from kenny_server.main import build_app
from kenny_server.store import TelemetryStore


def _seed_existing_db(db_path: str) -> None:
    async def seed() -> None:
        ts = TelemetryStore(db_path)
        await ts.connect()
        await ts.insert("OLD-PC", "2026-07-01T00:00:00+00:00", {"system": {"host": "OLD-PC"}})
        await ts.close()

    asyncio.run(seed())


def test_upgrade_preserves_hosts_and_shared_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", "legacy-secret")
    db_path = str(tmp_path / "old.sqlite")
    _seed_existing_db(db_path)

    app = build_app(db_path=db_path)
    with TestClient(app) as c:
        h = {"Authorization": "Bearer legacy-secret"}
        # The shared token still authorizes, as a back-compat superuser.
        me = c.get("/api/me", headers=h).json()
        assert me["role"] == "superuser"
        assert me["is_shared_token"] is True
        # The pre-existing host is still in inventory.
        agents = c.get("/api/fleet", headers=h).json()["agents"]
        assert any(a["agent_id"] == "OLD-PC" for a in agents)
        # No account yet → the browser is guided to first-run setup.
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/setup"

    # The new user tables were created idempotently in the existing DB.
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"users", "user_tokens", "sessions", "user_hosts"} <= tables
    # The reliability alarm suppression table (ADR-0045 / issue #166) too.
    assert "reliability_suppressions" in tables


def test_suppression_table_created_idempotently_and_survives_a_second_boot(tmp_path) -> None:
    db_path = str(tmp_path / "twice.sqlite")
    app1 = build_app(db_path=db_path)
    with TestClient(app1) as c:
        h = {"Authorization": f"Bearer {app1.state.operator_token}"}
        resp = c.post(
            "/api/reliability/suppressions", headers=h,
            json={"event_id": 4176, "source": "Microsoft-Windows-CAPI2"},
        )
        assert resp.status_code == 200

    # Booting a second app instance against the same DB file must not error,
    # and the rule inserted in boot #1 must survive and be in the mirror.
    app2 = build_app(db_path=db_path)
    with TestClient(app2) as c:
        h = {"Authorization": f"Bearer {app2.state.operator_token}"}
        rules = c.get("/api/reliability/suppressions", headers=h).json()["rules"]
        assert len(rules) == 1
        assert rules[0]["event_id"] == 4176
        assert app2.state.suppression.match("ANY-PC", "Microsoft-Windows-CAPI2", 4176) is not None


def test_setup_closes_after_first_account(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "fresh.sqlite"))
    with TestClient(app) as c:
        assert c.post("/setup", data={"username": "admin", "password": "pw-123456"},
                      follow_redirects=False).status_code == 303
        # Once an account exists, setup is closed.
        assert c.post("/setup", data={"username": "x", "password": "y"},
                      follow_redirects=False).status_code == 409

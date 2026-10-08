"""``/api/specialized-agents*``: who may read the agents, who may choose their mode (ADR-0071).

Through the real composition root, so the guard, the runner, the settings and
the triage binding are the ones the server runs: a mode written here for triage
must flip the very settings the Admin page shows, and rebind triage at once.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server.agents.catalog import CATALOG
from kenny_server.main import build_app


class _FakeAnthropic:
    """Stands in for the Anthropic client; no model is called here."""


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_AGENTS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def _pat_for(c: TestClient, username: str) -> str:
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    return c.post(f"/api/users/{users[username]['id']}/pats", json={"label": "t"}).json()["token"]


def _app_with_operator(tmp_path) -> tuple[Any, dict[str, str]]:
    app = build_app(db_path=str(tmp_path / "agents-api.sqlite"), client_factory=_FakeAnthropic)
    with TestClient(app) as c:
        r = c.post(
            "/setup", data={"username": "admin", "password": "pw-123456"}, follow_redirects=False
        )
        assert r.status_code == 303
        assert c.post(
            "/api/users", json={"username": "op", "password": "pw-123456", "role": "operator"}
        ).status_code == 201
        assert c.post(
            "/api/users", json={"username": "kid", "password": "pw-123456", "role": "user"}
        ).status_code == 201
        op_pat = _pat_for(c, "op")
        kid_pat = _pat_for(c, "kid")
        admin_pat = _pat_for(c, "admin")
    return app, {"op": op_pat, "kid": kid_pat, "admin": admin_pat}


def _h(pat: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {pat}"}


def test_an_operator_reads_the_agents_and_their_runs(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["op"])
        r = c.get("/api/specialized-agents", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        agents = {a["id"]: a for a in body["agents"]}
        assert set(agents) == set(CATALOG)
        triage = agents["triage"]
        assert triage["mode"] == "shadow"  # enabled, resolve off by default
        assert triage["spec_hash"] == CATALOG["triage"].spec_hash
        assert triage["latest_run"] is None
        assert c.get("/api/specialized-agents/runs", headers=h).json() == {"runs": []}

        run = c.portal.call(
            lambda: app.state.agent_store.start_run(
                agent_id="triage", spec_hash="h", trigger="t", mode="shadow"
            )
        )
        listed = c.get("/api/specialized-agents/runs?agent_id=triage&limit=5", headers=h).json()["runs"]
        assert [r["id"] for r in listed] == [run.id]
        one = c.get(f"/api/specialized-agents/runs/{run.id}", headers=h)
        assert one.status_code == 200 and one.json()["status"] == "running"
        again = {a["id"]: a for a in c.get("/api/specialized-agents", headers=h).json()["agents"]}
        assert again["triage"]["latest_run"]["id"] == run.id


def test_a_user_reads_nothing(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["kid"])
        assert c.get("/api/specialized-agents", headers=h).status_code == 403
        assert c.get("/api/specialized-agents/runs", headers=h).status_code == 403
        assert c.get("/api/specialized-agents/runs/x", headers=h).status_code == 403
        assert c.put("/api/specialized-agents/triage/mode", json={"mode": "act"}, headers=h).status_code == 403


def test_unknown_agents_runs_and_bad_queries(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["op"])
        assert c.get("/api/specialized-agents/runs?agent_id=nope", headers=h).status_code == 404
        assert c.get("/api/specialized-agents/runs?limit=many", headers=h).status_code == 400
        assert c.get("/api/specialized-agents/runs?limit=0", headers=h).status_code == 400
        assert c.get("/api/specialized-agents/runs/nope", headers=h).status_code == 404


def test_an_operator_cannot_choose_a_mode(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        r = c.put("/api/specialized-agents/triage/mode", json={"mode": "act"}, headers=_h(pats["op"]))
        assert r.status_code == 403
        assert app.state.settings.get("KENNY_TRIAGE_RESOLVE") is False


def test_a_superuser_moves_triage_through_its_settings(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["admin"])
        r = c.put("/api/specialized-agents/triage/mode", json={"mode": "act"}, headers=h)
        assert r.status_code == 200
        assert r.json() == {"agent_id": "triage", "mode": "act", "requested": "act"}
        # The settings the Admin page shows, and the live consumer they drive.
        assert app.state.settings.get("KENNY_TRIAGE_RESOLVE") is True
        assert app.state.triage.resolve_enabled is True
        assert c.portal.call(app.state.agents.mode_of, "triage") == "act"
        assert {a["id"]: a for a in c.get("/api/specialized-agents", headers=h).json()["agents"]}[
            "triage"
        ]["mode"] == "act"

        r = c.put("/api/specialized-agents/triage/mode", json={"mode": "off"}, headers=h)
        assert r.status_code == 200 and r.json()["mode"] == "off"
        assert app.state.settings.get("KENNY_TRIAGE_ENABLED") is False
        # The binding follows: a new ticket no longer reaches the runner.
        assert app.state.tickets._triage is None

        r = c.put("/api/specialized-agents/triage/mode", json={"mode": "shadow"}, headers=h)
        assert r.json()["mode"] == "shadow"
        assert app.state.tickets._triage is not None
        assert app.state.settings.get("KENNY_TRIAGE_RESOLVE") is False

        # Who chose each mode is on the event log.
        logs = c.portal.call(lambda: app.state.event_store.query(kind="log", limit=500))
        changes = [
            (e["fields"]["mode"], e["fields"]["actor"])
            for e in reversed(logs)
            if (e.get("fields") or {}).get("agent") == "triage"
        ]
        assert changes == [("act", "admin"), ("off", "admin"), ("shadow", "admin")]


def test_bad_mode_writes(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["admin"])
        assert c.put("/api/specialized-agents/nope/mode", json={"mode": "act"}, headers=h).status_code == 404
        assert c.put("/api/specialized-agents/triage/mode", json={"mode": "ACT"}, headers=h).status_code == 400
        assert c.put("/api/specialized-agents/triage/mode", json={}, headers=h).status_code == 400
        assert c.put("/api/specialized-agents/triage/mode", json=["act"], headers=h).status_code == 400
        r = c.put(
            "/api/specialized-agents/triage/mode",
            content=b"not json",
            headers={**h, "Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert app.state.settings.get("KENNY_TRIAGE_RESOLVE") is False
        assert app.state.settings.get("KENNY_TRIAGE_ENABLED") is True


def test_specialized_agent_routes_are_not_in_the_host_enroll_exemption(tmp_path) -> None:
    """``/api/agents/<host>/enroll`` is open to the agent's own token; the specialized
    agents live under a different prefix so no path of theirs can match it."""

    app, _ = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        c.cookies.clear()
        for method in ("get", "post", "put"):
            r = getattr(c, method)("/api/specialized-agents/runs/enroll", follow_redirects=False)
            assert r.status_code == 401, method
        # The host namespace is untouched: a host may be named "runs".
        r = c.post("/api/agents/runs/token", follow_redirects=False)
        assert r.status_code == 401

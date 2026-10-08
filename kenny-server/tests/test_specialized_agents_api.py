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
from kenny_server.auth import COOKIE_NAME
from kenny_server.main import build_app


class _FakeAnthropic:
    """Stands in for the Anthropic client; no model is called here."""


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_AGENTS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", LEGACY_TOKEN)


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


#: The legacy shared operator token this module's app accepts (``_env``).
LEGACY_TOKEN = "legacy-shared-token"

#: Headers for a request a signed-in browser makes: none, the session cookie
#: :func:`_login` left on the client does the work.
BROWSER: dict[str, str] = {}


def _login(c: TestClient, username: str = "admin") -> None:
    """Sign ``username`` in on ``c`` as a browser does, leaving its session cookie."""

    c.cookies.clear()
    r = c.post(
        "/login", data={"username": username, "password": "pw-123456"}, follow_redirects=False
    )
    assert r.status_code in (302, 303), r.text


def _shown_hash(c: TestClient, agent_id: str, headers: dict[str, str] = BROWSER) -> str:
    """The ``effective_hash`` the dashboard shows for ``agent_id``."""

    agents = {a["id"]: a for a in c.get("/api/specialized-agents", headers=headers).json()["agents"]}
    return agents[agent_id]["effective_hash"]


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
        _login(c)
        h = BROWSER
        r = c.put(
            "/api/specialized-agents/triage/mode",
            json={"mode": "act", "effective_hash": _shown_hash(c, "triage")},
            headers=h,
        )
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
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        h = BROWSER
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


# -- consent is a person's, about what they were shown --------------------------------


def test_act_names_the_hash_the_superuser_was_shown(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        url = "/api/specialized-agents/triage/mode"
        r = c.put(url, json={"mode": "act"}, headers=BROWSER)
        assert r.status_code == 400 and "effective_hash" in r.json()["error"]
        r = c.put(url, json={"mode": "act", "effective_hash": 7}, headers=BROWSER)
        assert r.status_code == 400
        r = c.put(url, json={"mode": "act", "effective_hash": "0" * 64}, headers=BROWSER)
        assert r.status_code == 409
        assert app.state.settings.get("KENNY_TRIAGE_RESOLVE") is False
        # Leaving act needs no hash: nothing is consented to.
        assert c.put(url, json={"mode": "shadow"}, headers=BROWSER).status_code == 200


def use_credential(c: TestClient, kind: str, pats: dict[str, str]) -> dict[str, str]:
    """Drop the browser session on ``c``; the headers that present superuser ``kind`` instead."""

    c.cookies.clear()
    if kind == "pat":
        return _h(pats["admin"])
    if kind == "legacy_bearer":
        return _h(LEGACY_TOKEN)
    assert kind == "legacy_cookie"
    c.cookies.set(COOKIE_NAME, LEGACY_TOKEN)
    return BROWSER


#: Every superuser credential that is not a person at a browser.
NOT_A_BROWSER = ["pat", "legacy_bearer", "legacy_cookie"]


@pytest.mark.parametrize("kind", NOT_A_BROWSER)
def test_a_mode_change_needs_a_person_at_a_browser(tmp_path, kind: str) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        shown = _shown_hash(c, "triage")
        h = use_credential(c, kind, pats)
        # Reads stay open to every superuser credential.
        assert c.get("/api/specialized-agents", headers=h).status_code == 200
        url = "/api/specialized-agents/triage/mode"
        r = c.put(url, json={"mode": "act", "effective_hash": shown}, headers=h)
        assert r.status_code == 403, (kind, r.text)
        assert "dashboard" in r.json()["error"]
        assert c.put(url, json={"mode": "off"}, headers=h).status_code == 403
        assert app.state.settings.get("KENNY_TRIAGE_RESOLVE") is False
        assert app.state.settings.get("KENNY_TRIAGE_ENABLED") is True


def _request_as(principal: Any) -> Any:
    from starlette.requests import Request

    return Request({"type": "http", "kenny_principal": principal, "headers": []})


def test_only_an_account_on_a_browser_session_is_a_person() -> None:
    from kenny_server.auth import Principal, _env_principal
    from kenny_server.webui import _person_at_a_browser

    def su(**kw: Any) -> Principal:
        return Principal(user_id=1, username="admin", role="superuser", **kw)

    assert _person_at_a_browser(_request_as(su(session_id="cookie")))
    assert not _person_at_a_browser(_request_as(su(pat_id="p")))
    assert not _person_at_a_browser(_request_as(su(oauth_token_id="o", oauth_client_id="claude")))
    assert not _person_at_a_browser(_request_as(su(session_id="cookie", pat_id="p")))
    assert not _person_at_a_browser(_request_as(su(session_id="cookie", oauth_token_id="o")))
    assert not _person_at_a_browser(_request_as(su()))
    assert not _person_at_a_browser(_request_as(_env_principal()))
    no_account = Principal(user_id=None, username="x", role="superuser", session_id="cookie")
    assert not _person_at_a_browser(_request_as(no_account))
    assert not _person_at_a_browser(_request_as(None))

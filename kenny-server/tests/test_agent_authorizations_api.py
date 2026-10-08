"""``/api/specialized-agents/{id}/authorizations`` and ``/params``: who may do what (ADR-0072).

Through the real composition root and the real guard. A superuser grants,
revokes and edits parameters; an operator reads; a ``user`` reaches none of
it. The authorization store is attached to the app's own runner, as
``main.py`` wires it, and a test agent with a ``normal_change`` is added to
that runner's catalog, because the shipped catalog has no such agent yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server.agents.authorizations import AuthorizationStore
from kenny_server.agents.catalog import CATALOG
from kenny_server.agents.spec import AgentSpec, ArgConstraint, Trigger, effective_hash

from test_specialized_agents_api import _app_with_operator, _h

SEVENZIP = "7zip.7zip"

INSTALLER = AgentSpec(
    id="installer",
    title="Installer",
    description="Installs what the household allowed.",
    prompt="You install the packages this household allowed, nothing else.",
    trigger=Trigger(kind="on_demand"),
    tools=frozenset({"winget_list", "winget_install"}),
    constraints=(ArgConstraint("winget_install", "id", param="packages"),),
    params=("packages",),
)

BASE = "/api/specialized-agents/installer"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_AGENTS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def _wire(c: TestClient, app: Any, db_path: str) -> AuthorizationStore:
    store = AuthorizationStore(db_path)
    c.portal.call(store.connect)
    app.state.agents.authorizations = store
    app.state.agents.catalog = MappingProxyType({**CATALOG, INSTALLER.id: INSTALLER})
    return store


def _expiry(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _grant_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tool": "winget_install",
        "scope": ["thomas-pc"],
        "max_attempts_per_day": 2,
        "expires_at": _expiry(),
        "note": "monthly 7-Zip",
    }
    body.update(overrides)
    return body


def test_a_user_reaches_none_of_it(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            h = _h(pats["kid"])
            assert c.get(f"{BASE}/authorizations", headers=h).status_code == 403
            assert c.post(f"{BASE}/authorizations", json=_grant_body(), headers=h).status_code == 403
            assert c.delete(f"{BASE}/authorizations/x", headers=h).status_code == 403
            assert c.get(f"{BASE}/params", headers=h).status_code == 403
            assert c.put(f"{BASE}/params", json={"params": {}}, headers=h).status_code == 403
        finally:
            c.portal.call(store.close)


def test_an_operator_reads_but_never_grants_revokes_or_edits(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            su, op = _h(pats["admin"]), _h(pats["op"])
            granted = c.post(f"{BASE}/authorizations", json=_grant_body(), headers=su)
            assert granted.status_code == 201, granted.text
            auth_id = granted.json()["id"]

            listed = c.get(f"{BASE}/authorizations", headers=op)
            assert listed.status_code == 200
            assert [a["id"] for a in listed.json()["authorizations"]] == [auth_id]
            assert c.get(f"{BASE}/params", headers=op).status_code == 200

            assert c.post(f"{BASE}/authorizations", json=_grant_body(), headers=op).status_code == 403
            assert c.delete(f"{BASE}/authorizations/{auth_id}", headers=op).status_code == 403
            r = c.put(f"{BASE}/params", json={"params": {"packages": ["x"]}}, headers=op)
            assert r.status_code == 403
            assert c.portal.call(app.state.agents.get_params, "installer") == {}
            assert c.portal.call(store.list, "installer")[0].revoked_at is None
        finally:
            c.portal.call(store.close)


def test_a_superuser_grants_against_the_current_hash_and_revokes(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            su = _h(pats["admin"])
            r = c.put(f"{BASE}/params", json={"params": {"packages": [SEVENZIP]}}, headers=su)
            assert r.status_code == 200, r.text
            live = effective_hash(INSTALLER, {"packages": [SEVENZIP]})
            assert r.json()["effective_hash"] == live

            granted = c.post(f"{BASE}/authorizations", json=_grant_body(), headers=su)
            assert granted.status_code == 201
            body = granted.json()
            assert (body["effective_hash"], body["granted_by"], body["status"]) == (
                live,
                "admin",
                "live",
            )
            assert body["attempts_last_24h"] == {}

            revoked = c.delete(f"{BASE}/authorizations/{body['id']}", headers=su)
            assert revoked.status_code == 200
            assert (revoked.json()["status"], revoked.json()["revoked_by"]) == ("revoked", "admin")
            assert c.delete(f"{BASE}/authorizations/nope", headers=su).status_code == 404
            other = "/api/specialized-agents/triage"
            assert c.delete(f"{other}/authorizations/{body['id']}", headers=su).status_code == 404
        finally:
            c.portal.call(store.close)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool": "shell_exec"},
        {"tool": "winget_list"},
        {"tool": "service_restart"},
        {"scope": []},
        {"scope": "server"},
        {"max_attempts_per_day": 0},
        {"expires_at": _expiry(days=181)},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"tool": None},
        {"note": 5},
    ],
)
def test_a_refused_grant_is_a_400_and_writes_nothing(tmp_path, overrides) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            r = c.post(
                f"{BASE}/authorizations", json=_grant_body(**overrides), headers=_h(pats["admin"])
            )
            assert r.status_code == 400, r.text
            assert c.portal.call(store.list, "installer") == []
        finally:
            c.portal.call(store.close)


def test_a_parameter_edit_drops_act_and_voids_through_the_api(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            su = _h(pats["admin"])
            c.put(f"{BASE}/params", json={"params": {"packages": [SEVENZIP]}}, headers=su)
            assert c.put(f"{BASE}/mode", json={"mode": "act"}, headers=su).json()["mode"] == "act"
            granted = c.post(f"{BASE}/authorizations", json=_grant_body(), headers=su).json()

            listing = {a["id"]: a for a in c.get("/api/specialized-agents", headers=su).json()["agents"]}
            assert listing["installer"]["act_bound"] is True
            assert listing["installer"]["params"] == {"packages": [SEVENZIP]}

            r = c.put(
                f"{BASE}/params", json={"params": {"packages": [SEVENZIP, "Evil.Pkg"]}}, headers=su
            )
            assert r.status_code == 200
            assert (r.json()["mode"], r.json()["voided"]) == ("shadow", 1)
            [after] = c.get(f"{BASE}/authorizations", headers=su).json()["authorizations"]
            assert (after["id"], after["status"]) == (granted["id"], "voided")
            listing = {a["id"]: a for a in c.get("/api/specialized-agents", headers=su).json()["agents"]}
            assert (listing["installer"]["mode"], listing["installer"]["act_bound"]) == (
                "shadow",
                False,
            )
        finally:
            c.portal.call(store.close)


def test_bad_parameters_and_unknown_agents(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            su = _h(pats["admin"])
            assert c.put(f"{BASE}/params", json={"params": {"nope": ["x"]}}, headers=su).status_code == 400
            assert c.put(f"{BASE}/params", json={"params": ["x"]}, headers=su).status_code == 400
            assert c.put(f"{BASE}/params", content=b"{", headers=su).status_code == 400
            nope = "/api/specialized-agents/nope"
            assert c.get(f"{nope}/params", headers=su).status_code == 404
            assert c.get(f"{nope}/authorizations", headers=su).status_code == 404
            assert c.post(f"{nope}/authorizations", json=_grant_body(), headers=su).status_code == 404
            got = c.get(f"{BASE}/params", headers=su).json()
            assert got == {
                "agent_id": "installer",
                "declared": ["packages"],
                "params": {},
                "effective_hash": effective_hash(INSTALLER, {}),
            }
        finally:
            c.portal.call(store.close)


def test_without_an_authorization_store_the_routes_say_so(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        app.state.agents.authorizations = None
        h = _h(pats["admin"])
        assert c.get("/api/specialized-agents/triage/authorizations", headers=h).status_code == 503
        r = c.post("/api/specialized-agents/triage/authorizations", json=_grant_body(), headers=h)
        assert r.status_code == 503

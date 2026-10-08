"""``/api/specialized-agents/{id}/authorizations`` and ``/params``: who may do what (ADR-0072).

Through the real composition root and the real guard. A superuser grants,
revokes and edits parameters — signed in at a browser, never with a token —
and a grant or ``act`` names the effective hash that superuser was shown; an
operator reads; a ``user`` reaches none of it. The authorization store is
attached to the app's own runner, as ``main.py`` wires it, and a test agent
with a ``normal_change`` is added to that runner's catalog, because the
shipped catalog has no such agent yet.
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

from test_specialized_agents_api import (
    BROWSER,
    NOT_A_BROWSER,
    _app_with_operator,
    _h,
    _login,
    _shown_hash,
    use_credential,
)

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
    from test_specialized_agents_api import LEGACY_TOKEN

    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_AGENTS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", LEGACY_TOKEN)


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


def _shown_body(c: TestClient, **overrides: Any) -> dict[str, Any]:
    """A grant naming the installer's hash as the dashboard shows it now."""

    return _grant_body(effective_hash=_shown_hash(c, "installer"), **overrides)


def _promote(c: TestClient) -> Any:
    return c.put(
        f"{BASE}/mode",
        json={"mode": "act", "effective_hash": _shown_hash(c, "installer")},
        headers=BROWSER,
    )


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
            _login(c)
            granted = c.post(f"{BASE}/authorizations", json=_shown_body(c), headers=BROWSER)
            assert granted.status_code == 201, granted.text
            auth_id = granted.json()["id"]

            op = _h(pats["op"])
            listed = c.get(f"{BASE}/authorizations", headers=op)
            assert listed.status_code == 200
            assert [a["id"] for a in listed.json()["authorizations"]] == [auth_id]
            assert c.get(f"{BASE}/params", headers=op).status_code == 200

            assert c.post(f"{BASE}/authorizations", json=_shown_body(c), headers=op).status_code == 403
            assert c.delete(f"{BASE}/authorizations/{auth_id}", headers=op).status_code == 403
            r = c.put(f"{BASE}/params", json={"params": {"packages": ["x"]}}, headers=op)
            assert r.status_code == 403
            assert c.portal.call(app.state.agents.get_params, "installer") == {}
            assert c.portal.call(store.list, "installer")[0].revoked_at is None
        finally:
            c.portal.call(store.close)


def test_an_operator_at_a_browser_still_cannot_grant(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            _login(c, "op")
            r = c.post(f"{BASE}/authorizations", json=_shown_body(c), headers=BROWSER)
            assert r.status_code == 403
            assert c.put(f"{BASE}/params", json={"params": {}}, headers=BROWSER).status_code == 403
            assert c.portal.call(store.list, "installer") == []
        finally:
            c.portal.call(store.close)


def test_a_superuser_grants_against_the_hash_shown_and_revokes(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            _login(c)
            r = c.put(f"{BASE}/params", json={"params": {"packages": [SEVENZIP]}}, headers=BROWSER)
            assert r.status_code == 200, r.text
            live = effective_hash(INSTALLER, {"packages": [SEVENZIP]})
            assert r.json()["effective_hash"] == live

            granted = c.post(f"{BASE}/authorizations", json=_shown_body(c), headers=BROWSER)
            assert granted.status_code == 201
            body = granted.json()
            assert (body["effective_hash"], body["granted_by"], body["status"]) == (
                live,
                "admin",
                "live",
            )
            assert body["attempts_last_24h"] == {}

            revoked = c.delete(f"{BASE}/authorizations/{body['id']}", headers=BROWSER)
            assert revoked.status_code == 200
            assert (revoked.json()["status"], revoked.json()["revoked_by"]) == ("revoked", "admin")
            assert c.delete(f"{BASE}/authorizations/nope", headers=BROWSER).status_code == 404
            other = "/api/specialized-agents/triage"
            assert c.delete(f"{other}/authorizations/{body['id']}", headers=BROWSER).status_code == 404
        finally:
            c.portal.call(store.close)


def test_a_grant_against_a_hash_nobody_was_shown_is_refused(tmp_path) -> None:
    """A concurrent parameter edit must not widen what a superuser approved.

    The superuser reviewed the agent with one package; before the grant
    arrives someone widens the allowlist. The grant names the reviewed hash,
    so it is a 409, not a grant bound to the wider agent.
    """

    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            _login(c)
            c.put(f"{BASE}/params", json={"params": {"packages": [SEVENZIP]}}, headers=BROWSER)
            reviewed = _shown_hash(c, "installer")
            widened = {"packages": [SEVENZIP, "Evil.Pkg"]}
            c.put(f"{BASE}/params", json={"params": widened}, headers=BROWSER)

            r = c.post(
                f"{BASE}/authorizations", json=_grant_body(effective_hash=reviewed), headers=BROWSER
            )
            assert r.status_code == 409, r.text
            r = c.put(
                f"{BASE}/mode", json={"mode": "act", "effective_hash": reviewed}, headers=BROWSER
            )
            assert r.status_code == 409, r.text
            assert c.portal.call(store.list, "installer") == []
            assert c.portal.call(app.state.agents.mode_of, "installer") == "shadow"

            missing = c.post(f"{BASE}/authorizations", json=_grant_body(), headers=BROWSER)
            assert missing.status_code == 400 and "effective_hash" in missing.json()["error"]
            assert c.put(f"{BASE}/mode", json={"mode": "act"}, headers=BROWSER).status_code == 400
            assert c.portal.call(store.list, "installer") == []
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
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            _login(c)
            r = c.post(f"{BASE}/authorizations", json=_shown_body(c, **overrides), headers=BROWSER)
            assert r.status_code == 400, r.text
            assert c.portal.call(store.list, "installer") == []
        finally:
            c.portal.call(store.close)


def test_a_parameter_edit_drops_act_and_voids_through_the_api(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            _login(c)
            c.put(f"{BASE}/params", json={"params": {"packages": [SEVENZIP]}}, headers=BROWSER)
            assert _promote(c).json()["mode"] == "act"
            granted = c.post(f"{BASE}/authorizations", json=_shown_body(c), headers=BROWSER).json()

            listing = {
                a["id"]: a for a in c.get("/api/specialized-agents", headers=BROWSER).json()["agents"]
            }
            assert listing["installer"]["act_bound"] is True
            assert listing["installer"]["params"] == {"packages": [SEVENZIP]}

            r = c.put(
                f"{BASE}/params",
                json={"params": {"packages": [SEVENZIP, "Evil.Pkg"]}},
                headers=BROWSER,
            )
            assert r.status_code == 200
            assert (r.json()["mode"], r.json()["voided"]) == ("shadow", 1)
            [after] = c.get(f"{BASE}/authorizations", headers=BROWSER).json()["authorizations"]
            assert (after["id"], after["status"]) == (granted["id"], "voided")
            listing = {
                a["id"]: a for a in c.get("/api/specialized-agents", headers=BROWSER).json()["agents"]
            }
            assert (listing["installer"]["mode"], listing["installer"]["act_bound"]) == (
                "shadow",
                False,
            )
        finally:
            c.portal.call(store.close)


@pytest.mark.parametrize("kind", NOT_A_BROWSER)
def test_grants_revocations_and_parameter_edits_need_a_person_at_a_browser(
    tmp_path, kind: str
) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            _login(c)
            body = _shown_body(c)
            existing = c.post(f"{BASE}/authorizations", json=body, headers=BROWSER).json()["id"]

            h = use_credential(c, kind, pats)
            # Reads stay as they are.
            assert c.get(f"{BASE}/authorizations", headers=h).status_code == 200
            assert c.get(f"{BASE}/params", headers=h).status_code == 200

            for r in (
                c.post(f"{BASE}/authorizations", json=body, headers=h),
                c.delete(f"{BASE}/authorizations/{existing}", headers=h),
                c.put(f"{BASE}/params", json={"params": {"packages": ["x"]}}, headers=h),
                c.put(
                    f"{BASE}/mode",
                    json={"mode": "act", "effective_hash": body["effective_hash"]},
                    headers=h,
                ),
            ):
                assert r.status_code == 403, (kind, r.request.method, r.text)
                assert "dashboard" in r.json()["error"]
            [only] = c.portal.call(store.list, "installer")
            assert (only.id, only.revoked_at) == (existing, None)
            assert c.portal.call(app.state.agents.get_params, "installer") == {}
            assert c.portal.call(app.state.agents.mode_of, "installer") == "shadow"
        finally:
            c.portal.call(store.close)


def test_bad_parameters_and_unknown_agents(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = _wire(c, app, str(tmp_path / "agents-api.sqlite"))
        try:
            _login(c)
            su = BROWSER
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
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        app.state.agents.authorizations = None
        _login(c)
        assert c.get("/api/specialized-agents/triage/authorizations", headers=BROWSER).status_code == 503
        r = c.post(
            "/api/specialized-agents/triage/authorizations", json=_grant_body(), headers=BROWSER
        )
        assert r.status_code == 503

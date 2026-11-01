"""``/api/stewards/builder/*`` and ``/api/stewards/custom*``: who may build a steward (ADR-0074).

Through the real composition root, so the guard, the consent check, the runner
and the catalog the scheduler and the event trigger hold are the ones the
server runs. Reading is an operator's; creating, saving and deleting take a
superuser signed in at the dashboard, sending JSON from its own origin.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server.stewards.builder import BUILDER_TOOLS
from kenny_server.urls import mcp_resource_url

from test_steward_builder import BANNED, ID, _data
from test_stewards_api import (
    BROWSER,
    LEGACY_TOKEN,
    _app_with_operator,
    _h,
    _login,
    use_credential,
)

CUSTOM = "/api/stewards/custom"
EVIL = "http://evil.example:3000"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_STEWARDS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", LEGACY_TOKEN)


def _listed(c: TestClient, headers: dict[str, str] = BROWSER) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in c.get("/api/stewards", headers=headers).json()["stewards"]}


def _oauth(c: TestClient, app: Any) -> dict[str, str]:
    async def mint() -> str:
        client = await app.state.oauth_store.register_client(["http://localhost:7777/cb"])
        admin = await app.state.user_store.get_by_username("admin")
        access, _refresh, _family = await app.state.oauth_store.issue_token_pair(
            client_id=client["client_id"], user_id=admin["id"], resource=mcp_resource_url()
        )
        return access

    return _h(c.portal.call(mint))


def _create(c: TestClient, **overrides: Any) -> Any:
    return c.post(CUSTOM, json={"spec": _data(**overrides)}, headers=BROWSER)


def test_an_operator_reads_what_a_built_steward_may_be_made_of(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["op"])
        r = c.get("/api/stewards/builder/catalog", headers=h)
        assert r.status_code == 200, r.text
        shown = r.json()
        names = {t["name"] for t in shown["tools"]}
        assert names == set(BUILDER_TOOLS) and not names & BANNED
        # The composition root registered its preconditions; the editor offers them.
        assert "disk_pressure" in shown["preconditions"]
        assert shown["max_expiry_days"] == 180
        assert set(shown["never_authorized"]) == {"shell_exec", "powershell_exec", "agent_update"}
        assert c.get("/api/stewards/builder/catalog", headers=_h(pats["kid"])).status_code == 403


def test_a_clone_is_offered_unsaved_with_what_stands_in_its_way(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["op"])
        patch = c.get("/api/stewards/builder/clone/patch", headers=h).json()
        assert patch["data"]["id"] == "custom_patch" and patch["errors"] == []
        assert patch["source"] == "clone:patch"
        rules = c.get("/api/stewards/builder/clone/config_hygiene?id=custom_rules", headers=h).json()
        assert rules["data"]["id"] == "custom_rules"
        assert "constraints[0].evidence" in {e["field"] for e in rules["errors"]}
        assert c.get("/api/stewards/builder/clone/nope", headers=h).status_code == 404
        assert "custom_patch" not in _listed(c, h)


def test_a_superuser_at_a_browser_creates_saves_and_deletes(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        r = _create(c)
        assert r.status_code == 201, r.text
        created = r.json()
        assert (created["steward_id"], created["version"], created["mode"]) == (ID, 1, "shadow")
        # One catalog: the scheduler and the event trigger see it too.
        assert ID in app.state.steward_scheduler.catalog
        assert app.state.steward_scheduler.catalog is app.state.stewards.catalog
        listed = _listed(c)[ID]
        assert listed["builtin"] is True and listed["invalid"] is False
        assert listed["source"] == "blank" and listed["version"] == 1
        assert listed["effective_hash"] == created["effective_hash"]
        # The editor's view of it.
        got = c.get(f"{CUSTOM}/{ID}", headers=BROWSER).json()
        assert got["data"]["prompt"] == _data()["prompt"] and got["version"] == 1
        assert got["effective_hash"] == created["effective_hash"] and got["invalid"] is False

        assert _create(c).status_code == 409  # the id is taken
        clone = c.post(CUSTOM, json={"clone_of": "patch"}, headers=BROWSER)
        assert clone.status_code == 201, clone.text
        assert clone.json()["source"] == "clone:patch" and "custom_patch" in _listed(c)
        refused = c.post(CUSTOM, json={"clone_of": "config_hygiene", "id": "custom_rules"}, headers=BROWSER)
        assert refused.status_code == 400
        assert "constraints[0].evidence" in {e["field"] for e in refused.json()["errors"]}
        assert refused.json()["data"]["id"] == "custom_rules"

        # A save names the hash shown; a stale one is 409, a bad spec 400 with fields.
        url = f"{CUSTOM}/{ID}"
        stale = c.put(url, json={"spec": _data(prompt="Edited."), "effective_hash": "0" * 64}, headers=BROWSER)
        assert stale.status_code == 409
        missing = c.put(url, json={"spec": _data(prompt="Edited.")}, headers=BROWSER)
        assert missing.status_code == 400
        bad = c.put(
            url,
            json={"spec": _data(tools=["winget_list", "shell_exec"]), "effective_hash": created["effective_hash"]},
            headers=BROWSER,
        )
        assert bad.status_code == 400 and "tools[1]" in {e["field"] for e in bad.json()["errors"]}
        saved = c.put(
            url, json={"spec": _data(prompt="Edited."), "effective_hash": created["effective_hash"]}, headers=BROWSER
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 2 and saved.json()["mode"] == "shadow"
        assert saved.json()["effective_hash"] != created["effective_hash"]
        assert c.put(f"{CUSTOM}/patch", json={"spec": {}, "effective_hash": "x"}, headers=BROWSER).status_code == 404

        gone = c.delete(url, headers=BROWSER)
        assert gone.status_code == 200 and gone.json()["deleted"] is True
        assert ID not in _listed(c) and ID not in app.state.steward_scheduler.catalog
        assert c.get(url, headers=BROWSER).status_code == 404
        assert c.delete(url, headers=BROWSER).status_code == 404
        assert c.delete(f"{CUSTOM}/patch", headers=BROWSER).status_code == 404
        assert "patch" in _listed(c)


def test_bad_create_requests_are_400s(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        for body in (
            ["spec"],
            {"spec": "x"},
            {"spec": _data(), "extra": 1},
            {"spec": _data(), "source": "clone:nope"},
            {"spec": _data(), "source": "made-up"},
            {"clone_of": "nope"},
            {"clone_of": "patch", "id": 7},
            {"clone_of": "patch", "spec": _data()},
        ):
            r = c.post(CUSTOM, json=body, headers=BROWSER)
            assert r.status_code == 400, (body, r.text)
        r = c.post(CUSTOM, content=b"not json", headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        assert ID not in _listed(c)
        r = c.post(CUSTOM, json={"spec": _data(), "source": "copilot"}, headers=BROWSER)
        assert r.status_code == 201 and r.json()["source"] == "copilot"


@pytest.mark.parametrize("kind", ["pat", "oauth", "legacy_bearer", "legacy_cookie", "operator_browser"])
def test_only_a_superuser_at_a_browser_builds(tmp_path, kind: str) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        made = _create(c, id="custom_kept").json()
        if kind == "oauth":
            headers = _oauth(c, app)
            c.cookies.clear()
        elif kind == "operator_browser":
            _login(c, "op")
            headers = BROWSER
        else:
            headers = use_credential(c, kind, pats)
        # Reads stay open to every operator credential.
        assert c.get("/api/stewards/builder/catalog", headers=headers).status_code == 200
        assert c.get(f"{CUSTOM}/custom_kept", headers=headers).status_code == 200
        r = c.post(CUSTOM, json={"spec": _data()}, headers=headers)
        assert r.status_code == 403, (kind, r.text)
        r = c.put(
            f"{CUSTOM}/custom_kept",
            json={"spec": _data(id="custom_kept", prompt="x"), "effective_hash": made["effective_hash"]},
            headers=headers,
        )
        assert r.status_code == 403, (kind, r.text)
        assert c.delete(f"{CUSTOM}/custom_kept", headers=headers).status_code == 403
        _login(c)
        listed = _listed(c)
        assert ID not in listed and listed["custom_kept"]["version"] == 1


def test_a_user_reaches_none_of_it(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["kid"])
        assert c.get("/api/stewards/builder/catalog", headers=h).status_code == 403
        assert c.get("/api/stewards/builder/clone/patch", headers=h).status_code == 403
        assert c.get(f"{CUSTOM}/{ID}", headers=h).status_code == 403


def test_another_origin_or_a_body_that_is_not_json_is_refused(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        made = _create(c, id="custom_kept").json()
        writes = [
            ("POST", CUSTOM, {"spec": _data()}),
            ("PUT", f"{CUSTOM}/custom_kept", {"spec": _data(id="custom_kept", prompt="x"),
                                              "effective_hash": made["effective_hash"]}),
            ("DELETE", f"{CUSTOM}/custom_kept", None),
        ]
        for method, path, body in writes:
            raw = None if body is None else json.dumps(body).encode()
            for headers in ({"Origin": EVIL}, {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"}):
                sent = {**headers, "Content-Type": "application/json"} if raw is not None else headers
                r = c.request(method, path, content=raw, headers=sent)
                assert r.status_code == 403, (method, path, headers, r.text)
            if raw is not None:
                for media in ("text/plain", "application/x-www-form-urlencoded"):
                    r = c.request(method, path, content=raw, headers={"Content-Type": media})
                    assert r.status_code == 415, (method, path, media, r.text)
        listed = _listed(c)
        assert ID not in listed and listed["custom_kept"]["version"] == 1
        # The dashboard's own request passes.
        own = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
        assert c.post(CUSTOM, json={"spec": _data()}, headers=own).status_code == 201


def test_an_invalid_built_steward_is_listed_after_a_restart_and_can_be_fixed(tmp_path, monkeypatch) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        assert _create(c, preconditions=["disk_pressure"]).status_code == 201
    # The next release no longer registers the precondition the steward names.
    import kenny_server.main as main_module

    monkeypatch.setattr(main_module, "register_steward_preconditions", lambda *_a, **_k: None)
    again = main_module.build_app(db_path=str(tmp_path / "stewards-api.sqlite"), client_factory=lambda: None)
    with TestClient(again) as c:
        _login(c)
        shown = _listed(c)[ID]
        assert shown["invalid"] is True and shown["mode"] == "off"
        assert [e["field"] for e in shown["errors"]] == ["preconditions[0]"]
        assert ID not in again.state.stewards.catalog
        got = c.get(f"{CUSTOM}/{ID}", headers=BROWSER).json()
        assert got["invalid"] is True and got["effective_hash"] == shown["effective_hash"]
        fixed = c.put(f"{CUSTOM}/{ID}", json={"spec": _data(), "effective_hash": shown["effective_hash"]},
                      headers=BROWSER)
        assert fixed.status_code == 200, fixed.text
        assert fixed.json()["version"] == 2 and _listed(c)[ID]["invalid"] is False

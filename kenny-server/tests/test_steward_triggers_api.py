"""``/api/stewards/{id}/triggers``: who may see and change what starts a steward.

Through the real composition root and the real guard, as the parameter routes
are tested. What may start a steward is part of what a person consented to, so
a change is a superuser's, signed in at a browser, and names the effective hash
that person was shown; an operator reads; a ``user`` reaches none of it.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server.stewards.authorizations import AuthorizationStore
from kenny_server.stewards.catalog import CATALOG
from kenny_server.stewards.spec import baseline_config, effective_hash

from test_stewards_api import (
    BROWSER,
    LEGACY_TOKEN,
    NOT_A_BROWSER,
    _app_with_operator,
    _h,
    _login,
    _shown_hash,
    use_credential,
)

BASE = "/api/stewards/patch/triggers"
BASELINE = {"schedule": {"min_interval_days": None}, "events": [], "command": False}
WITH_COMMAND = {**BASELINE, "command": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_STEWARDS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", LEGACY_TOKEN)


def _body(c: TestClient, triggers: dict[str, Any] = WITH_COMMAND, **overrides: Any) -> dict[str, Any]:
    return {"triggers": triggers, "effective_hash": _shown_hash(c, "patch"), **overrides}


def _stored(c: TestClient, app: Any) -> Any:
    return c.portal.call(app.state.steward_store.get_triggers, "patch")


def test_an_operator_reads_what_starts_a_steward(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        r = c.get(BASE, headers=_h(pats["op"]))
        assert r.status_code == 200, r.text
        assert r.json() == {
            "steward_id": "patch",
            "allowed": {"schedule": True, "events": [], "command": True, "editable": True},
            "baseline": BASELINE,
            "triggers": BASELINE,
            "effective_hash": effective_hash(CATALOG["patch"], {}),
        }
        triage = c.get("/api/stewards/triage/triggers", headers=_h(pats["op"])).json()
        assert triage["allowed"]["editable"] is False
        assert triage["triggers"] == triage["baseline"] == baseline_config(CATALOG["triage"])
        assert c.get("/api/stewards/nope/triggers", headers=_h(pats["op"])).status_code == 404


def test_a_user_reaches_none_of_it(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = _h(pats["kid"])
        assert c.get(BASE, headers=h).status_code == 403
        assert c.put(BASE, json={"triggers": WITH_COMMAND, "effective_hash": "x"}, headers=h).status_code == 403


def test_an_operator_never_changes_them(tmp_path) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        body = _body(c)
        assert c.put(BASE, json=body, headers=_h(pats["op"])).status_code == 403
        _login(c, "op")
        assert c.put(BASE, json=body, headers=BROWSER).status_code == 403
        assert _stored(c, app) is None


@pytest.mark.parametrize("kind", NOT_A_BROWSER)
def test_a_change_needs_a_person_at_a_browser(tmp_path, kind: str) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        body = _body(c)
        h = use_credential(c, kind, pats)
        assert c.get(BASE, headers=h).status_code == 200  # reads stay open
        r = c.put(BASE, json=body, headers=h)
        assert r.status_code == 403, (kind, r.text)
        assert "dashboard" in r.json()["error"]
        assert _stored(c, app) is None


def test_a_superuser_at_a_browser_changes_them_and_unbinds_what_was_granted(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        store = AuthorizationStore(str(tmp_path / "stewards-api.sqlite"))
        c.portal.call(store.connect)
        app.state.stewards.authorizations = store
        try:
            _login(c)
            before = _shown_hash(c, "patch")
            act = c.put("/api/stewards/patch/mode", json={"mode": "act", "effective_hash": before},
                        headers=BROWSER)
            assert act.status_code == 200 and act.json()["mode"] == "act"

            r = c.put(BASE, json=_body(c), headers=BROWSER)
            assert r.status_code == 200, r.text
            after = effective_hash(CATALOG["patch"], {}, WITH_COMMAND)
            assert r.json() == {
                "steward_id": "patch",
                "triggers": WITH_COMMAND,
                "effective_hash": after,
                "mode": "shadow",
                "voided": 0,
            }
            assert after != before and _shown_hash(c, "patch") == after
            got = c.get(BASE, headers=BROWSER).json()
            assert (got["triggers"], got["effective_hash"]) == (WITH_COMMAND, after)
            listing = {s["id"]: s for s in c.get("/api/stewards", headers=BROWSER).json()["stewards"]}
            assert listing["patch"]["triggers"] == WITH_COMMAND
            assert listing["patch"]["triggers_baseline"] == BASELINE

            # Back to the baseline: the old hash again, still in shadow.
            back = c.put(BASE, json=_body(c, BASELINE), headers=BROWSER)
            assert back.status_code == 200
            assert (back.json()["effective_hash"], back.json()["mode"]) == (before, "shadow")
        finally:
            c.portal.call(store.close)


def test_a_change_against_a_hash_nobody_was_shown_is_refused(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        reviewed = _shown_hash(c, "patch")
        widened = c.put("/api/stewards/patch/params", json={"params": {"hosts": ["a-pc"]}},
                        headers=BROWSER)
        assert widened.status_code == 200
        r = c.put(BASE, json={"triggers": WITH_COMMAND, "effective_hash": reviewed}, headers=BROWSER)
        assert r.status_code == 409, r.text
        assert _stored(c, app) is None


@pytest.mark.parametrize(
    "body",
    [
        {"triggers": WITH_COMMAND},  # no hash
        {"triggers": WITH_COMMAND, "effective_hash": ""},
        {"effective_hash": "x"},
        {"triggers": [], "effective_hash": "x"},
        "not an object",
    ],
)
def test_a_malformed_request_is_a_400(tmp_path, body: Any) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        assert c.put(BASE, json=body, headers=BROWSER).status_code == 400
        malformed = {**BROWSER, "Content-Type": "application/json"}
        assert c.put(BASE, content=b"{", headers=malformed).status_code == 400
        # Not declared JSON at all: refused before it is read.
        assert c.put(BASE, content=b"{", headers=BROWSER).status_code == 415
        assert _stored(c, app) is None


@pytest.mark.parametrize(
    "triggers",
    [
        {**BASELINE, "events": [{"name": "alert:offline"}]},  # not allowed for patch
        {**BASELINE, "events": [{"name": "alert:nope"}]},
        {**BASELINE, "surprise": True},
        {**BASELINE, "command": "yes"},
    ],
)
def test_a_configuration_the_steward_does_not_allow_is_a_400(tmp_path, triggers: Any) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        r = c.put(BASE, json=_body(c, triggers), headers=BROWSER)
        assert r.status_code == 400, r.text
        assert _stored(c, app) is None


def test_triage_triggers_cannot_be_changed(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        baseline = baseline_config(CATALOG["triage"])
        body = {"triggers": baseline, "effective_hash": _shown_hash(c, "triage")}
        r = c.put("/api/stewards/triage/triggers", json=body, headers=BROWSER)
        assert r.status_code == 400 and "fixed" in r.json()["error"]
        nope = c.put("/api/stewards/nope/triggers", json=body, headers=BROWSER)
        assert nope.status_code == 404

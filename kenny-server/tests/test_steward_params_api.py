"""``/api/stewards`` and ``/api/stewards/{id}/params``: parameters by kind.

The dashboard renders and submits each parameter by its kind, so the API must
say which kind every declared parameter is — and what it says must be what
``validate_params`` enforces on the way back in. Through the real composition
root, as the other steward API tests.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kenny_server.stewards.catalog import CATALOG
from kenny_server.stewards.preconditions import DEFAULT_MIN_PERCENT_USED, _threshold
from kenny_server.stewards.spec import param_kind

from test_stewards_api import BROWSER, LEGACY_TOKEN, _app_with_operator, _login

PARAMS = "/api/stewards/disk_hygiene/params"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_STEWARDS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", LEGACY_TOKEN)


def test_the_overview_and_the_params_route_carry_each_declared_params_kind(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        listing = {s["id"]: s for s in c.get("/api/stewards", headers=BROWSER).json()["stewards"]}
        disk = listing["disk_hygiene"]["param_kinds"]
        assert disk["min_percent_used"] == "percent"
        assert disk["window"] == "window" and disk["require_idle"] == "bool"
        assert disk["hosts"] == "list" and disk["targets"] == "list"
        for steward_id, entry in listing.items():
            declared = CATALOG[steward_id].params
            assert entry["param_kinds"] == {n: param_kind(n) for n in declared}
            assert set(entry["param_kinds"].values()) <= {"window", "bool", "percent", "list"}
        # The route the editor loads from says the same, beside the names it declares.
        got = c.get(PARAMS, headers=BROWSER).json()
        assert got["param_kinds"] == disk
        assert sorted(got["param_kinds"]) == got["declared"]


def test_a_percent_is_saved_and_an_unset_one_is_the_documented_default(tmp_path) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        r = c.put(PARAMS, json={"params": {"min_percent_used": 85}}, headers=BROWSER)
        assert r.status_code == 200, r.text
        assert c.get(PARAMS, headers=BROWSER).json()["params"] == {"min_percent_used": 85}
        # Unset means left out: the steward then uses its default threshold.
        r = c.put(PARAMS, json={"params": {"hosts": ["a-pc"]}}, headers=BROWSER)
        assert r.status_code == 200, r.text
        assert "min_percent_used" not in c.get(PARAMS, headers=BROWSER).json()["params"]
        stored = c.portal.call(app.state.stewards.get_params, "disk_hygiene")
        assert _threshold(stored) == DEFAULT_MIN_PERCENT_USED
        # What the dashboard must not send: an empty list, null, a string, a flag, out of range.
        for bad in ([], None, "85", True, 0, 100):
            r = c.put(PARAMS, json={"params": {"min_percent_used": bad}}, headers=BROWSER)
            assert r.status_code == 400, (bad, r.text)

"""Switching unattended action on through a setting is consent, too (ADR-0072).

Triage's ``act`` is its ``KENNY_TRIAGE_RESOLVE`` setting, and every agent run
needs ``KENNY_AGENTS_ENABLED``. Turning either on — or resetting it, which may
land on an "on" default or environment value — takes a superuser at a browser,
exactly like promoting an agent to ``act``. Turning either off is never a
widening and stays open to any superuser credential.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from test_specialized_agents_api import (
    BROWSER,
    NOT_A_BROWSER,
    _app_with_operator,
    _login,
    use_credential,
)

AUTONOMY_KEYS = ["KENNY_TRIAGE_RESOLVE", "KENNY_AGENTS_ENABLED"]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_specialized_agents_api import LEGACY_TOKEN

    for key in AUTONOMY_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", LEGACY_TOKEN)


@pytest.mark.parametrize("key", AUTONOMY_KEYS)
@pytest.mark.parametrize("kind", NOT_A_BROWSER)
def test_a_token_cannot_switch_autonomy_on_or_reset_it(tmp_path, kind: str, key: str) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        assert c.put(f"/api/settings/{key}", json={"value": "0"}, headers=BROWSER).status_code == 200
        h = use_credential(c, kind, pats)
        on = c.put(f"/api/settings/{key}", json={"value": "1"}, headers=h)
        assert on.status_code == 403, (kind, on.text)
        assert "dashboard" in on.json()["error"]
        reset = c.delete(f"/api/settings/{key}", headers=h)
        assert reset.status_code == 403, (kind, reset.text)
        assert app.state.settings.get(key) in (False, 0)
        # Off is never a widening: any superuser credential may switch it off.
        assert c.put(f"/api/settings/{key}", json={"value": "0"}, headers=h).status_code == 200


@pytest.mark.parametrize("key", AUTONOMY_KEYS)
def test_a_person_at_a_browser_switches_autonomy_on(tmp_path, key: str) -> None:
    app, _pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        _login(c)
        r = c.put(f"/api/settings/{key}", json={"value": "1"}, headers=BROWSER)
        assert r.status_code == 200, r.text
        assert app.state.settings.get(key) in (True, 1)
        assert c.delete(f"/api/settings/{key}", headers=BROWSER).status_code == 200


@pytest.mark.parametrize("kind", NOT_A_BROWSER)
def test_other_settings_keep_accepting_any_superuser_credential(tmp_path, kind: str) -> None:
    app, pats = _app_with_operator(tmp_path)
    with TestClient(app) as c:
        h = use_credential(c, kind, pats)
        r = c.put("/api/settings/KENNY_TRIAGE_MAX_ITERATIONS", json={"value": "6"}, headers=h)
        assert r.status_code == 200, r.text

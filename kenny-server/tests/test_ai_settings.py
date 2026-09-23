"""The AI section: key, feature switches and backups (ADR-0066), joined end to end.

Every AI feature asks ``kenny_server.ai`` whether it may run. These tests set the
key and the switches the way the dashboard does — ``PUT /api/settings/{key}`` — and
check the consumer on the other side: the chat and ticket routes, the host page's
``ai_enabled``, triage, the Discord surface, and the backup copy that must never
carry the key.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server import ai
from kenny_server.main import build_app


class _FakeAnthropic:
    """Stands in for the Anthropic client; no route below gets as far as a call."""


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ANTHROPIC_API_KEY", *ai.FEATURES.values()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KENNY_DISCORD_BOT_TOKEN", "bot-token")


def _bearer(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _put(c: TestClient, app: Any, key: str, value: Any) -> None:
    r = c.put(f"/api/settings/{key}", headers=_bearer(app), json={"value": value})
    assert r.status_code == 200, r.text


def test_the_feature_names_are_the_ones_the_dashboard_gates_on() -> None:
    """``/api/ai/status`` and the dashboard's gating share one list of names."""

    shared = Path(__file__).resolve().parents[2] / "kenny-web" / "src" / "api" / "aiFeatures.json"
    assert json.loads(shared.read_text()) == list(ai.FEATURES)


def test_a_key_saved_in_the_dashboard_switches_ai_on_everywhere(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "key.sqlite"), client_factory=_FakeAnthropic)
    with TestClient(app) as c:
        h = _bearer(app)
        status = c.get("/api/ai/status", headers=h).json()
        assert status == {
            "configured": False,
            "source": "none",
            "features": {name: False for name in ai.FEATURES},
        }
        assert app.state.tickets._triage is None
        assert c.post("/api/chat/stream", headers=h, json={"message": "hi"}).status_code == 503

        _put(c, app, "ANTHROPIC_API_KEY", "sk-from-the-dashboard")

        status = c.get("/api/ai/status", headers=h).json()
        assert status["configured"] is True and status["source"] == "db"
        assert all(status["features"].values())
        assert app.state.tickets._triage is not None
        assert c.get("/api/agent/example-pc", headers=h).json()["ai_enabled"] is True

        # Clearing it falls back to the environment, which has none.
        c.delete("/api/settings/ANTHROPIC_API_KEY", headers=h)
        assert c.get("/api/ai/status", headers=h).json()["configured"] is False
        assert app.state.tickets._triage is None


def test_each_switch_turns_its_feature_off_and_only_that_one(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-the-env")
    app = build_app(db_path=str(tmp_path / "switch.sqlite"), client_factory=_FakeAnthropic)
    with TestClient(app) as c:
        h = _bearer(app)
        assert c.get("/api/ai/status", headers=h).json()["source"] == "env"

        _put(c, app, "KENNY_AI_ASK_ENABLED", False)
        assert c.post("/api/chat/stream", headers=h, json={"message": "hi"}).status_code == 503
        features = c.get("/api/ai/status", headers=h).json()["features"]
        assert features["ask"] is False and features["recommend"] is True

        _put(c, app, "KENNY_AI_RECOMMEND_ENABLED", False)
        assert c.get("/api/agent/example-pc", headers=h).json()["ai_enabled"] is False
        r = c.post(
            "/api/recommendation/stream", headers=h, json={"agent_id": "pc", "section": "disk"}
        )
        assert r.status_code == 503

        _put(c, app, "KENNY_AI_TICKET_ASSISTANT_ENABLED", False)
        created = c.post("/api/tickets", json={"title": "printer"}, headers=h).json()
        ticket = c.get(f"/api/tickets/{created['id']}", headers=h).json()
        assert ticket["assistant_available"] is False
        assert app.state.discord_service.assistant_enabled() is False

        _put(c, app, "KENNY_AI_TICKET_ASSISTANT_ENABLED", True)
        assert app.state.discord_service.assistant_enabled() is True


def test_the_client_follows_a_changed_key(monkeypatch) -> None:
    """The real client is built for the key in force and rebuilt when it changes."""

    built: list[str] = []
    monkeypatch.setattr(ai, "_default_factory", lambda key: built.append(key) or object())

    class _Settings:
        values = {"ANTHROPIC_API_KEY": "sk-one"}

        def get(self, key: str) -> Any:
            return self.values.get(key, True)

    settings = _Settings()
    access = ai.AiAccess(settings)
    first = access.client()
    assert access.client() is first
    settings.values["ANTHROPIC_API_KEY"] = "sk-two"
    assert access.client() is not first
    assert built == ["sk-one", "sk-two"]


def test_a_backup_never_holds_the_key(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "live.sqlite"), client_factory=_FakeAnthropic)
    with TestClient(app) as c:
        _put(c, app, "ANTHROPIC_API_KEY", "sk-secret")
        _put(c, app, "KENNY_NTFY_TOKEN", "ntfy-token")
        result = c.portal.call(app.state.backup_mgr.create, "manual")
        copy = Path(app.state.backup_mgr.backup_dir) / result["name"]

        with sqlite3.connect(copy) as conn:
            stored = dict(conn.execute("SELECT key, value FROM settings"))
        assert "ANTHROPIC_API_KEY" not in stored
        assert stored["KENNY_NTFY_TOKEN"] == "ntfy-token"
        assert b"sk-secret" not in copy.read_bytes()

        # The live server keeps it.
        assert c.get("/api/ai/status", headers=_bearer(app)).json()["source"] == "db"


def test_the_key_test_reports_a_missing_key_without_calling_out() -> None:
    access = ai.AiAccess(env={})
    assert asyncio.run(asyncio.to_thread(access.probe)) == {
        "ok": False,
        "error": "no API key is set",
    }

"""Composition-root tests: what ``build_app`` actually wires up.

Three properties this module pins down, because each of them is a promise the
integration makes rather than a property of any single component:

* **Tickets do not depend on Discord.** A server with no Discord configuration
  starts, serves the fleet API and serves the whole ticket API, and creates no
  Discord task at all.
* **The optional loops are opt-out at startup.** The ticket sweeper is created
  when its interval is non-zero and not created when it is zero, the same
  convention the alert/backup/update loops follow.
* **The Discord surface can never take the server down.** With the optional
  ``discord.py`` dependency missing, a fully configured Discord surface still
  starts the server; the gateway's ``GatewayUnavailable`` is logged once and
  swallowed.

Plus the alert -> ticket hook, which is injected into the alert engine and must
not make alert delivery any less best-effort (ADR-0029).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server.alerting import AlertEngine
from kenny_server.main import build_app
from kenny_server.notify import Notification
from kenny_server.store import AlertStateStore, EventStore, TelemetryStore
from kenny_server.ticketstore import TicketStore
from kenny_server.tickets import TicketService

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

# Discord keys are read at build/startup time; a leaked value from the ambient
# environment would silently change what these tests are testing.
_DISCORD_ENV = (
    "KENNY_DISCORD_BOT_TOKEN",
    "KENNY_DISCORD_ENABLED",
    "KENNY_DISCORD_GUILD_IDS",
    "KENNY_DISCORD_MODEL",
    "KENNY_TICKET_SWEEP_INTERVAL_SECS",
    "KENNY_TICKET_SWEEP_INITIAL_DELAY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _DISCORD_ENV:
        monkeypatch.delenv(key, raising=False)


class _FakeAnthropic:
    """Stands in for the Anthropic client so no API key is needed anywhere."""


def _bearer(app: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.operator_token}"}


# -- a server with no Discord configuration ------------------------------------


def test_server_without_discord_serves_fleet_and_tickets(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "nodiscord.sqlite"))
    assert app.state.discord_service is None

    with TestClient(app) as c:
        h = _bearer(app)
        assert app.state.discord_task is None
        assert c.get("/api/fleet", headers=h).status_code == 200

        # The whole ticket surface works without Discord.
        created = c.post("/api/tickets", json={"title": "printer jam"}, headers=h)
        assert created.status_code == 201
        ticket_id = created.json()["id"]
        assert c.get("/api/tickets", headers=h).json()["tickets"][0]["id"] == ticket_id
        assert c.get(f"/api/tickets/{ticket_id}", headers=h).status_code == 200
        assert c.get(f"/api/tickets/{ticket_id}/events", headers=h).status_code == 200
        assert c.get("/api/approvals", headers=h).json() == {"approvals": []}
        assert c.get("/api/tool-classes", headers=h).status_code == 200

        # The identity store exists even with no bot: linking is a server-side
        # mapping, so its routes answer rather than 503.
        assert c.get("/api/discord/identities", headers=h).json() == {"identities": []}
        assert c.get("/api/discord/claims", headers=h).json() == {"claims": []}
        # ... but anything needing a live gateway is honestly unavailable.
        assert c.get("/api/discord/members", headers=h).status_code == 503
        assert c.get("/api/discord/status", headers=h).json()["configured"] is False


# -- the sweeper's start/no-start convention -----------------------------------


def test_ticket_sweeper_task_is_created_when_the_interval_is_non_zero(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KENNY_TICKET_SWEEP_INTERVAL_SECS", "300")
    # A long initial delay keeps this short-lived app instance from ever
    # sweeping; the assertion is about the task existing, not about a pass.
    monkeypatch.setenv("KENNY_TICKET_SWEEP_INITIAL_DELAY", "3600")
    app = build_app(db_path=str(tmp_path / "sweep_on.sqlite"))
    with TestClient(app):
        assert app.state.ticket_task is not None
        assert not app.state.ticket_task.done()
    assert app.state.ticket_task.cancelled()


def test_ticket_sweeper_task_is_not_created_when_the_interval_is_zero(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KENNY_TICKET_SWEEP_INTERVAL_SECS", "0")
    app = build_app(db_path=str(tmp_path / "sweep_off.sqlite"))
    with TestClient(app):
        assert app.state.ticket_task is None


# -- Discord configured, discord.py missing ------------------------------------


def test_startup_survives_a_missing_discord_dependency(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The optional dependency is forced unimportable, installed or not.

    ``sys.modules["discord"] = None`` makes ``import discord`` raise
    ``ImportError`` in CPython, so this asserts the self-hoster-without-the-extra
    case even in an environment where the extra happens to be installed.
    """

    monkeypatch.setitem(sys.modules, "discord", None)
    monkeypatch.setenv("KENNY_DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("KENNY_DISCORD_ENABLED", "1")
    monkeypatch.setenv("KENNY_DISCORD_GUILD_IDS", "guild-1")
    app = build_app(
        db_path=str(tmp_path / "discord.sqlite"), client_factory=_FakeAnthropic
    )
    assert app.state.discord_service is not None
    assert app.state.discord_service.guild_ids == frozenset({"guild-1"})

    with caplog.at_level("WARNING", logger="kenny.discord"):
        with TestClient(app) as c:
            task = app.state.discord_task
            assert task is not None
            # The task ends on its own: the gateway reports the optional
            # dependency is missing and the loop returns instead of raising.
            c.portal.call(partial(asyncio.wait_for, task, 5))
            assert task.done() and task.exception() is None
            # The server is entirely unaffected.
            assert c.get("/api/fleet", headers=_bearer(app)).status_code == 200
            assert c.get("/api/tickets", headers=_bearer(app)).status_code == 200
    warnings = [r for r in caplog.records if "discord.py is not installed" in r.getMessage()]
    assert len(warnings) == 1

    # The routes still see the service, so status reports the real diagnostics.
    with TestClient(app) as c:
        body = c.get("/api/discord/status", headers=_bearer(app)).json()
        assert body["configured"] is True
        assert body["connected"] is False


def test_no_discord_task_without_the_enabled_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token alone never connects a bot: enabling it is a separate decision."""

    monkeypatch.setenv("KENNY_DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("KENNY_DISCORD_GUILD_IDS", "guild-1")
    app = build_app(
        db_path=str(tmp_path / "discord_off.sqlite"), client_factory=_FakeAnthropic
    )
    with TestClient(app):
        assert app.state.discord_task is None


# -- alerts opening tickets ----------------------------------------------------


class _Agent:
    online = True


class _Registry:
    def get(self, agent_id: str) -> Any:
        return _Agent()


def _snapshot(disk_pct: float) -> dict[str, Any]:
    return {
        "disk": {
            "status": "ok",
            "summary": f"C: {disk_pct:.0f}% full",
            "volumes": [{"mount": "C:", "percent_used": disk_pct}],
        }
    }


async def _alert_stores(tmp_path, name: str):
    db = str(tmp_path / name)
    store, events, state = TelemetryStore(db), EventStore(db), AlertStateStore(db)
    tickets = TicketStore(db)
    for s in (store, events, state, tickets):
        await s.connect()
    return store, events, state, tickets


async def test_an_alert_opens_an_unowned_ticket_for_the_alerting_agent(tmp_path) -> None:
    store, events, state, ticket_store = await _alert_stores(tmp_path, "alert_ticket.sqlite")
    service = TicketService(ticket_store, now=lambda: NOW)
    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=_Registry(),
        notifiers=[],
        open_ticket=lambda note: service.create(
            title=note.title,
            origin="alert",
            requester_user_id=None,
            agent_id=note.agent_id,
            summary=note.body,
            actor="system",
        ),
    )
    await store.insert(
        "pc1",
        (NOW - timedelta(minutes=1)).isoformat(),
        _snapshot(96.0),
        received_at=(NOW - timedelta(minutes=1)).isoformat(),
    )

    sent = await engine.evaluate_once(NOW)
    assert [n.kind for n in sent] == ["alert"]

    tickets = await ticket_store.list()
    assert len(tickets) == 1
    assert tickets[0].origin == "alert"
    assert tickets[0].agent_id == "pc1"
    assert tickets[0].requester_user_id is None  # nobody asked; operator-only
    assert "disk" in tickets[0].summary

    # A repeat pass is silent, so it opens no second ticket either.
    assert await engine.evaluate_once(NOW + timedelta(minutes=5)) == []
    assert len(await ticket_store.list()) == 1
    for s in (store, events, state, ticket_store):
        await s.close()


async def test_a_failing_ticket_hook_never_breaks_alert_delivery(tmp_path) -> None:
    store, events, state, ticket_store = await _alert_stores(tmp_path, "alert_boom.sqlite")
    sent_to_channel: list[Notification] = []

    class _Notifier:
        name = "fake"

        async def send(self, note: Notification) -> None:
            sent_to_channel.append(note)

    async def _boom(_note: Notification) -> None:
        raise RuntimeError("ticket store is on fire")

    engine = AlertEngine(
        store=store,
        alert_state=state,
        event_store=events,
        registry=_Registry(),
        notifiers=[_Notifier()],
        open_ticket=_boom,
    )
    await store.insert(
        "pc1",
        (NOW - timedelta(minutes=1)).isoformat(),
        _snapshot(96.0),
        received_at=(NOW - timedelta(minutes=1)).isoformat(),
    )

    sent = await engine.evaluate_once(NOW)
    assert len(sent) == 1
    assert len(sent_to_channel) == 1  # delivered anyway
    assert len(await events.query(kind="alert")) == 1  # and recorded anyway
    for s in (store, events, state, ticket_store):
        await s.close()

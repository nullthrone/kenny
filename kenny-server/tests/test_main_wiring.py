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
not make alert delivery any less best-effort (ADR-0027).
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
from kenny_server.discord_service import SLASH_COMMANDS
from kenny_server.main import _discord_loop, build_app
from kenny_server.notify import Notification
from kenny_server.store import AlertStateStore, EventStore, TelemetryStore
from kenny_server.ticketstore import TicketStore
from kenny_server.tickets import TicketService

from support.fake_discord import FakeDiscordGateway

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


def test_assistant_exists_without_a_discord_token(tmp_path) -> None:
    """The ticket assistant only needs a usable Anthropic client.

    Independent of ``KENNY_DISCORD_BOT_TOKEN`` — a self-hoster with an API key
    but no Discord bot still gets a working ticket chat, and the dashboard
    route it powers is registered on every server that has one.
    """

    app = build_app(
        db_path=str(tmp_path / "assistant_only.sqlite"), client_factory=_FakeAnthropic
    )
    assert app.state.discord_service is None
    assert app.state.ticket_assistant is not None

    chat_routes = [
        r for r in app.router.routes if getattr(r, "path", None) == "/api/tickets/{tid}/chat/stream"
    ]
    assert len(chat_routes) == 1
    assert "POST" in (chat_routes[0].methods or set())

    with TestClient(app) as c:
        h = _bearer(app)
        created = c.post("/api/tickets", json={"title": "no discord here"}, headers=h)
        assert created.status_code == 201
        body = c.get(f"/api/tickets/{created.json()['id']}", headers=h).json()
        assert body["assistant_available"] is True
        assert body["discord_thread"] is False


def test_no_assistant_without_a_usable_anthropic_client(tmp_path) -> None:
    """A client factory that raises (no API key) disables the assistant too."""

    def _boom() -> None:
        raise RuntimeError("no ANTHROPIC_API_KEY")

    app = build_app(db_path=str(tmp_path / "no_client.sqlite"), client_factory=_boom)
    assert app.state.ticket_assistant is None
    assert app.state.discord_service is None


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


class _StubDiscordService:
    """The minimum ``_discord_loop`` needs: a gateway, a guild allowlist, and a
    ``run()`` that returns once there is nothing left to consume."""

    def __init__(self, gateway: FakeDiscordGateway, guild_ids: frozenset[str]) -> None:
        self.gateway = gateway
        self.guild_ids = guild_ids
        self.startup_error: str | None = None
        self.ran = False

    async def run(self) -> None:
        self.ran = True
        async for _ in self.gateway.events():
            pass


async def test_discord_loop_registers_the_slash_commands_on_every_allowed_guild() -> None:
    """Reproduces the v2.0.1 gap: the gateway could register commands, the
    service could dispatch them, but nothing ever called the former — so
    the slash commands never appeared in Discord regardless of config. Pins the fix:
    ``_discord_loop`` must register ``SLASH_COMMANDS`` for every guild in the
    allowlist right after the gateway connects, before it starts consuming
    events.
    """

    gateway = FakeDiscordGateway()
    service = _StubDiscordService(gateway, frozenset({"guild-1", "guild-2"}))

    await gateway.close()  # events() ends immediately, so service.run() returns
    await _discord_loop(service)

    assert {guild_id for guild_id, _ in gateway.registered_commands} == {
        "guild-1",
        "guild-2",
    }
    for _, commands in gateway.registered_commands:
        assert [c.name for c in commands] == [c.name for c in SLASH_COMMANDS]


class _RaisingRegisterGateway(FakeDiscordGateway):
    async def register_commands(self, *, guild_id: str, commands) -> None:
        raise RuntimeError("boom")


async def test_discord_loop_reaches_service_run_even_if_register_commands_raises() -> None:
    """register_commands is documented to never raise (discord_adapter.py
    swallows every failure mode, including the RuntimeError a live run
    actually hit — see its test suite), but that is a contract on the
    implementation, not the type. Pins that _discord_loop defends against a
    DiscordGateway that breaks it anyway: service.run() is what actually
    processes mentions and interactions, and losing that over a slash-command
    registration failure would be far worse than losing the commands.
    """

    gateway = _RaisingRegisterGateway()
    service = _StubDiscordService(gateway, frozenset({"guild-1"}))
    await gateway.close()  # events() ends immediately once run() is reached

    await _discord_loop(service)  # must not raise

    assert service.ran is True


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


# -- auto-ticket rules (ticket_rules.py) -------------------------------------


def test_ticket_rules_are_wired_and_a_seeded_rule_survives_a_boot(tmp_path) -> None:
    """Seed a rule through the real API, reboot the app against the same DB,
    and assert the freshly-loaded mirror (not just the store) enforces it --
    the ``ticket_rules.load()`` seam in the lifespan."""

    db_path = str(tmp_path / "ticket_rules_wiring.sqlite")
    app1 = build_app(db_path=db_path)
    with TestClient(app1) as c:
        h = _bearer(app1)
        assert app1.state.ticket_rules is not None
        resp = c.post(
            "/api/ticket-rules", headers=h,
            json={"event_type": "offline", "decision": "never"},
        )
        assert resp.status_code == 201

    app2 = build_app(db_path=db_path)
    with TestClient(app2):
        # The lifespan's ``await ticket_rules.load()`` ran before any request
        # could be served -- the mirror already reflects the seeded row.
        rules = app2.state.ticket_rules.rules()
        assert len(rules) == 1
        assert rules[0]["event_type"] == "offline"
        assert rules[0]["decision"] == "never"

        # And it is the exact mirror the alert engine consults.
        assert app2.state.alert_engine._ticket_rules is app2.state.ticket_rules

        asyncio.run(
            app2.state.store.insert(
                "pc1",
                (NOW - timedelta(hours=3)).isoformat(),
                _snapshot(50.0),
                received_at=(NOW - timedelta(hours=3)).isoformat(),
            )
        )
        opened: list[Any] = []

        async def spy_open_ticket(note: Notification) -> None:
            opened.append(note)

        app2.state.alert_engine._open_ticket = spy_open_ticket

        # An empty registry (no live connection) makes this host read offline.
        class _OfflineRegistry:
            def get(self, agent_id: str) -> Any:
                return None

        app2.state.alert_engine._registry = _OfflineRegistry()
        sent = asyncio.run(app2.state.alert_engine.evaluate_once(NOW))
        assert len(sent) == 1
        assert sent[0].event_type == "offline"
        assert opened == []  # suppressed by the seeded rule


def _alert(agent: str, *, title: str, sections: dict[str, str]) -> Notification:
    return Notification(
        title=title,
        body="body",
        agent_id=agent,
        kind="alert",
        event_type="health",
        sections=sections,
    )


def test_a_recurring_alert_notes_itself_on_the_open_ticket_instead_of_opening_another(
    tmp_path,
) -> None:
    """One open ticket per subject; the recurrence is kept, the duplicate is not.

    The measured failure this closes: a condition that keeps re-crossing a
    threshold minted a fresh ticket every time, because ``crit`` escalations
    deliberately bypass the alert cooldown (``alerting.py``). On a real
    four-host fleet that produced 38 alert tickets in a month, 34 of which were
    cancelled or never touched.
    """

    app = build_app(db_path=str(tmp_path / "dedup.sqlite"))
    with TestClient(app):
        open_ticket = app.state.alert_engine._open_ticket
        store = app.state.ticket_store

        note = _alert("pc1", title="pc1 health: crit", sections={"reliability": "crit"})
        asyncio.run(open_ticket(note))
        asyncio.run(open_ticket(note))
        asyncio.run(open_ticket(note))

        tickets = asyncio.run(store.list(limit=50))
        assert len(tickets) == 1
        # The two suppressed alerts are still on the record, on that ticket.
        events = asyncio.run(store.list_events(tickets[0].id))
        recurrences = [e for e in events if "alerted again" in (e.summary or "")]
        assert len(recurrences) == 2


def test_a_different_subject_still_opens_its_own_ticket(tmp_path) -> None:
    """Deduplication must not merge two unrelated problems on one host.

    The key is built from the notification's structured discriminators, so a
    different section -- or a different producer -- is a different subject.
    """

    app = build_app(db_path=str(tmp_path / "dedup2.sqlite"))
    with TestClient(app):
        open_ticket = app.state.alert_engine._open_ticket
        store = app.state.ticket_store

        asyncio.run(open_ticket(_alert("pc1", title="t", sections={"reliability": "crit"})))
        asyncio.run(open_ticket(_alert("pc1", title="t", sections={"disk": "crit"})))
        # Same section, different host.
        asyncio.run(open_ticket(_alert("pc2", title="t", sections={"reliability": "crit"})))

        assert len(asyncio.run(store.list(limit=50))) == 3


def test_the_dedup_key_ignores_section_ordering_but_not_the_title(tmp_path) -> None:
    """The subject is the set of sections, not the wording of the headline.

    Keying on the title would tie this identity to a display string; keying on
    an unsorted join would make it depend on dict iteration order.
    """

    app = build_app(db_path=str(tmp_path / "dedup3.sqlite"))
    with TestClient(app):
        open_ticket = app.state.alert_engine._open_ticket
        store = app.state.ticket_store

        asyncio.run(
            open_ticket(_alert("pc1", title="one wording", sections={"disk": "warn", "memory": "warn"}))
        )
        asyncio.run(
            open_ticket(_alert("pc1", title="quite another", sections={"memory": "warn", "disk": "warn"}))
        )
        assert len(asyncio.run(store.list(limit=50))) == 1


def test_a_resolved_ticket_does_not_suppress_a_fresh_one(tmp_path) -> None:
    """Once somebody has dealt with the condition, its return is news again."""

    app = build_app(db_path=str(tmp_path / "dedup4.sqlite"))
    with TestClient(app):
        open_ticket = app.state.alert_engine._open_ticket
        service = app.state.tickets
        store = app.state.ticket_store

        note = _alert("pc1", title="pc1 health: crit", sections={"reliability": "crit"})
        asyncio.run(open_ticket(note))
        first = asyncio.run(store.list(limit=50))[0]
        asyncio.run(service.transition(first.id, "resolved", actor="operator", reason="handled"))

        asyncio.run(open_ticket(note))
        assert len(asyncio.run(store.list(limit=50))) == 2


def test_human_tickets_are_never_deduplicated(tmp_path) -> None:
    """Two people asking the same question are two cases.

    ``dedup_key`` defaults to empty, and an empty key never matches -- so the
    suppression can only ever reach tickets a producer deliberately keyed.
    """

    app = build_app(db_path=str(tmp_path / "dedup5.sqlite"))
    with TestClient(app):
        service = app.state.tickets
        store = app.state.ticket_store

        for _ in range(3):
            asyncio.run(service.create(title="my PC is slow", origin="discord", agent_id="pc1"))
        assert len(asyncio.run(store.list(limit=50))) == 3
        assert asyncio.run(store.find_open_by_dedup_key("")) is None


def test_triage_is_not_wired_without_an_api_key(tmp_path, monkeypatch) -> None:
    """No key, no investigation — and specifically not one doomed per ticket.

    ``anthropic.Anthropic()`` constructs happily without ``ANTHROPIC_API_KEY``
    and only fails when it is used, so "the assistant was built" is not the same
    question as "an investigation could work". Binding triage to the former
    would schedule one failing task for every ticket ever created, including in
    every test that opens one.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = build_app(db_path=str(tmp_path / "nokey.sqlite"))
    with TestClient(app):
        assert app.state.tickets._triage is None


def test_triage_is_wired_when_a_key_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    app = build_app(db_path=str(tmp_path / "key.sqlite"))
    with TestClient(app):
        assert app.state.tickets._triage is not None


def test_triage_can_be_switched_off_while_a_key_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_TRIAGE_ENABLED", "0")
    app = build_app(db_path=str(tmp_path / "off.sqlite"))
    with TestClient(app):
        assert app.state.tickets._triage is None

"""Ticket callbacks: a steward armed on a ticket for its machine's next push (ADR-0075).

Through the real composition root (``build_app``): the alert engine opens the
ticket from a real Defender finding, the dashboard routes arm the callback, the
tunnel's own after-insert hook is what a push calls, the runner and its gate run
the steward, and the alert engine's verdict on fresh telemetry decides whether
the ticket was fixed. Only the agent's wire send and the model are scripted.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server import alert_subject
from kenny_server.main import build_app
from kenny_server.notify import Notification
from kenny_server.stewards.callbacks import CALLBACK_TTL, MAX_ATTEMPTS, checkable_sections
from kenny_server.stewards.catalog import CATALOG
from kenny_server.stewards.commands import COMMANDS_PER_DAY_SETTING
from kenny_server.stewards.spec import ArgConstraint, StewardSpec, Trigger, TriggerSet
from kenny_server.toolloop import STEWARD_VERDICT_TOOL

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

HOST = "marianne-pc"
PASSWORD = "pw-123456"

PACKAGE = "defender.repair"

#: A steward whose one change is a ``standard_change`` bound to an allowlist.
#: In this world, updating :data:`PACKAGE` is what turns Defender back on.
FIXER = StewardSpec(
    id="fixer",
    title="Fixer",
    description="Updates the allowed repair package on a named machine.",
    prompt="You update the allowed package on this machine, nothing else.",
    trigger=Trigger(kind="schedule"),
    triggers_allowed=TriggerSet(schedule=True, command=True),
    tools=frozenset({"winget_update", STEWARD_VERDICT_TOOL}),
    verdict_tool=STEWARD_VERDICT_TOOL,
    constraints=(ArgConstraint("winget_update", "id", param="packages"),),
    params=("window", "hosts", "packages"),
)

COMMAND_ON = {"schedule": {"min_interval_days": None}, "events": [], "command": True}

DEFENDER_OFF = {"status": "crit", "summary": "off", "enabled": False, "realtime_protection": False}
DEFENDER_ON = {"status": "ok", "summary": "on", "enabled": True, "realtime_protection": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_RESOLVE", "KENNY_STEWARDS_ENABLED", COMMANDS_PER_DAY_SETTING):
        monkeypatch.delenv(key, raising=False)
    # Triage would answer the alert ticket with the scripted model first.
    monkeypatch.setenv("KENNY_TRIAGE_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def _call(tool: str, args: dict[str, Any], tool_id: str = "t1") -> _Response:
    return _Response([tool_use_block(tool_id, tool, args)], "tool_use")


def _fix_run() -> list[_Response]:
    return [
        _call("winget_update", {"id": PACKAGE}),
        _call(STEWARD_VERDICT_TOOL, {"verdict": "acted", "finding": "updated",
                                     "evidence": "winget_update"}, "v1"),
        _Response([text_block("Done.")], "end_turn"),
    ]


class World:
    def __init__(self, app: Any, c: TestClient) -> None:
        self.app = app
        self.c = c
        self.sent: list[dict[str, Any]] = []
        #: Whether the machine's Defender is on: what the "fix" changes.
        self.healthy = False
        #: Whether the update turns it on (a steward that works).
        self.flush_fixes = True
        self.announced: list[Notification] = []

    @property
    def runner(self) -> Any:
        return self.app.state.stewards

    @property
    def callbacks(self) -> Any:
        return self.app.state.ticket_callbacks

    def call(self, fn: Any, *args: Any) -> Any:
        return self.c.portal.call(fn, *args)

    def live_hash(self) -> str:
        return self.call(self.runner.live_hash, "fixer")

    def online(self) -> None:
        async def send(_frame: Any) -> None:
            return None

        self.app.state.registry.mark_online(HOST, {"os": "windows"}, send)

    def offline(self) -> None:
        self.app.state.registry.get(HOST).online = False

    def snapshot(self) -> dict[str, Any]:
        return {"defender": dict(DEFENDER_ON if self.healthy else DEFENDER_OFF)}

    def settle(self) -> None:
        async def go() -> None:
            for _ in range(20):
                await self.runner.wait_previews()
                await self.callbacks.wait()
                if not self.runner._previews and not self.callbacks._tasks:
                    return
                await asyncio.sleep(0)

        self.call(go)

    def push(self) -> None:
        """A telemetry push from the machine, through the tunnel's own hook."""

        async def go() -> None:
            snap = self.snapshot()
            await self.app.state.store.insert(HOST, datetime.now(timezone.utc).isoformat(), snap)
            self.app.state.tunnel.after_insert(HOST, snap)

        self.call(go)
        self.settle()

    def alert_ticket(self) -> Any:
        """Let the alert engine see the finding; it opens the ticket."""

        async def go() -> Any:
            snap = self.snapshot()
            await self.app.state.store.insert(HOST, datetime.now(timezone.utc).isoformat(), snap)
            await self.app.state.alert_engine.evaluate_once()
            key = alert_subject.dedup_key(HOST, "health", ["defender"])
            return await self.app.state.ticket_store.find_open_by_dedup_key(key)

        ticket = self.call(go)
        assert ticket is not None, "the alert engine opened no ticket"
        return ticket

    def ticket(self, tid: str) -> Any:
        return self.call(self.app.state.tickets.get, tid)

    def notes(self, tid: str) -> list[str]:
        r = self.c.get(f"/api/tickets/{tid}/events")
        assert r.status_code == 200, r.text
        return [e.get("summary", "") for e in r.json()["events"] if e.get("kind") == "note"]

    def arm(self, tid: str, **body: Any) -> Any:
        payload = {"steward_id": "fixer", "effective_hash": self.live_hash(), **body}
        return self.c.post(f"/api/tickets/{tid}/callback", json=payload)

    def view(self, tid: str) -> dict[str, Any]:
        r = self.c.get(f"/api/tickets/{tid}/callback")
        assert r.status_code == 200, r.text
        return r.json()

    def runs(self) -> list[Any]:
        return self.call(lambda: self.runner.store.list_runs(steward_id="fixer"))


def _start(tmp_path: Any, *responses: _Response) -> World:
    script = FakeAnthropic(list(responses))
    app = build_app(db_path=str(tmp_path / "callbacks.sqlite"), client_factory=lambda: script)
    c = TestClient(app)
    c.__enter__()
    assert c.post("/setup", data={"username": "admin", "password": PASSWORD},
                  follow_redirects=False).status_code == 303
    w = World(app, c)
    w.script = script
    w.runner.catalog = MappingProxyType({**CATALOG, "fixer": FIXER})

    async def send_request(agent_id: str, tool: str, args: dict[str, Any], timeout_s: float = 0):
        w.sent.append({"agent_id": agent_id, "tool": tool, "args": dict(args)})
        if tool == "winget_update" and w.flush_fixes:
            w.healthy = True
        if tool == "telemetry_collect":
            return w.snapshot()
        return {"ok": True}

    async def announce(note: Notification) -> None:
        w.announced.append(note)

    app.state.tunnel.send_request = send_request
    w.callbacks.announce = announce
    w.online()
    w.call(lambda: w.runner.set_params("fixer", {"hosts": [HOST], "packages": [PACKAGE]},
                                             actor="admin"))
    w.call(lambda: w.runner.set_triggers("fixer", COMMAND_ON, actor="admin"))
    shown = w.live_hash()
    w.call(lambda: w.runner.set_mode("fixer", "act", actor="admin", effective_hash=shown))
    return w


@pytest.fixture
def world(tmp_path):
    made: list[World] = []

    def make(*responses: _Response) -> World:
        w = _start(tmp_path, *responses)
        made.append(w)
        return w

    yield make
    for w in made:
        w.c.__exit__(None, None, None)


def _tools(w: World) -> list[str]:
    return [s["tool"] for s in w.sent]


# -- the whole path --------------------------------------------------------------


def test_an_armed_callback_fixes_the_ticket_at_the_next_push(world) -> None:
    w = world(*_fix_run())
    ticket = w.alert_ticket()
    w.offline()

    view = w.view(ticket.id)
    assert view["sections"] == ["defender"] and view["can_arm"] is True
    assert [s["steward_id"] for s in view["stewards"]] == ["fixer"]
    assert view["stewards"][0]["allowed"] is True
    assert view["ttl_days"] == 14

    r = w.arm(ticket.id)
    assert r.status_code == 201, r.text
    armed = r.json()
    assert armed["state"] == "armed" and armed["armed_by"] == "admin"
    expires = datetime.fromisoformat(armed["expires_at"]) - datetime.fromisoformat(armed["armed_at"])
    assert expires == CALLBACK_TTL == timedelta(days=14)
    # Offline: nothing ran.
    assert w.sent == [] and w.runs() == []

    # The machine comes back and pushes telemetry: the steward runs as the
    # person who armed it, the server collects telemetry and judges it.
    w.online()
    w.push()
    assert _tools(w) == ["winget_update", "telemetry_collect"]
    runs = w.runs()
    assert len(runs) == 1 and runs[0].trigger == "command:admin"
    assert runs[0].initiated_by == "admin" and runs[0].mode == "act"

    after = w.ticket(ticket.id)
    assert after.state in ("resolved", "closed")
    callback = w.view(ticket.id)["callback"]
    assert callback["state"] == "succeeded" and callback["attempts"] == 1
    assert callback["last_run_id"] == runs[0].id
    notes = w.notes(ticket.id)
    assert any("handed this ticket to the steward Fixer" in n for n in notes)
    assert any(n.startswith("Fixed by the steward fixer") for n in notes)
    assert w.announced == []


def test_armed_while_online_it_starts_at_once_and_briefs_from_records(world) -> None:
    w = world(*_fix_run())
    ticket = w.alert_ticket()
    assert w.arm(ticket.id).status_code == 201
    w.settle()  # online: it starts on the telemetry already sent
    assert len(w.runs()) == 1
    assert w.view(ticket.id)["callback"]["state"] == "succeeded"
    first = w.script.messages.calls[0]["messages"][0]["content"]
    text = first if isinstance(first, str) else " ".join(
        b.get("text", "") for b in first if isinstance(b, dict))
    assert f"ticket #{ticket.number}" in text and "defender" in text
    # The alert's free-text title never reaches the model.
    assert ticket.title not in text


def test_a_steward_that_does_not_fix_spends_its_attempts_then_tells_the_channel(world) -> None:
    w = world(*(_fix_run() * MAX_ATTEMPTS))
    w.flush_fixes = False
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    w.online()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        w.push()
        callback = w.view(ticket.id)["callback"]
        assert callback["attempts"] == attempt
        if attempt < MAX_ATTEMPTS:
            assert callback["state"] == "armed"
            assert w.announced == []
    assert callback["state"] == "failed"
    assert w.ticket(ticket.id).state not in ("resolved", "closed")
    assert len(w.runs()) == MAX_ATTEMPTS
    # One push to the channel, which opens no ticket and starts nothing.
    assert len(w.announced) == 1
    note = w.announced[0]
    assert note.kind == "steward" and note.route == "push" and note.agent_id == HOST
    assert "could not fix" in note.title and f"#{ticket.number}" in note.title
    assert any("did not fix this ticket" in n for n in w.notes(ticket.id))
    # Another push starts nothing more.
    w.push()
    assert len(w.runs()) == MAX_ATTEMPTS


def test_a_steward_switched_off_after_arming_ends_the_callback(world) -> None:
    w = world(*_fix_run())
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    w.call(lambda: w.runner.set_mode("fixer", "off", actor="admin"))
    # ``off`` is a refusal no wait lifts: the consent was given to a steward in act.
    w.online()
    w.push()
    callback = w.view(ticket.id)["callback"]
    assert callback["state"] == "failed" and callback["attempts"] == 0
    assert w.runs() == [] and len(w.announced) == 1


def test_a_global_switch_off_leaves_it_armed(world) -> None:
    w = world(*_fix_run())
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    original = w.runner.enabled
    w.runner.enabled = lambda: False
    w.online()
    w.push()
    callback = w.view(ticket.id)["callback"]
    assert callback["state"] == "armed" and callback["attempts"] == 0
    assert "switched off" in callback["last_error"]
    assert w.announced == []
    w.runner.enabled = original
    w.push()
    assert w.view(ticket.id)["callback"]["state"] == "succeeded"


def test_editing_the_steward_after_arming_ends_the_callback(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    # A parameter change moves the effective hash: the steward drops to shadow.
    w.call(lambda: w.runner.set_params("fixer", {"hosts": [HOST, "other"], "packages": [PACKAGE]},
                                             actor="admin"))
    w.online()
    w.push()
    callback = w.view(ticket.id)["callback"]
    assert callback["state"] == "failed"
    assert w.runs() == [] and w.sent == []
    assert len(w.announced) == 1


def test_it_expires_after_fourteen_days_without_the_machine(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    later = datetime.now(timezone.utc) + CALLBACK_TTL + timedelta(minutes=1)
    w.callbacks.now = lambda: later
    assert w.call(w.callbacks.sweep) == 1
    callback = w.view(ticket.id)["callback"]
    assert callback["state"] == "expired"
    assert len(w.announced) == 1 and "expired" in w.announced[0].body
    assert w.runs() == []


def test_a_ticket_closed_by_a_person_ends_the_callback_quietly(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    w.call(lambda: w.app.state.tickets.transition(ticket.id, "cancelled", actor="operator"))
    w.online()
    w.push()
    assert w.view(ticket.id)["callback"]["state"] == "cancelled"
    assert w.runs() == [] and w.announced == []


def test_withdrawing_it_stops_the_run(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    r = w.c.delete(f"/api/tickets/{ticket.id}/callback")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "cancelled" and r.json()["ended_by"] == "admin"
    w.online()
    w.push()
    assert w.runs() == []
    # It can be armed again; the old row stays on the record.
    assert w.c.get(f"/api/tickets/{ticket.id}/callback").json()["can_arm"] is True


# -- what may be armed, and by whom ---------------------------------------------


def test_a_second_callback_on_one_ticket_is_refused(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    w.offline()
    assert w.arm(ticket.id).status_code == 201
    r = w.arm(ticket.id)
    assert r.status_code == 409 and r.json()["code"] == "already_armed"


def test_only_a_superuser_at_a_browser_arms_one(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    users = {u["username"]: u for u in w.c.get("/api/users").json()["users"]}
    pat = w.c.post(f"/api/users/{users['admin']['id']}/pats", json={"label": "t"}).json()["token"]
    shown = w.live_hash()
    w.c.cookies.clear()
    r = w.c.post(f"/api/tickets/{ticket.id}/callback",
                 json={"steward_id": "fixer", "effective_hash": shown},
                 headers={"Authorization": f"Bearer {pat}"})
    assert r.status_code == 403, r.text
    assert "superuser signed in to the dashboard" in r.json()["error"]


def test_a_stale_hash_is_refused_at_arming(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    r = w.arm(ticket.id, effective_hash="0" * 64)
    assert r.status_code == 409 and r.json()["code"] == "hash_stale"


def test_a_ticket_a_person_wrote_cannot_be_handed_to_a_steward(world) -> None:
    w = world()

    async def create() -> Any:
        return await w.app.state.tickets.create(
            title="The printer is slow", origin="dashboard", requester_user_id=None,
            agent_id=HOST, priority="normal", category=None, summary="", actor="operator")

    ticket = w.call(create)
    view = w.view(ticket.id)
    assert view["can_arm"] is False and view["stewards"] == []
    r = w.arm(ticket.id)
    assert r.status_code == 400 and r.json()["code"] == "not_checkable"


def test_checkable_sections_reads_only_health_subjects_of_the_ticket_machine() -> None:
    class T:
        def __init__(self, key: str, agent: str | None = HOST) -> None:
            self.dedup_key = key
            self.agent_id = agent

    assert checkable_sections(T(alert_subject.dedup_key(HOST, "health", ["defender"]))) == ["defender"]
    assert checkable_sections(T(alert_subject.dedup_key(HOST, "offline", []))) is None
    assert checkable_sections(T(alert_subject.dedup_key(HOST, "change", ["software"]))) is None
    assert checkable_sections(T(alert_subject.dedup_key("other", "health", ["defender"]))) is None
    assert checkable_sections(T("")) is None


def test_a_running_callback_left_by_a_dead_process_is_armed_again(world) -> None:
    w = world()
    ticket = w.alert_ticket()
    w.offline()
    armed = w.arm(ticket.id).json()
    store = w.app.state.callback_store
    w.call(lambda: store.move(armed["id"], ("armed",), "running",
                              now=datetime.now(timezone.utc)))
    assert w.call(store.recover) == 1
    callback = w.view(ticket.id)["callback"]
    assert callback["state"] == "armed" and callback["attempts"] == 0

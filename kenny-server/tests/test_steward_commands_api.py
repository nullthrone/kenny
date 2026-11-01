"""Command runs: a person starts a steward now, and it may act (ADR-0073 rule 3).

Through the real composition root (``build_app``): the guard, the session and
token resolution, the runner, its gate, the real ``ToolExecutor`` and the
authorization store are the ones the server runs. Only the agent's wire send is
replaced, and the catalog gains one test steward with a ``normal_change`` (an
allowlisted install), so "the run consumed the authorization" means the grant
travelled from the store, through the gate, to the run's action.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server.auth import COOKIE_NAME
from kenny_server.main import build_app
from kenny_server.stewards.catalog import CATALOG
from kenny_server.stewards.commands import COMMANDS_PER_DAY_SETTING, COMPUTED_AT_RUN_START
from kenny_server.stewards.runner import BREAKER_TRIGGER_PREFIXES
from kenny_server.stewards.spec import ArgConstraint, StewardSpec, Trigger, TriggerSet
from kenny_server.toolloop import STEWARD_VERDICT_TOOL
from kenny_server.urls import mcp_resource_url

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

HOST = "pc-kid"
OTHER = "pc-den"
SEVENZIP = "7zip.7zip"
LEGACY_TOKEN = "legacy-shared-token"
PASSWORD = "pw-123456"

#: A scheduled steward a person may also start by command, whose one change is
#: a ``normal_change`` bound to an allowlist parameter.
INSTALLER = StewardSpec(
    id="installer",
    title="Installer",
    description="Installs the packages the household allowed.",
    prompt="You install the packages this household allowed, nothing else.",
    trigger=Trigger(kind="schedule"),
    triggers_allowed=TriggerSet(schedule=True, command=True),
    tools=frozenset({"winget_list", "winget_install", STEWARD_VERDICT_TOOL}),
    verdict_tool=STEWARD_VERDICT_TOOL,
    constraints=(ArgConstraint("winget_install", "id", param="packages"),),
    params=("window", "hosts", "packages"),
)

COMMAND_ON = {"schedule": {"min_interval_days": None}, "events": [], "command": True}
COMMAND_OFF = {"schedule": {"min_interval_days": None}, "events": [], "command": False}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("KENNY_TRIAGE_ENABLED", "KENNY_TRIAGE_RESOLVE", "KENNY_STEWARDS_ENABLED",
                COMMANDS_PER_DAY_SETTING):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", LEGACY_TOKEN)


class _Script:
    """One scripted model, shared by every AI feature the app builds."""

    def __init__(self, *responses: _Response) -> None:
        self.client = FakeAnthropic(list(responses))

    def __call__(self) -> FakeAnthropic:
        return self.client


def _call(tool: str, args: dict[str, Any], tool_id: str = "t1") -> _Response:
    return _Response([tool_use_block(tool_id, tool, args)], "tool_use")


def _verdict(verdict: str = "acted") -> _Response:
    return _call(STEWARD_VERDICT_TOOL, {
        "verdict": verdict, "finding": "installed what was allowed", "evidence": "winget_list"}, "v1")


def _done(text: str = "Done.") -> _Response:
    return _Response([text_block(text)], "end_turn")


def _install_run() -> list[_Response]:
    return [_call("winget_install", {"id": SEVENZIP}), _verdict("acted"), _done()]


class World:
    """The app, a signed-in client, the credentials, and what reached the agent."""

    def __init__(self, app: Any, c: TestClient, pats: dict[str, str]) -> None:
        self.app = app
        self.c = c
        self.pats = pats
        self.sent: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None

    @property
    def runner(self) -> Any:
        return self.app.state.stewards

    def call(self, fn: Any, *args: Any) -> Any:
        return self.c.portal.call(fn, *args)

    def login(self, username: str = "admin") -> None:
        self.c.cookies.clear()
        r = self.c.post("/login", data={"username": username, "password": PASSWORD},
                        follow_redirects=False)
        assert r.status_code in (302, 303), r.text

    def live_hash(self, steward_id: str = "installer") -> str:
        return self.call(self.runner.live_hash, steward_id)

    def set_params(self, steward_id: str = "installer", **params: Any) -> None:
        self.call(lambda: self.runner.set_params(steward_id, params, actor="admin"))

    def set_triggers(self, triggers: dict[str, Any], steward_id: str = "installer") -> None:
        self.call(lambda: self.runner.set_triggers(steward_id, triggers, actor="admin"))

    def set_mode(self, mode: str, steward_id: str = "installer") -> None:
        shown = self.live_hash(steward_id) if mode == "act" else None
        self.call(lambda: self.runner.set_mode(steward_id, mode, actor="admin", effective_hash=shown))

    def grant(self, tool: str = "winget_install", scope: Any = None, attempts: int = 2) -> Any:
        shown = self.live_hash()
        return self.call(lambda: self.runner.grant(
            "installer", tool=tool, scope=scope or [HOST], max_attempts_per_day=attempts,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7), actor="admin",
            effective_hash=shown))

    def online(self, host: str = HOST, os_name: str = "windows") -> None:
        async def send(_frame: Any) -> None:
            return None

        self.app.state.registry.mark_online(host, {"os": os_name}, send)

    def offline(self, host: str = HOST) -> None:
        self.app.state.registry.get(host).online = False

    def wait(self) -> None:
        self.call(self.runner.wait_previews)

    def command(self, steward_id: str = "installer", headers: dict[str, str] | None = None,
                **body: Any) -> Any:
        payload = {"mode": "act", "host_id": HOST, "effective_hash": self.live_hash(steward_id)}
        payload.update(body)
        payload = {k: v for k, v in payload.items() if v is not ...}
        return self.c.post(f"/api/stewards/{steward_id}/runs", json=payload, headers=headers or {})

    def readiness(self, steward_id: str = "installer", host: str | None = HOST,
                  headers: dict[str, str] | None = None) -> dict[str, Any]:
        query = f"?host_id={host}" if host else ""
        r = self.c.get(f"/api/stewards/{steward_id}/command{query}", headers=headers or {})
        assert r.status_code == 200, r.text
        return r.json()

    def runs(self, steward_id: str = "installer") -> list[Any]:
        return self.call(lambda: self.runner.store.list_runs(steward_id=steward_id))

    def logs(self) -> list[dict[str, Any]]:
        return self.call(lambda: self.app.state.event_store.query(kind="log", limit=500))


def _pat_for(c: TestClient, username: str) -> str:
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    return c.post(f"/api/users/{users[username]['id']}/pats", json={"label": "t"}).json()["token"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _start(tmp_path: Any, *responses: _Response, ready: bool = True):
    """The app with ``installer`` in its catalog; ready to act on ``HOST`` unless told not to."""

    app = build_app(db_path=str(tmp_path / "commands.sqlite"), client_factory=_Script(*responses))
    c = TestClient(app)
    c.__enter__()
    assert c.post("/setup", data={"username": "admin", "password": PASSWORD},
                  follow_redirects=False).status_code == 303
    assert c.post("/api/users", json={"username": "op", "password": PASSWORD,
                                      "role": "operator"}).status_code == 201
    pats = {name: _pat_for(c, name) for name in ("op", "admin")}
    w = World(app, c, pats)
    w.runner.catalog = MappingProxyType({**CATALOG, "installer": INSTALLER})

    async def send_request(agent_id: str, tool: str, args: dict[str, Any], timeout_s: float = 0):
        if w.gate is not None:
            await w.gate.wait()
        w.sent.append({"agent_id": agent_id, "tool": tool, "args": dict(args)})
        return {"ok": True, "tool": tool, "packages": [], "log": "done"}

    app.state.tunnel.send_request = send_request
    if ready:
        w.online()
        w.set_params(hosts=[HOST], packages=[SEVENZIP])
        w.set_triggers(COMMAND_ON)
        w.set_mode("act")
    w.login()
    return w


@pytest.fixture
def world_factory(tmp_path):
    made: list[World] = []

    def make(*responses: _Response, ready: bool = True) -> World:
        w = _start(tmp_path, *responses, ready=ready)
        made.append(w)
        return w

    yield make
    for w in made:
        w.c.__exit__(None, None, None)


# -- who may start a run that acts -----------------------------------------------


def _oauth_token(w: World) -> str:
    async def mint() -> str:
        client = await w.app.state.oauth_store.register_client(["http://localhost:7777/cb"])
        admin = await w.app.state.user_store.get_by_username("admin")
        access, _refresh, _family = await w.app.state.oauth_store.issue_token_pair(
            client_id=client["client_id"], user_id=admin["id"], resource=mcp_resource_url())
        return access

    return w.call(mint)


@pytest.mark.parametrize("kind", ["pat", "oauth", "legacy_bearer", "legacy_cookie", "operator_browser"])
def test_only_a_superuser_at_a_browser_starts_a_run_that_acts(world_factory, kind: str) -> None:
    w = world_factory()
    shown = w.live_hash()
    w.c.cookies.clear()
    headers: dict[str, str] = {}
    if kind == "pat":
        headers = _h(w.pats["admin"])
    elif kind == "oauth":
        headers = _h(_oauth_token(w))
        # The token is a working credential: reads pass.
        assert w.c.get("/api/stewards", headers=headers).status_code == 200
    elif kind == "legacy_bearer":
        headers = _h(LEGACY_TOKEN)
    elif kind == "legacy_cookie":
        w.c.cookies.set(COOKIE_NAME, LEGACY_TOKEN)
    else:
        w.login("op")
    r = w.command(headers=headers, effective_hash=shown)
    assert r.status_code == 403, (kind, r.text)
    assert "superuser signed in to the dashboard" in r.json()["error"]
    # The readiness view says the same, and nothing was started.
    assert w.readiness(headers=headers)["run"]["allowed"] is False
    assert w.runs() == [] and w.sent == []


def test_a_superuser_at_a_browser_runs_it_and_it_consumes_the_authorization(world_factory) -> None:
    w = world_factory(*_install_run())
    grant = w.grant(attempts=2)
    ready = w.readiness()
    assert ready["run"] == {"allowed": True, "reason": None, "code": None}
    [tool] = ready["tools"]
    assert tool["tool"] == "winget_install" and tool["tier"] == "normal_change"
    assert tool["values"] == {"id": [SEVENZIP]}
    assert tool["authorization"]["id"] == grant.id and tool["authorization"]["remaining_today"] == 2

    r = w.command(effective_hash=ready["effective_hash"])
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["steward_id"], body["host_id"], body["mode"]) == ("installer", HOST, "act")
    assert body["trigger"] == "command:admin" and body["status"] == "running"
    w.wait()

    run = w.call(lambda: w.runner.store.get_run(body["run_id"]))
    assert (run.status, run.mode, run.verdict) == ("completed", "act", "acted")
    assert run.trigger == "command:admin" and run.initiated_by == "admin"
    assert w.sent == [{"agent_id": HOST, "tool": "winget_install", "args": {"id": SEVENZIP}}]
    [action] = run.actions
    assert action["tool"] == "winget_install" and action["authorization_id"] == grant.id
    assert action["ok"] is True and run.recommendations == []
    # One attempt spent, and the readiness view shows it.
    assert w.readiness()["tools"][0]["authorization"]["remaining_today"] == 1
    listed = w.c.get("/api/stewards/installer/authorizations").json()["authorizations"]
    assert listed[0]["attempts_last_24h"] == {HOST: 1}
    # Who started it is on the event log.
    assert any(
        "command run started by admin on pc-kid" in row["message"] for row in w.logs()
    )
    # The circuit breaker reads only runs the server started.
    assert not run.trigger.startswith(BREAKER_TRIGGER_PREFIXES)
    assert w.call(lambda: w.runner.breaker_history("installer")) == []


def test_without_an_authorization_the_change_is_a_recommendation(world_factory) -> None:
    w = world_factory(*_install_run())
    ready = w.readiness()
    assert ready["run"]["allowed"] is True
    assert ready["tools"][0]["authorization"] is None
    r = w.command()
    assert r.status_code == 202, r.text
    w.wait()
    run = w.call(lambda: w.runner.store.get_run(r.json()["run_id"]))
    assert run.status == "completed" and run.error is None
    assert run.actions == [] and w.sent == []
    assert [rec["tool"] for rec in run.recommendations] == ["winget_install"]


# -- what refuses it ---------------------------------------------------------------


def test_a_shadow_steward_is_refused_and_the_preview_still_runs(world_factory) -> None:
    w = world_factory(_done("Looked."))
    w.set_mode("shadow")
    r = w.command()
    assert r.status_code == 409
    assert r.json()["error"] == "steward is in shadow; use preview or promote"
    ready = w.readiness()
    assert ready["run"]["code"] == "not_in_act" and ready["preview"]["allowed"] is True
    # The preview route is what it was: no mode, any operator credential.
    r = w.c.post("/api/stewards/installer/runs", json={"host_id": HOST}, headers=_h(w.pats["op"]))
    assert r.status_code == 202 and r.json()["mode"] == "shadow"
    assert r.json()["trigger"] == "preview:op"
    w.wait()


def test_the_command_trigger_must_be_on(world_factory) -> None:
    w = world_factory()
    w.set_triggers(COMMAND_OFF)
    w.set_mode("act")
    r = w.command()
    assert r.status_code == 400 and "command trigger off" in r.json()["error"]
    assert w.readiness()["run"]["code"] == "command_off"


def test_a_ticket_surface_steward_is_refused(world_factory) -> None:
    w = world_factory()
    r = w.command("triage")
    assert r.status_code == 400 and "ticket" in r.json()["error"]


def test_the_global_switch_refuses_it(world_factory) -> None:
    w = world_factory()
    w.call(lambda: w.app.state.settings.set("KENNY_STEWARDS_ENABLED", "0"))
    assert w.command().status_code == 409
    assert w.readiness()["run"]["code"] == "stewards_off"


def test_the_hash_the_person_was_shown(world_factory) -> None:
    w = world_factory()
    r = w.command(effective_hash=...)
    assert r.status_code == 400 and "effective_hash" in r.json()["error"]
    r = w.command(effective_hash="0" * 64)
    assert r.status_code == 409 and "changed since it was shown" in r.json()["error"]
    assert w.runs() == []


def test_unknown_mode_and_bad_host(world_factory) -> None:
    w = world_factory()
    r = w.c.post("/api/stewards/installer/runs", json={"mode": "now", "host_id": HOST})
    assert r.status_code == 400
    assert w.command(host_id=7).status_code == 400
    r = w.command(host_id="nobody")
    assert r.status_code == 400 and "no such machine" in r.json()["error"]
    r = w.command(host_id=None)
    assert r.status_code == 400 and "name one" in r.json()["error"]


def test_the_host_must_be_among_the_stewards_hosts(world_factory) -> None:
    w = world_factory()
    w.online(OTHER)
    w.set_params(hosts=[HOST], packages=[SEVENZIP])
    w.set_mode("act")
    r = w.command(host_id=OTHER)
    assert r.status_code == 400 and "not among" in r.json()["error"]
    assert w.readiness(host=OTHER)["run"]["code"] == "host_not_named"
    w.set_params(hosts=[], packages=[SEVENZIP])
    w.set_mode("act")
    r = w.command()
    assert r.status_code == 400 and "names no machine" in r.json()["error"]
    assert w.readiness()["run"]["code"] == "no_hosts"


def test_the_host_must_be_online_and_run_a_supported_os(world_factory) -> None:
    w = world_factory()
    w.offline()
    assert w.command().status_code == 409
    assert w.readiness()["run"]["code"] == "host_offline"
    w.online(os_name="linux")
    r = w.command()
    assert r.status_code == 400 and "linux" in r.json()["error"]
    assert w.readiness()["run"]["code"] == "os_unsupported"


def test_a_server_only_steward_takes_no_host(world_factory) -> None:
    w = world_factory(_done("Nothing unused."))
    w.set_triggers({"schedule": {"min_interval_days": 28}, "events": [], "command": True},
                   steward_id="config_hygiene")
    w.set_mode("act", steward_id="config_hygiene")
    r = w.command("config_hygiene")
    assert r.status_code == 400 and "leave the host out" in r.json()["error"]
    ready = w.readiness("config_hygiene", host=None)
    assert ready["run"]["allowed"] is True
    removal = {t["tool"]: t for t in ready["tools"]}["reliability_suppression_remove"]
    assert removal["values"] == {"rule_id": COMPUTED_AT_RUN_START}
    r = w.command("config_hygiene", host_id=None)
    assert r.status_code == 202, r.text
    assert r.json()["host_id"] is None and r.json()["mode"] == "act"
    w.wait()
    assert any(
        "command run started by admin on the server" in row["message"] for row in w.logs()
    )


def test_one_manual_run_per_steward_and_host_at_a_time(world_factory) -> None:
    w = world_factory(_call("winget_list", {}), _verdict("clean"), _done(),
                      _call("winget_list", {}), _verdict("clean"), _done())
    w.gate = w.call(_new_event)
    first = w.command()
    assert first.status_code == 202, first.text
    # A second command run, and a preview, of the same steward on the same host wait.
    second = w.command()
    assert second.status_code == 409 and "already running" in second.json()["error"]
    preview = w.c.post("/api/stewards/installer/runs", json={"host_id": HOST})
    assert preview.status_code == 409
    assert w.readiness()["run"]["code"] == "manual_run_running"
    assert w.readiness()["preview"]["allowed"] is False
    w.call(w.gate.set)
    w.wait()
    again = w.command()
    assert again.status_code == 202, again.text
    w.wait()


async def _new_event() -> asyncio.Event:
    return asyncio.Event()


def test_the_daily_command_limit_is_per_steward(world_factory) -> None:
    w = world_factory(_call("winget_list", {}), _verdict("clean"), _done())
    w.call(lambda: w.app.state.settings.set(COMMANDS_PER_DAY_SETTING, "1"))
    assert w.command().status_code == 202
    w.wait()
    r = w.command()
    assert r.status_code == 409 and "the limit is 1" in r.json()["error"]
    assert w.readiness()["run"]["code"] == "command_limit"
    # Previews keep their own limit.
    assert w.readiness()["preview"]["allowed"] is True


# -- the quick actions ---------------------------------------------------------------


def test_the_quick_actions_for_a_host(world_factory) -> None:
    w = world_factory()
    r = w.c.get(f"/api/stewards/commands?host_id={HOST}")
    assert r.status_code == 200
    listed = {s["steward_id"]: s for s in r.json()["stewards"]}
    assert set(listed) == {"installer"}
    assert listed["installer"] == {
        "steward_id": "installer",
        "title": "Installer",
        "summary": INSTALLER.description,
        "mode": "act",
        "preview_allowed": True,
        "run_allowed": True,
    }
    # An operator sees it too, but may only preview.
    w.c.cookies.clear()
    op = w.c.get(f"/api/stewards/commands?host_id={HOST}", headers=_h(w.pats["op"]))
    [entry] = op.json()["stewards"]
    assert (entry["preview_allowed"], entry["run_allowed"]) == (True, False)
    w.login()
    # Not a host it names; and with the command trigger off it is gone.
    w.online(OTHER)
    assert w.c.get(f"/api/stewards/commands?host_id={OTHER}").json()["stewards"] == []
    w.set_triggers(COMMAND_OFF)
    assert w.c.get(f"/api/stewards/commands?host_id={HOST}").json()["stewards"] == []
    # Server-only stewards are listed for no host.
    w.set_triggers({"schedule": {"min_interval_days": 28}, "events": [], "command": True},
                   steward_id="config_hygiene")
    none = w.c.get("/api/stewards/commands").json()["stewards"]
    assert [s["steward_id"] for s in none] == ["config_hygiene"]
    assert none[0]["run_allowed"] is False  # in shadow


def test_readiness_and_quick_actions_are_operator_reads(world_factory) -> None:
    w = world_factory()
    assert w.c.get("/api/stewards/nobody/command").status_code == 404
    w.c.cookies.clear()
    assert w.c.get("/api/stewards/commands").status_code == 401


# -- require_idle -----------------------------------------------------------------------

FIREFOX = "Mozilla.Firefox"


def _patch_ready(w: World, *, signed_in: Any, require_idle: bool | None = None) -> None:
    """``patch`` (which declares ``require_idle``) ready to act on ``HOST``; the host answers idle as told."""

    async def send_request(agent_id: str, tool: str, args: dict[str, Any], timeout_s: float = 0):
        w.sent.append({"agent_id": agent_id, "tool": tool, "args": dict(args)})
        if tool == "remotehelp_status":
            if isinstance(signed_in, Exception):
                raise signed_in
            return {"interactive_session": signed_in}
        return {"ok": True, "tool": tool, "packages": [], "log": "done"}

    w.app.state.tunnel.send_request = send_request
    params: dict[str, Any] = {"hosts": [HOST], "packages": [FIREFOX]}
    if require_idle is not None:
        params["require_idle"] = require_idle
    w.set_params("patch", **params)
    w.set_triggers(COMMAND_ON, steward_id="patch")
    w.set_mode("act", steward_id="patch")


def test_a_command_run_honours_require_idle(world_factory) -> None:
    w = world_factory(_call("winget_list", {}), _verdict("clean"), _done())
    _patch_ready(w, signed_in=True)
    r = w.command("patch")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "host_not_idle"
    assert body["error"] == (
        f"somebody is signed in to {HOST}; this steward runs only when it is idle"
    )
    ready = w.readiness("patch")
    assert ready["run"]["code"] == "host_not_idle" and HOST in ready["run"]["reason"]
    assert w.runs("patch") == []
    # Asked of the machine, as the server, not the model.
    assert {s["tool"] for s in w.sent} == {"remotehelp_status"}
    # A machine that cannot say is not idle either.
    _patch_ready(w, signed_in=None)
    r = w.command("patch")
    assert r.status_code == 409 and "could not tell" in r.json()["error"]
    # Nobody signed in: it starts.
    _patch_ready(w, signed_in=False)
    assert w.readiness("patch")["run"]["allowed"] is True
    r = w.command("patch")
    assert r.status_code == 202, r.text
    w.wait()


def test_require_idle_off_lets_a_command_run_start_with_somebody_signed_in(world_factory) -> None:
    w = world_factory(_call("winget_list", {}), _verdict("clean"), _done())
    _patch_ready(w, signed_in=True, require_idle=False)
    r = w.command("patch")
    assert r.status_code == 202, r.text
    w.wait()
    assert "remotehelp_status" not in {s["tool"] for s in w.sent}


# -- a host steward without hosts is never run on a machine a person names -----------------


def test_a_host_steward_without_a_hosts_parameter_is_refused(world_factory) -> None:
    """``check_dispatchable`` refuses such a spec at load; the route refuses it too."""

    from dataclasses import replace

    w = world_factory(*_install_run())
    loose = replace(
        INSTALLER,
        trigger=Trigger(kind="event", event="alert:offline"),
        triggers_allowed=TriggerSet(events=frozenset({"alert:offline"}), command=True),
        params=("packages",),
    )
    w.runner.catalog = MappingProxyType({**CATALOG, "installer": loose})
    w.set_params(packages=[SEVENZIP])
    w.set_triggers({"schedule": None, "events": [], "command": True})
    w.set_mode("act")
    r = w.command()
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "no_hosts_param"
    assert w.readiness()["run"]["code"] == "no_hosts_param"
    assert w.runs() == [] and w.sent == []


# -- the hash the person was shown is the hash the run starts with ------------------------------


def test_a_command_run_whose_hash_moves_before_it_starts_is_refused(world_factory) -> None:
    w = world_factory(*_install_run())
    w.grant()
    shown = w.live_hash()
    original = w.runner.run_generic

    async def edited_first(*a: Any, **k: Any) -> Any:
        # A parameter edit lands after the route's checks, before the run starts.
        await w.runner.set_params(
            "installer", {"hosts": [HOST], "packages": [SEVENZIP, "Other.Pkg"]}, actor="root")
        await w.runner.set_mode(
            "installer", "act", actor="root", effective_hash=await w.runner.live_hash("installer"))
        return await original(*a, **k)

    w.runner.run_generic = edited_first
    r = w.command(effective_hash=shown)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "hash_stale"
    w.wait()
    assert w.runs() == [] and w.sent == []


# -- consent writes come from the dashboard itself, as JSON ---------------------------------


EVIL = "http://evil.example:3000"


def _form_shaped(payload: dict[str, Any]) -> bytes:
    """What ``<form enctype="text/plain">`` posts when its one field is named to make JSON."""

    import json

    body = json.dumps({**payload, "x": "="})
    return body.encode()


def test_a_text_plain_cross_site_form_cannot_start_a_run_that_acts(world_factory) -> None:
    w = world_factory(*_install_run())
    payload = {"mode": "act", "host_id": HOST, "effective_hash": w.live_hash()}
    for headers in (
        {"Content-Type": "text/plain", "Origin": EVIL},
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Origin": EVIL},
        {"Content-Type": "application/json", "Sec-Fetch-Site": "same-site"},
        {"Content-Type": "application/json", "Origin": "null"},
    ):
        r = w.c.post("/api/stewards/installer/runs", content=_form_shaped(payload), headers=headers)
        assert r.status_code in (403, 415), (headers, r.status_code, r.text)
    w.wait()
    assert w.runs() == [] and w.sent == []
    # The dashboard's own request: same origin, JSON.
    r = w.c.post("/api/stewards/installer/runs", json=payload,
                 headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 202, r.text
    w.wait()


def _consent_writes(w: World) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every write that needs a person at a browser: ``(method, path, JSON body or None)``."""

    shown = w.live_hash()
    grant = w.grant()
    return [
        ("PUT", "/api/stewards/installer/mode", {"mode": "act", "effective_hash": shown}),
        ("POST", "/api/stewards/installer/authorizations", {
            "tool": "winget_install", "scope": [HOST], "max_attempts_per_day": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            "effective_hash": shown}),
        ("DELETE", f"/api/stewards/installer/authorizations/{grant.id}", None),
        ("PUT", "/api/stewards/installer/params", {"params": {"hosts": [HOST, OTHER]}}),
        ("PUT", "/api/stewards/installer/triggers", {"triggers": COMMAND_OFF, "effective_hash": shown}),
        ("POST", "/api/stewards/installer/runs", {"mode": "act", "host_id": HOST, "effective_hash": shown}),
        ("PUT", "/api/settings/KENNY_STEWARDS_ENABLED", {"value": "1"}),
        ("DELETE", "/api/settings/KENNY_STEWARDS_ENABLED", None),
    ]


def test_every_consent_write_refuses_another_origin_and_a_body_that_is_not_json(world_factory) -> None:
    import json

    w = world_factory()
    before = w.call(lambda: w.runner.overview())
    for method, path, body in _consent_writes(w):
        raw = None if body is None else json.dumps(body).encode()
        for headers, status in (
            ({"Origin": EVIL}, 403),
            ({"Origin": "https://testserver"}, 403),  # another scheme is another origin
            ({"Sec-Fetch-Site": "same-site"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403),
        ):
            sent = {**headers, "Content-Type": "application/json"} if raw is not None else headers
            r = w.c.request(method, path, content=raw, headers=sent)
            assert r.status_code == status, (method, path, headers, r.status_code, r.text)
        if raw is not None:
            for media in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
                r = w.c.request(method, path, content=raw, headers={"Content-Type": media})
                assert r.status_code == 415, (method, path, media, r.status_code, r.text)
            r = w.c.request(method, path, content=raw)
            assert r.status_code == 415, (method, path, "no content type", r.text)
    w.wait()
    # Nothing moved: same configuration, same mode, the grant still live, no run.
    after = w.call(lambda: w.runner.overview())
    assert [(s["id"], s["mode"], s["effective_hash"]) for s in after] == [
        (s["id"], s["mode"], s["effective_hash"]) for s in before
    ]
    assert w.runs() == []
    listed = w.c.get("/api/stewards/installer/authorizations").json()["authorizations"]
    assert [a["status"] for a in listed] == ["live"]


def test_the_dashboards_own_requests_still_pass(world_factory) -> None:
    """Same origin (as the request's host, or as ``KENNY_PUBLIC_URL``), JSON: accepted."""

    w = world_factory()
    own = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    r = w.c.put("/api/stewards/installer/params",
                json={"params": {"hosts": [HOST], "packages": [SEVENZIP, "Other.Pkg"]}},
                headers=own)
    assert r.status_code == 200, r.text
    r = w.c.put("/api/stewards/installer/mode",
                json={"mode": "act", "effective_hash": r.json()["effective_hash"]},
                headers={"Content-Type": "application/json; charset=utf-8", **own})
    assert r.status_code == 200 and r.json()["mode"] == "act", r.text
    # A request a person typed (``Sec-Fetch-Site: none``) and one without either header.
    r = w.c.put("/api/stewards/installer/mode", json={"mode": "shadow"},
                headers={"Sec-Fetch-Site": "none"})
    assert r.status_code == 200, r.text
    r = w.c.put("/api/stewards/installer/mode", json={"mode": "shadow"})
    assert r.status_code == 200, r.text


def test_the_public_url_is_an_own_origin(world_factory, monkeypatch) -> None:
    """Behind a TLS proxy the request's scheme can be http while the page's is https."""

    w = world_factory()
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.org/")
    r = w.c.put("/api/stewards/installer/mode", json={"mode": "shadow"},
                headers={"Origin": "https://kenny.example.org"})
    assert r.status_code == 200, r.text
    r = w.c.put("/api/stewards/installer/mode", json={"mode": "shadow"},
                headers={"Origin": "https://kenny.example.org.evil.example"})
    assert r.status_code == 403

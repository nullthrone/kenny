"""The agent scheduler: when a scheduled agent is due, on which host, and when to stop.

The scheduler is real, and so are the stores, the runner's mode handling and the
tool executor (its registry decides online/OS, its ``run_capability`` makes the
idle check, only the wire send is replaced). The one stand-in is
``ScriptedRunner.run_generic``: it writes the run row a real run would, with the
outcome a test scripts, so the tests can say "this run failed" or "this run
changed something and ended actionable" without a model. The last test goes the
whole way through the real ``run_generic``.

What these pin down, each a seam between two halves:

* **The window is webfilter's.** An occurrence is identified through
  ``schedule_state``, not a second parser, and an unreadable window means *never*.
* **"Already ran" is the run record.** A new scheduler on the same store does not
  run a host again within its window occurrence.
* **A stop outlasts the pass** — the next pass must not walk past a host that went
  wrong.
* **A refusal is not a fault.** ``disabled``/``blocked`` never trip the breaker.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kenny_server.agents import scheduler as scheduler_module
from kenny_server.agents.catalog import CATALOG
from kenny_server.agents.runner import ENABLED_SETTING, AgentRunner, validate_params
from kenny_server.agents.scheduler import (
    BENIGN_REFUSALS,
    BREAKER_ACTOR,
    MIN_INTERVAL_S,
    SCHEDULE_TRIGGER_PREFIX,
    AgentScheduler,
    halts,
    host_supports,
    made_change,
    run_failed,
    stops_canary,
)
from kenny_server.agents.store import INTERRUPTED_ERROR, AgentRun, AgentStore
from kenny_server.config import CATALOG as SETTINGS_CATALOG
from kenny_server.config import Settings
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.ticketstore import AGENT_ORIGIN, TicketStore
from kenny_server.tickets import TicketService
from kenny_server.toolloop import AGENT_VERDICT_TOOL, ToolExecutor
from kenny_server.tools import CAPABILITY_TOOLS, CallLog, ScreenshotStore
from kenny_server.agents.verdict import register as register_verdict
from kenny_server.tunnel import AgentTunnel, ToolError

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

WIN_A, WIN_B, WIN_C = "a-pc", "b-pc", "c-pc"
LINUX = "tux"
OFFLINE = "away-pc"
FIREFOX = "Mozilla.Firefox"
NIGHT = {"days": "daily", "start": "02:00", "end": "05:00", "tz": "UTC"}
IN_WINDOW = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)


# -- the world -----------------------------------------------------------------


class ScriptedRunner(AgentRunner):
    """The real runner (modes, store, event log) with ``run_generic`` replaced by a script."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[SimpleNamespace] = []
        #: ``(agent, host)`` or ``host`` or ``"*"`` -> outcome dict or ``callable(call) -> dict``.
        self.script: dict[Any, Any] = {}

    async def run_generic(  # type: ignore[override]
        self, spec: Any, *, host_id: str | None, trigger: str, brief: str, **kw: Any
    ) -> AgentRun | None:
        if not self.enabled():
            return None
        mode = await self._mode_for(spec)
        if mode == "off":
            return None
        call = SimpleNamespace(spec=spec, host=host_id, trigger=trigger, brief=brief, kw=kw, mode=mode)
        self.calls.append(call)
        outcome = (
            self.script.get((spec.id, host_id)) or self.script.get(host_id) or self.script.get("*") or {}
        )
        if callable(outcome):
            outcome = outcome(call)
        run = await self.store.start_run(
            agent_id=spec.id,
            spec_hash=spec.spec_hash,
            trigger=trigger,
            mode=mode,
            subject=f"host:{host_id}",
            host_id=host_id,
        )
        return await self.store.finish_run(
            run.id,
            status=outcome.get("status", "completed"),
            verdict=outcome.get("verdict", "clean"),
            error=outcome.get("error"),
            actions=outcome.get("actions", []),
            recommendations=outcome.get("recommendations", []),
        )


class World:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.now = IN_WINDOW
        self.params: dict[str, dict[str, Any]] = {}
        #: host -> what ``remotehelp_status`` answers, or an exception to raise.
        self.idle: dict[str, Any] = {}
        self.capability_calls: list[dict[str, Any]] = []

    async def setup(self) -> None:
        self.agent_store = AgentStore(self.db_path)
        await self.agent_store.connect()
        self.telemetry = TelemetryStore(db_path=self.db_path)
        await self.telemetry.connect()
        self.event_store = EventStore(db_path=self.db_path)
        await self.event_store.connect()
        self.registry = AgentRegistry(tokens={h: "t" for h in (WIN_A, WIN_B, WIN_C, LINUX, OFFLINE)})

        async def send(_frame: Any) -> None:  # pragma: no cover - never used
            return None

        for host, os_name in ((WIN_A, "windows"), (WIN_B, "windows"), (WIN_C, "windows"), (LINUX, "linux")):
            self.registry.mark_online(host, {"os": os_name}, send)
        tunnel = AgentTunnel(self.registry, self.telemetry, self.event_store)

        async def send_request(agent_id: str, tool: str, args: dict[str, Any], timeout_s: float):
            self.capability_calls.append({"agent_id": agent_id, "tool": tool, "args": args})
            answer = self.idle.get(agent_id, {"interactive_session": False})
            if isinstance(answer, Exception):
                raise answer
            return answer

        tunnel.send_request = send_request  # type: ignore[method-assign]
        self.executor = ToolExecutor(
            registry=self.registry,
            store=self.telemetry,
            tunnel=tunnel,
            call_log=CallLog(event_store=self.event_store),
            screenshots=ScreenshotStore(),
        )
        self.settings = Settings(None, env={"ANTHROPIC_API_KEY": "sk-test"})
        self.runner = ScriptedRunner(
            store=self.agent_store,
            settings=self.settings,
            catalog=CATALOG,
            event_store=self.event_store,
            now=lambda: self.now,
        )

    async def close(self) -> None:
        for store in (self.agent_store, self.event_store, self.telemetry):
            await store.close()

    async def params_of(self, agent_id: str) -> dict[str, Any]:
        return self.params.get(agent_id, {})

    def scheduler(self, runner: AgentRunner | None = None, **kw: Any) -> AgentScheduler:
        return AgentScheduler(
            runner or self.runner, CATALOG, self.params_of, self.executor, now=lambda: self.now, **kw
        )

    def configure(self, agent: str, **params: Any) -> None:
        self.params.setdefault(agent, {}).update(params)

    async def runs(self, agent: str) -> list[AgentRun]:
        return list(reversed(await self.agent_store.list_runs(agent_id=agent, limit=500)))

    def hosts_run(self, agent: str) -> list[str]:
        return [c.host for c in self.runner.calls if c.spec.id == agent]


@pytest.fixture
async def w(tmp_path):
    world = World(str(tmp_path / "sched.sqlite"))
    await world.setup()
    yield world
    await world.close()


def _posture(w: World, *hosts: str, window: Any = NIGHT) -> None:
    w.configure("posture", window=window, hosts=list(hosts))


def _patch(w: World, *hosts: str, **extra: Any) -> None:
    w.configure("patch", window=NIGHT, hosts=list(hosts), packages=[FIREFOX], **extra)


FAILED = {"status": "failed", "verdict": None, "error": "the model API was unreachable"}
CHANGED_ACTIONABLE = {
    "verdict": "actionable",
    "actions": [{"tool": "winget_update", "args": {"id": FIREFOX}, "ok": True}],
}


# -- the window ----------------------------------------------------------------


async def test_nothing_starts_outside_the_window(w: World) -> None:
    _posture(w, WIN_A)
    w.now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert await w.scheduler().pass_once() == []
    assert w.runner.calls == []
    w.now = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)  # the end is exclusive
    assert await w.scheduler().pass_once() == []


async def test_a_run_starts_inside_the_window_with_its_occurrence_as_trigger(w: World) -> None:
    _posture(w, WIN_A)
    [outcome] = await w.scheduler().pass_once()
    assert (outcome.kind, outcome.host_id) == ("ran", WIN_A)
    [call] = w.runner.calls
    assert call.host == WIN_A
    assert call.trigger == f"{SCHEDULE_TRIGGER_PREFIX}2026-10-08T05:00:00+00:00"
    assert call.spec.id == "posture"


async def test_the_window_is_read_in_its_own_timezone(w: World) -> None:
    # 03:00 UTC is 05:00 in Berlin (UTC+2 in October): outside a 02:00-05:00 window there.
    _posture(w, WIN_A, window={**NIGHT, "tz": "Europe/Berlin"})
    assert await w.scheduler().pass_once() == []
    w.now = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)  # 02:30 in Berlin
    assert len(await w.scheduler().pass_once()) == 1


@pytest.mark.parametrize(
    "window",
    [
        None,
        {},
        "02:00-05:00",
        {"days": "daily", "start": "02:00"},
        {"days": "funday", "start": "02:00", "end": "05:00"},
        {"days": "daily", "start": "02:00", "end": "05:00", "tz": "Mars/Olympus"},
        {"days": "daily", "start": "03:00", "end": "03:00"},
    ],
)
async def test_a_missing_or_unreadable_window_never_runs(w: World, window: Any) -> None:
    _posture(w, WIN_A, window=window)
    assert await w.scheduler().pass_once() == []
    assert w.runner.calls == []


async def test_a_window_that_wraps_midnight_is_one_occurrence(w: World) -> None:
    _posture(w, WIN_A, window={"days": "daily", "start": "23:00", "end": "04:00", "tz": "UTC"})
    w.now = datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc)
    await w.scheduler().pass_once()
    w.now = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)  # after midnight, same occurrence
    assert await w.scheduler().pass_once() == []
    assert w.hosts_run("posture") == [WIN_A]


# -- the hosts -----------------------------------------------------------------


@pytest.mark.parametrize("hosts", [None, [], "", "  ", ["", " "], 7, {"a": 1}])
async def test_no_hosts_is_no_host_never_all(w: World, hosts: Any) -> None:
    w.configure("posture", window=NIGHT, hosts=hosts)
    assert await w.scheduler().pass_once() == []
    assert w.runner.calls == []


async def test_hosts_missing_from_the_parameters_is_no_host(w: World) -> None:
    w.configure("posture", window=NIGHT)
    assert await w.scheduler().pass_once() == []


async def test_a_host_the_registry_has_never_heard_of_is_skipped_not_run(w: World) -> None:
    _posture(w, OFFLINE, "ghost-pc")
    outcomes = await w.scheduler().pass_once()
    assert [(o.host_id, o.kind) for o in outcomes] == [(OFFLINE, "skipped"), ("ghost-pc", "skipped")]
    assert all("offline" in o.detail for o in outcomes)
    assert w.runner.calls == []


async def test_an_agent_in_off_mode_starts_nothing_and_asks_no_host(w: World) -> None:
    _patch(w, WIN_A)
    await w.runner.set_mode("patch", "off", actor="admin")
    assert await w.scheduler().pass_once() == []
    assert w.capability_calls == []


async def test_the_global_switch_stops_the_pass(w: World) -> None:
    _posture(w, WIN_A)
    w.runner.settings = Settings(None, env={ENABLED_SETTING: "0"})
    assert not w.runner.enabled()
    assert await w.scheduler().pass_once() == []


async def test_only_schedule_agents_are_considered(w: World) -> None:
    # triage is an event agent; give it a window and hosts anyway.
    w.configure("triage", window=NIGHT, hosts=[WIN_A])
    assert await w.scheduler().pass_once() == []


# -- once per window occurrence --------------------------------------------------


async def test_a_host_runs_once_per_occurrence(w: World) -> None:
    _posture(w, WIN_A, WIN_B)
    sched = w.scheduler()
    first = await sched.pass_once()
    assert [o.host_id for o in first] == [WIN_A, WIN_B]
    w.now += timedelta(minutes=10)
    assert await sched.pass_once() == []
    assert w.hosts_run("posture") == [WIN_A, WIN_B]


async def test_a_restart_does_not_run_a_host_twice_in_one_occurrence(w: World) -> None:
    _posture(w, WIN_A)
    await w.scheduler().pass_once()
    w.now += timedelta(minutes=20)
    assert await w.scheduler().pass_once() == []  # a new scheduler: nothing in memory
    assert w.hosts_run("posture") == [WIN_A]


async def test_the_next_occurrence_runs_again(w: World) -> None:
    _posture(w, WIN_A)
    sched = w.scheduler()
    await sched.pass_once()
    w.now += timedelta(days=1)
    await sched.pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_A]
    triggers = {r.trigger for r in await w.runs("posture")}
    assert len(triggers) == 2


async def test_a_failed_run_is_not_retried_within_the_occurrence(w: World) -> None:
    _posture(w, WIN_A)
    w.runner.script[WIN_A] = FAILED
    sched = w.scheduler()
    await sched.pass_once()
    w.now += timedelta(minutes=5)
    await sched.pass_once()
    assert w.hosts_run("posture") == [WIN_A]


async def test_the_runs_of_two_agents_do_not_hide_each_other(w: World) -> None:
    _posture(w, WIN_A)
    w.configure("patch", window=NIGHT, hosts=[WIN_A], packages=[FIREFOX], require_idle=False)
    await w.scheduler().pass_once()
    assert w.hosts_run("posture") == [WIN_A] and w.hosts_run("patch") == [WIN_A]


async def test_a_run_a_cap_refused_is_tried_again_and_stops_the_agent(w: World) -> None:
    _posture(w, WIN_A, WIN_B)
    w.runner.script[WIN_A] = {"status": "skipped", "verdict": None, "error": "token cap reached"}
    sched = w.scheduler()
    outcomes = await sched.pass_once()
    assert [(o.host_id, o.kind) for o in outcomes] == [(WIN_A, "skipped")]  # B not tried
    w.runner.script.clear()
    w.now += timedelta(minutes=5)
    await sched.pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_A, WIN_B]


# -- canary order ----------------------------------------------------------------


async def test_hosts_run_in_sorted_order_one_after_another(w: World) -> None:
    _posture(w, WIN_C, WIN_A, WIN_B, WIN_A)
    await w.scheduler().pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_B, WIN_C]


async def test_a_failed_run_stops_the_hosts_after_it(w: World) -> None:
    _posture(w, WIN_A, WIN_B, WIN_C)
    w.runner.script[WIN_B] = FAILED
    sched = w.scheduler()
    await sched.pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_B]
    # ... and the next pass does not walk past it.
    w.now += timedelta(minutes=5)
    outcomes = await sched.pass_once()
    assert [o.kind for o in outcomes] == ["halted"]
    assert w.hosts_run("posture") == [WIN_A, WIN_B]
    # The next occurrence starts afresh.
    w.now += timedelta(days=1)
    w.runner.script.clear()
    await sched.pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_B, WIN_A, WIN_B, WIN_C]


@pytest.mark.parametrize("verdict", ["actionable", "inconclusive"])
async def test_a_bad_verdict_after_a_change_stops_the_canary(w: World, verdict: str) -> None:
    _patch(w, WIN_A, WIN_B, require_idle=False)
    w.runner.script[WIN_A] = {**CHANGED_ACTIONABLE, "verdict": verdict}
    await w.scheduler().pass_once()
    assert w.hosts_run("patch") == [WIN_A]


async def test_a_bad_verdict_without_a_change_does_not_stop_the_canary(w: World) -> None:
    # A review that only reads (or a shadow run that only proposed) changed nothing.
    _posture(w, WIN_A, WIN_B)
    w.runner.script[WIN_A] = {"verdict": "actionable"}
    await w.scheduler().pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_B]


async def test_a_good_verdict_after_a_change_carries_on(w: World) -> None:
    _patch(w, WIN_A, WIN_B, require_idle=False)
    w.runner.script[WIN_A] = {**CHANGED_ACTIONABLE, "verdict": "acted"}
    await w.scheduler().pass_once()
    assert w.hosts_run("patch") == [WIN_A, WIN_B]


# -- reading a run ---------------------------------------------------------------


def _row(**kw: Any) -> AgentRun:
    fields: dict[str, Any] = dict(
        id="r", agent_id="patch", spec_hash="h", trigger="schedule:x", subject=None, host_id=WIN_A,
        mode="act", status="completed", verdict="clean", summary=None, input_tokens=0,
        output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0, ticket_id=None,
        error=None, actions=[], recommendations=[], started_at="t", finished_at="t",
    )
    fields.update(kw)
    return AgentRun(**fields)


def test_a_change_that_errored_is_a_failed_run() -> None:
    run = _row(actions=[{"tool": "winget_update", "ok": False, "code": "exec_failed"}])
    assert run_failed(run) and stops_canary(run)


@pytest.mark.parametrize("code", sorted(BENIGN_REFUSALS))
def test_a_refusal_is_an_outcome_not_a_failure(code: str) -> None:
    run = _row(verdict="actionable", actions=[{"tool": "winget_update", "ok": False, "code": code}])
    assert not run_failed(run)
    assert not made_change(run)  # nothing changed on the host ...
    assert not stops_canary(run)  # ... so the next host is not endangered by it


def test_the_refusals_are_exactly_the_documented_error_codes() -> None:
    assert BENIGN_REFUSALS == {"disabled", "blocked", "paused"}


def test_a_restart_interrupting_a_run_is_not_its_failure() -> None:
    assert not run_failed(_row(status="failed", error=INTERRUPTED_ERROR))
    assert run_failed(_row(status="failed", error="boom"))


def test_a_skipped_or_clean_run_is_not_a_failure() -> None:
    assert not run_failed(_row(status="skipped", verdict=None, error="offline"))
    assert not run_failed(_row())


# -- the circuit breaker ---------------------------------------------------------


async def _nights(w: World, sched: AgentScheduler, count: int) -> None:
    for _ in range(count):
        await sched.pass_once()
        w.now += timedelta(days=1)


async def test_three_failed_runs_in_a_row_move_an_agent_in_act_back_to_shadow(w: World) -> None:
    _posture(w, WIN_A)
    await w.runner.set_mode(
        "posture", "act", actor="admin", effective_hash=await w.runner.live_hash("posture")
    )
    w.runner.script[WIN_A] = FAILED
    sched = w.scheduler()
    await _nights(w, sched, 2)
    assert await w.runner.mode_of("posture") == "act"
    outcomes = await sched.pass_once()
    assert [o.kind for o in outcomes] == ["ran", "tripped"]
    assert await w.runner.mode_of("posture") == "shadow"
    assert await w.agent_store.get_mode("posture") == "shadow"
    # Who did it is on the event log, once for the mode change and once for the reason.
    logs = [e for e in await w.event_store.query(kind="log") if e.get("target", "").startswith("kenny.agents")]
    actors = {e["fields"].get("actor") for e in logs if e["fields"].get("agent") == "posture"}
    assert BREAKER_ACTOR in actors
    assert any(e["level"] == "warning" and "circuit breaker" in e["message"] for e in logs)


async def test_a_success_in_between_resets_the_count(w: World) -> None:
    _posture(w, WIN_A)
    await w.runner.set_mode(
        "posture", "act", actor="admin", effective_hash=await w.runner.live_hash("posture")
    )
    sched = w.scheduler()
    for outcome in (FAILED, FAILED, {}, FAILED, FAILED):
        w.runner.script[WIN_A] = outcome
        await _nights(w, sched, 1)
    assert await w.runner.mode_of("posture") == "act"


async def test_a_refused_change_never_trips_the_breaker(w: World) -> None:
    _patch(w, WIN_A, require_idle=False)
    await w.runner.set_mode(
        "patch", "act", actor="admin", effective_hash=await w.runner.live_hash("patch")
    )
    for code in ("disabled", "blocked", "paused", "disabled", "blocked"):
        w.runner.script[WIN_A] = {
            "verdict": "actionable",
            "actions": [{"tool": "winget_update", "args": {"id": FIREFOX}, "ok": False, "code": code}],
        }
        await _nights(w, w.scheduler(), 1)
    assert len(w.hosts_run("patch")) == 5
    assert await w.runner.mode_of("patch") == "act"


async def test_errored_changes_do_trip_the_breaker(w: World) -> None:
    _patch(w, WIN_A, require_idle=False)
    await w.runner.set_mode(
        "patch", "act", actor="admin", effective_hash=await w.runner.live_hash("patch")
    )
    w.runner.script[WIN_A] = {
        "verdict": "inconclusive",
        "actions": [{"tool": "winget_update", "args": {"id": FIREFOX}, "ok": False, "code": "exec_failed"}],
    }
    await _nights(w, w.scheduler(), 3)
    assert await w.runner.mode_of("patch") == "shadow"


async def test_interrupted_and_skipped_runs_do_not_count_toward_the_breaker(w: World) -> None:
    _posture(w, WIN_A)
    await w.runner.set_mode(
        "posture", "act", actor="admin", effective_hash=await w.runner.live_hash("posture")
    )
    sched = w.scheduler()
    w.runner.script[WIN_A] = FAILED
    await _nights(w, sched, 1)
    w.runner.script[WIN_A] = {"status": "failed", "verdict": None, "error": INTERRUPTED_ERROR}
    await _nights(w, sched, 1)
    w.runner.script[WIN_A] = FAILED
    await _nights(w, sched, 1)
    assert await w.runner.mode_of("posture") == "act"  # two real failures, one interruption


async def test_failures_in_shadow_leave_the_mode_alone(w: World) -> None:
    _posture(w, WIN_A)
    w.runner.script[WIN_A] = FAILED
    await _nights(w, w.scheduler(), 4)
    assert await w.runner.mode_of("posture") == "shadow"
    assert await w.agent_store.get_mode("posture") is None  # nothing written


async def test_the_threshold_is_configurable(w: World) -> None:
    _posture(w, WIN_A)
    await w.runner.set_mode(
        "posture", "act", actor="admin", effective_hash=await w.runner.live_hash("posture")
    )
    w.runner.script[WIN_A] = FAILED
    await _nights(w, w.scheduler(breaker_threshold=1), 1)
    assert await w.runner.mode_of("posture") == "shadow"


async def test_an_interruption_neither_counts_nor_breaks_a_streak(w: World) -> None:
    _posture(w, WIN_A)
    await w.runner.set_mode(
        "posture", "act", actor="admin", effective_hash=await w.runner.live_hash("posture")
    )
    sched = w.scheduler()
    for outcome in (FAILED, FAILED, {"status": "failed", "verdict": None, "error": INTERRUPTED_ERROR}):
        w.runner.script[WIN_A] = outcome
        await _nights(w, sched, 1)
    assert await w.runner.mode_of("posture") == "act"
    w.runner.script[WIN_A] = FAILED
    await _nights(w, sched, 1)
    assert await w.runner.mode_of("posture") == "shadow"  # three real failures, one pause


# -- a change nobody confirmed -----------------------------------------------------

CHANGED_NO_VERDICT = {
    "verdict": None,
    "actions": [{"tool": "winget_update", "args": {"id": FIREFOX}, "ok": True}],
}


async def test_a_change_with_no_verdict_stops_the_canary(w: World) -> None:
    _patch(w, WIN_A, WIN_B, require_idle=False)
    w.runner.script[WIN_A] = CHANGED_NO_VERDICT
    await w.scheduler().pass_once()
    assert w.hosts_run("patch") == [WIN_A]


@pytest.mark.parametrize("verdict", [None, "actionable", "inconclusive", "clean"])
def test_only_acted_confirms_a_change(verdict: str | None) -> None:
    changed = [{"tool": "winget_update", "args": {"id": FIREFOX}, "ok": True}]
    assert halts(_row(verdict=verdict, actions=changed))
    assert stops_canary(_row(verdict=verdict, actions=changed))
    assert not halts(_row(verdict="acted", actions=changed))
    assert not halts(_row(verdict=verdict))  # nothing changed, nothing to confirm


@pytest.mark.parametrize("outcome", [CHANGED_NO_VERDICT, CHANGED_ACTIONABLE])
async def test_halts_count_toward_the_circuit_breaker(w: World, outcome: dict[str, Any]) -> None:
    """A halt that never trips anything lasts only until the next window."""

    _patch(w, WIN_A, require_idle=False)
    await w.runner.set_mode(
        "patch", "act", actor="admin", effective_hash=await w.runner.live_hash("patch")
    )
    w.runner.script[WIN_A] = outcome
    sched = w.scheduler()
    await _nights(w, sched, 2)
    assert await w.runner.mode_of("patch") == "act"
    outcomes = await sched.pass_once()
    assert [o.kind for o in outcomes] == ["ran", "tripped"]
    assert await w.runner.mode_of("patch") == "shadow"


async def test_an_acted_night_resets_the_halt_streak(w: World) -> None:
    _patch(w, WIN_A, require_idle=False)
    await w.runner.set_mode(
        "patch", "act", actor="admin", effective_hash=await w.runner.live_hash("patch")
    )
    sched = w.scheduler()
    acted = {**CHANGED_ACTIONABLE, "verdict": "acted"}
    for outcome in (CHANGED_NO_VERDICT, CHANGED_NO_VERDICT, acted, CHANGED_NO_VERDICT):
        w.runner.script[WIN_A] = outcome
        await _nights(w, sched, 1)
    assert await w.runner.mode_of("patch") == "act"


# -- the window bounds the work, not only the start --------------------------------


async def test_a_pass_stops_when_the_window_closes_between_hosts(w: World) -> None:
    """Window 02:00-05:00, pass at 03:00, each run takes 90 minutes.

    A runs 03:00-04:30, B 04:30-06:00; C would start at 06:00, outside the
    window, so it is not started — it waits for the next occurrence.
    """

    _posture(w, WIN_A, WIN_B, WIN_C)

    def ninety_minutes(_call: Any) -> dict[str, Any]:
        w.now = w.now + timedelta(minutes=90)
        return {"verdict": "clean"}

    w.runner.script["*"] = ninety_minutes
    outcomes = await w.scheduler().pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_B]
    assert (outcomes[-1].kind, outcomes[-1].host_id) == ("halted", WIN_C)
    assert "window" in outcomes[-1].detail
    # The next occurrence starts with the first host again, as always.
    w.runner.script.clear()
    w.now = IN_WINDOW + timedelta(days=1)
    await w.scheduler().pass_once()
    assert w.hosts_run("posture") == [WIN_A, WIN_B, WIN_A, WIN_B, WIN_C]


async def test_a_window_edited_shut_mid_pass_stops_it(w: World) -> None:
    _posture(w, WIN_A, WIN_B)

    def close_the_window(_call: Any) -> dict[str, Any]:
        w.configure("posture", window={**NIGHT, "start": "04:00", "end": "05:00"})
        return {"verdict": "clean"}

    w.runner.script[WIN_A] = close_the_window
    await w.scheduler().pass_once()
    assert w.hosts_run("posture") == [WIN_A]


async def test_a_scheduled_run_is_handed_the_end_of_its_occurrence(w: World) -> None:
    _posture(w, WIN_A)
    await w.scheduler().pass_once()
    [call] = w.runner.calls
    assert call.kw["act_until"] == datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)


async def test_a_window_whose_end_cannot_be_told_never_runs(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _posture(w, WIN_A)
    real = scheduler_module.schedule_state

    def no_end(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {**real(*args, **kwargs), "next_change_at": None}

    monkeypatch.setattr(scheduler_module, "schedule_state", no_end)
    assert await w.scheduler().pass_once() == []
    assert w.runner.calls == []


async def test_joined_a_change_after_the_window_closed_is_refused(w: World) -> None:
    """Through the real runner and gate: the run outlasts its window, its change does not run."""

    real = AgentRunner(
        store=w.agent_store,
        settings=w.settings,
        catalog=CATALOG,
        event_store=w.event_store,
        now=lambda: w.now,
    )
    await real.set_params(
        "patch",
        {"window": NIGHT, "hosts": [WIN_A], "packages": [FIREFOX], "require_idle": False},
        actor="admin",
    )
    await real.set_mode("patch", "act", actor="admin", effective_hash=await real.live_hash("patch"))

    tunnel = w.executor.tunnel
    send = tunnel.send_request

    async def slow_list(agent_id: str, tool: str, args: dict[str, Any], timeout_s: float) -> Any:
        if tool == "winget_list":
            w.now = datetime(2026, 10, 8, 5, 1, tzinfo=timezone.utc)  # the window has closed
        return await send(agent_id, tool, args, timeout_s)

    tunnel.send_request = slow_list  # type: ignore[method-assign]
    client = FakeAnthropic(
        [
            _Response([tool_use_block("t1", "winget_list", {})], "tool_use"),
            _Response([tool_use_block("t2", "winget_update", {"id": FIREFOX})], "tool_use"),
            _Response([text_block("done")], "end_turn"),
        ]
    )
    [outcome] = await AgentScheduler(
        real, CATALOG, real.get_params, w.executor, now=lambda: w.now, client=client, model="m"
    ).pass_once()
    assert outcome.kind == "ran" and outcome.run is not None
    assert outcome.run.mode == "act"
    assert outcome.run.actions == []
    assert [r["tool"] for r in outcome.run.recommendations] == ["winget_update"]
    assert [c["tool"] for c in w.capability_calls] == ["winget_list"]


# -- host_idle -------------------------------------------------------------------


async def test_a_signed_in_user_means_the_host_is_skipped_without_a_run(w: World) -> None:
    _patch(w, WIN_A)
    w.idle[WIN_A] = {"interactive_session": True}
    [outcome] = await w.scheduler().pass_once()
    assert (outcome.kind, outcome.host_id) == ("skipped", WIN_A)
    assert "signed in" in outcome.detail
    assert w.runner.calls == []
    [row] = await w.runs("patch")
    assert (row.status, row.host_id, row.error) == ("skipped", WIN_A, outcome.detail)
    assert not run_failed(row)


async def test_the_idle_check_is_the_servers_own_call_as_the_agent(w: World) -> None:
    _patch(w, WIN_A)
    spy: list[dict[str, Any]] = []
    real = w.executor.run_capability

    async def run_capability(tool: str, args: dict[str, Any], **kw: Any) -> Any:
        spy.append({"tool": tool, "args": args, **kw})
        return await real(tool, args, **kw)

    w.executor.run_capability = run_capability  # type: ignore[method-assign]
    await w.scheduler().pass_once()
    assert spy == [
        {"tool": "remotehelp_status", "args": {}, "agent_id": WIN_A, "actor": "agent:patch"}
    ]
    assert w.hosts_run("patch") == [WIN_A]


async def test_an_idle_host_is_run_on(w: World) -> None:
    _patch(w, WIN_A)
    w.idle[WIN_A] = {"interactive_session": False}
    await w.scheduler().pass_once()
    assert w.hosts_run("patch") == [WIN_A]


@pytest.mark.parametrize(
    "answer",
    [ToolError("timeout", "tool exceeded 30s"), RuntimeError("socket closed"), {}, {"installed": True},
     {"interactive_session": None}, {"interactive_session": "false"}, "idle"],
)
async def test_a_check_that_fails_or_does_not_say_is_do_not_disturb(w: World, answer: Any) -> None:
    _patch(w, WIN_A)
    w.idle[WIN_A] = answer
    [outcome] = await w.scheduler().pass_once()
    assert outcome.kind == "skipped"
    assert w.runner.calls == []
    assert not run_failed((await w.runs("patch"))[0])


async def test_a_skip_is_recorded_once_and_retried_next_pass(w: World) -> None:
    _patch(w, WIN_A)
    w.idle[WIN_A] = {"interactive_session": True}
    sched = w.scheduler()
    await sched.pass_once()
    w.now += timedelta(minutes=5)
    await sched.pass_once()
    assert len(await w.runs("patch")) == 1  # one skipped row, not one per pass
    w.idle[WIN_A] = {"interactive_session": False}
    w.now += timedelta(minutes=5)
    await sched.pass_once()
    assert w.hosts_run("patch") == [WIN_A]  # a skip never used up the occurrence


async def test_a_busy_host_does_not_hold_up_the_others(w: World) -> None:
    _patch(w, WIN_A, WIN_B)
    w.idle[WIN_A] = {"interactive_session": True}
    await w.scheduler().pass_once()
    assert w.hosts_run("patch") == [WIN_B]


async def test_require_idle_off_asks_nobody(w: World) -> None:
    _patch(w, WIN_A, require_idle=False)
    w.idle[WIN_A] = {"interactive_session": True}
    await w.scheduler().pass_once()
    assert w.capability_calls == []
    assert w.hosts_run("patch") == [WIN_A]


async def test_require_idle_missing_is_on(w: World) -> None:
    # Updating while somebody works is the unsafe direction, so unset means check.
    _patch(w, WIN_A)
    assert "require_idle" not in w.params["patch"]
    w.idle[WIN_A] = {"interactive_session": True}
    await w.scheduler().pass_once()
    assert w.runner.calls == []


@pytest.mark.parametrize("value", ["false", "no", 0, 1, None, [], ["false"], {"x": 1}])
def test_require_idle_is_a_json_boolean_and_nothing_else(value: Any) -> None:
    with pytest.raises(ValueError, match="true or false"):
        validate_params(CATALOG["patch"], {"require_idle": value})


@pytest.mark.parametrize("value", [True, False])
def test_require_idle_is_stored_as_the_boolean_given(value: bool) -> None:
    assert validate_params(CATALOG["patch"], {"require_idle": value}) == {"require_idle": value}


@pytest.mark.parametrize("stored", [["false"], [], "false", 0, None])
async def test_only_a_stored_false_switches_the_idle_check_off(w: World, stored: Any) -> None:
    # Values a parameter written before require_idle was a boolean may carry:
    # every one of them means "check", the safe direction.
    _patch(w, WIN_A, require_idle=stored)
    w.idle[WIN_A] = {"interactive_session": True}
    await w.scheduler().pass_once()
    assert w.runner.calls == []
    assert [c["tool"] for c in w.capability_calls] == ["remotehelp_status"]


async def test_joined_require_idle_false_through_the_runners_stored_parameters(w: World) -> None:
    await w.runner.set_params(
        "patch",
        {"window": NIGHT, "hosts": [WIN_A], "packages": [FIREFOX], "require_idle": False},
        actor="admin",
    )
    assert (await w.runner.get_params("patch"))["require_idle"] is False
    w.idle[WIN_A] = {"interactive_session": True}
    await AgentScheduler(
        w.runner, CATALOG, w.runner.get_params, w.executor, now=lambda: w.now
    ).pass_once()
    assert w.capability_calls == []
    assert w.hosts_run("patch") == [WIN_A]


async def test_an_agent_without_the_parameter_never_asks(w: World) -> None:
    _posture(w, WIN_A)
    w.idle[WIN_A] = {"interactive_session": True}
    await w.scheduler().pass_once()
    assert w.capability_calls == []
    assert w.hosts_run("posture") == [WIN_A]


# -- the OS ----------------------------------------------------------------------


async def test_patch_skips_a_host_whose_os_cannot_run_winget(w: World) -> None:
    _patch(w, LINUX, WIN_A, require_idle=False)
    outcomes = await w.scheduler().pass_once()
    assert [(o.host_id, o.kind) for o in outcomes] == [(WIN_A, "ran"), (LINUX, "skipped")]
    assert w.hosts_run("patch") == [WIN_A]
    assert "linux" in outcomes[1].detail


async def test_posture_runs_on_linux(w: World) -> None:
    _posture(w, LINUX)
    await w.scheduler().pass_once()
    assert w.hosts_run("posture") == [LINUX]


def test_host_supports_follows_the_tools_the_spec_names() -> None:
    assert host_supports(CATALOG["patch"], "windows")
    assert host_supports(CATALOG["patch"], "Windows")
    assert not host_supports(CATALOG["patch"], "linux")
    assert not host_supports(CATALOG["patch"], "macos")
    assert not host_supports(CATALOG["patch"], None)  # an OS nobody reported is not assumed
    assert host_supports(CATALOG["posture"], "linux")
    # A spec that names an OS-scoped tool of ``tools.py`` honours that scope too.
    shell = SimpleNamespace(tools=frozenset({"powershell_exec"}))
    assert host_supports(shell, "windows") and not host_supports(shell, "linux")  # type: ignore[arg-type]


def test_every_capability_a_scheduled_agent_names_is_known_to_the_scheduler() -> None:
    # If a scheduled agent grows a tool only one OS serves, tools._OS_SCOPED_TOOLS
    # or the scheduler's own table must say so; this fails the day it is neither.
    for spec in CATALOG.values():
        if spec.trigger.kind != "schedule":
            continue
        for tool in spec.tools & frozenset(CAPABILITY_TOOLS):
            if tool.startswith("winget_"):
                assert tool in scheduler_module._TOOL_OS


# -- the brief and the run call ---------------------------------------------------


async def test_the_brief_names_the_allowed_packages_and_no_other_host(w: World) -> None:
    w.configure("patch", window=NIGHT, hosts=[WIN_A], packages=["b.Pkg", FIREFOX], require_idle=False)
    await w.scheduler().pass_once()
    [call] = w.runner.calls
    assert f"{FIREFOX}, b.Pkg" in call.brief
    assert WIN_A in call.brief


async def test_an_empty_allowlist_is_stated_as_nothing_allowed(w: World) -> None:
    w.configure("patch", window=NIGHT, hosts=[WIN_A], packages=[], require_idle=False)
    await w.scheduler().pass_once()
    assert "update nothing" in w.runner.calls[0].brief


async def test_the_run_gets_the_spec_with_the_allowlist_filled_in(w: World) -> None:
    _patch(w, WIN_A, require_idle=False)
    await w.scheduler().pass_once()
    [constraint] = w.runner.calls[0].spec.constraints
    assert constraint.allowed == {FIREFOX}
    # ... and it is still the catalog's spec: parameter values are not in its hash.
    assert w.runner.calls[0].spec.spec_hash == CATALOG["patch"].spec_hash


async def test_client_and_model_are_passed_when_given_and_the_runner_takes_them(w: World) -> None:
    seen: dict[str, Any] = {}

    class Runner(ScriptedRunner):
        async def run_generic(self, spec, *, host_id, trigger, brief, client=None, model=None, executor=None):  # type: ignore[override]
            seen.update(client=client, model=model, executor=executor)
            return await super().run_generic(spec, host_id=host_id, trigger=trigger, brief=brief)

    runner = Runner(store=w.agent_store, settings=w.settings, catalog=CATALOG, event_store=w.event_store)
    _posture(w, WIN_A)
    sentinel = object()
    await w.scheduler(runner, client=sentinel, model="m").pass_once()
    assert seen == {"client": sentinel, "model": "m", "executor": w.executor}


async def test_a_runner_that_takes_no_client_is_not_given_one(w: World) -> None:
    seen: dict[str, Any] = {}

    class Runner(ScriptedRunner):
        async def run_generic(self, spec, *, host_id, trigger, brief):  # type: ignore[override]
            seen["called"] = True
            return await super().run_generic(spec, host_id=host_id, trigger=trigger, brief=brief)

    runner = Runner(store=w.agent_store, settings=w.settings, catalog=CATALOG, event_store=w.event_store)
    _posture(w, WIN_A)
    await w.scheduler(runner, client=object(), model="m").pass_once()
    assert seen == {"called": True}


# -- the loop --------------------------------------------------------------------


async def test_the_loop_survives_a_failing_pass_and_rereads_the_cadence(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sched = w.scheduler()
    passes = 0

    async def pass_once() -> list[Any]:
        nonlocal passes
        passes += 1
        if passes == 1:
            raise RuntimeError("boom")
        return []

    sched.pass_once = pass_once  # type: ignore[method-assign]
    sleeps: list[float] = []
    cadence = iter([120, 5, 600])

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await sched.run(lambda: next(cadence), initial_delay_s=7)
    assert passes == 3
    # The initial delay, then the live cadence each time -- never below the floor.
    assert sleeps == [7, 120, float(MIN_INTERVAL_S), 600]


async def test_a_bad_cadence_setting_does_not_stop_the_loop(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sched = w.scheduler()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", sleep)

    def broken() -> int:
        raise ValueError("not a number")

    with pytest.raises(asyncio.CancelledError):
        await sched.run(broken, initial_delay_s=0)
    assert sleeps == [0, float(MIN_INTERVAL_S)]


async def test_one_agent_failing_does_not_skip_the_others(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(w, WIN_A, require_idle=False)
    _posture(w, WIN_A)
    sched = w.scheduler()
    real = sched._pass_agent

    async def pass_agent(spec: Any) -> Any:
        if spec.id == "patch":
            raise RuntimeError("bad params")
        return await real(spec)

    monkeypatch.setattr(sched, "_pass_agent", pass_agent)
    await sched.pass_once()
    assert w.hosts_run("posture") == [WIN_A]


def test_the_interval_setting_is_a_live_setting_with_the_loops_floor() -> None:
    spec = SETTINGS_CATALOG["KENNY_AGENTS_SCHEDULE_INTERVAL_SECS"]
    assert spec.lifecycle == "live"
    assert spec.default_raw == "300"
    assert spec.min == MIN_INTERVAL_S == 60
    assert scheduler_module.INTERVAL_SETTING == spec.key


# -- joined: the real run_generic, the verdict handler, a real ticket ---------------


async def test_joined_a_scheduled_posture_run_reports_through_the_real_runner(
    w: World, tmp_path
) -> None:
    ticket_store = TicketStore(w.db_path)
    await ticket_store.connect()
    try:
        tickets = TicketService(ticket_store)
        register_verdict(w.executor, tickets=tickets)
        client = FakeAnthropic(
            [
                _Response([tool_use_block("t1", "diag_autostart", {})], "tool_use"),
                _Response(
                    [
                        tool_use_block(
                            "t2",
                            AGENT_VERDICT_TOOL,
                            {
                                "verdict": "actionable",
                                "finding": "An unknown updater starts at logon.",
                                "evidence": "diag_autostart lists Updater from %TEMP%.",
                            },
                        )
                    ],
                    "tool_use",
                ),
                _Response([text_block("done")], "end_turn"),
            ]
        )
        real = AgentRunner(
            store=w.agent_store, settings=w.settings, catalog=CATALOG, event_store=w.event_store
        )
        _posture(w, WIN_A)
        sched = w.scheduler(real, client=client, model="fake-model")
        [outcome] = await sched.pass_once()
        assert (outcome.kind, outcome.run.status, outcome.run.verdict) == (
            "ran",
            "completed",
            "actionable",
        )
        assert outcome.run.mode == "shadow"  # the catalog default; the report still happens
        assert outcome.run.trigger.startswith(SCHEDULE_TRIGGER_PREFIX)
        [ticket] = [t for t in await ticket_store.list(limit=50) if t.origin == AGENT_ORIGIN]
        assert ticket.agent_id == WIN_A
        # Once per occurrence, through the real run record.
        w.now += timedelta(minutes=30)
        assert await sched.pass_once() == []
    finally:
        await ticket_store.close()


async def test_previews_neither_count_toward_nor_break_the_breaker_streak(w: World) -> None:
    _posture(w, WIN_A)
    await w.runner.set_mode(
        "posture", "act", actor="admin", effective_hash=await w.runner.live_hash("posture")
    )
    w.runner.script[WIN_A] = FAILED
    sched = w.scheduler()
    await _nights(w, sched, 2)
    # A person previews the agent in between, and it goes well.
    preview = await w.agent_store.start_run(
        agent_id="posture", spec_hash="h", trigger="preview:op", mode="shadow", host_id=WIN_A
    )
    await w.agent_store.finish_run(preview.id, status="completed", verdict="clean")
    outcomes = await sched.pass_once()
    assert [o.kind for o in outcomes] == ["ran", "tripped"]
    assert await w.runner.mode_of("posture") == "shadow"

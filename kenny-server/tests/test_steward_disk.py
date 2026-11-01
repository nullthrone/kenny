"""The disk-hygiene steward: its declaration, its gate, its precondition and its run.

Every test that matters runs the real gate (``StewardPolicy``) or the real loop
(``drive_events`` -> the real ``ToolExecutor``); only the tunnel's wire send is
replaced. The seams pinned here:

* **The allowlist is the parameter.** ``fs_cleanup``'s ``target`` is bound to
  ``targets``; an empty or missing allowlist admits nothing, and nothing rides along.
* **The precondition is registered where the server is built.** A spec that
  declares a check nothing registers would skip every run, so the names the
  catalog declares are joined to what ``build_app`` registers.
* **The skip is stable and the model is never called for it.**
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kenny_server import cleanup_targets, toolloop
from kenny_server.main import build_app
from kenny_server.registry import AgentRegistry
from kenny_server.stewards import preconditions
from kenny_server.stewards.authorizations import Authorization, AuthorizationStore
from kenny_server.stewards.catalog import CATALOG
from kenny_server.stewards.catalog.disk import DISK_HYGIENE
from kenny_server.stewards.policy import StewardPolicy, StewardSession
from kenny_server.stewards.runner import validate_params
from kenny_server.stewards.scheduler import StewardScheduler
from kenny_server.stewards.spec import Trigger, TriggerSet, effective_hash, resolve
from kenny_server.stewards.windows import host_supports, machine_unready
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tool_classes import NORMAL_CHANGE, READ_ONLY, TOOL_CLASSES, classify
from kenny_server.toolloop import STEWARD_VERDICT_TOOL, Allow, Deny, ToolExecutor
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block
from test_steward_runner import HOST, World, _text, _tool
from test_steward_scheduler import NIGHT, WIN_A, WIN_B, ScriptedRunner
from test_steward_scheduler import World as SchedulerWorld
from test_steward_triggers import GOLDEN

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
SPEC = DISK_HYGIENE
SUGGESTED = list(cleanup_targets.SUGGESTED_TARGETS)


# -- the declaration -----------------------------------------------------------


def test_the_catalog_carries_the_disk_steward() -> None:
    assert CATALOG["disk_hygiene"] is DISK_HYGIENE


def test_the_declared_shape() -> None:
    assert SPEC.trigger == Trigger(kind="schedule", min_interval_days=7)
    assert SPEC.triggers_allowed == TriggerSet(
        schedule=True, command=True, events=frozenset({"alert:health:disk", "alert:disk_forecast"})
    )
    assert SPEC.tools == {"fs_disk_usage", "fs_cleanup_scan", "fs_cleanup", STEWARD_VERDICT_TOOL}
    assert SPEC.verdict_tool == STEWARD_VERDICT_TOOL
    assert SPEC.budget.max_iterations == 14
    assert SPEC.params == ("window", "hosts", "targets", "min_percent_used", "require_idle")
    assert SPEC.preconditions == ("disk_pressure",)
    assert [(t.tool, t.max_s) for t in SPEC.timeouts] == [("fs_cleanup", 300)]
    # Only the change is bound; the read-only scan is outside the gate's constraints.
    assert [(c.tool, c.arg, c.param) for c in SPEC.constraints] == [("fs_cleanup", "target", "targets")]
    assert classify("fs_cleanup") == NORMAL_CHANGE and classify("fs_cleanup_scan") == READ_ONLY


def test_it_starts_in_shadow_and_enables_no_event_by_default() -> None:
    from kenny_server.stewards.spec import baseline_config

    assert SPEC.default_mode == "shadow"
    assert baseline_config(SPEC) == {
        "schedule": {"min_interval_days": 7}, "events": [], "command": False
    }


@pytest.mark.parametrize("steward_id", sorted(GOLDEN))
def test_the_other_stewards_hashes_did_not_move(steward_id: str) -> None:
    spec = CATALOG[steward_id]
    assert (spec.spec_hash, effective_hash(spec, {})) == GOLDEN[steward_id]


def test_the_prompt_names_only_tools_the_spec_hands_over() -> None:
    named = {t for t in TOOL_CLASSES if re.search(rf"\b{re.escape(t)}\b", SPEC.prompt)}
    assert named and named <= SPEC.tools
    # And it uses all of them: a tool in the spec the prompt never mentions is dead weight.
    assert named == SPEC.tools


def test_the_prompt_states_the_thresholds_and_the_verdicts() -> None:
    for text in ("100 MiB", "95%", "90%", "unsupported", "clean:", "acted:", "actionable:", "inconclusive:"):
        assert text in SPEC.prompt
    assert "untrusted" in SPEC.prompt


# -- the allowlist parameter ---------------------------------------------------


def test_targets_are_validated_against_the_catalog() -> None:
    assert validate_params(SPEC, {"targets": ["tmp", "journal", "tmp"]}) == {"targets": ["journal", "tmp"]}
    assert validate_params(SPEC, {"targets": SUGGESTED})["targets"] == sorted(SUGGESTED)
    assert validate_params(SPEC, {"targets": []}) == {"targets": []}
    with pytest.raises(ValueError, match="tmpp"):
        validate_params(SPEC, {"targets": ["tmp", "tmpp"]})
    with pytest.raises(ValueError, match="C:/Windows"):
        validate_params(SPEC, {"targets": ["C:/Windows"]})


def test_min_percent_used_is_a_percent() -> None:
    assert validate_params(SPEC, {"min_percent_used": 85}) == {"min_percent_used": 85}
    for bad in (0, 100, "80", 80.0, True, [80]):
        with pytest.raises(ValueError):
            validate_params(SPEC, {"min_percent_used": bad})


# -- the gate, joined ----------------------------------------------------------


def _session(targets: Any, mode: str = "act", host: str = HOST) -> StewardSession:
    params = {} if targets is None else {"targets": targets}
    return StewardSession(id="run-1", spec=resolve(SPEC, params), mode=mode, agent_id=host)


def _authorizer(calls: list[tuple[str, dict[str, Any]]]):
    async def authorize(_session: Any, tool: str, args: dict[str, Any], _target: str | None):
        calls.append((tool, dict(args)))
        return Authorization(
            id="auth-1", steward_id=SPEC.id, effective_hash="h", tool=tool, scope=[HOST],
            max_attempts_per_day=3, expires_at=(NOW + timedelta(days=7)).isoformat(),
            granted_by="admin", granted_at=NOW.isoformat(), revoked_at=None, revoked_by=None,
            voided_at=None, voided_by=None, note="",
        )

    return authorize


async def _gate(session: StewardSession, tool: str, args: dict, authorizer: Any = None, host: str = HOST):
    return await StewardPolicy(session, authorizer=authorizer).gate(session, tool, dict(args), host)


async def test_an_allowed_target_runs_in_act_under_a_standing_authorization() -> None:
    spent: list[tuple[str, dict[str, Any]]] = []
    session = _session(["tmp"])
    decision = await _gate(session, "fs_cleanup", {"target": "tmp"}, _authorizer(spent))
    assert isinstance(decision, Allow)
    assert spent == [("fs_cleanup", {"target": "tmp"})]
    assert [a["args"] for a in session.actions] == [{"target": "tmp"}]


async def test_without_an_authorization_the_allowed_target_is_only_recommended() -> None:
    async def none(*_a: Any) -> None:
        return None

    session = _session(["tmp"])
    decision = await _gate(session, "fs_cleanup", {"target": "tmp"}, none)
    assert isinstance(decision, Deny) and decision.code == "not_authorized"
    assert session.actions == [] and [r["args"] for r in session.recommendations] == [{"target": "tmp"}]


async def test_in_shadow_the_allowed_target_is_only_a_recommendation() -> None:
    session = _session(["tmp"], mode="shadow")
    decision = await _gate(session, "fs_cleanup", {"target": "tmp"})
    assert isinstance(decision, Deny) and decision.code == "shadow"
    assert session.actions == [] and len(session.recommendations) == 1


async def test_another_target_is_refused_by_constraint() -> None:
    spent: list[Any] = []
    session = _session(["tmp"])
    decision = await _gate(session, "fs_cleanup", {"target": "journal"}, _authorizer(spent))
    assert isinstance(decision, Deny) and decision.code == "constraint"
    assert spent == [] and session.actions == [] and session.recommendations == []


@pytest.mark.parametrize("targets", [[], None, [""], [7], "tmp"])
async def test_an_empty_or_missing_allowlist_admits_nothing(targets: Any) -> None:
    spent: list[Any] = []
    session = _session(targets)
    for args in ({"target": "tmp"}, {"target": ""}, {}):
        decision = await _gate(session, "fs_cleanup", args, _authorizer(spent))
        assert isinstance(decision, Deny) and decision.code == "constraint", args
    assert spent == [] and session.actions == []


@pytest.mark.parametrize(
    "args",
    [
        {"target": "tmp", "path": "/etc"},
        {"target": "tmp", "all": True},
        {"target": "tmp", "timeout_s": 601},
        {"target": "tmp", "timeout_s": "300"},
        {"target": "tmp", "agent_id": "bob-pc"},
    ],
)
async def test_nothing_rides_along_with_an_allowed_target(args: dict) -> None:
    spent: list[Any] = []
    session = _session(["tmp"])
    decision = await _gate(session, "fs_cleanup", args, _authorizer(spent))
    assert isinstance(decision, Deny) and decision.code in ("constraint", "out_of_scope")
    assert spent == [] and session.actions == []


async def test_the_timeout_ceiling_is_the_declared_one() -> None:
    spent: list[Any] = []
    session = _session(["tmp"])
    assert isinstance(
        await _gate(session, "fs_cleanup", {"target": "tmp", "timeout_s": 300}, _authorizer(spent)), Allow
    )


async def test_reads_and_the_verdict_are_allowed_in_both_modes_and_nothing_else_is() -> None:
    for mode in ("shadow", "act"):
        session = _session([], mode=mode)
        assert isinstance(await _gate(session, "fs_disk_usage", {}), Allow)
        assert isinstance(await _gate(session, "fs_cleanup_scan", {"target": "journal"}), Allow)
        verdict = {"verdict": "clean", "finding": "-", "evidence": "-"}
        assert isinstance(await _gate(session, STEWARD_VERDICT_TOOL, verdict), Allow)
    session = _session(SUGGESTED)
    for tool, args in (
        ("shell_exec", {"command": "rm -rf /"}),
        ("fs_read", {"path": "/etc/shadow"}),
        ("agent_snapshot", {"id": HOST}),
        ("winget_update", {"id": "x"}),
    ):
        decision = await _gate(session, tool, args)
        assert isinstance(decision, Deny) and decision.code == "forbidden"


async def test_the_steward_reaches_no_other_host() -> None:
    spent: list[Any] = []
    session = _session(["tmp"])
    for tool, args in (
        ("fs_disk_usage", {"agent_id": "bob-pc"}),
        ("fs_cleanup_scan", {"target": "tmp", "agent_id": "bob-pc"}),
        ("fs_cleanup", {"target": "tmp", "agent_id": "bob-pc"}),
    ):
        decision = await _gate(session, tool, args, _authorizer(spent), host="bob-pc")
        assert isinstance(decision, Deny) and decision.code == "out_of_scope", tool
    assert spent == [] and session.actions == []


# -- the real loop -------------------------------------------------------------


async def test_joined_the_real_loop_sends_only_the_allowed_target(tmp_path) -> None:
    telemetry = TelemetryStore(db_path=str(tmp_path / "disk.sqlite"))
    await telemetry.connect()
    try:
        registry = AgentRegistry(tokens={HOST: "t"})
        tunnel = AgentTunnel(registry, telemetry, EventStore(db_path=telemetry.db_path))
        sent: list[tuple[str, str, dict]] = []

        async def send_request(agent_id: str, tool: str, args: dict, timeout_s: float):
            sent.append((agent_id, tool, dict(args)))
            return {"ok": True}

        tunnel.send_request = send_request  # type: ignore[method-assign]
        executor = ToolExecutor(
            registry=registry, store=telemetry, tunnel=tunnel, call_log=CallLog(),
            screenshots=ScreenshotStore(),
        )
        spent: list[Any] = []
        session = _session(["tmp"])
        session.messages.append({"role": "user", "content": "start"})
        client = FakeAnthropic(
            [
                _Response(
                    [
                        tool_use_block("a", "fs_cleanup", {"target": "journal"}),
                        tool_use_block("b", "fs_cleanup", {"target": "tmp"}),
                        tool_use_block("c", "fs_cleanup", {"target": "tmp", "path": "/"}),
                    ],
                    "tool_use",
                ),
                _Response([text_block("done")], "end_turn"),
            ]
        )
        events = [
            ev
            async for ev in toolloop.drive_events(
                session, executor, client=client, model="m",
                policy=StewardPolicy(session, authorizer=_authorizer(spent)),
                max_iterations=SPEC.budget.max_iterations,
            )
        ]
        assert sent == [(HOST, "fs_cleanup", {"target": "tmp"})]
        denied = [e for e in events if e["type"] == "denied"]
        assert [e["args"] for e in denied] == [{"target": "journal"}, {"target": "tmp", "path": "/"}]
    finally:
        await telemetry.close()


@pytest.fixture
async def world(tmp_path):
    w = World(str(tmp_path / "disk-runner.sqlite"))
    await w.setup()
    w.authorizations = AuthorizationStore(w.db_path)  # type: ignore[attr-defined]
    await w.authorizations.connect()  # type: ignore[attr-defined]

    async def send(_frame: Any) -> None:  # pragma: no cover - never used
        return None

    w.registry.mark_online(HOST, {"os": "linux"}, send)
    yield w
    await w.authorizations.close()  # type: ignore[attr-defined]
    await w.close()


async def test_joined_a_run_in_act_cleans_the_allowed_target_and_briefs_by_os(world: World) -> None:
    """``run_generic`` -> gate -> loop -> executor, with a real authorization."""

    runner = world.runner()
    runner.authorizations = world.authorizations  # type: ignore[attr-defined]
    runner._now = lambda: NOW  # type: ignore[method-assign]
    await runner.set_params(
        "disk_hygiene", {"targets": ["journal", "temp_users", "tmp"], "hosts": [HOST]}, actor="admin"
    )
    await runner.set_mode(
        "disk_hygiene", "act", actor="admin", effective_hash=await runner.live_hash("disk_hygiene")
    )
    grant = await runner.grant(
        "disk_hygiene", tool="fs_cleanup", scope=[HOST], max_attempts_per_day=3,
        expires_at=NOW + timedelta(days=7), actor="admin",
        effective_hash=await runner.live_hash("disk_hygiene"),
    )
    client = FakeAnthropic(
        [
            _tool("t0", "fs_disk_usage", {}),
            _tool("t1", "fs_cleanup_scan", {"target": "tmp"}),
            _tool("t2", "fs_cleanup", {"target": "tmp"}),
            _tool("t3", "fs_cleanup", {"target": "recycle_bin"}),
            _text("done"),
        ]
    )
    run = await runner.run_generic(
        CATALOG["disk_hygiene"], host_id=HOST, trigger="on_demand", brief="Scheduled run.",
        client=client, model="test-model", executor=world.executor,
    )

    assert run is not None and (run.status, run.mode) == ("completed", "act")
    assert [(s["tool"], s["args"]) for s in world.sent] == [
        ("fs_disk_usage", {}), ("fs_cleanup_scan", {"target": "tmp"}), ("fs_cleanup", {"target": "tmp"}),
    ]
    assert [(a["tool"], a["args"], a["authorization_id"]) for a in run.actions] == [
        ("fs_cleanup", {"target": "tmp"}, grant.id)
    ]
    assert run.recommendations == []  # recycle_bin was outside the allowlist: neither
    # The host is a Linux one: the brief names the allowed targets it serves, not temp_users.
    first_user_message = client.messages.calls[0]["messages"][0]["content"]
    assert "journal, tmp." in first_user_message and "temp_users" not in first_user_message


async def test_the_brief_says_so_when_no_target_is_allowed(world: World) -> None:
    runner = world.runner()
    run = await runner.run_generic(
        CATALOG["disk_hygiene"], host_id=HOST, trigger="on_demand", brief="Scheduled run.",
        client=(client := FakeAnthropic([_text("nothing to do")])), model="test-model",
        executor=world.executor,
    )
    assert run is not None and run.status == "completed"
    assert "No cleanup target is allowed on this machine" in client.messages.calls[0]["messages"][0]["content"]


# -- the host: Windows and Linux only ------------------------------------------


@pytest.mark.parametrize("os_name, served", [("windows", True), ("linux", True), ("macos", False), (None, False)])
def test_only_windows_and_linux_hosts_are_served(os_name: str | None, served: bool) -> None:
    assert host_supports(SPEC, os_name) is served


async def test_a_macos_host_is_not_run_on(tmp_path) -> None:
    w = SchedulerWorld(str(tmp_path / "disk-mac.sqlite"))
    await w.setup()
    try:
        async def send(_f: Any) -> None:  # pragma: no cover
            return None

        w.registry.mark_online("mac-pc", {"os": "macos"}, send)
        assert "macos" in (machine_unready(SPEC, w.executor, "mac-pc") or "")
        assert machine_unready(SPEC, w.executor, WIN_A) is None
    finally:
        await w.close()


# -- the precondition ----------------------------------------------------------


def _snapshot(*percents: Any) -> dict[str, Any]:
    return {"disk": {"status": "ok", "summary": "", "volumes": [
        {"mount": f"/v{i}", "percent_used": p} for i, p in enumerate(percents)
    ]}}


@pytest.fixture
async def telemetry(tmp_path):
    store = TelemetryStore(db_path=str(tmp_path / "pre.sqlite"))
    await store.connect()
    yield store
    await store.close()


async def _put(store: TelemetryStore, host: str, snapshot: dict, *, age: timedelta = timedelta(0)) -> None:
    stamp = (NOW - age).isoformat()
    await store.insert(host, stamp, snapshot, received_at=stamp)


async def _ask(store: TelemetryStore, host: str | None = "h", params: dict | None = None) -> str | None:
    return await preconditions.disk_pressure(store, host, params or {}, now=lambda: NOW)


async def test_below_the_threshold_skips_with_a_stable_reason(telemetry) -> None:
    await _put(telemetry, "h", _snapshot(40, 79))
    first = await _ask(telemetry)
    assert first == preconditions.DISK_PRESSURE_SKIP == "no volume at or above the usage threshold"
    # The reason carries no measurement: another reading skips with the same text.
    await _put(telemetry, "h", _snapshot(12))
    assert await _ask(telemetry) == first


async def test_the_default_threshold_is_80_and_the_parameter_moves_it(telemetry) -> None:
    await _put(telemetry, "h", _snapshot(80))
    assert await _ask(telemetry) is None
    assert await _ask(telemetry, params={"min_percent_used": 81}) == preconditions.DISK_PRESSURE_SKIP
    assert await _ask(telemetry, params={"min_percent_used": 80}) is None
    # Not a usable percent: the default applies.
    assert await _ask(telemetry, params={"min_percent_used": "5"}) is None
    await _put(telemetry, "h", _snapshot(79))
    assert await _ask(telemetry, params={"min_percent_used": "5"}) == preconditions.DISK_PRESSURE_SKIP
    assert await _ask(telemetry, params={"min_percent_used": 50}) is None


async def test_the_fullest_volume_decides(telemetry) -> None:
    await _put(telemetry, "h", _snapshot(10, 91, 30))
    assert await _ask(telemetry) is None


@pytest.mark.parametrize(
    "snapshot",
    [{}, {"disk": {}}, {"disk": {"volumes": []}}, {"disk": {"volumes": "x"}},
     {"disk": {"volumes": [{"percent_used": "95"}, {"percent_used": True}, {"percent_used": None}]}},
     {"disk": {"volumes": [{"percent_used": float("nan")}]}}],
)
async def test_unreadable_telemetry_proceeds(telemetry, snapshot: dict) -> None:
    await _put(telemetry, "h", snapshot)
    assert await _ask(telemetry) is None


async def test_missing_or_stale_telemetry_proceeds(telemetry) -> None:
    assert await _ask(telemetry, host="never-seen") is None
    assert await _ask(telemetry, host=None) is None
    await _put(telemetry, "old", _snapshot(5), age=preconditions.STALE_AFTER + timedelta(minutes=1))
    assert await _ask(telemetry, host="old") is None
    await _put(telemetry, "fresh", _snapshot(5), age=preconditions.STALE_AFTER - timedelta(minutes=1))
    assert await _ask(telemetry, host="fresh") == preconditions.DISK_PRESSURE_SKIP


async def test_the_scheduler_skips_a_host_below_the_threshold_without_a_model_call(tmp_path) -> None:
    w = SchedulerWorld(str(tmp_path / "disk-sched.sqlite"))
    await w.setup()
    try:
        runner = ScriptedRunner(
            store=w.steward_store, settings=w.settings, catalog=CATALOG,
            event_store=w.event_store, now=lambda: w.now,
        )
        preconditions.register(runner, w.telemetry, now=lambda: w.now)
        stamp = w.now.isoformat()
        await w.telemetry.insert(WIN_A, stamp, _snapshot(41), received_at=stamp)
        await w.telemetry.insert(WIN_B, stamp, _snapshot(93), received_at=stamp)
        w.configure(
            "disk_hygiene", window=NIGHT, hosts=[WIN_A, WIN_B], targets=SUGGESTED, require_idle=False
        )
        sched = StewardScheduler(runner, CATALOG, w.params_of, w.executor, now=lambda: w.now)
        outcomes = await sched.pass_once()
        assert [(o.host_id, o.kind) for o in outcomes] == [(WIN_A, "skipped"), (WIN_B, "ran")]
        assert outcomes[0].detail == preconditions.DISK_PRESSURE_SKIP
        assert [c.host for c in runner.calls] == [WIN_B]  # the skipped host never reached run_generic
        # Recorded once per occurrence, with the stable reason.
        w.now += timedelta(minutes=30)
        await sched.pass_once()
        runs = await w.steward_store.list_runs(steward_id="disk_hygiene", limit=20)
        skipped = [r for r in runs if r.status == "skipped"]
        assert [(r.host_id, r.error) for r in skipped] == [(WIN_A, preconditions.DISK_PRESSURE_SKIP)]
    finally:
        await w.close()


async def test_a_host_without_telemetry_is_run_on(tmp_path) -> None:
    w = SchedulerWorld(str(tmp_path / "disk-notel.sqlite"))
    await w.setup()
    try:
        runner = ScriptedRunner(
            store=w.steward_store, settings=w.settings, catalog=CATALOG,
            event_store=w.event_store, now=lambda: w.now,
        )
        preconditions.register(runner, w.telemetry, now=lambda: w.now)
        w.configure("disk_hygiene", window=NIGHT, hosts=[WIN_A], targets=SUGGESTED, require_idle=False)
        sched = StewardScheduler(runner, CATALOG, w.params_of, w.executor, now=lambda: w.now)
        assert [(o.host_id, o.kind) for o in await sched.pass_once()] == [(WIN_A, "ran")]
    finally:
        await w.close()


# -- the seam: declared preconditions vs registered ones ------------------------


def test_every_precondition_a_catalog_spec_declares_is_defined_here() -> None:
    declared = {name for spec in CATALOG.values() for name in spec.preconditions}
    runner = SimpleNamespace(names=set())
    runner.register_precondition = lambda name, _check: runner.names.add(name)
    preconditions.register(runner, object())
    assert declared == runner.names


def test_the_built_server_registers_every_declared_precondition(tmp_path, monkeypatch) -> None:
    app = build_app(db_path=str(tmp_path / "built.sqlite"))
    declared = {name for spec in CATALOG.values() for name in spec.preconditions}
    assert declared and declared <= app.state.stewards.preconditions()

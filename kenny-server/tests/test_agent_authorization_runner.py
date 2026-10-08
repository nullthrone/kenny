"""Standing authorizations and parameters, joined through the runner (ADR-0072).

Every test here runs the real path: ``AgentRunner.run_generic`` -> the real
``AgentPolicy`` -> ``toolloop.drive_events`` -> the real ``ToolExecutor`` ->
``CallLog`` on a real ``EventStore``, with the runner's own default authorizer
consuming from a real ``AuthorizationStore``. Only the tunnel's wire send is
fake. So "the audit row names the authorization" means the id travelled from
the store, through the gate's ``Allow``, through the loop, to the row.

The binding rules are tested where they bite: a parameter edit and a catalog
change each drop ``act`` to ``shadow`` *live* and void what was granted, and a
run in flight stops acting the moment the agent stops being what it started as.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest

from kenny_server import toolloop
from kenny_server.agents.authorizations import AuthorizationStore
from kenny_server.agents.catalog import CATALOG
from kenny_server.agents.runner import AgentRunner, validate_params
from kenny_server.agents.spec import AgentSpec, ArgConstraint, Trigger, effective_hash
from kenny_server.tool_classes import NORMAL_CHANGE

from test_agent_runner import HOST, World, _text, _tool
from test_chat import FakeAnthropic

SEVENZIP = "7zip.7zip"
FIREFOX = "Mozilla.Firefox"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _installer(**overrides: Any) -> AgentSpec:
    """An agent with one parameter-fed ``normal_change``: install from an allowlist."""

    fields: dict[str, Any] = {
        "id": "installer",
        "title": "Installer",
        "description": "Installs what the household allowed.",
        "prompt": "You install the packages this household allowed, nothing else.",
        "trigger": Trigger(kind="on_demand"),
        "tools": frozenset({"winget_list", "winget_install", "winget_update"}),
        "constraints": (
            ArgConstraint("winget_install", "id", param="packages"),
            ArgConstraint("winget_update", "id", param="packages"),
        ),
        "params": ("packages",),
    }
    fields.update(overrides)
    return AgentSpec(**fields)


def _catalog(*specs: AgentSpec) -> Any:
    return MappingProxyType({"triage": CATALOG["triage"], **{s.id: s for s in specs}})


@pytest.fixture
async def world(tmp_path):
    w = World(str(tmp_path / "auth-runner.sqlite"))
    await w.setup()
    w.authorizations = AuthorizationStore(w.db_path)  # type: ignore[attr-defined]
    await w.authorizations.connect()  # type: ignore[attr-defined]
    yield w
    await w.authorizations.close()  # type: ignore[attr-defined]
    await w.close()


def _runner(world: World, *specs: AgentSpec) -> AgentRunner:
    runner = world.runner(catalog=_catalog(*specs))
    runner.authorizations = world.authorizations  # type: ignore[attr-defined]
    runner._now = lambda: NOW  # type: ignore[method-assign]
    return runner


async def _run(world: World, runner: AgentRunner, spec: AgentSpec, *calls: tuple[str, dict]):
    scripted = [_tool(f"tu{i}", name, args) for i, (name, args) in enumerate(calls)]
    scripted.append(_text("done"))
    return await runner.run_generic(
        spec,
        host_id=HOST,
        trigger="on_demand",
        brief="Install what is allowed.",
        client=FakeAnthropic(scripted),
        model="test-model",
        executor=world.executor,
    )


async def _grant(runner: AgentRunner, **overrides: Any):
    kwargs: dict[str, Any] = {
        "tool": "winget_install",
        "scope": [HOST],
        "max_attempts_per_day": 1,
        "expires_at": NOW + timedelta(days=7),
        "actor": "admin",
    }
    kwargs.update(overrides)
    return await runner.grant("installer", **kwargs)


async def _logs(world: World) -> list[dict[str, Any]]:
    return await world.event_store.query(kind="log", limit=200)


# -- the joined gate path ------------------------------------------------------


async def test_an_authorized_normal_change_runs_and_names_its_authorization(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    assert await runner.set_mode("installer", "act", actor="admin") == "act"
    grant = await _grant(runner)

    run = await _run(world, runner, spec, ("winget_install", {"id": SEVENZIP}))

    assert run is not None and (run.status, run.mode) == ("completed", "act")
    assert world.sent == [{"agent_id": HOST, "tool": "winget_install", "args": {"id": SEVENZIP}}]
    assert run.actions == [
        {"tool": "winget_install", "args": {"id": SEVENZIP}, "agent_id": HOST,
         "tool_class": NORMAL_CHANGE, "authorization_id": grant.id, "ok": True}
    ]
    assert run.recommendations == []
    # The run is bound to what the agent was when it started.
    assert run.params == {"packages": [SEVENZIP]}
    assert run.effective_hash == effective_hash(spec, {"packages": [SEVENZIP]})
    assert run.effective_hash == grant.effective_hash
    # The audit row names the agent, the run and the authorization.
    [entry] = await world.call_log.list()
    assert (entry["tool"], entry["actor"], entry["run_id"], entry["authorization_id"]) == (
        "winget_install",
        "agent:installer",
        run.id,
        grant.id,
    )
    [row] = await world.event_store.query(kind="audit", limit=10)
    assert row["fields"]["authorization_id"] == grant.id
    assert "authorization_id" not in row["fields"]["args"]
    # The attempt was spent before the call ran, against this run.
    assert await world.authorizations.uses_since(grant.id, NOW - timedelta(hours=1)) == {HOST: 1}


async def test_without_a_matching_authorization_it_is_a_recommendation(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    await _grant(runner, scope=["another-pc"])

    run = await _run(world, runner, spec, ("winget_install", {"id": SEVENZIP}))

    assert run is not None and world.sent == []
    assert run.actions == []
    # A recommendation names no authorization: a person acts on it as themselves.
    assert run.recommendations == [
        {"tool": "winget_install", "args": {"id": SEVENZIP}, "agent_id": HOST,
         "tool_class": NORMAL_CHANGE}
    ]


async def test_a_spent_budget_turns_the_next_run_into_a_recommendation(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    await _grant(runner, max_attempts_per_day=1)

    first = await _run(world, runner, spec, ("winget_install", {"id": SEVENZIP}))
    second = await _run(world, runner, spec, ("winget_install", {"id": SEVENZIP}))

    assert first is not None and second is not None
    assert [a["tool"] for a in first.actions] == ["winget_install"]
    assert second.actions == [] and [r["tool"] for r in second.recommendations] == [
        "winget_install"
    ]
    assert len(world.sent) == 1


async def test_a_failed_call_still_spends_its_attempt(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    grant = await _grant(runner, max_attempts_per_day=1)

    async def fail(tool: str, args: dict[str, Any]) -> None:
        from kenny_server.tunnel import ToolError

        raise ToolError("exec_failed", "winget exited 1")

    world.on_send = fail
    run = await _run(world, runner, spec, ("winget_install", {"id": SEVENZIP}))
    assert run is not None and run.actions[0]["ok"] is False
    assert run.actions[0]["authorization_id"] == grant.id
    assert await world.authorizations.uses_since(grant.id, NOW - timedelta(hours=1)) == {HOST: 1}


async def test_a_parameter_feeds_the_constraint_the_gate_enforces(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    await _grant(runner, max_attempts_per_day=5)

    run = await _run(world, runner, spec, ("winget_install", {"id": FIREFOX}))
    # Outside the parameter is outside the agent: neither an action nor a recommendation.
    assert run is not None and run.actions == [] and run.recommendations == []
    assert world.sent == []


async def test_an_unset_parameter_admits_nothing(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    run = await _run(world, runner, spec, ("winget_install", {"id": SEVENZIP}))
    assert run is not None and run.recommendations == [] and world.sent == []
    assert run.params == {}


async def test_a_server_side_normal_change_names_its_authorization(world, monkeypatch) -> None:
    """``ticket_rule_remove`` dispatched in the loop, under the ``server`` sentinel."""

    monkeypatch.setitem(
        toolloop.SERVER_TOOLS,
        "ticket_rule_remove",
        {
            "description": "Remove an auto-ticket rule.",
            "properties": {"rule_id": {"type": "string"}},
            "required": ["rule_id"],
        },
    )
    removed: list[str] = []

    async def remove(args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        removed.append(args["rule_id"])
        return {"removed": args["rule_id"]}

    world.executor.register_server_tool("ticket_rule_remove", remove)
    spec = AgentSpec(
        id="pruner",
        title="Pruner",
        description="Drops rules nothing matches.",
        prompt="You prune ticket rules nothing has matched.",
        trigger=Trigger(kind="on_demand"),
        tools=frozenset({"ticket_rule_remove"}),
        constraints=(ArgConstraint("ticket_rule_remove", "rule_id", param="rules"),),
        params=("rules",),
    )
    runner = _runner(world, spec)
    await runner.set_params("pruner", {"rules": ["r1"]}, actor="admin")
    await runner.set_mode("pruner", "act", actor="admin")
    grant = await runner.grant(
        "pruner",
        tool="ticket_rule_remove",
        scope="server",
        max_attempts_per_day=1,
        expires_at=NOW + timedelta(days=1),
        actor="admin",
    )
    run = await runner.run_generic(
        spec,
        host_id=None,
        trigger="on_demand",
        brief="Prune.",
        client=FakeAnthropic(
            [_tool("tu0", "ticket_rule_remove", {"rule_id": "r1"}), _text("done")]
        ),
        model="test-model",
        executor=world.executor,
    )
    assert run is not None and removed == ["r1"]
    assert run.actions == [
        {"tool": "ticket_rule_remove", "args": {"rule_id": "r1"}, "agent_id": None,
         "tool_class": NORMAL_CHANGE, "authorization_id": grant.id, "ok": True}
    ]


# -- configure: the scheduler's way in -----------------------------------------


async def test_run_generic_uses_what_configure_set(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    client = FakeAnthropic([_tool("tu0", "winget_list", {}), _text("done")])
    with pytest.raises(RuntimeError, match="configure"):
        await runner.run_generic(spec, host_id=HOST, trigger="schedule", brief="go")
    runner.configure(executor=world.executor, client_factory=lambda: client, model=lambda: "m-1")
    run = await runner.run_generic(spec, host_id=HOST, trigger="schedule", brief="go")
    assert run is not None and run.status == "completed" and run.trigger == "schedule"
    assert client.messages.calls[0]["model"] == "m-1"
    assert [s["tool"] for s in world.sent] == ["winget_list"]


# -- rule 6: a parameter edit --------------------------------------------------


async def test_a_parameter_edit_drops_act_to_shadow_and_voids_for_good(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    grant = await _grant(runner)

    result = await runner.set_params(
        "installer", {"packages": [SEVENZIP, FIREFOX]}, actor="root"
    )

    assert result["mode"] == "shadow" and result["voided"] == 1
    assert result["effective_hash"] == effective_hash(spec, {"packages": [FIREFOX, SEVENZIP]})
    assert await runner.mode_of("installer") == "shadow"
    voided = await world.authorizations.get(grant.id)
    assert voided is not None and voided.status(NOW) == "voided" and voided.voided_by == "root"
    messages = [row["message"] for row in await _logs(world)]
    assert "agent installer: mode set to shadow by root: its parameters changed" in messages
    assert "agent installer: parameters changed by root" in messages

    # Putting the old parameters back revives neither act nor the grant.
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="root")
    assert await runner.mode_of("installer") == "shadow"
    assert (await world.authorizations.get(grant.id)).status(NOW) == "voided"  # type: ignore[union-attr]
    assert await _grant(runner) is not None  # a fresh grant binds to what it is now


async def test_writing_the_same_parameters_changes_nothing(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    grant = await _grant(runner)
    result = await runner.set_params("installer", {"packages": [SEVENZIP, SEVENZIP]}, actor="x")
    assert (result["mode"], result["voided"]) == ("act", 0)
    assert (await world.authorizations.get(grant.id)).status(NOW) == "live"  # type: ignore[union-attr]


async def test_a_parameter_edit_mid_run_ends_its_act_even_if_act_is_chosen_again(world) -> None:
    """``still_acting`` compares the live hash with the run's, not only the mode.

    A ``standard_change`` consults no authorizer, so ``still_acting`` is the
    only thing between it and the host here.
    """

    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    await _grant(runner, max_attempts_per_day=5)

    async def edit_then_promote(tool: str, args: dict[str, Any]) -> None:
        if tool == "winget_list":
            await runner.set_params("installer", {"packages": [SEVENZIP, FIREFOX]}, actor="root")
            await runner.set_mode("installer", "act", actor="root")
            await _grant(runner, max_attempts_per_day=5)

    world.on_send = edit_then_promote
    run = await _run(
        world,
        runner,
        spec,
        ("winget_list", {}),
        ("winget_update", {"id": SEVENZIP}),
        ("winget_install", {"id": SEVENZIP}),
    )
    assert await runner.mode_of("installer") == "act"  # bound again, to the new hash
    assert run is not None and [s["tool"] for s in world.sent] == ["winget_list"]
    assert run.actions == []
    assert [r["tool"] for r in run.recommendations] == ["winget_update", "winget_install"]


async def test_a_parameter_the_spec_does_not_take_is_refused(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    for bad in (
        {"other": ["x"]},
        {"packages": "7zip.7zip"},
        {"packages": [""]},
        {"packages": [1]},
        {"packages": ["x" * 201]},
    ):
        with pytest.raises(ValueError):
            await runner.set_params("installer", bad, actor="admin")
    with pytest.raises(ValueError):
        await runner.set_params("triage", {"packages": ["x"]}, actor="admin")
    with pytest.raises(KeyError):
        await runner.set_params("nope", {}, actor="admin")
    assert await runner.get_params("installer") == {}


def test_a_window_parameter_goes_through_the_web_filter_parser() -> None:
    spec = _installer(params=("packages", "window"))
    clean = validate_params(
        spec, {"window": {"days": "mon,tue", "start": "2:00", "end": "04:30", "tz": "UTC"}}
    )
    assert clean == {"window": {"days": ["mon", "tue"], "start": "02:00", "end": "04:30", "tz": "UTC"}}
    for bad in (
        {"window": {"days": "someday", "start": "02:00", "end": "04:00"}},
        {"window": {"days": "mon", "start": "02:00", "end": "02:00"}},
        {"window": {"days": "mon", "start": "25:00", "end": "02:00"}},
        {"window": {"days": "mon", "start": "01:00", "end": "02:00", "tz": "Mars/Base"}},
        {"window": {"days": "mon", "start": "01:00", "end": "02:00", "categories": ["x"]}},
        {"window": ["mon"]},
    ):
        with pytest.raises(ValueError):
            validate_params(spec, bad)


# -- rule 6: a catalog change --------------------------------------------------


async def test_a_catalog_change_drops_act_to_shadow_live_and_voids(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    grant = await _grant(runner)

    # A new release edits the spec (here: its prompt). Nothing else is touched.
    edited = _installer(prompt="You install whatever looks useful.")
    runner.catalog = _catalog(edited)
    assert await runner.mode_of("installer") == "shadow"
    assert (await world.authorizations.get(grant.id)).status(NOW) == "voided"  # type: ignore[union-attr]
    warnings = [r for r in await _logs(world) if r["level"] == "warning"]
    assert any("effective hash changed" in r["message"] for r in warnings)
    assert any(r["fields"].get("voided") == 1 for r in warnings)

    # Rolling the code back does not promote it or revive the grant.
    runner.catalog = _catalog(spec)
    assert await runner.mode_of("installer") == "shadow"
    assert (await world.authorizations.get(grant.id)).status(NOW) == "voided"  # type: ignore[union-attr]


async def test_a_retier_unbinds_act(world, monkeypatch) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_mode("installer", "act", actor="admin")
    from kenny_server import tool_classes

    monkeypatch.setitem(tool_classes.TOOL_CLASSES, "winget_install", "standard_change")
    assert await runner.mode_of("installer") == "shadow"


async def test_a_catalog_change_mid_run_ends_its_act(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    await _grant(runner, max_attempts_per_day=5)

    async def release(tool: str, args: dict[str, Any]) -> None:
        if tool == "winget_list":
            runner.catalog = _catalog(_installer(prompt="Edited mid-run."))

    world.on_send = release
    run = await _run(
        world, runner, spec, ("winget_list", {}), ("winget_update", {"id": SEVENZIP})
    )
    assert run is not None and run.actions == []
    assert [r["tool"] for r in run.recommendations] == ["winget_update"]


async def test_startup_voids_what_a_new_release_unbound(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    grant = await _grant(runner)  # shadow; the grant waits for act
    restarted = _runner(world, _installer(prompt="The next release's prompt."))
    await restarted.startup()
    assert (await world.authorizations.get(grant.id)).status(NOW) == "voided"  # type: ignore[union-attr]


async def test_act_stored_without_a_hash_is_not_act(world) -> None:
    """A row from before act was bound, or written past the runner, binds nothing."""

    spec = _installer()
    runner = _runner(world, spec)
    await world.agent_store.set_mode("installer", "act", actor="admin")
    assert await runner.mode_of("installer") == "shadow"
    assert await world.agent_store.get_mode("installer") == "shadow"


async def test_overview_says_what_act_is_bound_to(world) -> None:
    spec = _installer()
    runner = _runner(world, spec)
    await runner.set_params("installer", {"packages": [SEVENZIP]}, actor="admin")
    await runner.set_mode("installer", "act", actor="admin")
    overview = {a["id"]: a for a in await runner.overview()}
    installer = overview["installer"]
    assert installer["params"] == {"packages": [SEVENZIP]}
    assert installer["effective_hash"] == effective_hash(spec, {"packages": [SEVENZIP]})
    assert (installer["mode"], installer["act_bound"]) == ("act", True)
    assert overview["triage"]["act_bound"] is None
    assert overview["triage"]["params"] == {}

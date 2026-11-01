"""Built stewards (ADR-0074): what data may make, and how it is bound, joined through the runner.

The rules are tested against the sets they are made of (the copilot's tools,
the ticket surface, the fleet-wide reads, the steward-only rule tools, the
tools no authorization may cover), so a tool added to one of those sets is
refused to a built steward without anyone editing this file.

The binding rules run the real path: the runner's own stores, its consent lock,
:meth:`StewardRunner.startup` on a second runner over the same database (a
restart), and ``run_generic`` through the real gate and tool loop with only the
wire send faked — so "a built steward runs under the same gate" is shown, not
assumed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kenny_server.stewards.authorizations import AuthorizationStore, NEVER_AUTHORIZED
from kenny_server.stewards.builder import (
    BUILDER_TOOLS,
    LIMITS,
    BuilderError,
    build_from_data,
    builder_catalog,
    clone_data,
    spec_from_data,
    spec_to_data,
    tool_refusal,
    validate_builder,
)
from kenny_server.stewards.catalog import CATALOG, TICKET_SURFACE_TOOLS, StewardCatalog
from kenny_server.stewards.events import EventTrigger
from kenny_server.stewards.runner import HashMismatch, StewardRunner
from kenny_server.stewards.scheduler import StewardScheduler
from kenny_server.stewards.spec import TriggerSet
from kenny_server.stewards.store import SpecConflict
from kenny_server.ticket_assistant import FLEET_WIDE_TOOLS
from kenny_server.tool_classes import NORMAL_CHANGE, SENSITIVE_TOOLS
from kenny_server.toolloop import COPILOT_STEWARD_TOOLS, STEWARD_ONLY_TOOLS, STEWARD_VERDICT_TOOL

from test_chat import FakeAnthropic
from test_steward_runner import HOST, World, _text, _tool

SEVENZIP = "7zip.7zip"
FIREFOX = "Mozilla.Firefox"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ID = "custom_installer"

#: Every tool a built steward may never name, from the sets ADR-0074 rule 2 names.
BANNED: frozenset[str] = (
    NEVER_AUTHORIZED
    | COPILOT_STEWARD_TOOLS
    | TICKET_SURFACE_TOOLS
    | FLEET_WIDE_TOOLS
    | {"select_agent"}
    | STEWARD_ONLY_TOOLS
)


class _Registry:
    """The runner's precondition registry and catalog, as ``validate_builder`` reads them."""

    def __init__(self, *preconditions: str) -> None:
        self.catalog = StewardCatalog(CATALOG)
        self._pre = frozenset(preconditions)

    def preconditions(self) -> frozenset[str]:
        return self._pre


def _data(**overrides: Any) -> dict[str, Any]:
    """A built installer: a parameter-fed ``normal_change`` on the hosts an admin names."""

    data: dict[str, Any] = {
        "id": ID,
        "title": "Installer",
        "description": "Installs what the household allowed.",
        "prompt": "Install the packages the task message lists, one per call, nothing else.",
        "trigger": {"kind": "schedule", "min_interval_days": None},
        "triggers_allowed": {"schedule": True, "events": [], "command": True},
        "tools": ["winget_list", "winget_install"],
        "constraints": [{"tool": "winget_install", "arg": "id", "param": "packages"}],
        "params": ["window", "hosts", "packages"],
    }
    data.update(overrides)
    return data


def _errors(data: Any, runner: Any = None) -> list[dict[str, str]]:
    with pytest.raises(BuilderError) as caught:
        build_from_data(data, runner if runner is not None else _Registry("disk_pressure"))
    return caught.value.errors


def _fields(data: Any, runner: Any = None) -> set[str]:
    return {e["field"] for e in _errors(data, runner)}


# -- what data may make -----------------------------------------------------------


def test_a_valid_built_steward_is_forced_to_the_servers_choices() -> None:
    spec = build_from_data(_data(), _Registry())
    assert spec.verdict_tool == STEWARD_VERDICT_TOOL and STEWARD_VERDICT_TOOL in spec.tools
    assert spec.default_mode == "shadow" and spec.version == 1
    assert spec.triggers_allowed == TriggerSet(schedule=True, command=True, editable=True)
    # The canonical data form reads back into the same steward.
    again = spec_from_data(spec_to_data(spec))
    assert again.spec_hash == spec.spec_hash
    # The version is the server's: a later one is a different steward.
    assert spec_from_data(spec_to_data(spec), version=2).spec_hash != spec.spec_hash


@pytest.mark.parametrize("tool", sorted(BANNED))
def test_every_banned_tool_is_refused_at_its_field(tool: str) -> None:
    assert tool_refusal(tool) is not None
    assert tool not in BUILDER_TOOLS
    errors = _errors(_data(tools=["winget_list", "winget_install", tool]))
    assert any(e["field"] == "tools[2]" and tool in e["error"] for e in errors), errors


def test_unknown_and_undispatchable_tools_are_refused() -> None:
    assert "tools[1]" in _fields(_data(tools=["winget_list", "no_such_tool"]))
    # Classified, but no steward's tool loop can route it.
    assert tool_refusal("web_activity_query") is not None
    assert "tools[0]" in _fields(_data(tools=["web_activity_query", "winget_install"]))


def test_a_sensitive_tool_needs_its_flag() -> None:
    assert "screen_capture" in SENSITIVE_TOOLS
    data = _data(tools=["winget_list", "winget_install", "screen_capture"])
    errors = _errors(data)
    assert any(e["field"] == "tools[2]" and "sensitive_ok" in e["error"] for e in errors)
    assert "screen_capture" in build_from_data({**data, "sensitive_ok": True}, _Registry()).tools


def test_constraints_take_values_or_a_parameter_never_evidence() -> None:
    evidence = [{"tool": "winget_install", "arg": "id", "evidence": "unused_rules"}]
    assert "constraints[0].evidence" in _fields(_data(constraints=evidence))
    # Literal values work; an empty list is never "all".
    literal = [{"tool": "winget_install", "arg": "id", "allowed": [SEVENZIP]}]
    assert build_from_data(_data(constraints=literal), _Registry()).constraints[0].allowed == {SEVENZIP}
    empty = [{"tool": "winget_install", "arg": "id", "allowed": []}]
    assert "constraints[0].allowed" in _fields(_data(constraints=empty))
    both = [{"tool": "winget_install", "arg": "id", "allowed": [SEVENZIP], "param": "packages"}]
    assert "constraints[0]" in _fields(_data(constraints=both))
    # A parameter feeding a constraint is a list, never a window or a flag.
    window_fed = [{"tool": "winget_install", "arg": "id", "param": "window"}]
    assert "constraints[0].param" in _fields(_data(constraints=window_fed))
    undeclared = [{"tool": "winget_install", "arg": "id", "param": "allowlist"}]
    assert "constraints[0].param" in _fields(_data(constraints=undeclared))
    # Only an argument the tool takes; never the free timeout or the host override.
    for arg in ("version", "timeout_s", "agent_id"):
        bad = [{"tool": "winget_install", "arg": arg, "allowed": ["x"]}]
        assert "constraints[0].arg" in _fields(_data(constraints=bad)), arg
    # A change tool nothing binds is refused at its own field.
    assert "tools[1]" in _fields(_data(constraints=[]))


def test_preconditions_come_from_the_registered_library() -> None:
    data = _data(preconditions=["disk_pressure"])
    assert build_from_data(data, _Registry("disk_pressure")).preconditions == ("disk_pressure",)
    assert "preconditions[0]" in _fields(data, _Registry())


def test_a_new_ticket_never_starts_a_built_steward() -> None:
    on_ticket = _data(trigger={"kind": "event", "event": "ticket_created"})
    assert "trigger.event" in _fields(on_ticket)
    allowed = _data(triggers_allowed={"schedule": True, "events": ["ticket_created"], "command": True})
    assert "triggers_allowed.events[0]" in _fields(allowed)
    # An alert may.
    alert = _data(triggers_allowed={"schedule": True, "events": ["alert:offline"], "command": False})
    assert build_from_data(alert, _Registry()).triggers_allowed.events == {"alert:offline"}


def test_what_the_server_decides_is_not_the_datas() -> None:
    for key, value in (
        ("verdict_tool", "ticket_triage_verdict"),
        ("default_mode", "act"),
        ("version", 7),
        ("editable", False),
        ("evidence", []),
    ):
        assert key in _fields({**_data(), key: value}), key
    with_editable = _data(triggers_allowed={"schedule": True, "events": [], "command": True, "editable": False})
    assert "triggers_allowed.editable" in _fields(with_editable)
    # A spec assembled outside the data form is held to the same choices.
    spec = build_from_data(_data(), _Registry())
    from dataclasses import replace

    fields = {
        e["field"]
        for e in validate_builder(
            replace(spec, default_mode="off", triggers_allowed=TriggerSet(schedule=True, editable=False)),
            _Registry(),
        )
    }
    assert {"default_mode", "triggers_allowed"} <= fields


@pytest.mark.parametrize(
    "steward_id", ["installer", "patch", "custom_", "custom_X", "custom_" + "a" * 34, 7]
)
def test_ids_are_the_builders_own(steward_id: Any) -> None:
    assert "id" in _fields(_data(id=steward_id))


def test_limits() -> None:
    assert "prompt" in _fields(_data(prompt="x" * (LIMITS["prompt_max"] + 1)))
    assert "prompt" in _fields(_data(prompt="   "))
    assert "title" in _fields(_data(title="t" * (LIMITS["title_max"] + 1)))
    assert "description" in _fields(_data(description="d" * (LIMITS["description_max"] + 1)))
    many = [t for t in BUILDER_TOOLS if t != STEWARD_VERDICT_TOOL][: LIMITS["tools_max"]]
    assert "tools" in _fields(_data(tools=many))
    for bad in (0, 21, True, "8"):
        assert "budget.max_iterations" in _fields(_data(budget={"max_iterations": bad})), bad
    assert build_from_data(_data(budget={"max_iterations": 20}), _Registry()).budget.max_iterations == 20
    for bad in (0, 601, 1.5):
        timeouts = [{"tool": "winget_install", "max_s": bad}]
        assert "timeouts[0].max_s" in _fields(_data(timeouts=timeouts)), bad
    assert "params[3]" in _fields(_data(params=["window", "hosts", "packages", "Bad-Name"]))
    assert "params[3]" in _fields(_data(params=["window", "hosts", "packages", "hosts"]))
    assert "bogus" in _fields({**_data(), "bogus": 1})
    assert "spec" in _fields(["not", "an", "object"])


def test_machines_and_windows_need_their_parameters() -> None:
    assert "params" in _fields(_data(params=["hosts", "packages"]))  # a schedule needs a window
    assert "params" in _fields(_data(params=["window", "packages"]))  # host tools need hosts


def test_every_check_a_shipped_spec_passes_still_applies() -> None:
    # Well-formed, past every builder rule, but no health section raises it:
    # only ``check_dispatchable`` knows, and validate_builder still asks it.
    data = _data(triggers_allowed={"schedule": True, "events": ["alert:health:nonexistent"], "command": False})
    errors = _errors(data)
    assert [e["field"] for e in errors] == ["spec"] and "health section" in errors[0]["error"]


# -- clones ------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["patch", "posture", "disk_hygiene"])
def test_a_clone_of_a_shipped_steward_is_a_valid_built_one(source: str) -> None:
    shipped = CATALOG[source]
    data = clone_data(shipped, f"custom_{source}")
    spec = build_from_data(data, _Registry("disk_pressure"))
    assert spec.id == f"custom_{source}" and spec.title.endswith("(copy)")
    assert spec.tools == shipped.tools and spec.prompt == shipped.prompt
    assert spec.constraints == shipped.constraints and spec.params == shipped.params
    assert spec.spec_hash != shipped.spec_hash
    # Nothing widened.
    assert spec.triggers_allowed == shipped.triggers_allowed


def test_a_clone_keeps_what_a_built_steward_may_not_use_and_says_so() -> None:
    """Documented choice: a clone is faithful; reading it back names every obstacle."""

    data = clone_data(CATALOG["config_hygiene"], "custom_rules")
    assert [c.get("evidence") for c in data["constraints"]] == ["unused_suppressions", "unused_ticket_rules"]
    fields = _fields(data)
    assert {"constraints[0].evidence", "constraints[1].evidence"} <= fields
    banned = [i for i, t in enumerate(data["tools"]) if t in STEWARD_ONLY_TOOLS]
    assert banned and {f"tools[{i}]" for i in banned} <= fields
    # Triage runs on a ticket's surface: refused on its trigger and its tools.
    triage = _fields(clone_data(CATALOG["triage"], "custom_triage"))
    assert "trigger.event" in triage and any(f.startswith("tools[") for f in triage)


def test_the_builder_catalog_offers_nothing_banned() -> None:
    shown = builder_catalog(_Registry("disk_pressure"))
    names = {t["name"] for t in shown["tools"]}
    assert names == set(BUILDER_TOOLS) and not names & BANNED
    assert STEWARD_VERDICT_TOOL in names
    assert all(tool_refusal(n) is None for n in names)
    install = next(t for t in shown["tools"] if t["name"] == "winget_install")
    assert install["tier"] == NORMAL_CHANGE and install["target"] == "host"
    assert {"name": "id", "type": "string", "required": True, "bindable": True} in install["args"]
    assert next(t for t in shown["tools"] if t["name"] == "fs_cleanup")["os"] == ["linux", "windows"]
    assert next(t for t in shown["tools"] if t["name"] == "screen_capture")["sensitive"] is True
    assert shown["preconditions"] == ["disk_pressure"]
    assert "ticket_created" not in shown["events"] and "alert:offline" in shown["events"]
    assert shown["never_authorized"] == sorted(NEVER_AUTHORIZED)
    assert shown["limits"]["prompt_max"] == 8000


def test_the_live_catalog_is_a_snapshot_to_whoever_iterates_it() -> None:
    catalog = StewardCatalog(CATALOG)
    spec = build_from_data(_data(), _Registry())
    seen = []
    for key in catalog:
        seen.append(key)
        catalog.put_built(spec, {})  # mutated mid-iteration: no RuntimeError
    assert seen == list(CATALOG) and ID in catalog and catalog[ID] is spec
    assert list(catalog.values())[-1] is spec
    with pytest.raises(Exception):
        catalog.put_built(CATALOG["patch"], {})  # a shipped steward is never replaced


# -- bound through the runner -------------------------------------------------------


@pytest.fixture
async def world(tmp_path):
    w = World(str(tmp_path / "builder.sqlite"))
    await w.setup()
    w.authorizations = AuthorizationStore(w.db_path)  # type: ignore[attr-defined]
    await w.authorizations.connect()  # type: ignore[attr-defined]
    yield w
    await w.authorizations.close()  # type: ignore[attr-defined]
    await w.close()


def _runner(world: World, *, preconditions: bool = True) -> StewardRunner:
    runner = world.runner()
    runner.authorizations = world.authorizations  # type: ignore[attr-defined]
    runner._now = lambda: NOW  # type: ignore[method-assign]
    if preconditions:
        runner.register_precondition("disk_pressure", lambda _host, _params: None)
    return runner


async def _promote(runner: StewardRunner, steward_id: str = ID) -> Any:
    """Allow 7-Zip on the host, move to ``act`` and grant ``winget_install``: the full consent."""

    await runner.set_params(steward_id, {"hosts": [HOST], "packages": [SEVENZIP]}, actor="admin")
    live = await runner.live_hash(steward_id)
    assert await runner.set_mode(steward_id, "act", actor="admin", effective_hash=live) == "act"
    return await runner.grant(
        steward_id,
        tool="winget_install",
        scope=[HOST],
        max_attempts_per_day=3,
        expires_at=NOW + timedelta(days=7),
        actor="admin",
        effective_hash=live,
    )


async def _status(world: World, auth_id: str) -> str:
    found = await world.authorizations.get(auth_id)  # type: ignore[attr-defined]
    assert found is not None
    return found.status(NOW)


async def test_create_puts_it_in_the_one_catalog_every_consumer_holds(world) -> None:
    runner = _runner(world)
    scheduler = StewardScheduler(runner, runner.catalog, runner.get_params, world.executor)
    events = EventTrigger(runner, runner.catalog, runner.get_params, world.executor)
    created = await runner.create_built(_data(), actor="admin", source="blank")
    assert created["steward_id"] == ID and created["version"] == 1 and created["mode"] == "shadow"
    assert scheduler.catalog is runner.catalog and events.catalog is runner.catalog
    assert ID in scheduler.catalog and ID in events.catalog
    listed = {s["id"]: s for s in await runner.overview()}
    assert listed[ID]["builtin"] is True and listed[ID]["source"] == "blank"
    assert listed[ID]["created_by"] == "admin" and listed[ID]["invalid"] is False
    assert listed["patch"]["builtin"] is False
    with pytest.raises(SpecConflict):
        await runner.create_built(_data(), actor="admin")
    with pytest.raises(BuilderError):
        await runner.create_built(_data(id="custom_other", tools=["shell_exec"]), actor="admin")
    assert "custom_other" not in runner.catalog


async def test_a_save_is_a_new_version_shadow_and_void_and_a_revert_revives_nothing(world) -> None:
    runner = _runner(world)
    await runner.create_built(_data(), actor="admin")
    grant = await _promote(runner)
    first_hash = await runner.live_hash(ID)
    v1_spec_hash = runner.catalog[ID].spec_hash

    with pytest.raises(HashMismatch):
        await runner.update_built(ID, _data(prompt="Edited."), actor="admin", effective_hash="0" * 64)
    with pytest.raises(ValueError):
        await runner.update_built(ID, _data(prompt="Edited."), actor="admin", effective_hash=None)
    assert await runner.mode_of(ID) == "act" and await _status(world, grant.id) == "live"

    saved = await runner.update_built(ID, _data(prompt="Edited."), actor="admin", effective_hash=first_hash)
    assert saved["version"] == 2 and saved["mode"] == "shadow" and saved["voided"] == 1
    assert saved["effective_hash"] != first_hash
    assert await _status(world, grant.id) == "voided"
    assert await runner.get_params(ID) == {"hosts": [HOST], "packages": [SEVENZIP]}

    # Back to exactly what was granted against: a third version, a third hash.
    reverted = await runner.update_built(
        ID, _data(), actor="admin", effective_hash=saved["effective_hash"]
    )
    assert reverted["version"] == 3 and runner.catalog[ID].spec_hash != v1_spec_hash
    assert reverted["effective_hash"] != first_hash
    assert await runner.mode_of(ID) == "shadow" and await _status(world, grant.id) == "voided"
    assert await world.authorizations.remaining(ID, first_hash, "winget_install", HOST, NOW) is None  # type: ignore[attr-defined]

    # A parameter the new version no longer declares is dropped with it.
    fewer = _data(params=["window", "hosts"], constraints=[{"tool": "winget_install", "arg": "id", "allowed": [FIREFOX]}])
    await runner.update_built(ID, fewer, actor="admin", effective_hash=reverted["effective_hash"])
    assert await runner.get_params(ID) == {"hosts": [HOST]}
    assert await world.steward_store.get_params(ID) == {"hosts": [HOST]}


async def test_delete_unbinds_and_recreating_the_id_revives_nothing(world) -> None:
    runner = _runner(world)
    scheduler = StewardScheduler(runner, runner.catalog, runner.get_params, world.executor)
    await runner.create_built(_data(), actor="admin")
    grant = await _promote(runner)
    old_hash = await runner.live_hash(ID)
    old_spec_hash = runner.catalog[ID].spec_hash

    with pytest.raises(KeyError):
        await runner.delete_built("patch", actor="admin")
    deleted = await runner.delete_built(ID, actor="admin")
    assert deleted == {"steward_id": ID, "deleted": True, "voided": 1}
    assert ID not in runner.catalog and ID not in scheduler.catalog
    assert await _status(world, grant.id) == "voided"
    assert await world.steward_store.get_mode(ID) is None  # its settings went with it
    assert ID not in {s["id"] for s in await runner.overview()}
    with pytest.raises(KeyError):
        await runner.delete_built(ID, actor="admin")

    # The same id, the same definition: a later version, so a hash it never had.
    again = await runner.create_built(_data(), actor="admin")
    assert again["version"] == 2 and again["mode"] == "shadow"
    assert runner.catalog[ID].spec_hash != old_spec_hash and again["effective_hash"] != old_hash
    assert await runner.get_params(ID) == {}
    assert await _status(world, grant.id) == "voided"
    assert [a.status(NOW) for a in await world.authorizations.list(ID)] == ["voided"]  # type: ignore[attr-defined]
    logs = await world.event_store.query(kind="log", limit=200)
    assert any(f"steward {ID}: deleted by admin" in row["message"] for row in logs)


async def test_a_restart_keeps_built_stewards_and_what_was_granted_to_them(world) -> None:
    first = _runner(world)
    await first.create_built(_data(), actor="admin")
    grant = await _promote(first)
    live = await first.live_hash(ID)

    second = _runner(world)
    assert ID not in second.catalog
    await second.startup()
    assert ID in second.catalog and second.catalog.is_built(ID)
    assert await second.live_hash(ID) == live
    assert await second.mode_of(ID) == "act"
    assert await _status(world, grant.id) == "live"


async def test_a_built_steward_that_no_longer_validates_is_shown_unbound_and_never_blocks_start_up(world) -> None:
    first = _runner(world)
    await first.create_built(_data(preconditions=["disk_pressure"]), actor="admin")
    grant = await _promote(first)
    # A row nothing can read, beside it.
    await world.steward_store.create_built_spec(
        "custom_broken", {"id": "custom_broken"}, version=1, source="blank", actor="admin"
    )
    await world.steward_store._conn.execute(  # noqa: SLF001 - simulate a damaged row
        "UPDATE steward_specs SET spec = 'not json' WHERE id = 'custom_broken'"
    )
    await world.steward_store._conn.commit()  # noqa: SLF001

    # This release no longer has the precondition the steward names.
    second = _runner(world, preconditions=False)
    await second.startup()
    assert ID not in second.catalog and "custom_broken" not in second.catalog
    assert await world.steward_store.get_mode(ID) == "shadow"
    assert await _status(world, grant.id) == "voided"
    listed = {s["id"]: s for s in await second.overview()}
    shown = listed[ID]
    assert shown["invalid"] is True and shown["builtin"] is True and shown["mode"] == "off"
    assert [e["field"] for e in shown["errors"]] == ["preconditions[0]"]
    assert listed["custom_broken"]["invalid"] is True
    logs = await world.event_store.query(kind="log", limit=200)
    assert any("does not validate on this server" in row["message"] for row in logs)
    # Nothing runs it.
    with pytest.raises(ValueError):
        await second.run_generic(
            first.catalog[ID], host_id=HOST, trigger="t", brief="b",
            client=FakeAnthropic([]), model="m", executor=world.executor,
        )
    # A person fixes it, naming the hash they were shown: a new version, in shadow.
    fixed = await second.update_built(
        ID, _data(), actor="admin", effective_hash=shown["effective_hash"]
    )
    assert fixed["version"] == 2 and fixed["mode"] == "shadow" and ID in second.catalog
    assert ID not in second.catalog.invalid()


async def test_a_built_steward_runs_through_the_real_gate(world) -> None:
    runner = _runner(world)
    await runner.create_built(_data(), actor="admin")
    await runner.set_params(ID, {"hosts": [HOST], "packages": [SEVENZIP]}, actor="admin")

    async def run(*calls: tuple[str, dict[str, Any]]) -> Any:
        scripted = [_tool(f"tu{i}", name, args) for i, (name, args) in enumerate(calls)]
        scripted.append(_text("done"))
        return await runner.run_generic(
            runner.catalog[ID], host_id=HOST, trigger="schedule:test", brief="Install.",
            client=FakeAnthropic(scripted), model="test-model", executor=world.executor,
        )

    # Shadow: the allowed change is only a recommendation; the one outside the allowlist is refused.
    shadow = await run(("winget_install", {"id": SEVENZIP}), ("winget_install", {"id": FIREFOX}))
    assert shadow is not None and (shadow.status, shadow.mode) == ("completed", "shadow")
    assert world.sent == [] and shadow.actions == []
    assert [r["args"] for r in shadow.recommendations] == [{"id": SEVENZIP}]
    assert shadow.steward_id == ID and shadow.spec_hash == runner.catalog[ID].spec_hash

    # Act with an authorization: the allowed change runs and names it; the other is still refused.
    live = await runner.live_hash(ID)
    await runner.set_mode(ID, "act", actor="admin", effective_hash=live)
    grant = await runner.grant(
        ID, tool="winget_install", scope=[HOST], max_attempts_per_day=1,
        expires_at=NOW + timedelta(days=1), actor="admin", effective_hash=live,
    )
    acted = await run(("winget_install", {"id": FIREFOX}), ("winget_install", {"id": SEVENZIP}))
    assert acted is not None and acted.mode == "act"
    assert world.sent == [{"agent_id": HOST, "tool": "winget_install", "args": {"id": SEVENZIP}}]
    assert [(a["args"], a["authorization_id"]) for a in acted.actions] == [({"id": SEVENZIP}, grant.id)]

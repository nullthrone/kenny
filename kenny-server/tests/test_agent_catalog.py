"""The shipped agent catalog, and the seam between it and the code it describes.

``CATALOG["triage"]`` *declares* the unprompted triage that ``triage.py`` and
``ticket_assistant.py`` *run*. Two places that must agree, so the last tests
here build the real triage session and compare it to the declaration; they fail
when either side moves alone.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kenny_server import ticket_assistant, toolloop
from kenny_server.agents import catalog
from kenny_server.agents.catalog import CATALOG, build, get
from kenny_server.agents.catalog.hygiene import CONFIG_HYGIENE
from kenny_server.agents.catalog.patch import PATCH
from kenny_server.agents.catalog.posture import POSTURE
from kenny_server.agents.catalog.triage import TRIAGE
from kenny_server.agents.spec import (
    AgentSpec,
    ArgConstraint,
    Budget,
    SpecError,
    Trigger,
    validate,
)
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.ticket_assistant import TicketAssistant, TicketPolicy
from kenny_server.ticketstore import TicketStore
from kenny_server.tickets import TicketService
from kenny_server.tool_classes import READ_ONLY, classify
from kenny_server.toolloop import ToolExecutor
from kenny_server.tools import CAPABILITY_TOOLS, CallLog, ScreenshotStore
from kenny_server.triage import DEFAULT_MAX_ITERATIONS
from kenny_server.tunnel import AgentTunnel
from kenny_server.userstore import UserStore

HOST = "thomas-pc"


# -- the catalog ---------------------------------------------------------------


def test_triage_is_in_the_catalog() -> None:
    assert CATALOG["triage"] is TRIAGE
    assert get("triage") is TRIAGE


def test_get_unknown_is_none() -> None:
    assert get("nope") is None
    assert get("") is None


def test_every_catalog_spec_is_valid_and_keyed_by_its_id() -> None:
    assert CATALOG
    for agent_id, spec in CATALOG.items():
        assert spec.id == agent_id
        assert validate(spec) is spec


def test_the_catalog_cannot_be_mutated() -> None:
    with pytest.raises(TypeError):
        CATALOG["x"] = TRIAGE  # type: ignore[index]


def test_a_duplicate_id_is_refused() -> None:
    with pytest.raises(SpecError, match="duplicate"):
        build((TRIAGE, replace(TRIAGE, title="Again")))


def test_an_invalid_spec_fails_the_build() -> None:
    bad = replace(TRIAGE, id="Bad Id")
    with pytest.raises(SpecError):
        build((TRIAGE, bad))


def test_build_validates_every_spec_not_just_the_first() -> None:
    ok = replace(TRIAGE, id="second")
    unclassified = replace(TRIAGE, id="third", tools=frozenset({"no_such_tool"}), verdict_tool=None)
    assert set(build((TRIAGE, ok))) == {"triage", "second"}
    with pytest.raises(SpecError, match="unclassified"):
        build((TRIAGE, ok, unclassified))


def test_import_is_where_the_catalog_is_validated() -> None:
    # CATALOG is built by ``build`` at import, so a bad spec in the module's
    # tuple would stop the server booting. Pin that it goes through build().
    assert catalog.CATALOG == build((TRIAGE, PATCH, POSTURE, CONFIG_HYGIENE))


def test_triage_declares_the_documented_shape() -> None:
    assert TRIAGE.id == "triage"
    assert TRIAGE.trigger == Trigger(kind="event", event="ticket_created")
    assert TRIAGE.default_mode == "shadow"
    assert TRIAGE.sensitive_ok is False
    assert TRIAGE.verdict_tool == toolloop.TRIAGE_VERDICT_TOOL
    assert TRIAGE.budget == Budget(max_iterations=DEFAULT_MAX_ITERATIONS)
    assert TRIAGE.title and TRIAGE.description


def test_the_catalog_is_a_stable_set_of_agent_specs() -> None:
    assert all(isinstance(s, AgentSpec) for s in CATALOG.values())
    assert CATALOG["triage"].spec_hash == CATALOG["triage"].spec_hash


# -- the joined seam: the declaration vs the running triage --------------------


class _World:
    """Just enough of kenny to build a real triage session."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def setup(self) -> None:
        self.telemetry = TelemetryStore(db_path=self.db_path)
        await self.telemetry.connect()
        self.ticket_store = TicketStore(self.db_path)
        await self.ticket_store.connect()
        self.users = UserStore(self.db_path)
        await self.users.connect()
        self.tickets = TicketService(self.ticket_store)
        registry = AgentRegistry(tokens={HOST: "t"})
        tunnel = AgentTunnel(registry, self.telemetry, EventStore(db_path=self.db_path))
        executor = ToolExecutor(
            registry=registry,
            store=self.telemetry,
            tunnel=tunnel,
            call_log=CallLog(),
            screenshots=ScreenshotStore(),
        )
        self.assistant = TicketAssistant(
            tickets=self.tickets,
            users=self.users,
            executor=executor,
            client=object(),
            model="fake-model",
        )

    async def close(self) -> None:
        for store in (self.telemetry, self.ticket_store, self.users):
            await store.close()


@pytest.fixture
async def world(tmp_path):
    w = _World(str(tmp_path / "kenny.sqlite"))
    await w.setup()
    yield w
    await w.close()


async def _triage_session(world: _World):
    ticket = await world.tickets.create(
        title=f"{HOST} health: crit",
        origin="alert",
        requester_user_id=None,
        agent_id=HOST,
        category="alert",
        summary="reliability: crit",
        actor="system",
    )
    session = await world.assistant.triage_session_for(ticket)
    assert session is not None
    return session


def test_the_declared_tools_are_the_ones_triage_is_handed() -> None:
    assert CATALOG["triage"].tools == ticket_assistant.TRIAGE_TOOLS


def test_the_declared_prompt_is_the_triage_prompt() -> None:
    assert CATALOG["triage"].prompt == ticket_assistant._TRIAGE_SYSTEM_PROMPT


def test_the_declared_budget_is_the_one_triage_enforces() -> None:
    assert CATALOG["triage"].budget.max_iterations == DEFAULT_MAX_ITERATIONS


async def test_a_real_triage_session_stays_inside_the_declared_tools(world: _World) -> None:
    session = await _triage_session(world)
    assert session.triage is True
    assert session.allowed_tools, "an empty tool set would make the subset check vacuous"
    assert session.allowed_tools <= CATALOG["triage"].tools


async def test_the_real_triage_prompt_is_the_declared_one(world: _World) -> None:
    session = await _triage_session(world)
    policy = TicketPolicy(world.tickets, session)
    blocks = policy.system_blocks(session)
    assert blocks[0]["text"] == CATALOG["triage"].prompt

    # And the support prompt is not: a session that is not triage must not be
    # described by the triage declaration.
    session.triage = False
    assert policy.system_blocks(session)[0]["text"] != CATALOG["triage"].prompt


async def test_the_verdict_tool_is_offered_to_the_real_session(world: _World) -> None:
    session = await _triage_session(world)
    verdict = CATALOG["triage"].verdict_tool
    assert verdict is not None
    assert verdict in session.allowed_tools


def test_a_spec_naming_an_mcp_only_tool_is_refused() -> None:
    from kenny_server.agents.catalog import build
    from kenny_server.agents.spec import AgentSpec, SpecError, Trigger

    spec = AgentSpec(
        id="mcp_only",
        title="t",
        description="d",
        prompt="p",
        trigger=Trigger(kind="on_demand"),
        tools=frozenset({"webfilter_get"}),
    )
    with pytest.raises(SpecError, match="cannot run in the tool loop"):
        build((spec,))


def test_ticket_tools_are_refused_off_a_ticket_surface() -> None:
    from kenny_server.agents.catalog import build
    from kenny_server.agents.spec import AgentSpec, SpecError, Trigger

    spec = AgentSpec(
        id="no_ticket",
        title="t",
        description="d",
        prompt="p",
        trigger=Trigger(kind="on_demand"),
        tools=frozenset({"agent_health", "ticket_summary"}),
    )
    with pytest.raises(SpecError, match="need a ticket's surface"):
        build((spec,))


def _changer(tool: str, *constraints: ArgConstraint) -> AgentSpec:
    return AgentSpec(
        id="changer",
        title="t",
        description="d",
        prompt="p",
        trigger=Trigger(kind="on_demand"),
        tools=frozenset({tool}),
        constraints=constraints,
        sensitive_ok=True,
    )


def test_a_change_tool_with_an_unbound_required_argument_is_refused() -> None:
    # Only ``version`` bound: the gate would refuse every call that carries the
    # ``url``/``sha256`` the tool requires, so the spec names a tool it can
    # never call within its own bounds.
    spec = _changer("agent_update", ArgConstraint("agent_update", "version", frozenset({"1.2.3"})))
    validate(spec)
    with pytest.raises(SpecError, match="agent_update's required argument.*sha256, url"):
        catalog.check_dispatchable(spec)


def test_a_change_tool_with_every_required_argument_bound_is_dispatchable() -> None:
    spec = _changer(
        "agent_update",
        *(
            ArgConstraint("agent_update", arg, frozenset({"x"}))
            for arg in ("version", "url", "sha256")
        ),
    )
    assert catalog.check_dispatchable(validate(spec)) is spec
    # An optional argument (``winget_update``'s ``id?``) need not be bound for
    # the spec to load; binding it is what makes a call admissible at all.
    optional = _changer("winget_update", ArgConstraint("winget_update", "id", frozenset({"x"})))
    assert catalog.check_dispatchable(validate(optional)) is optional


def test_every_change_capability_is_checked_against_its_declared_arguments() -> None:
    """Sweep: a change-tier capability is dispatchable exactly when its required args are bound."""

    for tool, raw in CAPABILITY_TOOLS.items():
        if classify(tool) == READ_ONLY:
            continue
        required = [k for k in raw if not k.endswith("?")]
        optional = [k[:-1] for k in raw if k.endswith("?")]
        every = tuple(ArgConstraint(tool, k, frozenset({"x"})) for k in required + optional)
        if not every:
            continue  # nothing to bind; validate() refuses it unconstrained anyway
        catalog.check_dispatchable(validate(_changer(tool, *every)))
        if required:
            # Drop the first required argument's constraint (``every`` lists
            # required ones first); check_dispatchable alone, since validate()
            # would refuse a now-unconstrained tool for its own reason.
            with pytest.raises(SpecError, match=f"required argument.*{required[0]}"):
                catalog.check_dispatchable(_changer(tool, *every[1:]))


def test_the_triage_tools_read_no_other_host() -> None:
    # The ticket surface strips these for a scoped session anyway; declaring
    # them would put fleet-wide reads in the triage spec that it never gets.
    assert not (ticket_assistant.TRIAGE_TOOLS & ticket_assistant.FLEET_WIDE_TOOLS)
    assert not (CATALOG["triage"].tools & ticket_assistant.FLEET_WIDE_TOOLS)


# -- the scheduled agents ------------------------------------------------------

PATCH_HOST = "thomas-pc"
FIREFOX = "Mozilla.Firefox"
SEVENZIP = "7zip.7zip"


@pytest.mark.parametrize("spec", [PATCH, POSTURE], ids=lambda s: s.id)
def test_a_scheduled_agent_is_valid_dispatchable_and_ships_in_shadow(spec: AgentSpec) -> None:
    assert CATALOG[spec.id] is spec
    assert validate(spec) is spec
    assert catalog.check_dispatchable(spec) is spec
    assert spec.trigger == Trigger(kind="schedule")
    assert spec.default_mode == "shadow"
    assert spec.verdict_tool == toolloop.AGENT_VERDICT_TOOL
    assert toolloop.AGENT_VERDICT_TOOL in spec.tools
    assert spec.sensitive_ok is False
    assert spec.title and spec.description


@pytest.mark.parametrize("spec", [PATCH, POSTURE], ids=lambda s: s.id)
def test_the_scheduler_can_read_what_a_scheduled_agent_declares(spec: AgentSpec) -> None:
    # The scheduler reads exactly these two parameters of every schedule agent.
    assert {"window", "hosts"} <= set(spec.params)


@pytest.mark.parametrize("spec", [PATCH, POSTURE], ids=lambda s: s.id)
def test_a_prompt_names_only_tools_the_agent_has(spec: AgentSpec) -> None:
    from kenny_server.tool_classes import TOOL_CLASSES

    named = {t for t in TOOL_CLASSES if t in spec.prompt}
    assert named <= spec.tools, f"prompt names {sorted(named - spec.tools)}"
    # ... and says what a model must hear about tool output (ADR-0023).
    assert "untrusted" in spec.prompt


def test_the_patch_agent_declares_the_documented_shape() -> None:
    assert PATCH.id == "patch"
    assert PATCH.params == ("window", "hosts", "packages", "require_idle")
    assert PATCH.tools == {"winget_list", "winget_update", "agent_verdict"}
    assert PATCH.constraints == (ArgConstraint("winget_update", "id", param="packages"),)
    assert [(t.tool, t.max_s) for t in PATCH.timeouts] == [("winget_update", 600)]
    assert {t for t in PATCH.tools if classify(t) != READ_ONLY} == {"winget_update", "agent_verdict"}


def test_every_argument_of_winget_update_is_bound_or_the_one_free_one() -> None:
    # The gate refuses any argument no constraint binds except timeout_s. If the
    # agent's protocol grows another argument for winget_update, this fails.
    declared = {a.rstrip("?") for a in CAPABILITY_TOOLS["winget_update"]}
    bound = {c.arg for c in PATCH.constraints_for("winget_update")}
    assert declared - bound <= {"timeout_s"}


def test_the_patch_allowlist_is_a_parameter_so_widening_it_unbinds_the_agent() -> None:
    from kenny_server.agents.spec import effective_hash

    narrow = effective_hash(PATCH, {"packages": [FIREFOX]})
    wide = effective_hash(PATCH, {"packages": [FIREFOX, SEVENZIP]})
    assert narrow != wide
    assert effective_hash(PATCH, {"packages": [SEVENZIP, FIREFOX]}) == wide  # order is not content


def test_the_posture_agent_has_no_change_tool_but_its_verdict() -> None:
    assert POSTURE.tools == {"diag_autostart", "diag_services", "agent_snapshot", "agent_verdict"}
    assert POSTURE.constraints == () and POSTURE.timeouts == ()
    assert {t for t in POSTURE.tools if classify(t) != READ_ONLY} == {"agent_verdict"}
    assert POSTURE.params == ("window", "hosts")


def test_the_scheduled_agents_read_no_other_host() -> None:
    assert not (POSTURE.tools & ticket_assistant.FLEET_WIDE_TOOLS)
    assert not (PATCH.tools & ticket_assistant.FLEET_WIDE_TOOLS)


# The patch spec's gate behaviour, through the real AgentPolicy.


def _patch_session(packages, mode: str = "act"):
    from kenny_server.agents.policy import AgentSession
    from kenny_server.agents.spec import resolve

    params = {} if packages is None else {"packages": packages}
    return AgentSession(id="run-1", spec=resolve(PATCH, params), mode=mode, agent_id=PATCH_HOST)


async def _gate(session, tool: str, args: dict):
    from kenny_server.agents.policy import AgentPolicy

    return await AgentPolicy(session).gate(session, tool, dict(args), PATCH_HOST)


async def test_an_allowlisted_package_updates_in_act() -> None:
    from kenny_server.toolloop import Allow

    session = _patch_session([FIREFOX])
    decision = await _gate(session, "winget_update", {"id": FIREFOX, "timeout_s": 600})
    assert isinstance(decision, Allow)
    assert [a["args"] for a in session.actions] == [{"id": FIREFOX, "timeout_s": 600}]


async def test_another_package_is_refused_by_constraint() -> None:
    from kenny_server.toolloop import Deny

    session = _patch_session([FIREFOX])
    decision = await _gate(session, "winget_update", {"id": SEVENZIP})
    assert isinstance(decision, Deny) and decision.code == "constraint"
    assert session.actions == [] and session.recommendations == []


@pytest.mark.parametrize("packages", [[], None, [""], [7], "Mozilla.Firefox"])
async def test_an_empty_or_missing_allowlist_admits_nothing(packages) -> None:
    from kenny_server.toolloop import Deny

    session = _patch_session(packages)
    for args in ({"id": FIREFOX}, {"id": ""}, {}):
        decision = await _gate(session, "winget_update", args)
        assert isinstance(decision, Deny) and decision.code == "constraint", args
    assert session.actions == []


async def test_update_everything_is_not_possible_without_an_id() -> None:
    from kenny_server.toolloop import Deny

    session = _patch_session([FIREFOX])
    decision = await _gate(session, "winget_update", {"timeout_s": 600})
    assert isinstance(decision, Deny) and decision.code == "constraint"


@pytest.mark.parametrize(
    "args",
    [
        {"id": FIREFOX, "version": "1.0"},
        {"id": FIREFOX, "all": True},
        {"id": FIREFOX, "timeout_s": 601},
        {"id": FIREFOX, "timeout_s": "600"},
        {"id": FIREFOX, "agent_id": "bob-pc"},
    ],
)
async def test_nothing_else_may_ride_along_with_an_allowed_package(args: dict) -> None:
    from kenny_server.toolloop import Deny

    session = _patch_session([FIREFOX])
    decision = await _gate(session, "winget_update", args)
    assert isinstance(decision, Deny) and decision.code in ("constraint", "out_of_scope")
    assert session.actions == []


async def test_in_shadow_the_allowed_update_is_only_a_recommendation() -> None:
    from kenny_server.toolloop import Deny

    session = _patch_session([FIREFOX], mode="shadow")
    decision = await _gate(session, "winget_update", {"id": FIREFOX, "timeout_s": 600})
    assert isinstance(decision, Deny) and decision.code == "shadow"
    assert session.actions == []
    assert [r["args"]["id"] for r in session.recommendations] == [FIREFOX]


async def test_install_and_uninstall_are_not_available_to_the_patch_agent() -> None:
    from kenny_server.toolloop import Deny

    session = _patch_session([FIREFOX])
    for tool in ("winget_install", "winget_uninstall", "shell_exec", "powershell_exec"):
        decision = await _gate(session, tool, {"id": FIREFOX})
        assert isinstance(decision, Deny) and decision.code == "forbidden"


async def test_the_verdict_and_the_read_are_allowed_in_both_modes() -> None:
    from kenny_server.toolloop import Allow

    for mode in ("shadow", "act"):
        session = _patch_session([], mode=mode)
        assert isinstance(await _gate(session, "winget_list", {}), Allow)
        verdict = {"verdict": "clean", "finding": "-", "evidence": "-"}
        assert isinstance(await _gate(session, "agent_verdict", verdict), Allow)


async def test_the_posture_agent_cannot_change_anything() -> None:
    from kenny_server.agents.policy import AgentPolicy, AgentSession
    from kenny_server.toolloop import Allow, Deny

    session = AgentSession(id="run-2", spec=POSTURE, mode="act", agent_id=PATCH_HOST)
    policy = AgentPolicy(session)
    for tool in ("diag_autostart", "diag_services"):
        assert isinstance(await policy.gate(session, tool, {}, PATCH_HOST), Allow)
    snapshot = await policy.gate(
        session, "agent_snapshot", {"id": PATCH_HOST, "section": "local_accounts"}, None
    )
    assert isinstance(snapshot, Allow)
    for tool, args in (
        ("winget_update", {"id": FIREFOX}),
        ("account_set_admin", {"principal": "kid", "admin": True}),
        ("shell_exec", {"command": "id"}),
    ):
        decision = await policy.gate(session, tool, dict(args), PATCH_HOST)
        assert isinstance(decision, Deny) and decision.code == "forbidden"
    # Another machine's snapshot is not its to read.
    other = await policy.gate(session, "agent_snapshot", {"id": "bob-pc"}, None)
    assert isinstance(other, Deny) and other.code == "out_of_scope"


async def test_joined_the_real_loop_sends_only_the_allowlisted_update(tmp_path) -> None:
    """Two update requests through ``drive_events``: one reaches the wire, one does not."""

    from kenny_server.agents.policy import AgentPolicy
    from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

    telemetry = TelemetryStore(db_path=str(tmp_path / "patch.sqlite"))
    await telemetry.connect()
    try:
        registry = AgentRegistry(tokens={PATCH_HOST: "t"})
        tunnel = AgentTunnel(registry, telemetry, EventStore(db_path=telemetry.db_path))
        sent: list[tuple[str, str, dict]] = []

        async def send_request(agent_id: str, tool: str, args: dict, timeout_s: float):
            sent.append((agent_id, tool, dict(args)))
            return {"ok": True, "log": "updated", "packages": []}

        tunnel.send_request = send_request  # type: ignore[method-assign]
        executor = ToolExecutor(
            registry=registry,
            store=telemetry,
            tunnel=tunnel,
            call_log=CallLog(),
            screenshots=ScreenshotStore(),
        )
        session = _patch_session([FIREFOX])
        session.messages.append({"role": "user", "content": "start"})
        client = FakeAnthropic(
            [
                _Response(
                    [
                        tool_use_block("a", "winget_update", {"id": SEVENZIP, "timeout_s": 600}),
                        tool_use_block("b", "winget_update", {"id": FIREFOX, "timeout_s": 600}),
                    ],
                    "tool_use",
                ),
                _Response([text_block("done")], "end_turn"),
            ]
        )
        events = [
            ev
            async for ev in toolloop.drive_events(
                session,
                executor,
                client=client,
                model="m",
                policy=AgentPolicy(session),
                max_iterations=PATCH.budget.max_iterations,
            )
        ]
        assert sent == [(PATCH_HOST, "winget_update", {"id": FIREFOX, "timeout_s": 600})]
        denied = [e for e in events if e["type"] == "denied"]
        assert [e["args"]["id"] for e in denied] == [SEVENZIP]
    finally:
        await telemetry.close()

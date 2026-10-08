"""The agent gate: what an unattended run may do, and in which order it is asked.

Two halves. The unit tests call :class:`AgentPolicy` directly, one gate step at
a time, including the steps that only matter in combination (a forbidden call
that also names another host must be ``forbidden``, not ``out_of_scope``). The
joined tests drive the real :func:`toolloop.drive_events` with the real
:class:`ToolExecutor` and a real :class:`AgentTunnel` whose ``send_request`` is
the only fake, so "never reached the tunnel" means exactly that — the policy is
not trusted to be wired the way it was written to be.

The fake Anthropic client is the one ``test_toolloop.py`` uses.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kenny_server.agents import policy as policy_module
from kenny_server.agents.authorizations import NEVER_AUTHORIZED, Authorization
from kenny_server.agents.policy import HOST_ARG, MAX_TIMEOUT_S, AgentPolicy, AgentSession
from kenny_server.agents.spec import ToolTimeout
from kenny_server.agents.spec import VERDICT_TOOLS, AgentSpec, ArgConstraint, SpecError, Trigger
from kenny_server.ticket_assistant import FLEET_WIDE_TOOLS
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tool_classes import NORMAL_CHANGE, READ_ONLY, STANDARD_CHANGE, TOOL_CLASSES
from kenny_server.toolloop import (
    COPILOT_AGENT_TOOLS,
    SERVER_TOOLS,
    TRIAGE_VERDICT_TOOL,
    Allow,
    Deny,
    PendingCall,
    ToolExecutor,
    drive_events,
)
from kenny_server.tools import CAPABILITY_TOOLS, CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel, ToolError

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

HOST = "thomas-pc"
OTHER = "bob-pc"
FIREFOX = "Mozilla.Firefox"
SEVENZIP = "7zip.7zip"


def _spec(**overrides: Any) -> AgentSpec:
    fields: dict[str, Any] = {
        "id": "patcher",
        "title": "Patcher",
        "description": "Applies pending package updates.",
        "prompt": "You keep this machine's packages up to date.",
        "trigger": Trigger(kind="on_demand"),
        "tools": frozenset({"winget_list", "winget_update", "winget_install", "agent_health"}),
        "constraints": (
            ArgConstraint("winget_update", "id", frozenset({FIREFOX})),
            ArgConstraint("winget_install", "id", frozenset({SEVENZIP})),
        ),
    }
    fields.update(overrides)
    return AgentSpec(**fields)


def _session(mode: str = "shadow", agent_id: str | None = HOST, **spec: Any) -> AgentSession:
    return AgentSession(id="run-1", spec=_spec(**spec), mode=mode, agent_id=agent_id)


AUTH_ID = "auth-1"


def _grant(agent: str, tool: str, auth_id: str = AUTH_ID) -> Authorization:
    """A live authorization as the store would return it."""

    return Authorization(
        id=auth_id,
        agent_id=agent,
        effective_hash="h",
        tool=tool,
        scope=[HOST],
        max_attempts_per_day=1,
        expires_at="2999-01-01T00:00:00.000000+00:00",
        granted_by="admin",
        granted_at="2026-10-08T00:00:00.000000+00:00",
        revoked_at=None,
        revoked_by=None,
        voided_at=None,
        voided_by=None,
        note="",
    )


def _yes(calls: list[tuple[Any, ...]] | None = None) -> Any:
    async def authorizer(
        session: Any, tool: str, args: dict[str, Any], agent_id: Any
    ) -> Authorization:
        if calls is not None:
            calls.append((session, tool, args, agent_id))
        return _grant(session.spec.id, tool)

    return authorizer


async def _gate(
    policy: AgentPolicy, session: AgentSession, tool: str, args: dict[str, Any]
) -> Any:
    """resolve_target then gate, the way the loop asks them."""

    target = policy.resolve_target(session, tool, args)
    return await policy.gate(session, tool, args, target)


# -- the session ---------------------------------------------------------------


def test_session_derives_its_actor_and_run_id() -> None:
    session = _session()
    assert session.audit_actor == "agent:patcher"
    assert session.agent_run_id == "run-1"
    assert session.pending is None and session.usage is None
    assert session.recommendations == [] and session.actions == []


def test_policy_refuses_a_mode_that_is_not_a_run() -> None:
    for mode in ("off", "ACT", "", "unknown"):
        with pytest.raises(ValueError):
            AgentPolicy(_session(mode=mode))


def test_policy_revalidates_the_spec_rather_than_trusting_the_loader() -> None:
    # A change-tier tool with no constraint: refused at construction, not at
    # the first call that happens to use it.
    with pytest.raises(SpecError):
        AgentPolicy(_session(constraints=()))


def test_policy_gates_only_its_own_session() -> None:
    policy = AgentPolicy(_session())
    with pytest.raises(RuntimeError):
        policy.resolve_target(_session(), "winget_list", {})


# -- what the model sees -------------------------------------------------------


def test_schemas_are_exactly_the_spec_with_the_last_one_cached() -> None:
    policy = AgentPolicy(_session())
    schemas = policy.tool_schemas()
    assert {s["name"] for s in schemas} == set(_spec().tools)
    assert [s.get("cache_control") for s in schemas[:-1]] == [None] * (len(schemas) - 1)
    assert schemas[-1]["cache_control"] == {"type": "ephemeral"}
    # A caller mutating what it got must not change the next request's prefix.
    schemas[-1]["name"] = "powershell_exec"
    assert policy.tool_schemas()[-1]["name"] != "powershell_exec"


def test_a_spec_tool_the_loop_cannot_dispatch_has_no_schema() -> None:
    # Classified (so the spec loads) but MCP-only: the loop would forward it to
    # the host as if it were a capability.
    assert "reliability_suppression_list" in TOOL_CLASSES
    assert "reliability_suppression_list" not in set(SERVER_TOOLS) | set(CAPABILITY_TOOLS)
    policy = AgentPolicy(_session(tools=frozenset({"winget_list", "reliability_suppression_list"}),
                                  constraints=()))
    assert {s["name"] for s in policy.tool_schemas()} == {"winget_list"}


def test_system_blocks_cache_the_prompt_and_state_host_and_mode() -> None:
    session = _session()
    blocks = AgentPolicy(session).system_blocks(session)
    assert blocks[0] == {
        "type": "text",
        "text": _spec().prompt,
        "cache_control": {"type": "ephemeral"},
    }
    assert len(blocks) == 2 and "cache_control" not in blocks[1]
    assert f'"{HOST}"' in blocks[1]["text"]
    assert "not carried out" in blocks[1]["text"]
    assert "recommendations for a person" in blocks[1]["text"]

    act = _session(mode="act", agent_id=None)
    text = AgentPolicy(act).system_blocks(act)[1]["text"]
    assert "touches no machine" in text and "shadow" not in text


# -- step 1: not in the spec ---------------------------------------------------


async def test_a_tool_outside_the_spec_is_forbidden() -> None:
    session = _session(mode="act")
    policy = AgentPolicy(session, authorizer=_yes())
    for tool in ("powershell_exec", "list_agents", "fleet_overview", "made_up_tool"):
        decision = await _gate(policy, session, tool, {})
        assert decision == Deny("forbidden", f"{tool} is not available to this agent")
    assert session.recommendations == [] and session.actions == []


async def test_forbidden_wins_over_out_of_scope() -> None:
    session = _session(mode="act")
    policy = AgentPolicy(session)
    decision = await _gate(policy, session, "powershell_exec", {"agent_id": OTHER})
    assert isinstance(decision, Deny) and decision.code == "forbidden"


async def test_forbidden_wins_even_without_a_host() -> None:
    # resolve_target must not raise ``no_agent`` for a tool the spec lacks.
    session = _session(agent_id=None)
    policy = AgentPolicy(session)
    assert policy.resolve_target(session, "powershell_exec", {}) is None
    decision = await policy.gate(session, "powershell_exec", {}, None)
    assert isinstance(decision, Deny) and decision.code == "forbidden"


async def test_a_spec_tool_the_loop_cannot_dispatch_is_forbidden() -> None:
    session = _session(tools=frozenset({"winget_list", "reliability_suppression_list"}),
                       constraints=())
    policy = AgentPolicy(session)
    decision = await _gate(policy, session, "reliability_suppression_list", {})
    assert isinstance(decision, Deny) and decision.code == "forbidden"


async def test_fleet_wide_tools_are_reachable_only_when_named() -> None:
    session = _session(agent_id=None, tools=frozenset({"winget_list", "list_agents"}),
                       constraints=())
    policy = AgentPolicy(session)
    assert await _gate(policy, session, "list_agents", {}) == Allow()
    decision = await _gate(policy, session, "fleet_overview", {})
    assert isinstance(decision, Deny) and decision.code == "forbidden"


@pytest.mark.parametrize("tool", sorted(FLEET_WIDE_TOOLS))
async def test_a_fleet_wide_read_is_out_of_scope_on_a_run_frozen_to_a_host(tool: str) -> None:
    """Reading every host is the escape the one-host rule exists to stop.

    ``list_agents``/``fleet_overview`` name no host, so step 2's host checks had
    nothing to compare and the read was allowed on a frozen run.
    """

    tools = frozenset({"winget_list", "list_agents", "fleet_overview"})
    frozen = _session(mode="act", tools=tools, constraints=())
    policy = AgentPolicy(frozen)
    decision = await _gate(policy, frozen, tool, {})
    assert isinstance(decision, Deny) and decision.code == "out_of_scope"
    # A host-less, server-only run that names it may still use it.
    hostless = _session(mode="act", agent_id=None, tools=tools, constraints=())
    assert await _gate(AgentPolicy(hostless), hostless, tool, {}) == Allow()


# -- step 2: another host ------------------------------------------------------


@pytest.mark.parametrize("mode", ["shadow", "act"])
async def test_a_foreign_agent_id_is_refused_even_for_a_read(mode: str) -> None:
    session = _session(mode=mode)
    policy = AgentPolicy(session, authorizer=_yes())
    for tool, args in (
        ("winget_list", {"agent_id": OTHER}),
        ("winget_update", {"id": FIREFOX, "agent_id": OTHER}),
        ("winget_install", {"id": SEVENZIP, "agent_id": OTHER}),
    ):
        decision = await _gate(policy, session, tool, args)
        assert isinstance(decision, Deny) and decision.code == "out_of_scope", tool
    assert session.recommendations == [] and session.actions == []


async def test_the_frozen_host_as_agent_id_is_accepted_and_never_forwarded() -> None:
    session = _session()
    policy = AgentPolicy(session)
    for claimed in (HOST, f"  {HOST} ", "", "   "):
        args: dict[str, Any] = {"agent_id": claimed}
        assert await _gate(policy, session, "winget_list", args) == Allow()
        assert args == {}


async def test_a_non_string_agent_id_is_refused_not_coerced() -> None:
    session = _session()
    policy = AgentPolicy(session)
    for claimed in (0, False, [HOST], {"id": HOST}):
        decision = await _gate(policy, session, "winget_list", {"agent_id": claimed})
        assert isinstance(decision, Deny) and decision.code == "out_of_scope", claimed


async def test_a_host_id_naming_another_host_is_refused() -> None:
    session = _session()
    policy = AgentPolicy(session)
    decision = await _gate(policy, session, "agent_health", {"id": OTHER})
    assert isinstance(decision, Deny) and decision.code == "out_of_scope"
    decision = await _gate(policy, session, "agent_health", {"id": HOST, "agent_id": OTHER})
    assert isinstance(decision, Deny) and decision.code == "out_of_scope"


async def test_a_missing_host_id_is_pinned_to_the_frozen_host() -> None:
    session = _session()
    policy = AgentPolicy(session)
    for args in ({}, {"id": ""}, {"id": f" {HOST} "}, {"id": HOST, "agent_id": HOST}):
        assert await _gate(policy, session, "agent_health", args) == Allow()
        assert args == {"id": HOST}


async def test_without_a_host_a_capability_fails_to_route() -> None:
    session = _session(agent_id=None)
    policy = AgentPolicy(session)
    with pytest.raises(ToolError) as exc:
        policy.resolve_target(session, "winget_list", {})
    assert exc.value.code == "no_agent"
    # And the gate refuses it on its own, should routing ever be skipped.
    decision = await policy.gate(session, "winget_list", {}, None)
    assert isinstance(decision, Deny) and decision.code == "out_of_scope"


async def test_without_a_host_a_host_naming_tool_never_runs() -> None:
    session = _session(agent_id=None)
    policy = AgentPolicy(session)
    for args in ({}, {"id": OTHER}, {"id": HOST}):
        decision = await _gate(policy, session, "agent_health", args)
        assert isinstance(decision, Deny) and decision.code == "out_of_scope", args


async def test_without_a_host_any_agent_id_is_refused() -> None:
    session = _session(agent_id=None, tools=frozenset({"winget_list", "list_agents"}),
                       constraints=())
    policy = AgentPolicy(session)
    assert await _gate(policy, session, "list_agents", {}) == Allow()
    decision = await _gate(policy, session, "list_agents", {"agent_id": OTHER})
    assert isinstance(decision, Deny) and decision.code == "out_of_scope"


async def test_a_session_whose_host_drifted_is_refused() -> None:
    # Something (``select_agent``'s executor path writes ``session.agent_id``)
    # moved the session off the host the policy was built for.
    session = _session()
    policy = AgentPolicy(session)
    session.agent_id = OTHER
    decision = await _gate(policy, session, "winget_list", {})
    assert isinstance(decision, Deny) and decision.code == "out_of_scope"


# -- step 3: read-only ---------------------------------------------------------


async def test_a_read_runs_in_shadow_and_is_not_recorded() -> None:
    session = _session()
    policy = AgentPolicy(session)
    assert await _gate(policy, session, "winget_list", {}) == Allow()
    assert session.recommendations == [] and session.actions == []


# -- step 4: constraints -------------------------------------------------------


@pytest.mark.parametrize("mode", ["shadow", "act"])
@pytest.mark.parametrize(
    "args",
    [
        {},  # winget_update without id upgrades every package
        {"id": ""},
        {"id": "Evil.Package"},
        {"id": FIREFOX.lower()},
        {"id": [FIREFOX]},
        {"id": FIREFOX, "scope": "machine"},  # an argument the catalog does not declare
    ],
)
async def test_a_change_outside_its_constraints_is_refused_and_not_recommended(
    mode: str, args: dict[str, Any]
) -> None:
    session = _session(mode=mode)
    policy = AgentPolicy(session, authorizer=_yes())
    decision = await _gate(policy, session, "winget_update", dict(args))
    assert isinstance(decision, Deny) and decision.code == "constraint"
    decision = await _gate(policy, session, "winget_install", {**args, "id": args.get("id")})
    assert isinstance(decision, Deny) and decision.code == "constraint"
    assert session.recommendations == [] and session.actions == []


async def test_the_constraint_sees_the_args_without_the_routing_override() -> None:
    session = _session(mode="act")
    policy = AgentPolicy(session)
    args = {"id": FIREFOX, "agent_id": HOST, "timeout_s": 600}
    assert await _gate(policy, session, "winget_update", args) == Allow()
    assert args == {"id": FIREFOX, "timeout_s": 600}


_AGENT_UPDATE = {
    "version": "1.2.3",
    "url": "https://releases.example/kenny-agent-1.2.3.exe",
    "sha256": "ab" * 32,
}


@pytest.mark.parametrize("mode", ["shadow", "act"])
async def test_a_constraint_on_one_argument_leaves_no_other_free(mode: str) -> None:
    """Binding ``version`` must not leave ``url``/``sha256`` to the model."""

    session = _session(
        mode=mode,
        tools=frozenset({"agent_update"}),
        constraints=(ArgConstraint("agent_update", "version", frozenset({"1.2.3"})),),
    )
    policy = AgentPolicy(session, authorizer=_yes())
    evil = {**_AGENT_UPDATE, "url": "https://evil.example/x.exe"}
    decision = await _gate(policy, session, "agent_update", evil)
    assert isinstance(decision, Deny) and decision.code == "constraint"
    assert "url" in decision.message and "sha256" in decision.message
    assert session.recommendations == [] and session.actions == []


async def test_every_argument_bound_passes_the_constraints() -> None:
    session = _session(
        mode="act",
        tools=frozenset({"agent_update"}),
        constraints=tuple(
            ArgConstraint("agent_update", k, frozenset({v})) for k, v in _AGENT_UPDATE.items()
        ),
    )
    policy = AgentPolicy(session, authorizer=_yes())
    # Past the constraints, and still never run: agent_update is never authorized
    # ahead, whatever the authorizer answers (ADR-0072 rule 1).
    decision = await _gate(policy, session, "agent_update", dict(_AGENT_UPDATE))
    assert isinstance(decision, Deny) and decision.code == "not_authorized"
    decision = await _gate(
        policy, session, "agent_update", {**_AGENT_UPDATE, "url": "https://evil.example/x.exe"}
    )
    assert isinstance(decision, Deny) and decision.code == "constraint"


@pytest.mark.parametrize("enabled", [True, False, "true", None])
async def test_an_unbound_or_boolean_argument_is_refused(enabled: Any) -> None:
    """A boolean can never satisfy a constraint, so it can never slip through one."""

    session = _session(
        mode="act",
        tools=frozenset({"account_set_enabled"}),
        constraints=(ArgConstraint("account_set_enabled", "principal", frozenset({"kid"})),),
    )
    policy = AgentPolicy(session, authorizer=_yes())
    decision = await _gate(
        policy, session, "account_set_enabled", {"principal": "kid", "enabled": enabled}
    )
    assert isinstance(decision, Deny) and decision.code == "constraint"

    bound = _session(
        mode="act",
        tools=frozenset({"account_set_enabled"}),
        constraints=(
            ArgConstraint("account_set_enabled", "principal", frozenset({"kid"})),
            ArgConstraint("account_set_enabled", "enabled", frozenset({"false"})),
        ),
    )
    decision = await _gate(
        AgentPolicy(bound, authorizer=_yes()),
        bound,
        "account_set_enabled",
        {"principal": "kid", "enabled": False},
    )
    assert isinstance(decision, Deny) and decision.code == "constraint"


@pytest.mark.parametrize(
    "timeout_s", [0, -1, MAX_TIMEOUT_S + 1, 10**9, "30", 30.5, True, None, [30]]
)
async def test_timeout_s_must_be_a_bounded_whole_number(timeout_s: Any) -> None:
    session = _session(mode="act")
    policy = AgentPolicy(session)
    decision = await _gate(
        policy, session, "winget_update", {"id": FIREFOX, "timeout_s": timeout_s}
    )
    assert isinstance(decision, Deny) and decision.code == "constraint", timeout_s
    assert session.actions == [] and session.recommendations == []


@pytest.mark.parametrize("timeout_s", [1, 30, MAX_TIMEOUT_S])
async def test_timeout_s_within_bounds_needs_no_constraint(timeout_s: int) -> None:
    session = _session(mode="act")
    policy = AgentPolicy(session)
    args = {"id": FIREFOX, "timeout_s": timeout_s}
    assert await _gate(policy, session, "winget_update", args) == Allow()


async def test_a_spec_timeout_narrows_the_ceiling_for_its_tool_only() -> None:
    session = _session(mode="act", timeouts=(ToolTimeout("winget_update", 120),))
    policy = AgentPolicy(session)
    over = await _gate(policy, session, "winget_update", {"id": FIREFOX, "timeout_s": 121})
    assert isinstance(over, Deny) and over.code == "constraint"
    assert await _gate(
        policy, session, "winget_update", {"id": FIREFOX, "timeout_s": 120}
    ) == Allow()
    # Another tool of the same spec keeps the global ceiling.
    other = _session(mode="act", timeouts=(ToolTimeout("winget_update", 120),))
    assert await _gate(
        AgentPolicy(other, authorizer=_yes()),
        other,
        "winget_install",
        {"id": SEVENZIP, "timeout_s": MAX_TIMEOUT_S},
    ) == Allow(authorization_id=AUTH_ID)


async def test_a_verdict_exemption_for_a_non_verdict_tool_is_forbidden(monkeypatch) -> None:
    """Even with ``validate`` bypassed, a shell never gets the verdict exemption."""

    monkeypatch.setattr(policy_module, "validate", lambda spec: spec)
    assert "powershell_exec" not in VERDICT_TOOLS
    for mode in ("shadow", "act"):
        session = _session(
            mode=mode,
            tools=frozenset({"powershell_exec"}),
            constraints=(),
            verdict_tool="powershell_exec",
        )
        policy = AgentPolicy(session, authorizer=_yes())
        decision = await _gate(
            policy, session, "powershell_exec", {"script": "Remove-Item C:\\ -Recurse"}
        )
        assert isinstance(decision, Deny) and decision.code == "forbidden", mode
        assert session.actions == [] and session.recommendations == []


async def test_the_verdict_tool_runs_in_both_modes_and_is_not_an_action() -> None:
    """The verdict is how a run reports; a shadow run must still be able to.

    It needs no constraint, is never a recommendation, and is not counted as a
    change the run made: what the verdict leads to is its handler's decision,
    taken with the session's mode in hand.
    """

    tools = frozenset({"agent_health", TRIAGE_VERDICT_TOOL})
    args = {"verdict": "phantom", "finding": "f", "evidence": "e"}
    for mode in ("act", "shadow"):
        session = _session(mode=mode, tools=tools, constraints=(), verdict_tool=TRIAGE_VERDICT_TOOL)
        assert await _gate(AgentPolicy(session), session, TRIAGE_VERDICT_TOOL, dict(args)) == Allow()
        assert session.actions == [] and session.recommendations == []


async def test_unattended_schemas_offer_no_host_override_and_promise_no_confirmation() -> None:
    session = _session(mode="act")
    for schema in AgentPolicy(session).tool_schemas():
        assert "agent_id" not in schema["input_schema"]["properties"], schema["name"]
        assert "operator confirmation" not in schema["description"], schema["name"]


# -- step 5: shadow ------------------------------------------------------------


async def test_shadow_refuses_every_change_and_records_it() -> None:
    session = _session()
    policy = AgentPolicy(session, authorizer=_yes())
    for tool, tier, pkg in (
        ("winget_update", STANDARD_CHANGE, FIREFOX),
        ("winget_install", NORMAL_CHANGE, SEVENZIP),
    ):
        decision = await _gate(policy, session, tool, {"id": pkg, "agent_id": HOST})
        assert isinstance(decision, Deny) and decision.code == "shadow"
        assert "recommendation" in decision.message and "not carried out" in decision.message
        assert session.recommendations[-1] == {
            "tool": tool,
            "args": {"id": pkg},
            "agent_id": HOST,
            "tool_class": tier,
        }
    assert session.actions == []


async def test_demoting_a_run_mid_way_takes_effect_and_promoting_does_not() -> None:
    act = _session(mode="act")
    policy = AgentPolicy(act)
    act.mode = "shadow"
    decision = await _gate(policy, act, "winget_update", {"id": FIREFOX})
    assert isinstance(decision, Deny) and decision.code == "shadow"

    shadow = _session()
    policy = AgentPolicy(shadow)
    shadow.mode = "act"
    decision = await _gate(policy, shadow, "winget_update", {"id": FIREFOX})
    assert isinstance(decision, Deny) and decision.code == "shadow"


async def test_still_acting_is_asked_before_every_change() -> None:
    """The live predicate is how a demotion or the global switch reaches a run."""

    live = {"acting": True}
    asked: list[str] = []

    async def still_acting() -> bool:
        asked.append("asked")
        return live["acting"]

    session = _session(mode="act")
    policy = AgentPolicy(session, still_acting=still_acting)
    assert await _gate(policy, session, "winget_update", {"id": FIREFOX}) == Allow()
    live["acting"] = False
    decision = await _gate(policy, session, "winget_update", {"id": FIREFOX})
    assert isinstance(decision, Deny) and decision.code == "shadow"
    assert [a["tool"] for a in session.actions] == ["winget_update"]
    assert [r["tool"] for r in session.recommendations] == ["winget_update"]
    # Reads never ask: they run in shadow too.
    assert await _gate(policy, session, "winget_list", {}) == Allow()
    assert asked == ["asked", "asked"]


@pytest.mark.parametrize("answer", ["raise", None, 1, "yes"])
async def test_a_still_acting_that_fails_or_hedges_is_not_acting(answer: Any) -> None:
    async def still_acting() -> Any:
        if answer == "raise":
            raise RuntimeError("settings unavailable")
        return answer

    session = _session(mode="act")
    policy = AgentPolicy(session, authorizer=_yes(), still_acting=still_acting)
    decision = await _gate(policy, session, "winget_install", {"id": SEVENZIP})
    assert isinstance(decision, Deny) and decision.code == "shadow"
    assert len(session.recommendations) == 1 and session.actions == []


async def test_still_acting_cannot_promote_a_shadow_run() -> None:
    async def still_acting() -> bool:
        return True

    session = _session()
    policy = AgentPolicy(session, still_acting=still_acting)
    decision = await _gate(policy, session, "winget_update", {"id": FIREFOX})
    assert isinstance(decision, Deny) and decision.code == "shadow"


# -- steps 6 and 7: act --------------------------------------------------------


async def test_act_runs_a_constrained_standard_change_and_records_it() -> None:
    session = _session(mode="act")
    policy = AgentPolicy(session)
    assert await _gate(policy, session, "winget_update", {"id": FIREFOX}) == Allow()
    assert session.actions == [
        {"tool": "winget_update", "args": {"id": FIREFOX}, "agent_id": HOST,
         "tool_class": STANDARD_CHANGE}
    ]
    assert session.recommendations == []


async def test_act_refuses_a_normal_change_without_an_authorizer() -> None:
    session = _session(mode="act")
    decision = await _gate(AgentPolicy(session), session, "winget_install", {"id": SEVENZIP})
    assert isinstance(decision, Deny) and decision.code == "not_authorized"
    assert session.recommendations[0]["tool"] == "winget_install"
    assert session.actions == []


async def test_act_runs_a_normal_change_the_authorizer_allows() -> None:
    calls: list[tuple[Any, ...]] = []
    session = _session(mode="act")
    policy = AgentPolicy(session, authorizer=_yes(calls))
    args = {"id": SEVENZIP}
    assert await _gate(policy, session, "winget_install", args) == Allow(authorization_id=AUTH_ID)
    # The action names the authorization that let it run (ADR-0072 rule 5).
    assert session.actions == [
        {"tool": "winget_install", "args": {"id": SEVENZIP}, "agent_id": HOST,
         "tool_class": NORMAL_CHANGE, "authorization_id": AUTH_ID}
    ]
    assert calls == [(session, "winget_install", {"id": SEVENZIP}, HOST)]
    # The authorizer is handed a copy: it cannot change what runs.
    assert calls[0][2] is not args


@pytest.mark.parametrize(
    "answer",
    [
        False,
        None,
        "yes",
        1,
        True,
        AUTH_ID,
        _grant("patcher", "winget_install", auth_id=""),
        _grant("another_agent", "winget_install"),
        _grant("patcher", "winget_update"),
    ],
    ids=["false", "none", "str", "one", "true", "bare-id", "empty-id", "other-agent", "other-tool"],
)
async def test_only_an_authorization_for_this_agent_and_tool_authorizes(answer: Any) -> None:
    async def authorizer(*_a: Any) -> Any:
        return answer

    session = _session(mode="act")
    decision = await _gate(
        AgentPolicy(session, authorizer=authorizer), session, "winget_install", {"id": SEVENZIP}
    )
    assert isinstance(decision, Deny) and decision.code == "not_authorized"
    assert len(session.recommendations) == 1 and session.actions == []


async def test_an_authorizer_that_fails_has_not_authorized() -> None:
    async def authorizer(*_a: Any) -> bool:
        raise RuntimeError("standing authorizations unavailable")

    session = _session(mode="act")
    decision = await _gate(
        AgentPolicy(session, authorizer=authorizer), session, "winget_install", {"id": SEVENZIP}
    )
    assert isinstance(decision, Deny) and decision.code == "not_authorized"


@pytest.mark.parametrize("tool", sorted(NEVER_AUTHORIZED))
async def test_a_never_authorized_tool_is_refused_without_asking(tool: str) -> None:
    """Even an authorizer that says yes to everything is not consulted for these."""

    calls: list[tuple[Any, ...]] = []
    # Every argument bound, so the call gets past step 4 and reaches step 8.
    arg_names = [a.rstrip("?") for a in CAPABILITY_TOOLS[tool] if a.rstrip("?") != "timeout_s"]
    session = _session(
        mode="act",
        tools=frozenset({tool}),
        constraints=tuple(ArgConstraint(tool, a, frozenset({"v"})) for a in arg_names),
    )
    policy = AgentPolicy(session, authorizer=_yes(calls))
    decision = await _gate(policy, session, tool, {a: "v" for a in arg_names})
    assert isinstance(decision, Deny) and decision.code == "not_authorized"
    assert calls == []
    assert session.actions == []
    assert [r["tool"] for r in session.recommendations] == [tool]


async def test_the_authorizer_is_not_asked_about_a_standard_change() -> None:
    calls: list[tuple[Any, ...]] = []
    session = _session(mode="act")
    policy = AgentPolicy(session, authorizer=_yes(calls))
    assert await _gate(policy, session, "winget_update", {"id": FIREFOX}) == Allow()
    assert calls == []


# -- it never holds ------------------------------------------------------------


async def test_on_hold_raises() -> None:
    session = _session()
    pending = PendingCall(id="p", tool_use_id="t", tool="winget_install", args={}, agent_id=HOST)
    with pytest.raises(RuntimeError):
        await AgentPolicy(session).on_hold(session, pending)


@pytest.mark.parametrize("mode", ["shadow", "act"])
@pytest.mark.parametrize("agent_id", [HOST, None])
async def test_no_tool_in_the_catalog_is_ever_held(mode: str, agent_id: str | None) -> None:
    """Sweep every classified tool, with every argument shape the gate cares about."""

    changes = sorted(t for t, tier in TOOL_CLASSES.items() if tier != READ_ONLY)
    session = _session(
        mode=mode,
        agent_id=agent_id,
        tools=frozenset(TOOL_CLASSES),
        sensitive_ok=True,
        verdict_tool=TRIAGE_VERDICT_TOOL,
        constraints=tuple(
            ArgConstraint(t, "x", frozenset({"v"})) for t in changes if t != TRIAGE_VERDICT_TOOL
        ),
    )
    policy = AgentPolicy(session, authorizer=_yes())
    for tool in sorted(TOOL_CLASSES):
        for args in ({}, {"x": "v"}, {"agent_id": OTHER}, {"id": OTHER}):
            try:
                target = policy.resolve_target(session, tool, dict(args))
            except ToolError:
                continue
            decision = await policy.gate(session, tool, dict(args), target)
            assert isinstance(decision, (Allow, Deny)), (tool, args, decision)


# -- seams ---------------------------------------------------------------------


def test_every_host_naming_server_tool_is_pinned() -> None:
    """A server tool that grows an ``id``/``agent_id`` argument must be listed."""

    for name, schema in SERVER_TOOLS.items():
        props = schema["properties"]
        named = [k for k in ("id", "agent_id") if k in props]
        if name in COPILOT_AGENT_TOOLS:
            # Their ``agent_id`` is a specialized agent, not a machine, so it is
            # not pinned -- and no run may hold them: the catalog refuses a spec
            # naming one (tests/test_copilot_agents.py joins that half).
            continue
        if named:
            assert name in HOST_ARG, f"{name} names a host in {named} and is not pinned"
            assert HOST_ARG[name] in named
    for name in HOST_ARG:
        assert name in SERVER_TOOLS, f"{name} is not a server tool"
        assert name not in CAPABILITY_TOOLS, f"{name}'s id would be a package, not a host"


# -- joined: the real loop, the real executor ---------------------------------


@pytest.fixture
async def store(tmp_path) -> TelemetryStore:
    s = TelemetryStore(db_path=str(tmp_path / "agents.sqlite"))
    await s.connect()
    yield s
    await s.close()


class Rig:
    """The real executor and tunnel; only the wire send is replaced."""

    def __init__(self, store: TelemetryStore) -> None:
        self.sent: list[dict[str, Any]] = []
        registry = AgentRegistry(tokens={HOST: "t1", OTHER: "t2"})
        self.tunnel = AgentTunnel(registry, store, EventStore(db_path=store.db_path))

        async def send_request(agent_id: str, tool: str, args: dict[str, Any], timeout_s: float):
            self.sent.append({"agent_id": agent_id, "tool": tool, "args": dict(args)})
            return {"ok": True, "log": "done", "packages": []}

        self.tunnel.send_request = send_request  # type: ignore[method-assign]
        self.executor = ToolExecutor(
            registry=registry,
            store=store,
            tunnel=self.tunnel,
            call_log=CallLog(),
            screenshots=ScreenshotStore(),
        )

    async def run(
        self, session: AgentSession, calls: list[tuple[str, dict[str, Any]]], authorizer: Any = None
    ) -> tuple[list[dict[str, Any]], FakeAnthropic]:
        """One model turn per call, then an ending turn."""

        scripted = [
            _Response([tool_use_block(f"tu{i}", name, args)], "tool_use")
            for i, (name, args) in enumerate(calls)
        ]
        scripted.append(_Response([text_block("done")], "end_turn"))
        client = FakeAnthropic(scripted)
        session.messages.append({"role": "user", "content": "start"})
        policy = AgentPolicy(session, authorizer=authorizer)
        events = [
            ev
            async for ev in drive_events(
                session,
                self.executor,
                client=client,
                model="test-model",
                policy=policy,
                max_iterations=session.spec.budget.max_iterations,
            )
        ]
        return events, client


def _results_fed_back(client: FakeAnthropic) -> dict[str, dict[str, Any]]:
    """tool_use id -> the tool_result block the model was finally shown."""

    out: dict[str, dict[str, Any]] = {}
    for msg in client.messages.calls[-1]["messages"]:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out[block["tool_use_id"]] = block
    return out


def _error_code(block: dict[str, Any]) -> str | None:
    if not block.get("is_error"):
        return None
    return json.loads(block["content"])["error"]["code"]


def _assert_never_held(events: list[dict[str, Any]], session: AgentSession) -> None:
    assert not [e for e in events if e["type"] == "pending"]
    assert session.pending is None
    assert events[-1]["type"] == "done" and events[-1]["done"] is True


async def test_joined_the_model_is_offered_exactly_the_spec(store: TelemetryStore) -> None:
    rig = Rig(store)
    session = _session()
    _, client = await rig.run(session, [])
    request = client.messages.calls[0]
    assert {t["name"] for t in request["tools"]} == set(_spec().tools)
    assert request["system"][0]["text"] == _spec().prompt


async def test_joined_shadow_reads_but_never_changes(store: TelemetryStore) -> None:
    rig = Rig(store)
    session = _session()
    events, client = await rig.run(
        session,
        [("winget_list", {}), ("winget_update", {"id": FIREFOX})],
    )
    assert rig.sent == [{"agent_id": HOST, "tool": "winget_list", "args": {}}]
    assert session.recommendations == [
        {"tool": "winget_update", "args": {"id": FIREFOX}, "agent_id": HOST,
         "tool_class": STANDARD_CHANGE}
    ]
    fed = _results_fed_back(client)
    assert _error_code(fed["tu0"]) is None
    assert _error_code(fed["tu1"]) == "shadow"
    assert [e["code"] for e in events if e["type"] == "denied"] == ["shadow"]
    _assert_never_held(events, session)


async def test_joined_act_runs_a_constrained_update_on_the_frozen_host(
    store: TelemetryStore,
) -> None:
    rig = Rig(store)
    session = _session(mode="act")
    events, client = await rig.run(
        session,
        [
            ("winget_update", {"id": FIREFOX, "agent_id": HOST}),
            ("winget_update", {"id": FIREFOX, "agent_id": OTHER}),
        ],
    )
    # The first reached the frozen host, with the routing override stripped;
    # the second named another host and reached nothing.
    assert rig.sent == [{"agent_id": HOST, "tool": "winget_update", "args": {"id": FIREFOX}}]
    fed = _results_fed_back(client)
    assert _error_code(fed["tu0"]) is None
    assert _error_code(fed["tu1"]) == "out_of_scope"
    assert len(session.actions) == 1 and session.recommendations == []
    _assert_never_held(events, session)


async def test_joined_act_holds_an_update_to_its_constraints(store: TelemetryStore) -> None:
    rig = Rig(store)
    session = _session(mode="act")
    events, client = await rig.run(
        session,
        [
            ("winget_update", {}),
            ("winget_update", {"id": "Evil.Package"}),
            ("winget_update", {"id": FIREFOX}),
        ],
    )
    assert rig.sent == [{"agent_id": HOST, "tool": "winget_update", "args": {"id": FIREFOX}}]
    fed = _results_fed_back(client)
    assert [_error_code(fed[f"tu{i}"]) for i in range(3)] == ["constraint", "constraint", None]
    assert session.recommendations == []
    _assert_never_held(events, session)


async def test_joined_act_installs_only_with_an_authorization(store: TelemetryStore) -> None:
    rig = Rig(store)
    session = _session(mode="act")
    events, client = await rig.run(session, [("winget_install", {"id": SEVENZIP})])
    assert rig.sent == []
    assert _error_code(_results_fed_back(client)["tu0"]) == "not_authorized"
    assert session.recommendations[0]["tool"] == "winget_install"
    _assert_never_held(events, session)

    rig = Rig(store)
    session = _session(mode="act")
    events, client = await rig.run(
        session, [("winget_install", {"id": SEVENZIP})], authorizer=_yes()
    )
    assert rig.sent == [{"agent_id": HOST, "tool": "winget_install", "args": {"id": SEVENZIP}}]
    assert _error_code(_results_fed_back(client)["tu0"]) is None
    assert session.actions[0]["authorization_id"] == AUTH_ID
    _assert_never_held(events, session)


async def test_joined_a_tool_outside_the_spec_is_forbidden(store: TelemetryStore) -> None:
    rig = Rig(store)
    session = _session(mode="act")
    events, client = await rig.run(
        session,
        [("powershell_exec", {"script": "Remove-Item C:\\ -Recurse", "agent_id": HOST})],
        authorizer=_yes(),
    )
    assert rig.sent == []
    assert _error_code(_results_fed_back(client)["tu0"]) == "forbidden"
    _assert_never_held(events, session)


async def test_joined_a_fleet_wide_read_never_runs_on_a_frozen_run(store: TelemetryStore) -> None:
    rig = Rig(store)
    session = _session(tools=frozenset({"winget_list", "list_agents", "fleet_overview"}),
                       constraints=())
    events, client = await rig.run(session, [("list_agents", {}), ("fleet_overview", {})])
    fed = _results_fed_back(client)
    assert [_error_code(fed[f"tu{i}"]) for i in range(2)] == ["out_of_scope", "out_of_scope"]
    assert not [e for e in events if e["type"] == "tool_result"]
    _assert_never_held(events, session)


async def test_joined_agent_health_stays_on_the_frozen_host(store: TelemetryStore) -> None:
    rig = Rig(store)
    session = _session()
    events, client = await rig.run(
        session,
        [("agent_health", {"id": OTHER}), ("agent_health", {})],
    )
    fed = _results_fed_back(client)
    assert _error_code(fed["tu0"]) == "out_of_scope"
    assert _error_code(fed["tu1"]) is None
    assert json.loads(fed["tu1"]["content"])["agent_id"] == HOST
    results = [e for e in events if e["type"] == "tool_result"]
    assert [e["args"] for e in results] == [{"id": HOST}]
    _assert_never_held(events, session)

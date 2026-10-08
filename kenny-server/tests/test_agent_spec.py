"""``AgentSpec`` validation and the hash that binds grants to behaviour (ADR-0071)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kenny_server.agents.spec import (
    EVENTS,
    MODES,
    TRIGGER_KINDS,
    VERDICT_TOOLS,
    AgentSpec,
    ArgConstraint,
    Budget,
    SpecError,
    Trigger,
    validate,
)
from kenny_server.tool_classes import READ_ONLY, SENSITIVE_TOOLS, TOOL_CLASSES
from kenny_server.toolloop import TRIAGE_VERDICT_TOOL


def make_spec(**overrides: object) -> AgentSpec:
    base = AgentSpec(
        id="disk_watch",
        title="Disk watch",
        description="Looks at disks.",
        prompt="Look at the disk and say what you see.",
        trigger=Trigger(kind="event", event="ticket_created"),
        tools=frozenset({"diag_services", "ticket_triage_verdict"}),
        verdict_tool="ticket_triage_verdict",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# -- validate ------------------------------------------------------------------


def test_a_well_formed_spec_is_returned_unchanged() -> None:
    spec = make_spec()
    assert validate(spec) is spec


def test_the_fixture_tools_are_classified() -> None:
    # The helper's tool names are real; otherwise the refusal tests below could
    # pass for the wrong reason.
    assert {"diag_services", "ticket_triage_verdict"} <= set(TOOL_CLASSES)


@pytest.mark.parametrize("bad_id", ["", "A", "a", "Triage", "1triage", "has-dash", "x" * 42, "sp ace"])
def test_a_malformed_id_is_refused(bad_id: str) -> None:
    with pytest.raises(SpecError, match="agent id"):
        validate(make_spec(id=bad_id))


@pytest.mark.parametrize("good_id", ["ab", "triage", "disk_watch", "a1_b2", "x" * 41])
def test_a_well_formed_id_is_accepted(good_id: str) -> None:
    validate(make_spec(id=good_id))


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t "])
def test_an_empty_prompt_is_refused(prompt: str) -> None:
    with pytest.raises(SpecError, match="prompt is empty"):
        validate(make_spec(prompt=prompt))


def test_an_empty_tool_set_is_refused() -> None:
    with pytest.raises(SpecError, match="tool set is empty"):
        validate(make_spec(tools=frozenset(), verdict_tool=None))


def test_an_unclassified_tool_is_refused() -> None:
    spec = make_spec(tools=frozenset({"diag_services", "diag_srevices"}), verdict_tool=None)
    with pytest.raises(SpecError, match="unclassified tool.*diag_srevices"):
        validate(spec)


def test_a_sensitive_tool_needs_sensitive_ok() -> None:
    tools = frozenset({"diag_services", "screen_capture"})
    assert "screen_capture" in SENSITIVE_TOOLS
    with pytest.raises(SpecError, match="sensitive tool.*screen_capture"):
        validate(make_spec(tools=tools, verdict_tool=None))
    validate(make_spec(tools=tools, verdict_tool=None, sensitive_ok=True))


def test_sensitive_ok_alone_is_fine_without_a_sensitive_tool() -> None:
    validate(make_spec(sensitive_ok=True))


def test_a_verdict_tool_outside_the_tool_set_is_refused() -> None:
    with pytest.raises(SpecError, match="verdict tool"):
        validate(make_spec(tools=frozenset({"diag_services"})))


def test_no_verdict_tool_is_allowed() -> None:
    validate(make_spec(tools=frozenset({"diag_services"}), verdict_tool=None))


def test_the_verdict_tools_are_the_loops_verdict_tool() -> None:
    # spec.py stays stdlib-only, so it names the verdict tool as a literal;
    # joined here to the name the loop and triage actually route.
    assert VERDICT_TOOLS == frozenset({TRIAGE_VERDICT_TOOL})


@pytest.mark.parametrize("tool", ["powershell_exec", "winget_update", "diag_services"])
def test_only_a_verdict_tool_may_be_the_verdict_tool(tool: str) -> None:
    """The verdict exemption must not be a way to name an unconstrained change.

    ``powershell_exec`` as the verdict tool used to validate and then run in
    shadow with no constraint at all.
    """

    spec = make_spec(tools=frozenset({"diag_services", tool}), verdict_tool=tool)
    with pytest.raises(SpecError, match="not a verdict tool"):
        validate(spec)


def test_an_unknown_trigger_kind_is_refused() -> None:
    with pytest.raises(SpecError, match="trigger kind"):
        validate(make_spec(trigger=Trigger(kind="webhook")))


@pytest.mark.parametrize("kind", TRIGGER_KINDS)
def test_every_declared_trigger_kind_can_be_expressed(kind: str) -> None:
    event = EVENTS[0] if kind == "event" else None
    validate(make_spec(trigger=Trigger(kind=kind, event=event)))


@pytest.mark.parametrize("event", [None, "ticket_deleted", ""])
def test_an_event_trigger_needs_a_known_event(event: str | None) -> None:
    with pytest.raises(SpecError, match="unknown event"):
        validate(make_spec(trigger=Trigger(kind="event", event=event)))


@pytest.mark.parametrize("kind", ["schedule", "on_demand"])
def test_only_an_event_trigger_names_an_event(kind: str) -> None:
    with pytest.raises(SpecError, match="only an event trigger"):
        validate(make_spec(trigger=Trigger(kind=kind, event="ticket_created")))


def test_an_unknown_default_mode_is_refused() -> None:
    with pytest.raises(SpecError, match="default mode"):
        validate(make_spec(default_mode="yolo"))


@pytest.mark.parametrize("mode", [m for m in MODES if m != "act"])
def test_off_and_shadow_are_valid_defaults(mode: str) -> None:
    validate(make_spec(default_mode=mode))


def test_act_is_never_a_default_mode() -> None:
    # Moving an agent to act is a superuser's decision; a spec that shipped in
    # act would make it for every fresh install.
    assert "act" in MODES
    with pytest.raises(SpecError, match="default mode act"):
        validate(make_spec(default_mode="act"))


@pytest.mark.parametrize("n", [0, -1])
def test_a_budget_below_one_iteration_is_refused(n: int) -> None:
    with pytest.raises(SpecError, match="max_iterations"):
        validate(make_spec(budget=Budget(max_iterations=n)))


@pytest.mark.parametrize("version", [0, -3])
def test_a_version_below_one_is_refused(version: int) -> None:
    with pytest.raises(SpecError, match="version"):
        validate(make_spec(version=version))


def test_spec_error_is_a_value_error() -> None:
    assert issubclass(SpecError, ValueError)


# -- spec_hash -----------------------------------------------------------------


def test_the_hash_is_stable() -> None:
    first = make_spec().spec_hash
    assert first == make_spec().spec_hash
    assert len(first) == 64
    int(first, 16)


def test_the_hash_ignores_tool_ordering() -> None:
    a = make_spec(tools=frozenset({"diag_services", "ticket_triage_verdict"}))
    b = make_spec(tools=frozenset(["ticket_triage_verdict", "diag_services"]))
    assert a.spec_hash == b.spec_hash


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "A different prompt."},
        {"tools": frozenset({"diag_services", "diag_processes", "ticket_triage_verdict"})},
        {"trigger": Trigger(kind="on_demand")},
        {"budget": Budget(max_iterations=3)},
        {"version": 2},
        {"sensitive_ok": True},
        {"verdict_tool": None},
        {"id": "other_agent"},
    ],
    ids=lambda c: next(iter(c)),
)
def test_the_hash_changes_with_what_the_agent_does(change: dict[str, object]) -> None:
    assert make_spec(**change).spec_hash != make_spec().spec_hash


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Another title"},
        {"description": "Another description."},
        {"default_mode": "act"},
    ],
    ids=lambda c: next(iter(c)),
)
def test_the_hash_ignores_presentation_and_install_defaults(change: dict[str, object]) -> None:
    assert make_spec(**change).spec_hash == make_spec().spec_hash


def test_the_extra_tool_in_the_hash_case_is_classified() -> None:
    # Otherwise the "tools change the hash" case would pass on an unclassified
    # name that validate() refuses anyway.
    assert "diag_processes" in TOOL_CLASSES


def test_to_public_carries_the_hash_and_tool_classes() -> None:
    spec = make_spec()
    public = spec.to_public()
    assert public["spec_hash"] == spec.spec_hash
    assert public["tool_classes"] == {t: TOOL_CLASSES[t] for t in sorted(spec.tools)}
    assert "prompt" not in public


# -- argument constraints ------------------------------------------------------

CHANGE_TOOL = "winget_update"


def constrained(**overrides: object) -> AgentSpec:
    base = make_spec(
        tools=frozenset({"diag_services", CHANGE_TOOL, "ticket_triage_verdict"}),
        constraints=(ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git"})),),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_the_change_tool_in_these_cases_is_change_tier() -> None:
    assert TOOL_CLASSES[CHANGE_TOOL] != READ_ONLY
    assert TOOL_CLASSES["diag_services"] == READ_ONLY


def test_a_constrained_change_tier_tool_is_accepted() -> None:
    validate(constrained())


def test_a_change_tier_tool_without_a_constraint_is_refused() -> None:
    with pytest.raises(SpecError, match=f"change-tier.*{CHANGE_TOOL}.*no argument constraint"):
        validate(constrained(constraints=()))


def test_the_verdict_tool_is_exempt_from_needing_a_constraint() -> None:
    assert TOOL_CLASSES["ticket_triage_verdict"] != READ_ONLY
    validate(make_spec())  # verdict tool in tools, no constraints


def test_a_change_tier_tool_that_is_not_the_verdict_tool_needs_one_even_if_a_verdict_exists() -> None:
    spec = constrained(constraints=(), verdict_tool="ticket_triage_verdict")
    with pytest.raises(SpecError, match="no argument constraint"):
        validate(spec)


def test_a_constraint_on_a_read_only_tool_is_refused() -> None:
    spec = constrained(
        constraints=(
            ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git"})),
            ArgConstraint("diag_services", "name", frozenset({"Spooler"})),
        )
    )
    with pytest.raises(SpecError, match="read-only diag_services"):
        validate(spec)


def test_a_constraint_naming_a_tool_outside_the_tool_set_is_refused() -> None:
    spec = constrained(
        constraints=(
            ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git"})),
            ArgConstraint("winget_install", "id", frozenset({"Git.Git"})),
        )
    )
    with pytest.raises(SpecError, match="winget_install.*not in its tools"):
        validate(spec)


def test_a_duplicate_tool_arg_constraint_is_refused() -> None:
    spec = constrained(
        constraints=(
            ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git"})),
            ArgConstraint(CHANGE_TOOL, "id", frozenset({"Other.Pkg"})),
        )
    )
    with pytest.raises(SpecError, match=f"two constraints on {CHANGE_TOOL}.id"):
        validate(spec)


def test_two_different_args_of_one_tool_are_not_duplicates() -> None:
    validate(
        constrained(
            constraints=(
                ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git"})),
                ArgConstraint(CHANGE_TOOL, "source", frozenset({"winget"})),
            )
        )
    )


@pytest.mark.parametrize(
    "bad",
    [
        ArgConstraint(CHANGE_TOOL, "", frozenset({"Git.Git"})),
        ArgConstraint(CHANGE_TOOL, "id", frozenset()),
        ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git", ""})),
    ],
)
def test_a_constraint_needs_an_arg_and_non_empty_values(bad: ArgConstraint) -> None:
    with pytest.raises(SpecError, match="non-empty"):
        validate(constrained(constraints=(bad,)))


def test_constraints_change_the_hash() -> None:
    base = constrained()
    widened = constrained(
        constraints=(ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git", "Other.Pkg"})),)
    )
    other_arg = constrained(
        constraints=(ArgConstraint(CHANGE_TOOL, "source", frozenset({"Git.Git"})),)
    )
    assert len({base.spec_hash, widened.spec_hash, other_arg.spec_hash}) == 3


def test_the_constraint_order_does_not_change_the_hash() -> None:
    a = ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git"}))
    b = ArgConstraint(CHANGE_TOOL, "source", frozenset({"winget"}))
    assert constrained(constraints=(a, b)).spec_hash == constrained(constraints=(b, a)).spec_hash


def test_an_absent_or_empty_argument_never_satisfies_a_constraint() -> None:
    c = ArgConstraint(CHANGE_TOOL, "id", frozenset({"Git.Git"}))
    assert c.admits({"id": "Git.Git"})
    assert not c.admits({})
    assert not c.admits({"id": ""})
    assert not c.admits({"id": None})
    assert not c.admits({"id": "git.git"})
    assert not c.admits({"id": ["Git.Git"]})


def test_to_public_lists_constraints() -> None:
    public = constrained().to_public()
    assert public["constraints"] == [{"tool": CHANGE_TOOL, "arg": "id", "allowed": ["Git.Git"]}]

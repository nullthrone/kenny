"""The config-hygiene agent, joined end to end (ADR-0071, ADR-0072).

What is guaranteed here, each where it can break:

* **Hit tracking happens where the server applies a rule.** A suppression
  records its match when ``TelemetryStore`` stamps a snapshot through the real
  annotator; an auto-ticket rule when the real ``AlertEngine`` decides an alert
  with it. A table that predates the hit columns gains them on connect.
* **"Unused" is the server's evidence.** Age, never matched, recently created,
  and a provider that fails (the empty set, which admits nothing).
* **The gate admits only computed ids**, and no free argument beside them.
* **The removal re-checks at execution**: a rule that matched after the run's
  evidence was computed is refused, not removed.
* **The tools exist only on the agent executor**: absent from the copilot's
  schemas and every ticket session, registered on ``main.py``'s agent executor.
* **Joined runs**: shadow (recommendations), act with a ``server`` grant (the
  removal happens and the audit row names the authorization), act without one
  (recommendation only).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kenny_server import chat, toolloop
from kenny_server.agents import catalog
from kenny_server.agents.authorizations import AuthorizationStore
from kenny_server.agents.catalog import CATALOG
from kenny_server.agents.catalog.hygiene import CONFIG_HYGIENE
from kenny_server.agents.hygiene import (
    EVIDENCE_NAMES,
    UNUSED_AFTER_DAYS,
    UNUSED_SUPPRESSIONS,
    UNUSED_TICKET_RULES,
    RuleHygiene,
)
from kenny_server.agents.hygiene import register as register_hygiene
from kenny_server.agents.policy import AgentPolicy, AgentSession
from kenny_server.agents.spec import (
    AgentSpec,
    ArgConstraint,
    SpecError,
    Trigger,
    evidence_names,
    validate,
    with_evidence,
)
from kenny_server.agents.verdict import register as register_verdict
from kenny_server.alerting import AlertEngine
from kenny_server.reliability_suppression import SuppressionList
from kenny_server.rule_hits import iso, unused, unused_ids
from kenny_server.store import (
    AlertStateStore,
    ReliabilitySuppressionStore,
    TicketRuleStore,
)
from kenny_server.ticket_assistant import EXCLUDED_TOOLS, allowed_tools_for
from kenny_server.ticket_rules import TicketRuleList, decide
from kenny_server.ticket_rules import rule_id as ticket_rule_id
from kenny_server.tool_classes import NORMAL_CHANGE, READ_ONLY, classify
from kenny_server.toolloop import AGENT_ONLY_TOOLS, Allow, Deny, ToolExecutor
from kenny_server.tunnel import ToolError

from test_agent_runner import World, _text, _tool

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=UNUSED_AFTER_DAYS + 10)
RECENT = NOW - timedelta(days=5)

S_OLD = "|Microsoft-Windows-CAPI2|4176"
S_USED = "|Microsoft-Windows-Kernel-Power|41"
T_OLD = ticket_rule_id("", "health", "gpu")
T_USED = ticket_rule_id("", "health", "disk")


def _snapshot(*events: dict[str, Any]) -> dict[str, Any]:
    return {"reliability": {"status": "warn", "summary": "events", "events": list(events)}}


def _event(source: str, event_id: int, last_seen: str) -> dict[str, Any]:
    return {"source": source, "event_id": event_id, "level": "error", "count": 3,
            "last_seen": last_seen}


async def _set_record(
    store: Any, table: str, rule: str, *, created: datetime, matched: datetime | None = None
) -> None:
    """Write a rule's creation and last match straight to its row (a test's clock)."""

    await store._conn.execute(
        f"UPDATE {table} SET created_at = ?, last_matched_at = ? WHERE id = ?",
        (iso(created), iso(matched) if matched else None, rule),
    )
    await store._conn.commit()


# -- the world -------------------------------------------------------------------


class HygieneWorld:
    """The runner world plus the two real rule stores and their mirrors."""

    def __init__(self, base: World) -> None:
        self.base = base

    async def setup(self) -> None:
        db = self.base.db_path
        self.supp_store = ReliabilitySuppressionStore(db)
        await self.supp_store.connect()
        self.rule_store = TicketRuleStore(db)
        await self.rule_store.connect()
        self.authorizations = AuthorizationStore(db)
        await self.authorizations.connect()
        self.suppression = SuppressionList(self.supp_store, clock=lambda: NOW)
        self.ticket_rules = TicketRuleList(self.rule_store)
        # The real annotator seam, as main.py wires it.
        self.base.telemetry.annotators = [self.suppression.mark]

    async def close(self) -> None:
        for store in (self.authorizations, self.rule_store, self.supp_store):
            await store.close()

    async def rules(self) -> None:
        """S_OLD/T_OLD unused for months, S_USED/T_USED matched last week."""

        await self.suppression.add(event_id=4176, source="Microsoft-Windows-CAPI2", note="quirk")
        await self.suppression.add(event_id=41, source="Microsoft-Windows-Kernel-Power")
        await self.ticket_rules.add(event_type="health", decision="never", section="gpu")
        await self.ticket_rules.add(event_type="health", decision="open_crit", section="disk")
        await _set_record(self.supp_store, "reliability_suppressions", S_OLD, created=OLD)
        await _set_record(
            self.supp_store, "reliability_suppressions", S_USED, created=OLD, matched=RECENT
        )
        await _set_record(self.rule_store, "ticket_rules", T_OLD, created=OLD)
        await _set_record(self.rule_store, "ticket_rules", T_USED, created=OLD, matched=RECENT)
        await self.suppression.load()
        await self.ticket_rules.load()

    def runner(self):
        runner = self.base.runner()
        runner.authorizations = self.authorizations
        runner._now = lambda: NOW  # type: ignore[method-assign]
        register_verdict(self.base.executor, tickets=self.base.tickets)
        self.hygiene = register_hygiene(
            runner,
            self.base.executor,
            suppression=self.suppression,
            ticket_rules=self.ticket_rules,
            call_log=self.base.call_log,
        )
        return runner


@pytest.fixture
async def hw(tmp_path):
    base = World(str(tmp_path / "hygiene.sqlite"))
    await base.setup()
    world = HygieneWorld(base)
    await world.setup()
    yield world
    await world.close()
    await base.close()


# -- hit tracking: suppressions ----------------------------------------------------


async def test_a_snapshot_stamped_by_a_suppression_records_the_match(hw: HygieneWorld) -> None:
    await hw.rules()
    seen = NOW - timedelta(hours=3)
    await hw.base.telemetry.insert(
        "pc1", iso(NOW), _snapshot(_event("Microsoft-Windows-CAPI2", 4176, iso(seen)))
    )

    record = await hw.base.telemetry.latest("pc1")

    assert record is not None and record["snapshot"]["reliability"]["events"][0]["suppressed"]
    rule = hw.suppression.get(S_OLD)
    assert rule is not None
    assert rule["last_matched_at"] == iso(seen)  # the event's own time, not the read's
    assert rule["match_count"] == 1
    await hw.suppression.flush()
    [stored] = [r for r in await hw.supp_store.list() if r["id"] == S_OLD]
    assert (stored["last_matched_at"], stored["match_count"]) == (iso(seen), 1)
    # ... and it is no longer unused.
    assert S_OLD not in unused_ids(hw.suppression.rules(), NOW, UNUSED_AFTER_DAYS)


async def test_re_reading_an_old_snapshot_does_not_make_a_rule_look_recent(hw: HygieneWorld) -> None:
    await hw.rules()
    long_ago = OLD - timedelta(days=1)
    await hw.base.telemetry.insert(
        "pc1", iso(long_ago), _snapshot(_event("Microsoft-Windows-CAPI2", 4176, iso(long_ago)))
    )
    await hw.base.telemetry.history("pc1")
    rule = hw.suppression.get(S_OLD)
    assert rule is not None and rule["match_count"] == 1
    # Matched, but long ago: the record says so, and the rule is still unused.
    assert unused(rule, NOW, UNUSED_AFTER_DAYS)


async def test_a_match_in_the_future_is_clamped_to_now(hw: HygieneWorld) -> None:
    await hw.rules()
    await hw.base.telemetry.insert(
        "pc1", iso(NOW),
        _snapshot(_event("Microsoft-Windows-CAPI2", 4176, iso(NOW + timedelta(days=400)))),
    )
    await hw.base.telemetry.latest("pc1")
    assert hw.suppression.get(S_OLD)["last_matched_at"] == iso(NOW)  # type: ignore[index]


async def test_a_reload_keeps_matches_not_yet_written(hw: HygieneWorld) -> None:
    await hw.rules()
    hw.suppression.mark("pc1", _snapshot(_event("Microsoft-Windows-CAPI2", 4176, iso(RECENT))))
    # add() reloads the mirror from the store; the queued match must survive it.
    await hw.suppression.add(event_id=7, source="x")
    assert hw.suppression.get(S_OLD)["last_matched_at"] == iso(RECENT)  # type: ignore[index]


async def test_old_tables_gain_the_hit_columns(tmp_path) -> None:
    import aiosqlite

    db = str(tmp_path / "old.sqlite")
    async with aiosqlite.connect(db) as conn:
        await conn.executescript(
            """
            CREATE TABLE reliability_suppressions (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '', event_id INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '');
            INSERT INTO reliability_suppressions VALUES ('|s|1', '', 's', 1, '', '2026-01-01', '');
            CREATE TABLE ticket_rules (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL, section TEXT NOT NULL DEFAULT '',
                decision TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '');
            INSERT INTO ticket_rules VALUES ('|health|', '', 'health', '', 'never', '', '2026-01-01', '');
            """
        )
        await conn.commit()
    supp, rules = ReliabilitySuppressionStore(db), TicketRuleStore(db)
    for store in (supp, rules):
        await store.connect()
        await store.connect()  # idempotent
    try:
        [s] = await supp.list()
        [t] = await rules.list()
        assert (s["last_matched_at"], s["match_count"]) == (None, 0)
        assert (t["last_matched_at"], t["match_count"]) == (None, 0)
        await supp.record_matches({"|s|1": (iso(NOW), 2)})
        await rules.record_matches({"|health|": (iso(NOW), 1)})
        # Only forward: an older match does not move it back.
        await supp.record_matches({"|s|1": (iso(OLD), 1)})
        [s] = await supp.list()
        [t] = await rules.list()
        assert (s["last_matched_at"], s["match_count"]) == (iso(NOW), 3)
        assert (t["last_matched_at"], t["match_count"]) == (iso(NOW), 1)
    finally:
        await supp.close()
        await rules.close()


# -- hit tracking: auto-ticket rules -------------------------------------------------


def test_decide_reports_every_rule_it_applied() -> None:
    rules = {
        ("", "health", "gpu"): {"id": T_OLD, "decision": "never"},
        ("", "health", "disk"): {"id": T_USED, "decision": "open_crit"},
    }
    kept_closed = decide(rules, kind="alert", agent_id="pc1", event_type="health",
                         priority="normal", sections={"gpu": "crit"})
    # A ``never`` rule that kept a ticket closed was applied too.
    assert (kept_closed.open, kept_closed.matched) == (False, (T_OLD,))
    opened = decide(rules, kind="alert", agent_id="pc1", event_type="health",
                    priority="high", sections={"disk": "crit", "gpu": "crit"})
    assert (opened.open, opened.matched) == (True, (T_USED,))
    nothing = decide(rules, kind="alert", agent_id="pc1", event_type="health",
                     priority="high", sections={"memory": "crit"})
    assert nothing.matched == ()


async def test_an_alert_decided_by_a_rule_records_the_match(hw: HygieneWorld) -> None:
    await hw.rules()
    state = AlertStateStore(hw.base.db_path)
    await state.connect()
    opened: list[Any] = []

    async def open_ticket(note: Any) -> None:
        opened.append(note)

    class _Online:
        def get(self, agent_id: str) -> Any:
            return type("A", (), {"online": True})()

    engine = AlertEngine(
        store=hw.base.telemetry,
        alert_state=state,
        event_store=hw.base.event_store,
        registry=_Online(),
        notifiers=[],
        open_ticket=open_ticket,
        ticket_rules=hw.ticket_rules,
    )
    disk = {"disk": {"status": "ok", "summary": "C: 96% full",
                     "volumes": [{"mount": "C:", "percent_used": 96.0}]}}
    try:
        await hw.base.telemetry.insert("pc1", iso(NOW - timedelta(minutes=1)), disk)
        await engine.evaluate_once(NOW)
    finally:
        await state.close()

    assert len(opened) == 1  # the open_crit rule decided it
    rule = hw.ticket_rules.get(T_USED)
    assert rule is not None and (rule["last_matched_at"], rule["match_count"]) == (iso(NOW), 1)
    [stored] = [r for r in await hw.rule_store.list() if r["id"] == T_USED]
    assert (stored["last_matched_at"], stored["match_count"]) == (iso(NOW), 1)
    # The gpu rule decided nothing and records nothing.
    assert hw.ticket_rules.get(T_OLD)["match_count"] == 0  # type: ignore[index]


# -- evidence ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("created", "matched", "expected"),
    [
        (OLD, None, True),  # never matched, created long ago
        (OLD, NOW - timedelta(days=UNUSED_AFTER_DAYS + 1), True),  # last match too old
        (OLD, RECENT, False),  # matched recently
        (RECENT, None, False),  # created recently: creation counts as a match
        (NOW - timedelta(days=UNUSED_AFTER_DAYS), None, True),  # exactly N days
    ],
)
def test_unused_is_age_of_the_last_activity(created, matched, expected) -> None:
    rule = {"id": "r", "created_at": iso(created),
            "last_matched_at": iso(matched) if matched else None}
    assert unused(rule, NOW, UNUSED_AFTER_DAYS) is expected


@pytest.mark.parametrize("created", [None, "", "not a date"])
def test_a_rule_that_cannot_be_dated_is_never_unused(created) -> None:
    assert not unused({"id": "r", "created_at": created}, NOW, UNUSED_AFTER_DAYS)


async def test_the_providers_compute_from_the_server_record(hw: HygieneWorld) -> None:
    await hw.rules()
    runner = hw.runner()
    assert runner.evidence_providers() >= EVIDENCE_NAMES
    assert await runner.resolve_evidence(CONFIG_HYGIENE) == {
        UNUSED_SUPPRESSIONS: [S_OLD],
        UNUSED_TICKET_RULES: [T_OLD],
    }


async def test_a_failing_missing_or_malformed_provider_admits_nothing(hw: HygieneWorld) -> None:
    runner = hw.base.runner()

    async def boom() -> list[str]:
        raise RuntimeError("store down")

    async def one_string() -> str:
        return "abc"

    async def mixed() -> list[Any]:
        return ["ok", 3, "", None]

    runner.register_evidence(UNUSED_SUPPRESSIONS, boom)  # type: ignore[arg-type]
    assert await runner.resolve_evidence(CONFIG_HYGIENE) == {
        UNUSED_SUPPRESSIONS: [],
        UNUSED_TICKET_RULES: [],  # never registered
    }
    with pytest.raises(ValueError, match="already registered"):
        runner.register_evidence(UNUSED_SUPPRESSIONS, mixed)
    runner.register_evidence(UNUSED_TICKET_RULES, one_string)  # type: ignore[arg-type]
    assert (await runner.resolve_evidence(CONFIG_HYGIENE))[UNUSED_TICKET_RULES] == []
    other = hw.base.runner()
    other.register_evidence(UNUSED_SUPPRESSIONS, mixed)
    assert (await other.resolve_evidence(CONFIG_HYGIENE))[UNUSED_SUPPRESSIONS] == ["ok"]


def test_every_evidence_the_catalog_names_has_a_provider_in_the_wiring() -> None:
    # Joined: the names the specs declare against the names register() feeds.
    named = {n for spec in CATALOG.values() for n in evidence_names(spec)}
    assert named == EVIDENCE_NAMES

    class _Runner:
        def __init__(self) -> None:
            self.names: set[str] = set()
            self.now = lambda: NOW

        def register_evidence(self, name: str, _provider: Any) -> None:
            self.names.add(name)

    fake = _Runner()

    class _Executor:
        def register_server_tool(self, *_a: Any) -> None:
            return None

    register_hygiene(fake, _Executor(), suppression=None, ticket_rules=None, call_log=None)
    assert fake.names == named


# -- the spec ------------------------------------------------------------------------


def test_the_agent_declares_the_documented_shape() -> None:
    spec = CATALOG["config_hygiene"]
    assert spec is CONFIG_HYGIENE
    assert catalog.check_dispatchable(validate(spec)) is spec
    assert spec.default_mode == "shadow"
    assert spec.trigger == Trigger(kind="schedule", min_interval_days=28)
    assert spec.params == ("window",)
    assert spec.verdict_tool == toolloop.AGENT_VERDICT_TOOL
    assert {t for t in spec.tools if classify(t) == READ_ONLY} == {
        "reliability_suppression_list", "ticket_rule_list"}
    assert {t for t in spec.tools if classify(t) == NORMAL_CHANGE} == {
        "reliability_suppression_remove", "ticket_rule_remove"}
    assert spec.constraints == (
        ArgConstraint("reliability_suppression_remove", "rule_id", evidence=UNUSED_SUPPRESSIONS),
        ArgConstraint("ticket_rule_remove", "rule_id", evidence=UNUSED_TICKET_RULES),
    )
    assert str(UNUSED_AFTER_DAYS) in spec.prompt and "untrusted" in spec.prompt


def test_evidence_is_hashed_by_declaration_and_never_declared_with_values() -> None:
    filled = with_evidence(CONFIG_HYGIENE, {UNUSED_SUPPRESSIONS: [S_OLD]})
    assert filled.spec_hash == CONFIG_HYGIENE.spec_hash
    assert filled.constraints_for("reliability_suppression_remove")[0].allowed == {S_OLD}
    assert filled.constraints_for("ticket_rule_remove")[0].allowed == frozenset()
    declared = replace(
        CONFIG_HYGIENE,
        constraints=(
            ArgConstraint("reliability_suppression_remove", "rule_id",
                          frozenset({S_OLD}), evidence=UNUSED_SUPPRESSIONS),
            CONFIG_HYGIENE.constraints[1],
        ),
    )
    with pytest.raises(SpecError, match="never declared"):
        catalog.check_dispatchable(declared)
    both = replace(
        CONFIG_HYGIENE,
        params=("window", "rules"),
        constraints=(
            ArgConstraint("reliability_suppression_remove", "rule_id",
                          param="rules", evidence=UNUSED_SUPPRESSIONS),
            CONFIG_HYGIENE.constraints[1],
        ),
    )
    with pytest.raises(SpecError, match="both a param and evidence"):
        validate(both)


def test_a_server_only_schedule_agent_may_not_name_a_host_tool() -> None:
    spec = replace(CONFIG_HYGIENE, tools=CONFIG_HYGIENE.tools | {"diag_services"})
    with pytest.raises(SpecError, match="runs on no host"):
        catalog.check_dispatchable(spec)


@pytest.mark.parametrize("bad", [0, 367, True, "28"])
def test_min_interval_is_whole_days_on_a_schedule(bad: Any) -> None:
    with pytest.raises(SpecError):
        validate(replace(CONFIG_HYGIENE, trigger=Trigger(kind="schedule", min_interval_days=bad)))
    with pytest.raises(SpecError):
        validate(AgentSpec(id="od", title="t", description="d", prompt="p",
                           trigger=Trigger(kind="on_demand", min_interval_days=28),
                           tools=frozenset({"ticket_rule_list"})))


# -- the gate ------------------------------------------------------------------------


def _session(mode: str, **evidence: list[str]) -> AgentSession:
    return AgentSession(id="run1", spec=with_evidence(CONFIG_HYGIENE, evidence), mode=mode)


async def _gate(policy: AgentPolicy, session: AgentSession, tool: str, args: dict[str, Any]):
    target = policy.resolve_target(session, tool, args)
    return await policy.gate(session, tool, args, target)


async def test_the_gate_admits_only_an_id_the_server_computed() -> None:
    session = _session("shadow", unused_suppressions=[S_OLD])
    policy = AgentPolicy(session)
    tool = "reliability_suppression_remove"
    for args in ({"rule_id": S_USED}, {"rule_id": ""}, {}, {"rule_id": [S_OLD]}):
        decision = await _gate(policy, session, tool, dict(args))
        assert isinstance(decision, Deny) and decision.code == "constraint", args
    # A free argument beside an admitted id is refused, not ignored.
    decision = await _gate(policy, session, tool, {"rule_id": S_OLD, "agent_id": "pc1"})
    assert isinstance(decision, Deny) and decision.code in ("constraint", "out_of_scope")
    decision = await _gate(policy, session, tool, {"rule_id": S_OLD, "note": "x"})
    assert isinstance(decision, Deny) and decision.code == "constraint"
    # Nothing computed for ticket rules: nothing admitted.
    decision = await _gate(policy, session, "ticket_rule_remove", {"rule_id": T_OLD})
    assert isinstance(decision, Deny) and decision.code == "constraint"
    assert session.recommendations == []
    # The computed id, in shadow: a recommendation.
    decision = await _gate(policy, session, tool, {"rule_id": S_OLD})
    assert isinstance(decision, Deny) and decision.code == "shadow"
    assert session.recommendations == [
        {"tool": tool, "args": {"rule_id": S_OLD}, "agent_id": None, "tool_class": NORMAL_CHANGE}
    ]
    # The reads are read-only and need nothing.
    assert await _gate(policy, session, "ticket_rule_list", {}) == Allow()


# -- the removal re-checks at execution -------------------------------------------------


async def test_a_rule_that_matched_since_the_evidence_is_refused(hw: HygieneWorld) -> None:
    await hw.rules()
    hygiene = RuleHygiene(suppression=hw.suppression, ticket_rules=hw.ticket_rules,
                          call_log=hw.base.call_log, now=lambda: NOW)
    assert await hygiene.unused_suppressions() == [S_OLD]
    # Between the evidence and the call, the server applies the rule again.
    hw.suppression.mark("pc1", _snapshot(_event("Microsoft-Windows-CAPI2", 4176, iso(NOW))))
    session = AgentSession(id="run1", spec=CONFIG_HYGIENE, mode="act")

    with pytest.raises(ToolError) as refused:
        await hygiene.remove_suppression({"rule_id": S_OLD}, session=session,
                                         authorization_id="auth1")
    assert refused.value.code == "in_use" and "not removed" in refused.value.message
    assert hw.suppression.get(S_OLD) is not None
    [entry] = await hw.base.call_log.list()
    assert (entry["tool"], entry["ok"], entry["actor"], entry["run_id"],
            entry["authorization_id"], entry["agent_id"]) == (
        "reliability_suppression_remove", False, "agent:config_hygiene", "run1", "auth1", None)


async def test_the_handlers_refuse_outside_an_agent_run(hw: HygieneWorld) -> None:
    await hw.rules()
    hygiene = RuleHygiene(suppression=hw.suppression, ticket_rules=hw.ticket_rules,
                          call_log=hw.base.call_log, now=lambda: NOW)
    with pytest.raises(ToolError) as refused:
        await hygiene.remove_ticket_rule({"rule_id": T_OLD}, session=None)
    assert refused.value.code == "no_run"
    assert hw.ticket_rules.get(T_OLD) is not None


# -- only on the agent executor ----------------------------------------------------------


def test_the_rule_tools_are_withheld_from_the_copilot_and_every_ticket_session() -> None:
    assert AGENT_ONLY_TOOLS <= set(toolloop.SERVER_TOOLS)
    assert AGENT_ONLY_TOOLS <= toolloop.SURFACE_ONLY_TOOLS
    copilot = {s["name"] for s in toolloop.build_tool_schemas()}
    assert not (copilot & AGENT_ONLY_TOOLS)
    assert not ({s["name"] for s in chat._TOOL_SCHEMAS} & AGENT_ONLY_TOOLS)
    assert AGENT_ONLY_TOOLS <= EXCLUDED_TOOLS
    for profile in ("operator", "power-user", "self-service-basic", None):
        for scoped in (False, True):
            for triage in (False, True):
                offered = allowed_tools_for(profile=profile, scoped=scoped, triage=triage)
                assert not (offered & AGENT_ONLY_TOOLS), (profile, scoped, triage)


async def test_an_executor_without_the_handlers_cannot_run_them(hw: HygieneWorld) -> None:
    plain = ToolExecutor(registry=hw.base.registry, store=hw.base.telemetry,
                         tunnel=hw.base.tunnel, call_log=hw.base.call_log,
                         screenshots=hw.base.executor.screenshots)
    for tool in sorted(AGENT_ONLY_TOOLS):
        with pytest.raises(ToolError) as refused:
            await plain.run_server_tool(tool, {"rule_id": S_OLD})
        assert refused.value.code == "unknown_tool"


def test_main_registers_the_tools_on_the_agent_executor_alone(tmp_path) -> None:
    from kenny_server.main import build_app

    app = build_app(db_path=str(tmp_path / "app.sqlite"))
    agent_executor = app.state.agents._executor
    assert AGENT_ONLY_TOOLS <= set(agent_executor.server_tool_handlers)
    assert app.state.agents.evidence_providers() >= EVIDENCE_NAMES
    assistant = app.state.ticket_assistant
    if assistant is not None:
        assert not (set(assistant.executor.server_tool_handlers) & AGENT_ONLY_TOOLS)


# -- joined runs -----------------------------------------------------------------------


def _script() -> list[Any]:
    return [
        _tool("t1", "reliability_suppression_list", {}),
        _tool("t2", "ticket_rule_list", {}),
        _tool("t3", "reliability_suppression_remove", {"rule_id": S_OLD}),
        _tool("t4", "ticket_rule_remove", {"rule_id": T_OLD}),
        _tool("t5", toolloop.AGENT_VERDICT_TOOL, {
            "verdict": "actionable",
            "finding": "Two rules have done nothing for months.",
            "evidence": "reliability_suppression_list, ticket_rule_list",
        }),
        _text("done"),
    ]


async def _run(runner: Any, script: list[Any]):
    from test_chat import FakeAnthropic

    client = FakeAnthropic(script)
    run = await runner.run_generic(
        CONFIG_HYGIENE,
        host_id=None,
        trigger="on_demand",
        brief="Scheduled rule hygiene run on the server.",
        client=client,
        model="test-model",
        executor=None,
    )
    return run, client


async def test_a_shadow_run_recommends_what_it_would_remove(hw: HygieneWorld) -> None:
    await hw.rules()
    runner = hw.runner()
    runner.configure(executor=hw.base.executor)

    run, client = await _run(runner, _script())

    assert run is not None and (run.status, run.mode, run.verdict) == (
        "completed", "shadow", "actionable")
    assert run.host_id is None
    assert run.evidence == {UNUSED_SUPPRESSIONS: [S_OLD], UNUSED_TICKET_RULES: [T_OLD]}
    assert run.recommendations == [
        {"tool": "reliability_suppression_remove", "args": {"rule_id": S_OLD},
         "agent_id": None, "tool_class": NORMAL_CHANGE},
        {"tool": "ticket_rule_remove", "args": {"rule_id": T_OLD},
         "agent_id": None, "tool_class": NORMAL_CHANGE},
    ]
    assert run.actions == []
    assert hw.suppression.get(S_OLD) is not None and hw.ticket_rules.get(T_OLD) is not None
    # The model was told the computed ids; it did not compute them.
    brief = client.messages.calls[0]["messages"][0]["content"]
    assert S_OLD in brief and T_OLD in brief and S_USED not in brief
    # The shadow finding reaches a person, with what the run proposed.
    ticket = await hw.base.ticket_store.find_open_by_dedup_key("agent|config_hygiene|")
    assert ticket is not None and S_OLD in ticket.summary


async def _act(hw: HygieneWorld, runner: Any) -> None:
    live = await runner.live_hash("config_hygiene")
    assert await runner.set_mode("config_hygiene", "act", actor="admin", effective_hash=live) == "act"


async def test_an_authorized_act_run_removes_and_names_the_authorization(hw: HygieneWorld) -> None:
    await hw.rules()
    runner = hw.runner()
    runner.configure(executor=hw.base.executor)
    await _act(hw, runner)
    grant = await runner.grant(
        "config_hygiene",
        tool="reliability_suppression_remove",
        scope="server",
        max_attempts_per_day=5,
        expires_at=NOW + timedelta(days=30),
        actor="admin",
        effective_hash=await runner.live_hash("config_hygiene"),
    )

    run, _ = await _run(runner, _script())

    assert run is not None and run.mode == "act"
    # The authorized removal ran; the unauthorized one stayed a recommendation.
    assert run.actions == [
        {"tool": "reliability_suppression_remove", "args": {"rule_id": S_OLD},
         "agent_id": None, "tool_class": NORMAL_CHANGE, "authorization_id": grant.id,
         "ok": True},
    ]
    assert [r["tool"] for r in run.recommendations] == ["ticket_rule_remove"]
    assert hw.suppression.get(S_OLD) is None
    assert S_OLD not in {r["id"] for r in await hw.supp_store.list()}
    assert hw.ticket_rules.get(T_OLD) is not None
    assert hw.suppression.get(S_USED) is not None
    audit = [e for e in await hw.base.call_log.list() if e["tool"] == "reliability_suppression_remove"]
    assert len(audit) == 1
    assert (audit[0]["ok"], audit[0]["actor"], audit[0]["run_id"], audit[0]["authorization_id"],
            audit[0]["agent_id"], audit[0]["args"]) == (
        True, "agent:config_hygiene", run.id, grant.id, None, {"rule_id": S_OLD})


async def test_act_without_an_authorization_only_recommends(hw: HygieneWorld) -> None:
    await hw.rules()
    runner = hw.runner()
    runner.configure(executor=hw.base.executor)
    await _act(hw, runner)

    run, _ = await _run(runner, _script())

    assert run is not None and run.mode == "act" and run.actions == []
    assert [r["tool"] for r in run.recommendations] == [
        "reliability_suppression_remove", "ticket_rule_remove"]
    assert all("authorization_id" not in r for r in run.recommendations)
    assert hw.suppression.get(S_OLD) is not None and hw.ticket_rules.get(T_OLD) is not None
    assert [e for e in await hw.base.call_log.list() if e["tool"].endswith("_remove")] == []


async def test_a_match_after_run_start_is_refused_in_a_joined_act_run(hw: HygieneWorld) -> None:
    await hw.rules()
    runner = hw.runner()
    runner.configure(executor=hw.base.executor)
    await _act(hw, runner)
    await runner.grant(
        "config_hygiene", tool="reliability_suppression_remove", scope="server",
        max_attempts_per_day=5, expires_at=NOW + timedelta(days=30), actor="admin",
        effective_hash=await runner.live_hash("config_hygiene"),
    )
    # The listing the model makes first reads a fresh snapshot, as a dashboard
    # read or the alert loop would: the rule applies again after the evidence.
    await hw.base.telemetry.insert(
        "pc1", iso(NOW), _snapshot(_event("Microsoft-Windows-CAPI2", 4176, iso(NOW)))
    )
    listed = hw.hygiene.list_suppressions

    async def list_after_a_read(args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        await hw.base.telemetry.latest("pc1")
        return await listed(args, session=session)

    hw.base.executor.register_server_tool("reliability_suppression_list", list_after_a_read)

    run, _ = await _run(runner, _script())

    assert run is not None and run.evidence[UNUSED_SUPPRESSIONS] == [S_OLD]  # type: ignore[index]
    [action] = run.actions
    assert (action["tool"], action["ok"], action["code"]) == (
        "reliability_suppression_remove", False, "in_use")
    assert hw.suppression.get(S_OLD) is not None


# -- the schedule: server-only, about monthly ----------------------------------------


@pytest.fixture
async def sw(tmp_path):
    from test_agent_scheduler import World as SchedulerWorld

    world = SchedulerWorld(str(tmp_path / "hygiene-sched.sqlite"))
    await world.setup()
    yield world
    await world.close()


async def test_the_scheduler_runs_it_once_per_occurrence_on_no_host(sw: Any) -> None:
    from test_agent_scheduler import IN_WINDOW, NIGHT

    sw.configure("config_hygiene", window=NIGHT)
    [outcome] = await sw.scheduler().pass_once()
    assert (outcome.agent_id, outcome.kind, outcome.host_id) == ("config_hygiene", "ran", None)
    [call] = sw.runner.calls
    assert call.host is None and "on the server" in call.brief
    # The same occurrence never runs it twice.
    assert await sw.scheduler().pass_once() == []
    # Nor does the next night's: the interval is 28 days between occurrences.
    sw.now = IN_WINDOW + timedelta(days=1)
    assert await sw.scheduler().pass_once() == []
    sw.now = IN_WINDOW + timedelta(days=27)
    assert await sw.scheduler().pass_once() == []
    sw.now = IN_WINDOW + timedelta(days=28)
    [outcome] = await sw.scheduler().pass_once()
    assert (outcome.kind, outcome.host_id) == ("ran", None)
    assert [c.host for c in sw.runner.calls] == [None, None]


async def test_a_skipped_occurrence_does_not_start_the_interval(sw: Any) -> None:
    from test_agent_scheduler import IN_WINDOW, NIGHT

    sw.configure("config_hygiene", window=NIGHT)
    sw.runner.script["*"] = {"status": "skipped", "error": "over the token cap"}
    [outcome] = await sw.scheduler().pass_once()
    assert outcome.kind == "skipped"
    sw.runner.script.clear()
    sw.now = IN_WINDOW + timedelta(days=1)
    [outcome] = await sw.scheduler().pass_once()
    assert outcome.kind == "ran"

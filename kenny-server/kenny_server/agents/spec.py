"""The declarative shape of one specialized agent (ADR-0071).

An agent is a purpose-bound session kenny starts on its own: a prompt, the
tools it is handed, what starts it, and what bounds it. This module says what a
well-formed agent *is*; it decides nothing about whether one may act. That is
the gate's job (``agents.policy``), for the same reason the tier belongs to the
tool and the gate to the surface (ADR-0045).

Two properties carry the whole safety argument, and both are checked here, at
load time, not trusted at run time:

* **The tool set is explicit and closed.** Every name must be classified in
  :data:`~kenny_server.tool_classes.TOOL_CLASSES` — an unknown name refuses the
  spec rather than being treated as the most consequential tool it could be,
  because a spec is written by a person and a typo there is a bug to surface,
  not a call to gate. Nothing is ever derived from a profile:
  ``profile_allows(None, ...)`` allows everything, and an agent has no account.
* **Privacy-touching tools are opt-in.** A spec that names one of
  :data:`~kenny_server.tool_classes.SENSITIVE_TOOLS` must say so with
  ``sensitive_ok``; an investigation nobody asked for does not read files or
  look at a screen by accident.

:attr:`AgentSpec.spec_hash` fingerprints exactly the fields that change what an
agent *does*. Anything granted to an agent later is bound to that hash, so
editing the prompt or the tool set is never a silent widening of something a
person already agreed to.

Dependency discipline: stdlib plus :mod:`kenny_server.tool_classes` only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..tool_classes import READ_ONLY, SENSITIVE_TOOLS, TOOL_CLASSES

__all__ = [
    "EVENTS",
    "MAX_TIMEOUT_S",
    "MODES",
    "PARAM_KINDS",
    "TRIGGER_KINDS",
    "VERDICT_TOOLS",
    "AgentSpec",
    "ArgConstraint",
    "Budget",
    "SpecError",
    "ToolTimeout",
    "Trigger",
    "effective_hash",
    "evidence_names",
    "param_kind",
    "resolve",
    "validate",
    "with_evidence",
]

#: What an agent may do with a change-tier call.
#:
#: * ``off`` — never started.
#: * ``shadow`` — runs the whole investigation and records what it *would* do;
#:   a change is refused at the gate and kept as a recommendation.
#: * ``act`` — a change runs when the gate allows it.
MODES: tuple[str, ...] = ("off", "shadow", "act")

#: The longest ``timeout_s`` any change-tier call of an agent run may ask for,
#: in seconds. A spec narrows it per tool (:class:`ToolTimeout`); nothing widens
#: it. ``timeout_s`` is the one argument no constraint binds, and unbounded it
#: would let a run park on one call indefinitely.
MAX_TIMEOUT_S = 600

#: What starts a run.
TRIGGER_KINDS: tuple[str, ...] = ("event", "schedule", "on_demand")

#: The server events an ``event`` trigger may name.
EVENTS: tuple[str, ...] = ("ticket_created",)

#: The only tools a spec may name as its verdict tool. The verdict tool is
#: exempt from argument constraints because its own server-side handler decides
#: its effect; a tool whose effect is decided by its arguments (a shell, an
#: install) must never be able to claim that exemption. A literal, so this
#: module stays stdlib-only; ``tests/test_agent_spec.py`` joins it to
#: ``toolloop.TRIAGE_VERDICT_TOOL`` and ``toolloop.AGENT_VERDICT_TOOL``.
VERDICT_TOOLS: frozenset[str] = frozenset({"ticket_triage_verdict", "agent_verdict"})

#: The kind of value a parameter takes, by name. ``window`` is a maintenance
#: window (``{days, start, end[, tz]}``) and ``bool`` a JSON ``true``/``false``;
#: every parameter not named here is a list of strings (an allowlist, a host
#: list). Keyed by name so the same name means the same thing in every agent.
PARAM_KINDS: Mapping[str, str] = {"window": "window", "require_idle": "bool"}

_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class SpecError(ValueError):
    """A spec that must not be loaded."""


@dataclass(frozen=True)
class Trigger:
    """What starts a run of this agent."""

    kind: str
    #: For ``kind="event"``: one of :data:`EVENTS`.
    event: str | None = None
    #: For ``kind="schedule"``: the fewest days between two window occurrences
    #: this agent runs in. ``None`` runs in every occurrence of its window; 28
    #: with a weekly window runs every fourth week.
    min_interval_days: int | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        out: dict[str, str | int | None] = {"kind": self.kind, "event": self.event}
        # Only when set, so every spec that names none keeps the hash it had.
        if self.min_interval_days is not None:
            out["min_interval_days"] = self.min_interval_days
        return out


@dataclass(frozen=True)
class ArgConstraint:
    """One argument of one change-tier tool may only take these exact values.

    Enforced in code by the gate on every call, in every mode — never stated in
    the prompt and left to the model. Matching is exact string equality over an
    enumerated set (no patterns: ADR-0064's lesson that a pattern is a policy
    nobody can read). **An omitted or empty argument never satisfies a
    constraint**: for most tools an absent selector means "everything"
    (``winget_update`` without ``id`` upgrades every package).
    """

    tool: str
    arg: str
    allowed: frozenset[str] = frozenset()
    #: When set, the values are not part of the spec but one of the agent's
    #: parameters (ADR-0072): what an install must be able to set without a code
    #: change, a package allowlist say. The spec declares *which* argument the
    #: parameter feeds; :func:`resolve` fills ``allowed`` from the parameter at
    #: run start. An empty parameter admits nothing — never "everything".
    param: str | None = None
    #: When set, the values are neither the spec's nor a parameter's: server
    #: code computes them at run start from the server's own records, through
    #: the evidence provider of this name (the rules nothing has matched, say),
    #: and the run freezes them (ADR-0072 rule 6). Evidence only ever narrows:
    #: the model chooses among values the server computed, a provider that
    #: fails yields the empty set, and the empty set admits nothing. Exclusive
    #: with ``param`` and with declared ``allowed`` values.
    evidence: str | None = None

    def admits(self, args: dict[str, object]) -> bool:
        value = args.get(self.arg)
        return isinstance(value, str) and value != "" and value in self.allowed

    def to_dict(self) -> dict[str, object]:
        # A parameter-fed constraint is hashed by its declaration only; its
        # values are fingerprinted by :func:`effective_hash`, with the rest of
        # the agent's parameters.
        if self.param is not None:
            return {"tool": self.tool, "arg": self.arg, "param": self.param}
        # Likewise an evidence-fed one: its values differ from run to run by
        # design; what a person reviewed is which records feed it.
        if self.evidence is not None:
            return {"tool": self.tool, "arg": self.arg, "evidence": self.evidence}
        return {"tool": self.tool, "arg": self.arg, "allowed": sorted(self.allowed)}


@dataclass(frozen=True)
class ToolTimeout:
    """The longest ``timeout_s`` this agent's calls of one change-tier tool may ask for.

    A fast tool (a DNS flush) gets seconds, a slow one (a package update)
    minutes; a tool without one is bounded by :data:`MAX_TIMEOUT_S` alone.
    """

    tool: str
    max_s: int

    def to_dict(self) -> dict[str, object]:
        return {"tool": self.tool, "max_s": self.max_s}


@dataclass(frozen=True)
class Budget:
    """What bounds one run, independent of what the model decides to do."""

    #: Model round-trips per run. A run that spends them all ends without a
    #: verdict — the honest outcome, not a disguised "inconclusive".
    max_iterations: int = 8

    def to_dict(self) -> dict[str, int]:
        return {"max_iterations": self.max_iterations}


@dataclass(frozen=True)
class AgentSpec:
    """One specialized agent, as declared in the catalog."""

    id: str
    title: str
    description: str
    prompt: str
    trigger: Trigger
    tools: frozenset[str]
    #: The tool a run ends by calling; its result is the run's verdict. One of
    #: :data:`VERDICT_TOOLS`.
    verdict_tool: str | None = None
    budget: Budget = field(default_factory=Budget)
    #: Argument constraints on change-tier tools; every one naming a tool must
    #: hold for a call to that tool to run. A change-tier tool other than the
    #: verdict tool must carry at least one (see :func:`validate`).
    constraints: tuple[ArgConstraint, ...] = ()
    #: Per-tool ceilings on ``timeout_s``, each at most :data:`MAX_TIMEOUT_S`.
    timeouts: tuple[ToolTimeout, ...] = ()
    #: The parameters this agent takes (ADR-0072): names only; the values are
    #: per install, superuser-edited, and fingerprinted by :func:`effective_hash`.
    #: A ``schedule`` trigger reads its maintenance window from ``window``.
    params: tuple[str, ...] = ()
    #: Must be true for a spec that names any sensitive tool.
    sensitive_ok: bool = False
    #: The mode a fresh install starts in: ``shadow`` or ``off``. Never ``act``
    #: — moving an agent to ``act`` is a superuser's decision, not a default.
    default_mode: str = "shadow"
    #: Bumped by hand when behaviour changes in a way the hash cannot see
    #: (e.g. server-side code the agent's verdict tool runs).
    version: int = 1

    @property
    def spec_hash(self) -> str:
        """Fingerprint of everything that changes what this agent does.

        Title, description and the default mode are presentation and install
        defaults; they are deliberately outside the hash, so rewording a
        description does not unbind anything.
        """

        canonical = json.dumps(
            {
                "id": self.id,
                "version": self.version,
                "prompt": self.prompt,
                "trigger": self.trigger.to_dict(),
                "tools": sorted(self.tools),
                # A re-tier must unbind what was granted against the old tier:
                # a tool moved down to ``standard_change`` would otherwise run
                # without the authorization it needed (ADR-0072).
                "tiers": {t: TOOL_CLASSES.get(t) for t in sorted(self.tools)},
                "verdict_tool": self.verdict_tool,
                "budget": self.budget.to_dict(),
                "constraints": self._sorted_constraints(),
                "timeouts": self._sorted_timeouts(),
                "params": sorted(self.params),
                "sensitive_ok": self.sensitive_ok,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _sorted_constraints(self) -> list[dict[str, object]]:
        return sorted(
            (c.to_dict() for c in self.constraints),
            key=lambda d: (str(d["tool"]), str(d["arg"])),
        )

    def _sorted_timeouts(self) -> list[dict[str, object]]:
        return sorted((t.to_dict() for t in self.timeouts), key=lambda d: str(d["tool"]))

    def constraints_for(self, tool: str) -> tuple[ArgConstraint, ...]:
        return tuple(c for c in self.constraints if c.tool == tool)

    def timeout_for(self, tool: str) -> int:
        """The longest ``timeout_s`` a call of ``tool`` may carry in this agent."""

        for t in self.timeouts:
            if t.tool == tool:
                return t.max_s
        return MAX_TIMEOUT_S

    def to_public(self) -> dict[str, object]:
        """The catalog entry as the dashboard and the API show it."""

        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "trigger": self.trigger.to_dict(),
            "tools": sorted(self.tools),
            "tool_classes": {t: TOOL_CLASSES[t] for t in sorted(self.tools)},
            "verdict_tool": self.verdict_tool,
            "budget": self.budget.to_dict(),
            "constraints": self._sorted_constraints(),
            "timeouts": self._sorted_timeouts(),
            "params": sorted(self.params),
            "sensitive_ok": self.sensitive_ok,
            "default_mode": self.default_mode,
            "version": self.version,
            "spec_hash": self.spec_hash,
        }


def validate(spec: AgentSpec) -> AgentSpec:
    """Return ``spec`` unchanged, or raise :class:`SpecError` saying why not."""

    if not _ID_RE.match(spec.id):
        raise SpecError(f"agent id {spec.id!r} must match {_ID_RE.pattern}")
    if not spec.prompt.strip():
        raise SpecError(f"agent {spec.id}: prompt is empty")
    if not spec.tools:
        raise SpecError(f"agent {spec.id}: tool set is empty")
    unknown = sorted(t for t in spec.tools if t not in TOOL_CLASSES)
    if unknown:
        raise SpecError(f"agent {spec.id}: unclassified tool(s) {', '.join(unknown)}")
    sensitive = sorted(spec.tools & SENSITIVE_TOOLS)
    if sensitive and not spec.sensitive_ok:
        raise SpecError(
            f"agent {spec.id}: names sensitive tool(s) {', '.join(sensitive)} "
            "without sensitive_ok"
        )
    if spec.verdict_tool is not None and spec.verdict_tool not in VERDICT_TOOLS:
        raise SpecError(
            f"agent {spec.id}: {spec.verdict_tool} is not a verdict tool "
            f"(one of {', '.join(sorted(VERDICT_TOOLS))})"
        )
    if spec.verdict_tool is not None and spec.verdict_tool not in spec.tools:
        raise SpecError(f"agent {spec.id}: verdict tool {spec.verdict_tool} is not in its tools")
    seen: set[tuple[str, str]] = set()
    for c in spec.constraints:
        if c.tool not in spec.tools:
            raise SpecError(f"agent {spec.id}: constraint names {c.tool}, which is not in its tools")
        if TOOL_CLASSES[c.tool] == READ_ONLY:
            raise SpecError(f"agent {spec.id}: constraint on read-only {c.tool} would bound nothing")
        if c.param is not None and c.evidence is not None:
            raise SpecError(
                f"agent {spec.id}: constraint on {c.tool} names both a param and evidence"
            )
        if c.evidence is not None:
            if not c.arg or not _PARAM_RE.match(c.evidence):
                raise SpecError(
                    f"agent {spec.id}: evidence constraint on {c.tool} needs an arg and "
                    f"an evidence name matching {_PARAM_RE.pattern}"
                )
            # A run's resolved spec carries the frozen evidence as values and is
            # validated again by the gate; they must be non-empty strings.
            # Declaring values for one is refused by ``catalog.check_dispatchable``.
            if any(not isinstance(v, str) or not v for v in c.allowed):
                raise SpecError(f"agent {spec.id}: evidence values for {c.tool} must be non-empty")
        elif c.param is not None:
            if c.param not in spec.params:
                raise SpecError(
                    f"agent {spec.id}: constraint on {c.tool} names undeclared param {c.param!r}"
                )
            if any(not isinstance(v, str) or not v for v in c.allowed):
                raise SpecError(f"agent {spec.id}: param values for {c.tool} must be non-empty")
        elif not c.arg or not c.allowed or any(not v for v in c.allowed):
            raise SpecError(f"agent {spec.id}: constraint on {c.tool} needs an arg and non-empty values")
        if (c.tool, c.arg) in seen:
            raise SpecError(f"agent {spec.id}: two constraints on {c.tool}.{c.arg}")
        seen.add((c.tool, c.arg))
    timed: set[str] = set()
    for t in spec.timeouts:
        if t.tool not in spec.tools:
            raise SpecError(f"agent {spec.id}: timeout names {t.tool}, which is not in its tools")
        if TOOL_CLASSES[t.tool] == READ_ONLY:
            raise SpecError(f"agent {spec.id}: timeout on read-only {t.tool} would bound nothing")
        if isinstance(t.max_s, bool) or not isinstance(t.max_s, int):
            raise SpecError(f"agent {spec.id}: timeout on {t.tool} must be whole seconds")
        if not 1 <= t.max_s <= MAX_TIMEOUT_S:
            raise SpecError(
                f"agent {spec.id}: timeout on {t.tool} must be 1 to {MAX_TIMEOUT_S} seconds"
            )
        if t.tool in timed:
            raise SpecError(f"agent {spec.id}: two timeouts on {t.tool}")
        timed.add(t.tool)
    # A change the agent may make with any arguments at all is the tier acting
    # as the permission, which ADR-0045 forbids. The verdict tool is exempt: its
    # effect is decided server-side by its own handler, not by its arguments.
    unbounded = sorted(
        t
        for t in spec.tools
        if TOOL_CLASSES[t] != READ_ONLY and t != spec.verdict_tool and not spec.constraints_for(t)
    )
    if unbounded:
        raise SpecError(
            f"agent {spec.id}: change-tier tool(s) {', '.join(unbounded)} carry no argument constraint"
        )
    for name in spec.params:
        if not _PARAM_RE.match(name):
            raise SpecError(f"agent {spec.id}: param name {name!r} must match {_PARAM_RE.pattern}")
    if len(set(spec.params)) != len(spec.params):
        raise SpecError(f"agent {spec.id}: a param is declared twice")
    if spec.trigger.kind == "schedule" and "window" not in spec.params:
        raise SpecError(f"agent {spec.id}: a schedule trigger needs a 'window' param")
    if spec.trigger.kind not in TRIGGER_KINDS:
        raise SpecError(f"agent {spec.id}: unknown trigger kind {spec.trigger.kind!r}")
    if spec.trigger.kind == "event" and spec.trigger.event not in EVENTS:
        raise SpecError(f"agent {spec.id}: unknown event {spec.trigger.event!r}")
    if spec.trigger.kind != "event" and spec.trigger.event is not None:
        raise SpecError(f"agent {spec.id}: only an event trigger names an event")
    interval = spec.trigger.min_interval_days
    if interval is not None:
        if spec.trigger.kind != "schedule":
            raise SpecError(f"agent {spec.id}: only a schedule trigger has a minimum interval")
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 366:
            raise SpecError(f"agent {spec.id}: min_interval_days must be 1 to 366 whole days")
    if spec.default_mode not in MODES:
        raise SpecError(f"agent {spec.id}: unknown default mode {spec.default_mode!r}")
    if spec.default_mode == "act":
        # A spec that starts in ``act`` would put a fresh install's agent to
        # work without anybody having chosen that; ``act`` is set, never shipped.
        raise SpecError(f"agent {spec.id}: default mode act; moving to act is a superuser's choice")
    if spec.budget.max_iterations < 1:
        raise SpecError(f"agent {spec.id}: max_iterations must be at least 1")
    if spec.version < 1:
        raise SpecError(f"agent {spec.id}: version must be at least 1")
    return spec


def param_kind(name: str) -> str:
    """``window``, ``bool`` or ``list``: what parameter ``name`` holds (:data:`PARAM_KINDS`)."""

    return PARAM_KINDS.get(name, "list")


def _canonical_params(spec: AgentSpec, params: Mapping[str, Any]) -> dict[str, Any]:
    """The declared parameters only, in a form whose JSON is stable."""

    out: dict[str, Any] = {}
    for name in sorted(spec.params):
        value = params.get(name)
        if isinstance(value, (list, tuple, set, frozenset)):
            value = sorted(str(v) for v in value)
        out[name] = value
    return out


def effective_hash(spec: AgentSpec, params: Mapping[str, Any]) -> str:
    """What an agent *is* on this install: its spec and its parameter values.

    ``act`` and every standing authorization bind to this, not to the spec hash
    alone (ADR-0072): widening a package allowlist changes what the agent may
    do exactly as editing its spec would, and must unbind what was granted.
    Undeclared keys are ignored, so stray settings cannot move the hash.
    """

    canonical = json.dumps(
        {"spec": spec.spec_hash, "params": _canonical_params(spec, params)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve(spec: AgentSpec, params: Mapping[str, Any]) -> AgentSpec:
    """``spec`` with every parameter-fed constraint filled from ``params``.

    Called at run start; the result is what the run's gate enforces, frozen for
    the run. A missing or empty parameter yields an empty set, which admits
    nothing. Non-string and empty values are dropped rather than coerced. The
    spec hash is unchanged (parameter values are not part of it).
    """

    resolved: list[ArgConstraint] = []
    for c in spec.constraints:
        if c.param is None or c.evidence is not None:
            resolved.append(c)
            continue
        raw = params.get(c.param) or ()
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else ()
        resolved.append(
            replace(c, allowed=frozenset(v for v in values if isinstance(v, str) and v))
        )
    return replace(spec, constraints=tuple(resolved))


def evidence_names(spec: AgentSpec) -> tuple[str, ...]:
    """The evidence providers ``spec``'s constraints are fed by, sorted, once each."""

    return tuple(sorted({c.evidence for c in spec.constraints if c.evidence is not None}))


def with_evidence(spec: AgentSpec, evidence: Mapping[str, Any]) -> AgentSpec:
    """``spec`` with every evidence-fed constraint filled from ``evidence``.

    ``evidence`` maps a provider name to the values it computed at run start
    (ADR-0072 rule 6). A name it lacks, or a value that is not a collection of
    strings, yields the empty set, which admits nothing; non-string and empty
    values are dropped. Every other constraint is left as it is, and the spec
    hash is unchanged (an evidence-fed constraint is hashed by its declaration).
    """

    resolved: list[ArgConstraint] = []
    for c in spec.constraints:
        if c.evidence is None:
            resolved.append(c)
            continue
        raw = evidence.get(c.evidence) or ()
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else ()
        resolved.append(
            replace(c, allowed=frozenset(v for v in values if isinstance(v, str) and v))
        )
    return replace(spec, constraints=tuple(resolved))


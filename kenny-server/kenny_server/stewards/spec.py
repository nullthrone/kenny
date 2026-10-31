"""The declarative shape of one steward (ADR-0071).

A steward is a purpose-bound session kenny starts on its own: a prompt, the
tools it is handed, what starts it, and what bounds it. This module says what a
well-formed steward *is*; it decides nothing about whether one may act. That is
the gate's job (``stewards.policy``), for the same reason the tier belongs to the
tool and the gate to the surface (ADR-0045).

Two properties carry the whole safety argument, and both are checked here, at
load time, not trusted at run time:

* **The tool set is explicit and closed.** Every name must be classified in
  :data:`~kenny_server.tool_classes.TOOL_CLASSES` — an unknown name refuses the
  spec rather than being treated as the most consequential tool it could be,
  because a spec is written by a person and a typo there is a bug to surface,
  not a call to gate. Nothing is ever derived from a profile:
  ``profile_allows(None, ...)`` allows everything, and a steward has no account.
* **Privacy-touching tools are opt-in.** A spec that names one of
  :data:`~kenny_server.tool_classes.SENSITIVE_TOOLS` must say so with
  ``sensitive_ok``; an investigation nobody asked for does not read files or
  look at a screen by accident.

:attr:`StewardSpec.spec_hash` fingerprints exactly the fields that change what an
steward *does*. Anything granted to a steward later is bound to that hash, so
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
    "ALERT_EVENT_TYPES",
    "DEFAULT_COOLDOWN_HOURS",
    "EVENTS",
    "EVENT_RE",
    "MAX_TIMEOUT_S",
    "MODES",
    "PARAM_KINDS",
    "TICKET_CREATED",
    "TRIGGER_KINDS",
    "VERDICT_TOOLS",
    "StewardSpec",
    "ArgConstraint",
    "Budget",
    "SpecError",
    "ToolTimeout",
    "Trigger",
    "TriggerSet",
    "allowed_triggers",
    "baseline_config",
    "effective_hash",
    "effective_triggers",
    "evidence_names",
    "is_event_name",
    "param_kind",
    "resolve",
    "runs_on_ticket",
    "validate",
    "validate_triggers",
    "with_evidence",
]

#: What a steward may do with a change-tier call.
#:
#: * ``off`` — never started.
#: * ``shadow`` — runs the whole investigation and records what it *would* do;
#:   a change is refused at the gate and kept as a recommendation.
#: * ``act`` — a change runs when the gate allows it.
MODES: tuple[str, ...] = ("off", "shadow", "act")

#: The longest ``timeout_s`` any change-tier call of a steward run may ask for,
#: in seconds. A spec narrows it per tool (:class:`ToolTimeout`); nothing widens
#: it. ``timeout_s`` is the one argument no constraint binds, and unbounded it
#: would let a run park on one call indefinitely.
MAX_TIMEOUT_S = 600

#: What starts a run of a steward by default: its *baseline* trigger
#: (:attr:`StewardSpec.trigger`). Whether a person may also start one by
#: command is part of its trigger configuration (:class:`TriggerSet`), not a
#: kind of its own.
TRIGGER_KINDS: tuple[str, ...] = ("event", "schedule")

#: The event a newly created ticket raises. Only the steward that runs on a
#: ticket's own surface may name it (``catalog.check_dispatchable``).
TICKET_CREATED = "ticket_created"

#: The alert types whose firing an ``alert:<type>`` event names. Health alerts
#: are named per section instead (``alert:health:<section>``).
#: ``tests/test_steward_triggers.py`` joins these to ``ticket_rules.EVENT_TYPES``.
ALERT_EVENT_TYPES: tuple[str, ...] = ("offline", "disk_forecast", "hardware_forecast", "change")

#: Every event name a trigger may carry, and nothing else: a closed pattern, so
#: a typo is refused rather than waiting forever for an event nobody raises.
EVENT_RE = re.compile(
    r"^(" + TICKET_CREATED + r"|alert:(" + "|".join(ALERT_EVENT_TYPES) + r")"
    r"|alert:health:[a-z][a-z0-9_]{1,40})$"
)

#: The event names :data:`EVENT_RE` admits that are not per health section.
EVENTS: tuple[str, ...] = (TICKET_CREATED, *(f"alert:{t}" for t in ALERT_EVENT_TYPES))

#: The fewest hours between two event-started runs of one steward on one host,
#: when its configuration names none; and the bounds a configuration may set.
DEFAULT_COOLDOWN_HOURS = 24
_COOLDOWN_HOURS = (1, 168)

#: Bounds of a schedule's ``min_interval_days``.
_INTERVAL_DAYS = (1, 366)

#: Most event entries a trigger configuration may hold.
_MAX_EVENTS = 64

#: The only tools a spec may name as its verdict tool. The verdict tool is
#: exempt from argument constraints because its own server-side handler decides
#: its effect; a tool whose effect is decided by its arguments (a shell, an
#: install) must never be able to claim that exemption. A literal, so this
#: module stays stdlib-only; ``tests/test_steward_spec.py`` joins it to
#: ``toolloop.TRIAGE_VERDICT_TOOL`` and ``toolloop.STEWARD_VERDICT_TOOL``.
VERDICT_TOOLS: frozenset[str] = frozenset({"ticket_triage_verdict", "steward_verdict"})

#: The kind of value a parameter takes, by name. ``window`` is a maintenance
#: window (``{days, start, end[, tz]}``) and ``bool`` a JSON ``true``/``false``;
#: every parameter not named here is a list of strings (an allowlist, a host
#: list). Keyed by name so the same name means the same thing in every steward.
#: ``percent`` is a whole number from 1 to 99 (a disk-usage threshold).
PARAM_KINDS: Mapping[str, str] = {
    "window": "window",
    "require_idle": "bool",
    "min_percent_used": "percent",
}

_PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class SpecError(ValueError):
    """A spec that must not be loaded."""


@dataclass(frozen=True)
class Trigger:
    """What starts a run of this steward."""

    kind: str
    #: For ``kind="event"``: a name :data:`EVENT_RE` admits.
    event: str | None = None
    #: For ``kind="schedule"``: the fewest days between two window occurrences
    #: this steward runs in. ``None`` runs in every occurrence of its window; 28
    #: with a weekly window runs every fourth week.
    min_interval_days: int | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        out: dict[str, str | int | None] = {"kind": self.kind, "event": self.event}
        # Only when set, so every spec that names none keeps the hash it had.
        if self.min_interval_days is not None:
            out["min_interval_days"] = self.min_interval_days
        return out


@dataclass(frozen=True)
class TriggerSet:
    """The ceiling on what may start a steward: what its configuration may switch on.

    The spec declares it; a superuser's trigger configuration
    (:func:`validate_triggers`) chooses within it, and a release that narrows it
    narrows every configuration with it (:func:`effective_triggers`). Not part
    of :attr:`StewardSpec.spec_hash`: what a steward *does* when it runs is the
    same whatever started it, and what is switched on is fingerprinted by
    :func:`effective_hash` with the rest of the install's configuration.
    """

    #: The steward may run in its maintenance window (needs a ``window`` param).
    schedule: bool = False
    #: The events (:data:`EVENT_RE`) that may start it.
    events: frozenset[str] = frozenset()
    #: A person may start a run of it by command.
    command: bool = False
    #: Whether its configuration may differ from the baseline at all.
    editable: bool = True

    def to_public(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule,
            "events": sorted(self.events),
            "command": self.command,
            "editable": self.editable,
        }


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
    #: When set, the values are not part of the spec but one of the steward's
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
        # the steward's parameters.
        if self.param is not None:
            return {"tool": self.tool, "arg": self.arg, "param": self.param}
        # Likewise an evidence-fed one: its values differ from run to run by
        # design; what a person reviewed is which records feed it.
        if self.evidence is not None:
            return {"tool": self.tool, "arg": self.arg, "evidence": self.evidence}
        return {"tool": self.tool, "arg": self.arg, "allowed": sorted(self.allowed)}


@dataclass(frozen=True)
class ToolTimeout:
    """The longest ``timeout_s`` this steward's calls of one change-tier tool may ask for.

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
class StewardSpec:
    """One steward, as declared in the catalog."""

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
    #: The parameters this steward takes (ADR-0072): names only; the values are
    #: per install, superuser-edited, and fingerprinted by :func:`effective_hash`.
    #: A ``schedule`` trigger reads its maintenance window from ``window``.
    params: tuple[str, ...] = ()
    #: Must be true for a spec that names any sensitive tool.
    sensitive_ok: bool = False
    #: The mode a fresh install starts in: ``shadow`` or ``off``. Never ``act``
    #: — moving a steward to ``act`` is a superuser's decision, not a default.
    default_mode: str = "shadow"
    #: Bumped by hand when behaviour changes in a way the hash cannot see
    #: (e.g. server-side code the steward's verdict tool runs).
    version: int = 1
    #: What may start this steward beyond its baseline :attr:`trigger`
    #: (:class:`TriggerSet`); ``None`` allows the baseline and nothing else.
    #: Outside the spec hash (see :class:`TriggerSet`).
    triggers_allowed: TriggerSet | None = None
    #: Named server-side checks that must pass before a run starts on a host
    #: (the runner's precondition registry); a failing one skips the run
    #: before any model is called. In the spec hash when non-empty.
    preconditions: tuple[str, ...] = ()

    @property
    def spec_hash(self) -> str:
        """Fingerprint of everything that changes what this steward does.

        Title, description and the default mode are presentation and install
        defaults; they are deliberately outside the hash, so rewording a
        description does not unbind anything.
        """

        fields: dict[str, Any] = {
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
        }
        # Only when declared, so every spec that declares none keeps its hash.
        if self.preconditions:
            fields["preconditions"] = list(self.preconditions)
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
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
        """The longest ``timeout_s`` a call of ``tool`` may carry in this steward."""

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


def validate(spec: StewardSpec) -> StewardSpec:
    """Return ``spec`` unchanged, or raise :class:`SpecError` saying why not."""

    if not _ID_RE.match(spec.id):
        raise SpecError(f"steward id {spec.id!r} must match {_ID_RE.pattern}")
    if not spec.prompt.strip():
        raise SpecError(f"steward {spec.id}: prompt is empty")
    if not spec.tools:
        raise SpecError(f"steward {spec.id}: tool set is empty")
    unknown = sorted(t for t in spec.tools if t not in TOOL_CLASSES)
    if unknown:
        raise SpecError(f"steward {spec.id}: unclassified tool(s) {', '.join(unknown)}")
    sensitive = sorted(spec.tools & SENSITIVE_TOOLS)
    if sensitive and not spec.sensitive_ok:
        raise SpecError(
            f"steward {spec.id}: names sensitive tool(s) {', '.join(sensitive)} "
            "without sensitive_ok"
        )
    if spec.verdict_tool is not None and spec.verdict_tool not in VERDICT_TOOLS:
        raise SpecError(
            f"steward {spec.id}: {spec.verdict_tool} is not a verdict tool "
            f"(one of {', '.join(sorted(VERDICT_TOOLS))})"
        )
    if spec.verdict_tool is not None and spec.verdict_tool not in spec.tools:
        raise SpecError(f"steward {spec.id}: verdict tool {spec.verdict_tool} is not in its tools")
    seen: set[tuple[str, str]] = set()
    for c in spec.constraints:
        if c.tool not in spec.tools:
            raise SpecError(f"steward {spec.id}: constraint names {c.tool}, which is not in its tools")
        if TOOL_CLASSES[c.tool] == READ_ONLY:
            raise SpecError(f"steward {spec.id}: constraint on read-only {c.tool} would bound nothing")
        if c.param is not None and c.evidence is not None:
            raise SpecError(
                f"steward {spec.id}: constraint on {c.tool} names both a param and evidence"
            )
        if c.evidence is not None:
            if not c.arg or not _PARAM_RE.match(c.evidence):
                raise SpecError(
                    f"steward {spec.id}: evidence constraint on {c.tool} needs an arg and "
                    f"an evidence name matching {_PARAM_RE.pattern}"
                )
            # A run's resolved spec carries the frozen evidence as values and is
            # validated again by the gate; they must be non-empty strings.
            # Declaring values for one is refused by ``catalog.check_dispatchable``.
            if any(not isinstance(v, str) or not v for v in c.allowed):
                raise SpecError(f"steward {spec.id}: evidence values for {c.tool} must be non-empty")
        elif c.param is not None:
            if c.param not in spec.params:
                raise SpecError(
                    f"steward {spec.id}: constraint on {c.tool} names undeclared param {c.param!r}"
                )
            if any(not isinstance(v, str) or not v for v in c.allowed):
                raise SpecError(f"steward {spec.id}: param values for {c.tool} must be non-empty")
        elif not c.arg or not c.allowed or any(not v for v in c.allowed):
            raise SpecError(f"steward {spec.id}: constraint on {c.tool} needs an arg and non-empty values")
        if (c.tool, c.arg) in seen:
            raise SpecError(f"steward {spec.id}: two constraints on {c.tool}.{c.arg}")
        seen.add((c.tool, c.arg))
    timed: set[str] = set()
    for t in spec.timeouts:
        if t.tool not in spec.tools:
            raise SpecError(f"steward {spec.id}: timeout names {t.tool}, which is not in its tools")
        if TOOL_CLASSES[t.tool] == READ_ONLY:
            raise SpecError(f"steward {spec.id}: timeout on read-only {t.tool} would bound nothing")
        if isinstance(t.max_s, bool) or not isinstance(t.max_s, int):
            raise SpecError(f"steward {spec.id}: timeout on {t.tool} must be whole seconds")
        if not 1 <= t.max_s <= MAX_TIMEOUT_S:
            raise SpecError(
                f"steward {spec.id}: timeout on {t.tool} must be 1 to {MAX_TIMEOUT_S} seconds"
            )
        if t.tool in timed:
            raise SpecError(f"steward {spec.id}: two timeouts on {t.tool}")
        timed.add(t.tool)
    # A change the steward may make with any arguments at all is the tier acting
    # as the permission, which ADR-0045 forbids. The verdict tool is exempt: its
    # effect is decided server-side by its own handler, not by its arguments.
    unbounded = sorted(
        t
        for t in spec.tools
        if TOOL_CLASSES[t] != READ_ONLY and t != spec.verdict_tool and not spec.constraints_for(t)
    )
    if unbounded:
        raise SpecError(
            f"steward {spec.id}: change-tier tool(s) {', '.join(unbounded)} carry no argument constraint"
        )
    for name in spec.params:
        if not _PARAM_RE.match(name):
            raise SpecError(f"steward {spec.id}: param name {name!r} must match {_PARAM_RE.pattern}")
    if len(set(spec.params)) != len(spec.params):
        raise SpecError(f"steward {spec.id}: a param is declared twice")
    if spec.trigger.kind == "schedule" and "window" not in spec.params:
        raise SpecError(f"steward {spec.id}: a schedule trigger needs a 'window' param")
    if spec.trigger.kind not in TRIGGER_KINDS:
        raise SpecError(f"steward {spec.id}: unknown trigger kind {spec.trigger.kind!r}")
    if spec.trigger.kind == "event" and not is_event_name(spec.trigger.event):
        raise SpecError(f"steward {spec.id}: unknown event {spec.trigger.event!r}")
    if spec.trigger.kind != "event" and spec.trigger.event is not None:
        raise SpecError(f"steward {spec.id}: only an event trigger names an event")
    interval = spec.trigger.min_interval_days
    if interval is not None:
        if spec.trigger.kind != "schedule":
            raise SpecError(f"steward {spec.id}: only a schedule trigger has a minimum interval")
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 366:
            raise SpecError(f"steward {spec.id}: min_interval_days must be 1 to 366 whole days")
    if spec.default_mode not in MODES:
        raise SpecError(f"steward {spec.id}: unknown default mode {spec.default_mode!r}")
    if spec.default_mode == "act":
        # A spec that starts in ``act`` would put a fresh install's steward to
        # work without anybody having chosen that; ``act`` is set, never shipped.
        raise SpecError(f"steward {spec.id}: default mode act; moving to act is a superuser's choice")
    if spec.budget.max_iterations < 1:
        raise SpecError(f"steward {spec.id}: max_iterations must be at least 1")
    if spec.version < 1:
        raise SpecError(f"steward {spec.id}: version must be at least 1")
    _validate_triggers_allowed(spec)
    for name in spec.preconditions:
        if not isinstance(name, str) or not _PARAM_RE.match(name):
            raise SpecError(
                f"steward {spec.id}: precondition name {name!r} must match {_PARAM_RE.pattern}"
            )
    if len(set(spec.preconditions)) != len(spec.preconditions):
        raise SpecError(f"steward {spec.id}: a precondition is declared twice")
    return spec


def _validate_triggers_allowed(spec: StewardSpec) -> None:
    """Refuse a trigger ceiling that is malformed or does not hold the baseline."""

    allowed = spec.triggers_allowed
    if allowed is None:
        return
    if not isinstance(allowed, TriggerSet):
        raise SpecError(f"steward {spec.id}: triggers_allowed must be a TriggerSet")
    for name in ("schedule", "command", "editable"):
        if not isinstance(getattr(allowed, name), bool):
            raise SpecError(f"steward {spec.id}: triggers_allowed.{name} must be true or false")
    if not isinstance(allowed.events, frozenset):
        raise SpecError(f"steward {spec.id}: triggers_allowed.events must be a frozenset")
    unknown = sorted(str(e) for e in allowed.events if not is_event_name(e))
    if unknown:
        raise SpecError(f"steward {spec.id}: triggers_allowed names unknown event(s) {', '.join(unknown)}")
    if allowed.schedule and "window" not in spec.params:
        raise SpecError(f"steward {spec.id}: a schedule needs a 'window' param")
    # The baseline is what an install runs with until somebody configures
    # otherwise; a ceiling below it would narrow it at once and move the hash
    # of every install that never touched the configuration.
    if spec.trigger.kind == "schedule" and not allowed.schedule:
        raise SpecError(f"steward {spec.id}: triggers_allowed does not allow its baseline schedule")
    if spec.trigger.kind == "event" and spec.trigger.event not in allowed.events:
        raise SpecError(
            f"steward {spec.id}: triggers_allowed does not allow its baseline event "
            f"{spec.trigger.event!r}"
        )


def param_kind(name: str) -> str:
    """``window``, ``bool``, ``percent`` or ``list``: what parameter ``name`` holds (:data:`PARAM_KINDS`)."""

    return PARAM_KINDS.get(name, "list")


def _canonical_params(spec: StewardSpec, params: Mapping[str, Any]) -> dict[str, Any]:
    """The declared parameters only, in a form whose JSON is stable."""

    out: dict[str, Any] = {}
    for name in sorted(spec.params):
        value = params.get(name)
        if isinstance(value, (list, tuple, set, frozenset)):
            value = sorted(str(v) for v in value)
        out[name] = value
    return out


def effective_hash(
    spec: StewardSpec, params: Mapping[str, Any], triggers: Any = None
) -> str:
    """What a steward *is* on this install: its spec, parameters and triggers.

    ``act`` and every standing authorization bind to this, not to the spec hash
    alone (ADR-0072): widening a package allowlist changes what the steward may
    do exactly as editing its spec would, and must unbind what was granted.
    Undeclared keys are ignored, so stray settings cannot move the hash.

    ``triggers`` is the stored trigger configuration (``None`` while none was
    ever stored). What is hashed is its :func:`effective_triggers` — the
    configuration within the spec's ceiling — and only when that differs from
    :func:`baseline_config`: a steward whose triggers are at their baseline
    hashes exactly as it did before triggers were configurable, so switching a
    trigger on moves the hash and switching it back restores it.
    """

    fields: dict[str, Any] = {"spec": spec.spec_hash, "params": _canonical_params(spec, params)}
    effective = effective_triggers(spec, triggers)
    if effective != baseline_config(spec):
        fields["triggers"] = effective
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- triggers ------------------------------------------------------------------


def is_event_name(name: Any) -> bool:
    """Whether ``name`` is an event a trigger may carry (:data:`EVENT_RE`)."""

    # ``fullmatch``: ``$`` alone would admit a trailing newline.
    return isinstance(name, str) and EVENT_RE.fullmatch(name) is not None


def allowed_triggers(spec: StewardSpec) -> TriggerSet:
    """``spec``'s trigger ceiling; for a spec that declares none, its baseline alone."""

    if spec.triggers_allowed is not None:
        return spec.triggers_allowed
    if spec.trigger.kind == "schedule":
        return TriggerSet(schedule=True)
    if spec.trigger.kind == "event" and spec.trigger.event is not None:
        return TriggerSet(events=frozenset({spec.trigger.event}))
    return TriggerSet()


def runs_on_ticket(spec: StewardSpec) -> bool:
    """Whether ``spec`` runs on a ticket's own surface: a new ticket starts it.

    Such a steward is started by ``StewardRunner.on_ticket_created`` alone and
    keeps the ticket as its surface; it is never run, or previewed, elsewhere.
    """

    if spec.trigger.kind == "event" and spec.trigger.event == TICKET_CREATED:
        return True
    return TICKET_CREATED in allowed_triggers(spec).events


def baseline_config(spec: StewardSpec) -> dict[str, Any]:
    """The trigger configuration ``spec`` runs with while nobody has configured one.

    The same canonical form :func:`validate_triggers` returns, so the two
    compare equal exactly when they mean the same thing.
    """

    if spec.trigger.kind == "schedule":
        return {
            "schedule": {"min_interval_days": spec.trigger.min_interval_days},
            "events": [],
            "command": False,
        }
    events = (
        [{"name": spec.trigger.event, "cooldown_hours": DEFAULT_COOLDOWN_HOURS}]
        if spec.trigger.kind == "event" and spec.trigger.event is not None
        else []
    )
    return {"schedule": None, "events": events, "command": False}


def _nothing() -> dict[str, Any]:
    """A configuration that switches nothing on: what an unreadable stored one stands for.

    Failing closed here means fewer runs, never more.
    """

    return {"schedule": None, "events": [], "command": False}


def _whole(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _canonical_triggers(raw: Any) -> dict[str, Any]:
    """``raw`` read into the canonical form, structure only; else :class:`ValueError`.

    Says nothing about what a steward allows; :func:`validate_triggers` does.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("triggers must be an object with schedule, events and command")
    keys = {"schedule", "events", "command"}
    unknown = sorted(str(k) for k in raw if k not in keys)
    if unknown:
        raise ValueError(f"triggers take no field(s) {', '.join(unknown)}")
    missing = sorted(k for k in keys if k not in raw)
    if missing:
        raise ValueError(f"triggers need {', '.join(missing)}")

    schedule_raw = raw["schedule"]
    schedule: dict[str, Any] | None
    if schedule_raw is None:
        schedule = None
    elif isinstance(schedule_raw, Mapping):
        extra = sorted(str(k) for k in schedule_raw if k != "min_interval_days")
        if extra:
            raise ValueError(f"schedule takes no field(s) {', '.join(extra)}")
        interval = schedule_raw.get("min_interval_days")
        if interval is not None and not _whole(interval, *_INTERVAL_DAYS):
            raise ValueError(
                f"schedule.min_interval_days must be null or {_INTERVAL_DAYS[0]} to "
                f"{_INTERVAL_DAYS[1]} whole days"
            )
        schedule = {"min_interval_days": interval}
    else:
        raise ValueError("schedule must be null or an object")

    events_raw = raw["events"]
    if not isinstance(events_raw, (list, tuple)):
        raise ValueError("events must be a list")
    if len(events_raw) > _MAX_EVENTS:
        raise ValueError(f"events take at most {_MAX_EVENTS} entries")
    events: dict[str, dict[str, Any]] = {}
    for entry in events_raw:
        if not isinstance(entry, Mapping):
            raise ValueError("every event must be an object with a name")
        extra = sorted(str(k) for k in entry if k not in ("name", "cooldown_hours"))
        if extra:
            raise ValueError(f"an event takes no field(s) {', '.join(extra)}")
        name = entry.get("name")
        if not is_event_name(name):
            raise ValueError(f"unknown event {name!r}")
        cooldown = entry.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)
        if not _whole(cooldown, *_COOLDOWN_HOURS):
            raise ValueError(
                f"{name}: cooldown_hours must be {_COOLDOWN_HOURS[0]} to "
                f"{_COOLDOWN_HOURS[1]} whole hours"
            )
        if name in events:
            raise ValueError(f"event {name} is named twice")
        events[name] = {"name": name, "cooldown_hours": cooldown}

    command = raw["command"]
    if not isinstance(command, bool):
        raise ValueError("command must be true or false")
    return {
        "schedule": schedule,
        "events": [events[name] for name in sorted(events)],
        "command": command,
    }


def validate_triggers(spec: StewardSpec, raw: Any) -> dict[str, Any]:
    """``raw`` checked against what ``spec`` allows, in canonical form; else :class:`ValueError`.

    The canonical form: ``schedule`` is ``null`` or ``{min_interval_days}``
    (``null`` or 1-366); ``events`` a list of ``{name, cooldown_hours}``
    (1-168, default 24), sorted by name, each once; ``command`` a boolean.
    Every field is required and no other is taken. Refused: a schedule on a
    spec without a ``window`` parameter, anything the spec's
    :class:`TriggerSet` does not allow, and any difference from
    :func:`baseline_config` on a spec whose triggers are not editable.
    """

    config = _canonical_triggers(raw)
    allowed = allowed_triggers(spec)
    if config["schedule"] is not None:
        if "window" not in spec.params:
            raise ValueError(f"steward {spec.id} has no maintenance window to run on a schedule")
        if not allowed.schedule:
            raise ValueError(f"steward {spec.id} may not run on a schedule")
    refused = [e["name"] for e in config["events"] if e["name"] not in allowed.events]
    if refused:
        raise ValueError(f"steward {spec.id} may not be started by {', '.join(refused)}")
    if config["command"] and not allowed.command:
        raise ValueError(f"steward {spec.id} may not be started by command")
    if not allowed.editable and config != baseline_config(spec):
        raise ValueError(f"steward {spec.id}'s triggers are fixed by its spec")
    return config


def effective_triggers(spec: StewardSpec, stored: Any) -> dict[str, Any]:
    """The trigger configuration in force: ``stored`` within ``spec``'s ceiling.

    ``None`` (nothing stored) is :func:`baseline_config`; so is anything on a
    spec whose triggers are not editable. A stored configuration that cannot be
    read switches nothing on. Entries the ceiling no longer allows — a release
    narrowed it — are dropped, so the configuration in force (and the hash it
    feeds) follows the ceiling without anyone writing it again.
    """

    allowed = allowed_triggers(spec)
    if stored is None or not allowed.editable:
        config = baseline_config(spec)
    else:
        try:
            config = _canonical_triggers(stored)
        except ValueError:
            config = _nothing()
    schedule = config["schedule"] if allowed.schedule and "window" in spec.params else None
    return {
        "schedule": schedule,
        "events": [dict(e) for e in config["events"] if e["name"] in allowed.events],
        "command": bool(config["command"] and allowed.command),
    }


def resolve(spec: StewardSpec, params: Mapping[str, Any]) -> StewardSpec:
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


def evidence_names(spec: StewardSpec) -> tuple[str, ...]:
    """The evidence providers ``spec``'s constraints are fed by, sorted, once each."""

    return tuple(sorted({c.evidence for c in spec.constraints if c.evidence is not None}))


def with_evidence(spec: StewardSpec, evidence: Mapping[str, Any]) -> StewardSpec:
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


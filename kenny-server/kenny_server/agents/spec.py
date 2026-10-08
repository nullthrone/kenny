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
from dataclasses import dataclass, field

from ..tool_classes import SENSITIVE_TOOLS, TOOL_CLASSES

__all__ = [
    "EVENTS",
    "MODES",
    "TRIGGER_KINDS",
    "AgentSpec",
    "Budget",
    "SpecError",
    "Trigger",
    "validate",
]

#: What an agent may do with a change-tier call.
#:
#: * ``off`` — never started.
#: * ``shadow`` — runs the whole investigation and records what it *would* do;
#:   a change is refused at the gate and kept as a recommendation.
#: * ``act`` — a change runs when the gate allows it.
MODES: tuple[str, ...] = ("off", "shadow", "act")

#: What starts a run.
TRIGGER_KINDS: tuple[str, ...] = ("event", "schedule", "on_demand")

#: The server events an ``event`` trigger may name.
EVENTS: tuple[str, ...] = ("ticket_created",)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class SpecError(ValueError):
    """A spec that must not be loaded."""


@dataclass(frozen=True)
class Trigger:
    """What starts a run of this agent."""

    kind: str
    #: For ``kind="event"``: one of :data:`EVENTS`.
    event: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind, "event": self.event}


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
    #: The tool a run ends by calling; its result is the run's verdict.
    verdict_tool: str | None = None
    budget: Budget = field(default_factory=Budget)
    #: Must be true for a spec that names any sensitive tool.
    sensitive_ok: bool = False
    #: The mode a fresh install starts in. ``shadow`` unless there is a reason.
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
                "verdict_tool": self.verdict_tool,
                "budget": self.budget.to_dict(),
                "sensitive_ok": self.sensitive_ok,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
    if spec.verdict_tool is not None and spec.verdict_tool not in spec.tools:
        raise SpecError(f"agent {spec.id}: verdict tool {spec.verdict_tool} is not in its tools")
    if spec.trigger.kind not in TRIGGER_KINDS:
        raise SpecError(f"agent {spec.id}: unknown trigger kind {spec.trigger.kind!r}")
    if spec.trigger.kind == "event" and spec.trigger.event not in EVENTS:
        raise SpecError(f"agent {spec.id}: unknown event {spec.trigger.event!r}")
    if spec.trigger.kind != "event" and spec.trigger.event is not None:
        raise SpecError(f"agent {spec.id}: only an event trigger names an event")
    if spec.default_mode not in MODES:
        raise SpecError(f"agent {spec.id}: unknown default mode {spec.default_mode!r}")
    if spec.budget.max_iterations < 1:
        raise SpecError(f"agent {spec.id}: max_iterations must be at least 1")
    if spec.version < 1:
        raise SpecError(f"agent {spec.id}: version must be at least 1")
    return spec

"""The stewards kenny ships (ADR-0071).

Every spec is validated when this module is imported, so a malformed spec stops
the server from starting instead of surfacing the first time it would run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ...tool_classes import READ_ONLY, classify
from ...toolloop import COPILOT_STEWARD_TOOLS, SERVER_TOOLS
from ...tools import CAPABILITY_TOOLS
from ...ticket_rules import KNOWN_SECTIONS
from ..policy import host_bound
from ..spec import (
    TICKET_CREATED,
    SpecError,
    StewardSpec,
    allowed_triggers,
    runs_on_ticket,
    validate,
)
from .disk import DISK_HYGIENE
from .hygiene import CONFIG_HYGIENE
from .patch import PATCH
from .posture import POSTURE
from .triage import TRIAGE

__all__ = ["CATALOG", "TICKET_SURFACE_TOOLS", "build", "check_dispatchable", "get"]


#: Tools whose handlers expect a ticket session (the ticket id as session id).
#: Only a steward that runs on a ticket's own surface may name them; on any
#: other run they would be handed a run id where they expect a ticket.
TICKET_SURFACE_TOOLS: frozenset[str] = frozenset(
    {"ticket_summary", "ticket_triage_verdict", "ticket_draft", "ticket_find"}
)


#: Tools of the dashboard copilot alone. Their ``steward_id`` names a
#: steward, not a machine, so no run may hold one: the gate would read it as a host.
COPILOT_ONLY_TOOLS: frozenset[str] = COPILOT_STEWARD_TOOLS


#: The steward that runs on a ticket's own surface; the only one a new ticket
#: may start. A literal (``runner.TRIAGE_STEWARD_ID`` imports this module).
_TICKET_STEWARD_ID = "triage"

#: The prefix of an event that names one health section.
_HEALTH_EVENT_PREFIX = "alert:health:"


def check_dispatchable(spec: StewardSpec) -> StewardSpec:
    """Refuse a spec naming a tool the tool loop cannot route where it says.

    Separate from :func:`~kenny_server.stewards.spec.validate`, which stays free
    of the catalogs: a classified tool can still be MCP-only, and the loop
    forwards any name outside ``SERVER_TOOLS`` to the host as a capability.
    """

    undispatchable = sorted(
        t for t in spec.tools if t not in SERVER_TOOLS and t not in CAPABILITY_TOOLS
    )
    if undispatchable:
        raise SpecError(
            f"steward {spec.id}: tool(s) {', '.join(undispatchable)} cannot run in the tool loop"
        )
    copilot_tools = sorted(spec.tools & COPILOT_ONLY_TOOLS)
    if copilot_tools:
        raise SpecError(
            f"steward {spec.id}: tool(s) {', '.join(copilot_tools)} belong to the copilot, not a run"
        )
    ticket_tools = sorted(spec.tools & TICKET_SURFACE_TOOLS)
    if ticket_tools and not runs_on_ticket(spec):
        raise SpecError(
            f"steward {spec.id}: ticket tool(s) {', '.join(ticket_tools)} need a ticket's surface"
        )
    # The gate refuses a change-tier call carrying any argument no constraint
    # binds, so a required argument left unbound is a tool the steward names but
    # could never call within its own bounds — a spec that misleads its reader.
    for tool in sorted(spec.tools):
        if classify(tool) == READ_ONLY or tool == spec.verdict_tool:
            continue
        if tool in CAPABILITY_TOOLS:
            required = [raw for raw in CAPABILITY_TOOLS[tool] if not raw.endswith("?")]
        else:
            required = list(SERVER_TOOLS[tool].get("required", []))
        bound = {c.arg for c in spec.constraints_for(tool)}
        unbound = sorted(arg for arg in required if arg not in bound)
        if unbound:
            raise SpecError(
                f"steward {spec.id}: {tool}'s required argument(s) {', '.join(unbound)} "
                "carry no constraint"
            )
    # Evidence is computed at run start from the server's records (ADR-0072
    # rule 6); values declared beside it would be a second, silent source.
    declared = sorted(c.tool for c in spec.constraints if c.evidence is not None and c.allowed)
    if declared:
        raise SpecError(
            f"steward {spec.id}: evidence constraint(s) on {', '.join(declared)} declare values; "
            "evidence values are computed at run start, never declared"
        )
    # A new ticket starts triage on the ticket's own surface; any other steward
    # started by one would be handed a ticket where it expects a run.
    if runs_on_ticket(spec) and spec.id != _TICKET_STEWARD_ID:
        raise SpecError(
            f"steward {spec.id}: only {_TICKET_STEWARD_ID} may be started by {TICKET_CREATED}"
        )
    # ``spec.validate`` admits any well-formed section name; only a section the
    # server scores can ever raise its alert, so any other would never fire.
    unknown_sections = sorted(
        e
        for e in allowed_triggers(spec).events
        if e.startswith(_HEALTH_EVENT_PREFIX)
        and e[len(_HEALTH_EVENT_PREFIX):] not in KNOWN_SECTIONS["health"]
    )
    if unknown_sections:
        raise SpecError(
            f"steward {spec.id}: no health section raises {', '.join(unknown_sections)}"
        )
    # A scheduled steward without a ``hosts`` parameter runs once per occurrence
    # on no host (``scheduler``); a capability would have nowhere to run.
    if spec.trigger.kind == "schedule" and "hosts" not in spec.params:
        on_host = sorted(spec.tools & frozenset(CAPABILITY_TOOLS))
        if on_host:
            raise SpecError(
                f"steward {spec.id}: runs on no host, yet names host tool(s) {', '.join(on_host)}"
            )
    # A command or an alert starts a run on a machine only among the steward's
    # ``hosts``; without that parameter there is no list to check the machine
    # against, so a steward that works on a machine would run wherever a person
    # pointed it or an alert came from.
    if host_bound(spec) and "hosts" not in spec.params:
        allowed = allowed_triggers(spec)
        starters = sorted(e for e in allowed.events if e.startswith("alert:"))
        if allowed.command:
            starters.insert(0, "command")
        if starters:
            raise SpecError(
                f"steward {spec.id}: names host tools and may be started by "
                f"{', '.join(starters)}, yet takes no hosts parameter"
            )
    return spec


def build(specs: Sequence[StewardSpec]) -> Mapping[str, StewardSpec]:
    """Validate ``specs`` and index them by id; a bad or duplicate spec raises."""

    built: dict[str, StewardSpec] = {}
    for spec in specs:
        check_dispatchable(validate(spec))
        if spec.id in built:
            raise SpecError(f"duplicate steward id {spec.id!r} in the catalog")
        built[spec.id] = spec
    return MappingProxyType(built)


CATALOG: Mapping[str, StewardSpec] = build((TRIAGE, PATCH, POSTURE, CONFIG_HYGIENE, DISK_HYGIENE))


def get(steward_id: str) -> StewardSpec | None:
    """The catalog spec for ``steward_id``, or ``None`` if there is none."""

    return CATALOG.get(steward_id)

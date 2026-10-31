"""The stewards kenny ships (ADR-0071).

Every spec is validated when this module is imported, so a malformed spec stops
the server from starting instead of surfacing the first time it would run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ...tool_classes import READ_ONLY, classify
from ...toolloop import SERVER_TOOLS
from ...tools import CAPABILITY_TOOLS
from ..spec import StewardSpec, SpecError, validate
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


def _runs_on_a_ticket(spec: StewardSpec) -> bool:
    return spec.trigger.kind == "event" and spec.trigger.event == "ticket_created"


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
    ticket_tools = sorted(spec.tools & TICKET_SURFACE_TOOLS)
    if ticket_tools and not _runs_on_a_ticket(spec):
        raise SpecError(
            f"steward {spec.id}: ticket tool(s) {', '.join(ticket_tools)} need a ticket's surface"
        )
    # The gate refuses a change-tier call carrying any argument no constraint
    # binds, so a required argument left unbound is a tool the steward names but
    # could never call within its own bounds — a spec that misleads its reader.
    for tool in sorted(spec.tools & frozenset(CAPABILITY_TOOLS)):
        if classify(tool) == READ_ONLY:
            continue
        bound = {c.arg for c in spec.constraints_for(tool)}
        unbound = sorted(
            raw for raw in CAPABILITY_TOOLS[tool] if not raw.endswith("?") and raw not in bound
        )
        if unbound:
            raise SpecError(
                f"steward {spec.id}: {tool}'s required argument(s) {', '.join(unbound)} "
                "carry no constraint"
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


CATALOG: Mapping[str, StewardSpec] = build((TRIAGE, PATCH, POSTURE))


def get(steward_id: str) -> StewardSpec | None:
    """The catalog spec for ``steward_id``, or ``None`` if there is none."""

    return CATALOG.get(steward_id)

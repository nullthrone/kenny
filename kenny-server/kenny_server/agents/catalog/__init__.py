"""The specialized agents kenny ships (ADR-0071).

Every spec is validated when this module is imported, so a malformed spec stops
the server from starting instead of surfacing the first time it would run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ..spec import AgentSpec, SpecError, validate
from .triage import TRIAGE

__all__ = ["CATALOG", "build", "get"]


def build(specs: Sequence[AgentSpec]) -> Mapping[str, AgentSpec]:
    """Validate ``specs`` and index them by id; a bad or duplicate spec raises."""

    built: dict[str, AgentSpec] = {}
    for spec in specs:
        validate(spec)
        if spec.id in built:
            raise SpecError(f"duplicate agent id {spec.id!r} in the catalog")
        built[spec.id] = spec
    return MappingProxyType(built)


CATALOG: Mapping[str, AgentSpec] = build((TRIAGE,))


def get(agent_id: str) -> AgentSpec | None:
    """The catalog spec for ``agent_id``, or ``None`` if there is none."""

    return CATALOG.get(agent_id)

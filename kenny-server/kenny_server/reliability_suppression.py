"""Operator-managed reliability alarm suppression (issue #166, ADR-0045).

A single, well-known-benign Windows event pattern (e.g. the CAPI2/4176
"AuthSafes count" quirk emitted by CryptSvc) can repeat hundreds of times a
day and dominate the `reliability` section's severity scoring, drowning out
the one event that actually matters. This module lets the operator mute a
specific ``(source, event_id)`` pattern out of *scoring* — never out of the
displayed raw counts — either fleet-wide or for one host.

:class:`SuppressionList` is an in-memory mirror of the operator's rules,
loaded from :class:`kenny_server.store.ReliabilitySuppressionStore` at
startup and kept in sync on every write — the same single-process,
single-event-loop argument ADR-0036 makes for ``Settings``: matching is a
synchronous, lock-free dict lookup, and the DB is touched only on write and
once at startup.

:meth:`SuppressionList.mark` is wired into
:attr:`kenny_server.store.TelemetryStore.annotate` so *every* health
consumer sees the same annotation — alerting, the weekly digest, the fleet
list, the dashboard, and MCP alike — not just the two read paths that already
run the ADR-0028 LLM categorization. Matching needs no LLM and no API key, so
unlike that categorization it can safely run on every snapshot read.
"""

from __future__ import annotations

from typing import Any

from .store import ReliabilitySuppressionStore

__all__ = ["SuppressionList", "rule_id", "validate_source_or_agent"]


def validate_source_or_agent(value: str, field: str) -> str:
    """Trim ``value`` and reject characters that would break the ``id`` key.

    Raises ``ValueError`` (caller turns this into a 400 / MCP ``ToolError``)
    for a ``|`` (the ``id`` separator) or a value over 128 chars.
    """

    value = (value or "").strip()
    if "|" in value:
        raise ValueError(f"{field} must not contain '|'")
    if len(value) > 128:
        raise ValueError(f"{field} must be at most 128 characters")
    return value


def rule_id(agent_id: str, source: str, event_id: int) -> str:
    """The deterministic PK for a rule: ``"<agent_id>|<source>|<event_id>"``.

    Empty ``agent_id`` = fleet-wide; empty ``source`` = wildcard on any source
    reporting ``event_id``. Encodes uniqueness of the triple directly in the
    primary key, so the API/MCP layer can address a rule for removal without
    a lookup.
    """

    return f"{agent_id}|{source}|{int(event_id)}"


class SuppressionList:
    """In-memory mirror of the operator's reliability suppression rules."""

    def __init__(self, store: ReliabilitySuppressionStore | None) -> None:
        self._store = store
        # (agent_id, source, event_id) -> rule dict
        self._rules: dict[tuple[str, str, int], dict[str, Any]] = {}

    async def load(self) -> None:
        """Load persisted rules into the in-memory mirror (call once at startup)."""

        if self._store is None:
            return
        self.set_rules(await self._store.list())

    def set_rules(self, rules: list[dict[str, Any]]) -> None:
        """Rebuild the lookup dict from a full rule list (mirrors PolicyEngine's
        ``set_operator_rules`` idiom)."""

        self._rules = {
            (r["agent_id"], r["source"], int(r["event_id"])): r for r in rules
        }

    def rules(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """All rules, or (when ``agent_id`` is given) fleet-wide rules plus
        that host's own rules — oldest-first, same order as the store."""

        rules = sorted(self._rules.values(), key=lambda r: (r["created_at"], r["id"]))
        if agent_id is None:
            return rules
        return [r for r in rules if r["agent_id"] in ("", agent_id)]

    def match(self, agent_id: str, source: Any, event_id: Any) -> dict[str, Any] | None:
        """Return the most specific rule covering this event group, or None.

        Precedence, most specific first: exact host+source, host+wildcard,
        fleet+source, fleet+wildcard.
        """

        try:
            eid = int(event_id)
        except (TypeError, ValueError):
            return None
        src = str(source or "").strip()
        for aid in (agent_id, ""):
            for s in (src, ""):
                rule = self._rules.get((aid, s, eid))
                if rule is not None:
                    return rule
        return None

    def mark(self, agent_id: str, snapshot: dict[str, Any] | None) -> None:
        """Stamp ``suppressed``/``suppressed_by`` onto each reliability event
        group in ``snapshot``, in place. A no-op with no rules loaded or no
        reliability events. ``category``/``severity``/``suspected_cause`` (the
        ADR-0028 LLM annotation) are left untouched — suppression is an
        orthogonal, operator-explained signal, deliberately distinguishable
        from the classifier's own ``benign`` verdict.
        """

        if not isinstance(snapshot, dict):
            return
        rel = snapshot.get("reliability")
        if not isinstance(rel, dict):
            return
        events = rel.get("events")
        if not isinstance(events, list):
            return
        for e in events:
            if not isinstance(e, dict):
                continue
            rule = self.match(agent_id, e.get("source"), e.get("event_id"))
            if rule is None:
                e.pop("suppressed", None)
                e.pop("suppressed_by", None)
                continue
            e["suppressed"] = True
            e["suppressed_by"] = {
                "id": rule["id"],
                "scope": "host" if rule["agent_id"] else "fleet",
                "source": rule["source"],
                "event_id": rule["event_id"],
                "note": rule["note"],
            }

    async def add(
        self,
        *,
        event_id: int,
        source: str = "",
        agent_id: str = "",
        note: str = "",
        created_by: str = "",
    ) -> list[dict[str, Any]]:
        """Validate and persist a new rule, refresh the mirror, and return the
        full rule list (for the API/MCP response)."""

        if self._store is None:
            raise RuntimeError("SuppressionList has no store configured")
        agent_id = validate_source_or_agent(agent_id, "agent_id")
        source = validate_source_or_agent(source, "source")
        try:
            eid = int(event_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("event_id must be an integer") from exc
        if not (0 <= eid < 2**31):
            raise ValueError("event_id out of range")
        note = (note or "").strip()[:200]
        rid = rule_id(agent_id, source, eid)
        await self._store.add(
            id=rid,
            agent_id=agent_id,
            source=source,
            event_id=eid,
            note=note,
            created_by=(created_by or "").strip(),
        )
        await self.load()
        return self.rules()

    async def remove(self, rule_id_: str) -> tuple[bool, list[dict[str, Any]]]:
        """Delete a rule by id, refresh the mirror, and return
        ``(removed, all_rules)``."""

        if self._store is None:
            raise RuntimeError("SuppressionList has no store configured")
        removed = await self._store.remove(rule_id_)
        await self.load()
        return removed, self.rules()

    async def delete_agent(self, agent_id: str) -> int:
        """Drop host-scoped rules for a removed host (inventory purge, see
        :mod:`kenny_server.inventory`) and refresh the mirror. Fleet-wide
        rules are untouched."""

        if self._store is None:
            raise RuntimeError("SuppressionList has no store configured")
        n = await self._store.delete_agent(agent_id)
        await self.load()
        return n

"""Operator-configurable rules for which events open a ticket.

`alerting.AlertEngine` used to hardcode "every genuine alert opens a ticket,
nothing else does" as a single `if note.kind == "alert"` check. This module
replaces that with an operator-authored rule table, following the same shape
as :mod:`kenny_server.reliability_suppression` (ADR-0041): a deterministic
`"<agent_id>|<event_type>|<section>"` primary key, a most-specific-wins
in-memory matcher, and a store that persists only *deviations* from a coded
default. An empty rule table therefore reproduces today's behavior exactly.

:func:`decide` is pure and I/O-free -- no import of `tickets.py`, `alerting.py`
or any store -- so it is directly unit-testable and keeps ticket-opening
policy out of the ticket lifecycle module, per the project's seam discipline
(kenny-server/CLAUDE.md).

Vocabulary:

* ``event_type`` -- which producer raised the notification: ``health``,
  ``offline``, ``disk_forecast`` or ``change``. Closed and validated; a
  notification with `kind in NEVER_TICKETED_KINDS` (``recovery``, ``digest``)
  can never open a ticket, checked *before* any rule is consulted, so no rule
  -- however written -- can violate that invariant.
* ``section`` -- deliberately **not** validated against a closed set. Health
  sections are not limited to `health_rules.RULES`: `evaluate_snapshot` scores
  *every* section the agent reports a `status` for, rule or no rule (see
  `health_rules.evaluate_section`). `KNOWN_SECTIONS` is therefore an
  *advertised* vocabulary for the UI/API, derived from the live registries
  plus an explicit list of ruleless-but-scoreable sections -- never a gate
  that rejects a legitimate rule for a section with no dedicated rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import diffs, health_rules
from .notify import Notification
from .store import TicketRuleStore

__all__ = [
    "EVENT_TYPES",
    "DECISIONS",
    "DEFAULT_DECISION",
    "NEVER_TICKETED_KINDS",
    "DEFAULT_TICKET_KINDS",
    "KNOWN_SECTIONS",
    "TicketRuleList",
    "decide",
    "rule_id",
    "validate_field",
]

# -- vocabulary --------------------------------------------------------------

EVENT_TYPES: tuple[str, ...] = ("health", "offline", "disk_forecast", "change")
DECISIONS: tuple[str, ...] = ("open_all", "open_crit", "never")

# A notification of this ``kind`` can never open a ticket, no matter what any
# rule says -- checked first and unconditionally in ``decide``. A ``recovery``
# legitimately carries ``event_type="health"`` and populated ``sections`` (the
# webhook payload wants that detail), which is exactly why this guard keys on
# ``kind`` and never on ``event_type``.
NEVER_TICKETED_KINDS: frozenset[str] = frozenset({"recovery", "digest"})

# Today's hardcoded rule, named: only a genuine alert opens a ticket by
# default. Every other ``kind`` (recovery, change, digest) defaults to closed.
DEFAULT_TICKET_KINDS: frozenset[str] = frozenset({"alert"})

DEFAULT_DECISION: dict[str, str] = {
    "health": "open_all",
    "offline": "open_all",
    "disk_forecast": "open_all",
    "change": "never",
}

# Sections a health snapshot can report a status for but that carry no rule in
# health_rules.RULES -- see the module docstring. Kept as an explicit literal
# (rather than parsed from docs/protocol.md) so it stays a plain, greppable
# Python list; extend it when a new telemetry section ships.
_EXTRA_HEALTH_SECTIONS: frozenset[str] = frozenset(
    {
        "firewall",
        "encryption",
        "av_thirdparty",
        "defender_quarantine",
        "app_updates",
        "uptime",
        "time_sync",
        "printers",
        "wifi_quality",
        "disk_smart",
        "network",
        "routing",
        "processes",
        "services",
        "screen_time",
        "autostart",
        "installed_software",
        "browser_extensions",
        "scheduled_tasks",
        "peripherals",
    }
)

# Advertised (not enforced) per-event_type section vocabulary, derived from the
# live registries so a new health rule or diff spec widens this automatically.
KNOWN_SECTIONS: dict[str, frozenset[str]] = {
    "health": frozenset(health_rules.RULES) | _EXTRA_HEALTH_SECTIONS,
    "change": frozenset(diffs.SPECS),
    "offline": frozenset(),
    "disk_forecast": frozenset(),
}


def _severity_from_priority(priority: str) -> str:
    return "crit" if priority in ("high", "urgent") else "warn"


@dataclass(frozen=True)
class Decision:
    """The outcome of matching one notification against the rule set."""

    open: bool
    rule: dict[str, Any] | None = None
    subject: tuple[str, str] | None = None  # (section, severity) that decided it


def rule_id(agent_id: str, event_type: str, section: str) -> str:
    """The deterministic PK for a rule: ``"<agent_id>|<event_type>|<section>"``.

    Empty ``agent_id`` = fleet-wide; empty ``section`` = any section. Encodes
    uniqueness of the pair directly in the primary key, mirroring
    ``reliability_suppression.rule_id``.
    """

    return f"{agent_id}|{event_type}|{section}"


def validate_field(value: str, field_name: str) -> str:
    """Trim ``value`` and reject characters that would break the ``id`` key."""

    value = (value or "").strip()
    if "|" in value:
        raise ValueError(f"{field_name} must not contain '|'")
    if len(value) > 128:
        raise ValueError(f"{field_name} must be at most 128 characters")
    return value


def decide(
    rules: dict[tuple[str, str, str], dict[str, Any]],
    *,
    kind: str,
    agent_id: str,
    event_type: str,
    priority: str,
    sections: dict[str, str],
) -> Decision:
    """Decide whether a notification should open a ticket.

    ``rules`` is keyed ``(agent_id, event_type, section)`` with ``""`` as the
    wildcard in each position -- the same shape ``TicketRuleList`` mirrors from
    the store. Pure and I/O-free.

    Algorithm:

    1. ``kind`` in :data:`NEVER_TICKETED_KINDS` -> never opens. This is checked
       first and unconditionally: no rule can override it.
    2. One *subject* per ``(section, severity)`` pair in ``sections``, or a
       single subject with an empty section when ``sections`` is empty
       (offline, disk_forecast). Subjects are visited in a deterministic,
       sorted-by-section order.
    3. For each subject, look up the most specific matching rule (host beats
       fleet, section beats any-section). Its ``decision`` is one of
       ``open_all`` / ``open_crit`` / ``never``. With no match, the subject
       falls back to :data:`DEFAULT_DECISION` for ``event_type`` -- except
       that the true legacy default is keyed off ``kind``, not ``event_type``,
       so a notification with no ``event_type`` at all (back-compat: every
       existing construction site) still resolves via ``kind``.
    4. The first subject that resolves to "open" wins: one notification opens
       at most one ticket, matching today's one-notification-one-ticket
       granularity even when several sections escalated together.
    """

    if kind in NEVER_TICKETED_KINDS:
        return Decision(False)

    default_open = kind in DEFAULT_TICKET_KINDS

    subjects: list[tuple[str, str]] = (
        sorted(sections.items()) if sections else [("", _severity_from_priority(priority))]
    )

    for section, severity in subjects:
        rule = _match(rules, agent_id, event_type, section)
        if rule is not None:
            rule_decision = rule["decision"]
        else:
            # No event_type-specific default is looked up when the producer
            # left event_type empty (back-compat notifications predating this
            # feature) -- those fall back to the kind-based legacy rule.
            rule_decision = (
                DEFAULT_DECISION.get(event_type, "never")
                if event_type
                else ("open_all" if default_open else "never")
            )
        if rule_decision == "open_all":
            return Decision(True, rule, (section, severity))
        if rule_decision == "open_crit" and severity == "crit":
            return Decision(True, rule, (section, severity))
    return Decision(False)


def _match(
    rules: dict[tuple[str, str, str], dict[str, Any]],
    agent_id: str,
    event_type: str,
    section: str,
) -> dict[str, Any] | None:
    """Most specific match first: host beats fleet, named section beats any."""

    for aid in (agent_id, ""):
        for sec in (section, ""):
            rule = rules.get((aid, event_type, sec))
            if rule is not None:
                return rule
    return None


class TicketRuleList:
    """In-memory mirror of the operator's auto-ticket rules.

    Same idiom as ``reliability_suppression.SuppressionList``: a synchronous,
    lock-free dict lookup for the hot path (every dispatched alert), backed by
    a store touched only on write and once at startup (single-process,
    single-event-loop, per ADR-0032's reasoning for ``Settings``).
    """

    def __init__(self, store: TicketRuleStore | None) -> None:
        self._store = store
        # (agent_id, event_type, section) -> rule dict
        self._rules: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def load(self) -> None:
        """Load persisted rules into the in-memory mirror (call once at startup)."""

        if self._store is None:
            return
        self.set_rules(await self._store.list())

    def set_rules(self, rules: list[dict[str, Any]]) -> None:
        self._rules = {
            (r["agent_id"], r["event_type"], r["section"]): r for r in rules
        }

    def mapping(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        """The raw ``(agent_id, event_type, section) -> rule`` mirror, for
        callers (``alerting._dispatch``) that want to call ``decide`` directly
        rather than through :meth:`should_open`."""

        return self._rules

    def rules(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """All rules, or (when ``agent_id`` is given) fleet-wide rules plus
        that host's own rules -- oldest-first, same order as the store."""

        rules = sorted(self._rules.values(), key=lambda r: (r["created_at"], r["id"]))
        if agent_id is None:
            return rules
        return [r for r in rules if r["agent_id"] in ("", agent_id)]

    def should_open(self, note: Notification) -> bool:
        """Whether ``note`` should open a ticket, per the current rule set."""

        decision = decide(
            self._rules,
            kind=note.kind,
            agent_id=note.agent_id or "",
            event_type=note.event_type,
            priority=note.priority,
            sections=note.sections,
        )
        return decision.open

    async def add(
        self,
        *,
        event_type: str,
        decision: str,
        section: str = "",
        agent_id: str = "",
        note: str = "",
        created_by: str = "",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Validate and persist a new rule, refresh the mirror.

        Returns ``(rules, warnings)`` -- ``warnings`` carries a note when
        ``section`` is outside the advertised vocabulary for ``event_type``
        (never rejected: see the module docstring on why section validation
        stays lenient).
        """

        if self._store is None:
            raise RuntimeError("TicketRuleList has no store configured")
        agent_id = validate_field(agent_id, "agent_id")
        event_type = validate_field(event_type, "event_type")
        section = validate_field(section, "section")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"event_type must be one of {', '.join(EVENT_TYPES)}")
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        note = (note or "").strip()[:200]
        warnings: list[str] = []
        if section and section not in KNOWN_SECTIONS.get(event_type, frozenset()):
            warnings.append(
                f"{section!r} is not a known {event_type} section (rule saved anyway)"
            )
        rid = rule_id(agent_id, event_type, section)
        await self._store.add(
            id=rid,
            agent_id=agent_id,
            event_type=event_type,
            section=section,
            decision=decision,
            note=note,
            created_by=(created_by or "").strip(),
        )
        await self.load()
        return self.rules(), warnings

    async def remove(self, rule_id_: str) -> tuple[bool, list[dict[str, Any]]]:
        if self._store is None:
            raise RuntimeError("TicketRuleList has no store configured")
        removed = await self._store.remove(rule_id_)
        await self.load()
        return removed, self.rules()

    async def delete_agent(self, agent_id: str) -> int:
        """Drop host-scoped rules for a removed host; fleet-wide rules survive."""

        if self._store is None:
            raise RuntimeError("TicketRuleList has no store configured")
        n = await self._store.delete_agent(agent_id)
        await self.load()
        return n

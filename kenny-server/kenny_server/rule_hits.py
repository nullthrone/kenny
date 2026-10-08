"""When an operator rule last did anything: hit tracking for rule mirrors.

Reliability suppressions (ADR-0041) and auto-ticket rules (``ticket_rules.py``)
are operator rules the server applies on its own — a suppression when it stamps
a snapshot's event group, an auto-ticket rule when ``ticket_rules.decide``
picks it for an alert. Each records *that it was applied*: ``last_matched_at``
(the latest moment it matched) and ``match_count`` (how often it was applied).
That record is the server's evidence of whether a rule still does anything; the
config-hygiene agent (``agents/hygiene.py``) proposes removing only rules it
shows unused, and never on the model's say-so.

:class:`PendingHits` is the in-memory half: the rule mirrors are matched
synchronously (``SuppressionList.mark`` runs inside every telemetry read), so a
hit updates the mirror's rule dict at once and is queued for the store, which
the owning mirror flushes. The mirror is therefore always current, and a hit
not yet flushed is lost only by a crash — and for a suppression it comes back
with the next read, because its match time is the event's own ``last_seen``.

Pure apart from the clock the caller passes; no I/O here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "PendingHits",
    "iso",
    "last_activity",
    "observed_at",
    "parse_instant",
    "unused",
    "unused_ids",
]


def iso(moment: datetime) -> str:
    """``moment`` as UTC ISO-8601 text with microseconds, so stamps compare as text."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_instant(value: Any) -> datetime | None:
    """An ISO-8601 instant as an aware UTC datetime, or ``None`` if it is not one."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def observed_at(value: Any, now: datetime) -> str:
    """When a matched event happened: its own timestamp, never later than ``now``.

    An event group carries ``last_seen``; using it rather than the moment of the
    read means re-reading an old snapshot (a trend chart over 30 days) cannot
    make a rule look recently used. A missing or unreadable stamp counts as
    ``now`` — the safe direction: a rule that may be in use is kept.
    """

    seen = parse_instant(value)
    if seen is None or seen > now:
        return iso(now)
    return iso(seen)


def last_activity(rule: Mapping[str, Any]) -> datetime | None:
    """The later of a rule's creation and its last match; ``None`` if it cannot be dated.

    Creation counts as a match: a rule nobody has had a chance to see apply is
    not unused. A rule without a readable ``created_at`` cannot be dated, and is
    never unused.
    """

    created = parse_instant(rule.get("created_at"))
    if created is None:
        return None
    matched = parse_instant(rule.get("last_matched_at"))
    return max(created, matched) if matched is not None else created


def unused(rule: Mapping[str, Any], now: datetime, days: int) -> bool:
    """Whether nothing has matched ``rule``, and it was not created, in the last ``days``."""

    active = last_activity(rule)
    return active is not None and active <= now - timedelta(days=days)


def unused_ids(rules: Iterable[Mapping[str, Any]], now: datetime, days: int) -> list[str]:
    """The ids of ``rules`` that are :func:`unused`, sorted."""

    return sorted(str(r["id"]) for r in rules if r.get("id") and unused(r, now, days))


class PendingHits:
    """Hits applied to a rule mirror and not yet written to its store."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, int]] = {}

    def __bool__(self) -> bool:
        return bool(self._pending)

    def add(self, rule: dict[str, Any], at: str) -> None:
        """Record one application of ``rule`` matching at ``at``, on the mirror and queued."""

        rule_id = str(rule.get("id") or "")
        if not rule_id:
            return
        previous = rule.get("last_matched_at")
        if not previous or previous < at:
            rule["last_matched_at"] = at
        rule["match_count"] = int(rule.get("match_count") or 0) + 1
        queued_at, times = self._pending.get(rule_id, ("", 0))
        self._pending[rule_id] = (max(queued_at, at), times + 1)

    def take(self) -> dict[str, tuple[str, int]]:
        """Everything queued, emptying the queue."""

        taken, self._pending = self._pending, {}
        return taken

    def restore(self, hits: Mapping[str, tuple[str, int]]) -> None:
        """Queue ``hits`` again after a failed write, merged with what arrived since."""

        for rule_id, (at, times) in hits.items():
            queued_at, queued = self._pending.get(rule_id, ("", 0))
            self._pending[rule_id] = (max(queued_at, at), queued + times)

    def overlay(self, rules: Iterable[dict[str, Any]]) -> None:
        """Apply what is still queued onto freshly loaded rule dicts."""

        for rule in rules:
            queued = self._pending.get(str(rule.get("id") or ""))
            if queued is None:
                continue
            at, times = queued
            if not rule.get("last_matched_at") or rule["last_matched_at"] < at:
                rule["last_matched_at"] = at
            rule["match_count"] = int(rule.get("match_count") or 0) + times

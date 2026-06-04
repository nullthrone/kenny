"""Authoritative, server-side health thresholds.

The agent sets a reasonable ``status`` per section, but these rules are
authoritative for fleet aggregation (see ``docs/protocol.md`` § Telemetry
sections). Rules are data-driven: each entry is a function that inspects one
section's raw fields and returns ``(status, reason)`` overrides, or ``None`` to
defer to the agent-reported ``status``.

``evaluate_snapshot`` applies the rules, takes the worst of the rule status and
the agent-reported status per section, and rolls those up to an overall agent
health via worst-of.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

Status = str  # "ok" | "warn" | "crit"

_ORDER = {"ok": 0, "warn": 1, "crit": 2}


def worst(*statuses: Status) -> Status:
    """Return the most severe of the given statuses (crit > warn > ok)."""

    return max((s for s in statuses if s), key=lambda s: _ORDER.get(s, 0), default="ok")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Accept trailing Z (UTC) and offset forms.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(value: Any, *, now: datetime) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


# A rule maps a section payload -> (status, reason) or None to defer.
Rule = Callable[[dict[str, Any], datetime], "tuple[Status, str] | None"]


def _rule_disk(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    worst_pct = -1.0
    worst_mount = ""
    for vol in payload.get("volumes", []) or []:
        pct = vol.get("percent_used")
        if isinstance(pct, (int, float)) and pct > worst_pct:
            worst_pct = float(pct)
            worst_mount = vol.get("mount", "?")
    if worst_pct < 0:
        return None
    if worst_pct > 90:
        return "crit", f"{worst_mount} {worst_pct:.0f}% full (>90%)"
    if worst_pct > 80:
        return "warn", f"{worst_mount} {worst_pct:.0f}% full (>80%)"
    return "ok", f"{worst_mount} {worst_pct:.0f}% full"


def _rule_defender(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    enabled = payload.get("enabled", True)
    realtime = payload.get("realtime_protection", True)
    if enabled is False or realtime is False:
        return "crit", "Defender disabled / real-time protection off"
    age = _age_days(payload.get("last_scan"), now=now)
    if age is not None and age > 14:
        return "warn", f"Last scan {age:.0f}d ago (>14d)"
    return "ok", "Defender healthy"


def _rule_win_update(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    recent = payload.get("recent", []) or []
    failed = [u for u in recent if str(u.get("result", "")).lower() == "failed"]
    if failed:
        return "warn", f"{len(failed)} update(s) failed"
    return "ok", "Updates healthy"


def _rule_reboot_pending(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    if payload.get("pending") is True:
        reasons = payload.get("reasons") or []
        why = ", ".join(str(r) for r in reasons) if reasons else "unknown"
        return "warn", f"Reboot pending ({why})"
    return "ok", "No reboot pending"


def _rule_battery(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    health = payload.get("health_percent")
    if isinstance(health, (int, float)):
        if health < 50:
            return "crit", f"Battery health {health:.0f}% (<50%)"
        if health < 70:
            return "warn", f"Battery health {health:.0f}% (<70%)"
    return None


def _rule_memory(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    pct = payload.get("percent_used")
    if isinstance(pct, (int, float)):
        if pct > 95:
            return "crit", f"Memory {pct:.0f}% used (>95%)"
        if pct > 85:
            return "warn", f"Memory {pct:.0f}% used (>85%)"
        return "ok", f"Memory {pct:.0f}% used"
    return None


def _rule_os_support(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    if payload.get("eol") is True:
        return "crit", "OS is end-of-life"
    age = _age_days(payload.get("eol_date"), now=now)
    # eol_date in the past => EOL crit; within 90 days => warn.
    if age is not None:
        if age > 0:
            return "crit", "OS past end-of-life date"
        if age > -90:
            return "warn", f"OS end-of-life in {-age:.0f}d"
    return None


# Section name -> rule. Easy to extend: add an entry.
RULES: dict[str, Rule] = {
    "disk": _rule_disk,
    "defender": _rule_defender,
    "win_update": _rule_win_update,
    "reboot_pending": _rule_reboot_pending,
    "battery": _rule_battery,
    "memory": _rule_memory,
    "os_support": _rule_os_support,
}


def evaluate_section(
    name: str, payload: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return ``{status, summary, reason?}`` for one section after applying rules."""

    now = now or datetime.now(timezone.utc)
    reported = payload.get("status", "ok")
    summary = payload.get("summary", "")
    rule = RULES.get(name)
    if rule is None:
        return {"status": reported, "summary": summary}
    outcome = rule(payload, now)
    if outcome is None:
        return {"status": reported, "summary": summary}
    rule_status, reason = outcome
    final = worst(reported, rule_status)
    return {"status": final, "summary": summary, "reason": reason}


def evaluate_snapshot(
    snapshot: dict[str, dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Evaluate every section and roll up to an overall agent health.

    Returns ``{"overall": status, "sections": {name: {status, summary, reason?}}}``.
    """

    now = now or datetime.now(timezone.utc)
    sections: dict[str, Any] = {}
    for name, payload in snapshot.items():
        sections[name] = evaluate_section(name, dict(payload), now=now)
    overall = worst(*(s["status"] for s in sections.values())) if sections else "ok"
    return {"overall": overall, "sections": sections}

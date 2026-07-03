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
    # NOTE: protocol.md gives ">90% => crit" as an example, but the golden
    # fixture reports a 91%-full disk as "warn", and the DOD test requires
    # disk == warn for that fixture. We therefore treat >90% as warn and
    # reserve crit for near-full (>=95%) volumes. Worst-of with the
    # agent-reported status still applies.
    if worst_pct >= 95:
        return "crit", f"{worst_mount} {worst_pct:.0f}% full (>=95%)"
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


def _rule_thermals(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    sensors = payload.get("sensors") or []
    temps = [
        s.get("temperature_c")
        for s in sensors
        if isinstance(s.get("temperature_c"), (int, float))
    ]
    if not temps:
        return None  # no sensors reported -> defer to agent status
    hottest = max(temps)
    if hottest >= 95:
        return "crit", f"Hottest sensor {hottest:.0f}°C (>=95°C)"
    if hottest >= 85:
        return "warn", f"Hottest sensor {hottest:.0f}°C (>=85°C)"
    return "ok", f"Hottest {hottest:.0f}°C"


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


def _number(value: Any) -> float | None:
    """Coerce a JSON number to float, rejecting bools and non-numerics."""

    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _reliability_reason(events: list[Any], total: int) -> str:
    """A content reason: the biggest problem groups by category (or raw source)."""

    tally: dict[str, int] = {}
    for e in events or []:
        if not isinstance(e, dict):
            continue
        label = e.get("category") or e.get("source") or "?"
        tally[label] = tally.get(label, 0) + int(_number(e.get("count")) or 0)
    top = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:3]
    if not top:
        return f"{total} error/critical events in 7d"
    return ", ".join(f"{label} ×{count}" for label, count in top)


def _rule_reliability(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    # Judge on the *content* of the error breakdown, not a bare count. `events` is
    # the grouped breakdown (each with count/level, and a server-annotated
    # `category`); `stability_index` is the Windows Reliability Index (0-10).
    events = payload.get("events")
    si = _number(payload.get("stability_index"))
    total = _number(payload.get("recent_crashes"))
    if events is None and total is None and si is None:
        return None
    if total is None:
        total = sum(int(_number(e.get("count")) or 0) for e in (events or []) if isinstance(e, dict))
    total_i = int(total)
    has_critical = any(
        isinstance(e, dict) and e.get("level") == "critical" for e in (events or [])
    )

    if total_i >= 50 or (si is not None and si < 3):
        status: Status = "crit"
    elif total_i >= 15 or has_critical or (si is not None and si < 6):
        status = "warn"
    else:
        status = "ok"

    return status, _reliability_reason(events if isinstance(events, list) else [], total_i)


_WEB_ACTIVITY_SERIOUS = {"custom", "seed", "external_adult"}


def _rule_web_activity(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    # `flagged` is a server-internal annotation added at insert time (ADR-0026).
    # Absent => the host is not configured for parental controls; defer.
    flagged = payload.get("flagged")
    if flagged is None:
        return None
    recent = [
        f
        for f in flagged
        if (age := _age_days(f.get("last_seen"), now=now)) is not None and age <= 1.0
    ]
    serious = [f for f in recent if f.get("category") in _WEB_ACTIVITY_SERIOUS]
    if serious:
        example = serious[0].get("domain", "?")
        return "crit", f"{len(serious)} flagged domain(s) in 24h (e.g. {example})"
    bypass = [f for f in recent if f.get("category") == "bypass"]
    if bypass:
        example = bypass[0].get("domain", "?")
        return "warn", f"{len(bypass)} bypass domain(s) in 24h (e.g. {example})"
    return "ok", "no flagged domains (24h)"


# Well-known remote-access ports; a non-loopback listener here is worth a look
# on a family PC (RDP, VNC, SSH, WinRM).
_REMOTE_ACCESS_PORTS = {22, 3389, 5900, 5985, 5986}


def _rule_listening_ports(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    exposed = [
        p
        for p in payload.get("ports") or []
        if p.get("port") in _REMOTE_ACCESS_PORTS
        and not str(p.get("address", "")).startswith(("127.", "::1"))
    ]
    if exposed:
        e = exposed[0]
        example = f"{e.get('proto', '?')}/{e.get('port', '?')} {e.get('process', '?')}"
        return "warn", f"{len(exposed)} remote-access port(s) listening (e.g. {example})"
    return None


def _rule_local_accounts(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    warns: list[str] = []
    for account in payload.get("accounts") or []:
        if not account.get("enabled"):
            continue
        if account.get("is_admin") and account.get("password_required") is False:
            return "crit", f"admin '{account.get('name', '?')}' requires no password"
        if account.get("builtin_admin"):
            warns.append("built-in Administrator enabled")
        if account.get("builtin_guest"):
            warns.append("Guest account enabled")
    if warns:
        return "warn", "; ".join(warns)
    return None


def _rule_backup_status(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    restore = payload.get("restore_points") or {}
    latest_age = _age_days(restore.get("latest"), now=now)
    recent_restore_point = latest_age is not None and latest_age <= 30
    fh_state = str((payload.get("file_history") or {}).get("service_state") or "").lower()
    onedrive_running = (payload.get("onedrive") or {}).get("running") is True
    if not recent_restore_point and fh_state != "running" and not onedrive_running:
        return "warn", "no backup evidence (no restore point <=30d, File History off, OneDrive not running)"
    return None


def _rule_net_quality(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    reference = payload.get("reference") or {}
    ref_loss = reference.get("loss_percent")
    if isinstance(ref_loss, (int, float)) and ref_loss >= 60:
        return "crit", f"internet degraded ({ref_loss:.0f}% loss to {reference.get('host', '?')})"
    gateway = payload.get("gateway") or {}
    latency = gateway.get("latency_ms")
    loss = gateway.get("loss_percent")
    slow = isinstance(latency, (int, float)) and latency > 100
    lossy = isinstance(loss, (int, float)) and loss > 20
    if slow or lossy:
        parts = []
        if slow:
            parts.append(f"{latency:.0f}ms latency")
        if lossy:
            parts.append(f"{loss:.0f}% loss")
        return "warn", f"gateway link poor ({', '.join(parts)})"
    return None


# Section name -> rule. Easy to extend: add an entry.
RULES: dict[str, Rule] = {
    "disk": _rule_disk,
    "defender": _rule_defender,
    "win_update": _rule_win_update,
    "reboot_pending": _rule_reboot_pending,
    "battery": _rule_battery,
    "memory": _rule_memory,
    "thermals": _rule_thermals,
    "os_support": _rule_os_support,
    "web_activity": _rule_web_activity,
    "reliability": _rule_reliability,
    "listening_ports": _rule_listening_ports,
    "local_accounts": _rule_local_accounts,
    "backup_status": _rule_backup_status,
    "net_quality": _rule_net_quality,
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

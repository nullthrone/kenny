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


# -- reliability: volume-based fallback (no severity annotation present) ----
#
# Used only when events carry no `severity` field at all — i.e. the read-path
# LLM categorization (ADR-0028) has never run over this payload (a raw agent
# snapshot, or a test payload built by hand). Kept as today's thresholds, plus
# one addition (a distinct-pattern escalation) so this path is never *less*
# sensitive than the original volume-based rule — see _rule_reliability_by_volume.
_RELIABILITY_FALLBACK_CRIT_TOTAL = 50
_RELIABILITY_FALLBACK_WARN_TOTAL = 15
# This many distinct (source, event_id) patterns, even if each is individually
# low-count, is itself a signal worth a look when there's no severity info to
# tell benign repetition from real diversity.
_RELIABILITY_FALLBACK_WARN_DISTINCT = 8

# -- reliability: weighted-pattern scoring (severity annotation present) ----
#
# Score on WHAT is recurring, not how often. A single benign pattern
# repeating hundreds of times must not out-rank a handful of distinct,
# unclassified or serious ones.
_RELIABILITY_SERIOUS_RECURRENCE_CRIT = 10  # one 'serious' pattern this often -> crit alone
_RELIABILITY_SIGNIFICANT_PATTERNS_CRIT = 5  # this many distinct non-benign patterns -> crit

# Shared by both scoring paths: the Windows Reliability Index (0-10) is an
# independent, agent-computed signal that content-based pattern scoring can't
# see into, so it always applies on top. It is deliberately NOT suppressible —
# an operator muting a noisy event pattern must never be able to hide a
# genuinely low reliability index (issue #166 / ADR-0045).
_RELIABILITY_SI_CRIT = 3
_RELIABILITY_SI_WARN = 6


def _reliability_reason(events: list[Any], total: int) -> str:
    """A content reason: the biggest problem groups by category (or raw source).

    Used for the volume-based fallback path, where there is no severity/cause
    to name — see :func:`_reliability_pattern_reason` for the annotated path.
    Suppressed groups (ADR-0045) are excluded from the tally — a muted pattern
    must not out-rank the events that still matter — and counted in a trailing
    note instead, so an operator can tell "quiet" from "quieted".
    """

    tally: dict[str, int] = {}
    suppressed_n = 0
    for e in events or []:
        if not isinstance(e, dict):
            continue
        if e.get("suppressed"):
            suppressed_n += 1
            continue
        label = e.get("category") or e.get("source") or "?"
        tally[label] = tally.get(label, 0) + int(_number(e.get("count")) or 0)
    top = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:3]
    suffix = f" ({suppressed_n} pattern(s) suppressed)" if suppressed_n else ""
    if not top:
        return f"{total} error/critical events in 7d{suffix}"
    return ", ".join(f"{label} ×{count}" for label, count in top) + suffix


def _cadence_label(count: int, window_days: Any) -> str:
    """A short, deterministic cadence label from count/window (e.g. '~43/day')."""

    days = _number(window_days) or 7.0
    if days <= 0 or count <= 0:
        return f"{count} total"
    per_day = count / days
    if per_day >= 1:
        return f"~{per_day:.0f}/day"
    return f"~{per_day * 7:.1f}/week"


def _reliability_pattern_reason(patterns: list[dict[str, Any]], total: int, window_days: Any) -> str:
    """Name the dominant *significant* pattern — source/event id, cadence, and
    the LLM's suspected cause — so a reader can judge severity from
    ``agent_health`` alone, without a manual ``diag_eventlog`` call. A host
    whose events are all known-benign says so explicitly instead of hiding the
    count behind a scary number.

    Suppressed patterns (ADR-0045 / issue #166) are never named here — that is
    the whole point of muting them — but their existence is always noted in a
    trailing clause, so the reader can tell "quiet" from "quieted". A pattern
    the operator muted is never folded into "known-benign": that phrase is the
    LLM's verdict, and a suppression is the operator's, a different claim.
    """

    suppressed = [p for p in patterns if p.get("suppressed")]
    scored = [p for p in patterns if not p.get("suppressed")]
    suffix = f" ({len(suppressed)} pattern(s) suppressed)" if suppressed else ""

    significant = [p for p in scored if p["severity"] != "benign"]
    if not significant:
        if not patterns:
            return f"{total} error/critical events in 7d"
        if not scored:
            return f"{total} events, all {len(suppressed)} pattern(s) suppressed"
        by_category: dict[str, int] = {}
        for p in scored:
            label = p.get("category") or "?"
            by_category[label] = by_category.get(label, 0) + p["count"]
        top_cat = max(by_category, key=lambda c: by_category[c])
        return f"{total} events, all known-benign ({top_cat}){suffix}"

    significant.sort(key=lambda p: p["count"], reverse=True)
    top = significant[0]
    label = str(top.get("source") or "?")
    if top.get("event_id") is not None:
        label += f"/{top['event_id']}"
    cadence = _cadence_label(top["count"], window_days)
    cause = top.get("cause") or top.get("category") or "cause unclear"
    reason = f"{label} ×{top['count']} ({cadence}) — {cause}"
    extra = len(significant) - 1
    if extra > 0:
        reason += f", +{extra} more pattern(s)"
    return reason + suffix


def _rule_reliability_by_volume(
    events: list[dict[str, Any]], total_i: int, si: float | None
) -> "tuple[Status, str]":
    """Fallback scoring when events carry no severity annotation (see module
    comment above). Strictly at least as sensitive as the original volume-based rule.

    Operator suppression (ADR-0045 / issue #166) still applies on this path.
    Unlike the ADR-0028 LLM categorization, matching a suppression rule needs
    no LLM and no API key, so it is available here too — and this is the path
    that drives push alerting, the weekly digest, and the fleet list, which is
    exactly where a single dominant noisy pattern is loudest (see the module
    that installs the ``TelemetryStore.annotate`` hook). Suppressed volume is
    subtracted from the SCORING total only; the raw ``total_i`` used in the
    displayed reason (and ``recent_crashes`` itself) is untouched.
    """

    scored_events = [e for e in events if not e.get("suppressed")]
    suppressed_total = sum(
        int(_number(e.get("count")) or 0) for e in events if e.get("suppressed")
    )
    scored_total = max(0, total_i - suppressed_total)
    has_critical = any(e.get("level") == "critical" for e in scored_events)
    distinct = len({(e.get("source"), e.get("event_id")) for e in scored_events})

    if scored_total >= _RELIABILITY_FALLBACK_CRIT_TOTAL or (si is not None and si < _RELIABILITY_SI_CRIT):
        status: Status = "crit"
    elif (
        scored_total >= _RELIABILITY_FALLBACK_WARN_TOTAL
        or has_critical
        or distinct >= _RELIABILITY_FALLBACK_WARN_DISTINCT
        or (si is not None and si < _RELIABILITY_SI_WARN)
    ):
        status = "warn"
    else:
        status = "ok"

    return status, _reliability_reason(events, total_i)


def _rule_reliability_by_severity(
    events: list[dict[str, Any]], total_i: int, si: float | None, window_days: Any
) -> "tuple[Status, str]":
    """Weighted-pattern scoring once events carry a server-annotated severity
    (ADR-0028 read-path categorization). Distinct *patterns* drive escalation,
    not raw volume — see the module comment above.
    """

    patterns: list[dict[str, Any]] = []
    for e in events:
        severity = e.get("severity")
        if severity not in ("benign", "notable", "serious", "unknown"):
            severity = "unknown"
        suppressed = bool(e.get("suppressed"))
        if e.get("level") == "critical" and not suppressed:
            # A Windows-critical entry always counts as serious, regardless of
            # what the LLM made of the message -- unless the operator has
            # explicitly suppressed this exact pattern (ADR-0045 / issue #166),
            # in which case that explicit intent overrides the automatic
            # escalation. Without this, a suppressed Kernel-Power/41 could
            # never actually be muted.
            severity = "serious"
        patterns.append(
            {
                "source": e.get("source"),
                "event_id": e.get("event_id"),
                "count": int(_number(e.get("count")) or 0),
                "severity": severity,
                "category": e.get("category"),
                "cause": e.get("suspected_cause"),
                "suppressed": suppressed,
            }
        )

    # Suppressed patterns stay in `patterns` (so they're still counted and
    # reportable) but are excluded from every scoring list below.
    scored = [p for p in patterns if not p["suppressed"]]
    serious = [p for p in scored if p["severity"] == "serious"]
    # "unknown" is deliberately included here — never silently treated as
    # benign — so genuinely novel/unclassifiable errors keep surfacing. Only an
    # explicit operator suppression rule removes a pattern from this list.
    significant = [p for p in scored if p["severity"] != "benign"]
    worst_serious_count = max((p["count"] for p in serious), default=0)

    if (
        worst_serious_count >= _RELIABILITY_SERIOUS_RECURRENCE_CRIT
        or len(significant) >= _RELIABILITY_SIGNIFICANT_PATTERNS_CRIT
        or (si is not None and si < _RELIABILITY_SI_CRIT)
    ):
        status: Status = "crit"
    elif significant or (si is not None and si < _RELIABILITY_SI_WARN):
        status = "warn"
    else:
        status = "ok"

    return status, _reliability_pattern_reason(patterns, total_i, window_days)


def _rule_reliability(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    # `events` is the grouped Error/Critical breakdown; `stability_index` is the
    # Windows Reliability Index (0-10). Once the read path has annotated each
    # group with a `severity` (ADR-0028 categorization),
    # score on WHAT is recurring — see _rule_reliability_by_severity. Without
    # that annotation (e.g. a raw payload in a unit test) fall back to the
    # original volume-based thresholds — see _rule_reliability_by_volume.
    events_raw = payload.get("events")
    events = [e for e in events_raw if isinstance(e, dict)] if isinstance(events_raw, list) else []
    si = _number(payload.get("stability_index"))
    total = _number(payload.get("recent_crashes"))
    if events_raw is None and total is None and si is None:
        return None
    if total is None:
        total = sum(int(_number(e.get("count")) or 0) for e in events)
    total_i = int(total)

    annotated = any("severity" in e for e in events)
    if annotated:
        return _rule_reliability_by_severity(events, total_i, si, payload.get("window_days"))
    return _rule_reliability_by_volume(events, total_i, si)


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
        # `password_required is False` reflects the Windows UF_PASSWD_NOTREQD flag
        # ("a blank password is permitted"), NOT "this account has no password".
        # OEM/sysprep'd machines set it on accounts that do have a password, so we
        # only crit when the account has ALSO genuinely never had a password set
        # (`password_last_set is None`). A real password means this is a benign
        # OEM flag. Auth-probing to be certain is deliberately out of scope
        # (account-lockout risk). See ADR-0031.
        if (
            account.get("is_admin")
            and account.get("password_required") is False
            and account.get("password_last_set") is None
        ):
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
    file_history = payload.get("file_history") or {}
    onedrive = payload.get("onedrive") or {}
    # An all-null stub (e.g. the "n/a on this platform" shape a non-Windows agent
    # emits) carries no backup evidence at all — that is *absence of data*, not a
    # missing backup. Defer rather than warn against it.
    if (
        restore.get("enabled") is None
        and restore.get("latest") is None
        and file_history.get("service_state") is None
        and onedrive.get("running") is None
    ):
        return None
    latest_age = _age_days(restore.get("latest"), now=now)
    recent_restore_point = latest_age is not None and latest_age <= 30
    fh_state = str(file_history.get("service_state") or "").lower()
    onedrive_running = onedrive.get("running") is True
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


# Rules whose section is a Windows-only concept (Microsoft Defender, Windows
# Update / KB numbers, the registry reboot-pending flags, and System Restore /
# File History / OneDrive backup evidence). A non-Windows agent emits an
# "n/a on this platform" stub for these; scoring them would mislead. They are
# skipped for agents whose OS is not Windows (see ADR-0035).
WINDOWS_ONLY_SECTIONS: frozenset[str] = frozenset(
    {"defender", "win_update", "reboot_pending", "backup_status"}
)


def _is_windows(agent_os: str | None) -> bool:
    return str(agent_os or "windows").lower() == "windows"


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
    snapshot: dict[str, dict[str, Any]],
    *,
    agent_os: str = "windows",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate every section and roll up to an overall agent health.

    ``agent_os`` is the agent's OS family (``windows`` | ``linux`` | ``macos``);
    it defaults to ``windows`` so legacy/unknown agents keep their current
    behavior. For non-Windows agents the Windows-only sections
    (:data:`WINDOWS_ONLY_SECTIONS`) are skipped rather than scored against their
    ``n/a`` stubs (see ADR-0035). Portable sections (e.g. ``listening_ports``,
    ``local_accounts``) apply for every OS.

    Returns ``{"overall": status, "sections": {name: {status, summary, reason?}}}``.
    """

    now = now or datetime.now(timezone.utc)
    is_windows = _is_windows(agent_os)
    sections: dict[str, Any] = {}
    for name, payload in snapshot.items():
        if not is_windows and name in WINDOWS_ONLY_SECTIONS:
            continue
        sections[name] = evaluate_section(name, dict(payload), now=now)
    overall = worst(*(s["status"] for s in sections.values())) if sections else "ok"
    return {"overall": overall, "sections": sections}

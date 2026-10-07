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

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import hardware_catalog

Status = str  # "ok" | "posture" | "warn" | "crit"

# ``posture`` is a server-side verdict only (ADR-0058): a standing configuration
# fact -- an unencrypted system drive, a remote-access port that is meant to be
# open, an updater service that is idle by design -- that is worth listing and
# ageing but is neither new nor time-bound, so it never alarms and never rolls
# up into a host's overall status. It ranks with ``ok`` here on purpose:
# :func:`worst` maps it back to ``ok`` so a host whose only findings are
# posture is a healthy host. The wire ``Section.status`` stays
# ``ok|warn|crit`` (``protocol.Status``); an agent can never send posture.
_ORDER = {"ok": 0, "posture": 0, "warn": 1, "crit": 2}

# Bus types of media the user plugs in and out: USB drives and SD / MMC card
# readers. Neither is judged as a disk, and storage retries on them are set aside.
_REMOVABLE_BUS_TYPES = frozenset({"usb", "sd"})

# Which findings alarm: a section in one of these states is an *incident*.
INCIDENT_STATUSES: frozenset[str] = frozenset({"warn", "crit"})


def worst(*statuses: Status) -> Status:
    """Return the most severe of the given statuses (crit > warn > ok).

    ``posture`` never wins: it shares ``ok``'s rank and, because ``max`` keeps
    the first of equally-ranked candidates, is mapped to ``ok`` explicitly so a
    posture section listed first cannot leak into a roll-up.
    """

    result = max((s for s in statuses if s), key=lambda s: _ORDER.get(s, 0), default="ok")
    return "ok" if result == "posture" else result


def tier_of(status: Status) -> str:
    """``incident`` (warn/crit), ``posture``, or ``none`` (ok/unknown)."""

    if status in INCIDENT_STATUSES:
        return "incident"
    return "posture" if status == "posture" else "none"


def _valid_status(value: Any) -> Status:
    """Coerce an untrusted ``status`` value to one of ``ok``/``warn``/``crit``.

    The wire ``Section.status`` field is ``Literal["ok", "warn", "crit"]``, so a
    pushed ``telemetry`` frame can never carry anything else. But the
    ``telemetry_collect`` **request/response** round trip (an agent replying to
    a server-initiated tool call) carries its result as an unvalidated
    ``dict[str, Any]`` (``protocol.Response.result``) that is stored and later
    read the same way as a pushed snapshot -- so a compromised/buggy agent can
    make ``status`` anything JSON allows, including an unhashable list/dict.
    :func:`worst` needs a hashable known literal; treat anything else as
    ``warn`` (a malformed status is itself worth a look) rather than let it
    propagate into a `TypeError` on read.
    """

    return value if value in ("ok", "warn", "crit") else "warn"


def parse_ts(value: Any) -> datetime | None:
    """Parse a stored ISO-8601 timestamp (trailing ``Z`` or offset forms);
    ``None`` for anything else. Public so read paths can turn a snapshot's
    ``collected_at`` into the ``now`` they evaluate history "as of"."""

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_parse_ts = parse_ts


def _dicts(value: Any) -> list[dict[str, Any]]:
    """Return only the dict entries of a list-like telemetry field.

    Every list field scored below (``volumes``, ``recent``, ``sensors``,
    ``accounts``, ``ports``, ...) comes straight from an agent-reported
    telemetry section, whose extra fields are accepted as-is (``Section``
    uses ``extra="allow"``) with no shape validation. A buggy or compromised
    agent can put anything JSON allows in there -- e.g. a list of strings
    instead of objects -- and a rule must not crash the caller (the alert
    loop, or an operator's ``agent_health``/``agent_snapshot`` MCP call) just
    because one entry is not a dict. Non-dict entries are silently dropped
    rather than scored.
    """

    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else ``{}`` (see :func:`_dicts`)."""

    return value if isinstance(value, dict) else {}


def _age_days(value: Any, *, now: datetime) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


# A rule maps a section payload -> (status, reason) or None to defer.
# A rule returns ``(status, reason)`` -- or ``(status, reason, details)`` when
# it has structured evidence worth handing to consumers verbatim (per-pattern
# activity for ``reliability``): :func:`evaluate_section` copies ``details``
# into the section dict so no client has to re-derive a threshold to show it.
RuleOutcome = "tuple[Status, str] | tuple[Status, str, dict[str, Any]] | None"
Rule = Callable[[dict[str, Any], datetime], RuleOutcome]
# A rule that also needs the agent's OS. Listed in :data:`OS_AWARE_RULES` and
# called with the extra argument by :func:`evaluate_section`; the OS parameter is
# keyword-defaulted so such a rule still satisfies :data:`Rule`.
OsAwareRule = Callable[[dict[str, Any], datetime, str], RuleOutcome]


def _rule_disk(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    worst_pct = -1.0
    worst_mount = ""
    for vol in _dicts(payload.get("volumes")):
        pct = _number(vol.get("percent_used"))
        if pct is not None and pct > worst_pct:
            worst_pct = pct
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


# An update that keeps failing across days is an incident the machine cannot
# resolve on its own; a single failed attempt is ordinary Windows Update noise
# that usually clears on the next retry. Recurrence across days is the signal
# -- never the localized title (the live fleet's are German), and never the
# raw row count (the collector caps ``recent`` at 25, so a KB retrying every
# four hours fills the whole list by itself).
_WIN_UPDATE_REPEAT_ATTEMPTS_CRIT = 3
_WIN_UPDATE_REPEAT_DAYS_CRIT = 2
_WIN_UPDATE_STALE_CHECK_DAYS = 7
_WIN_UPDATE_NAMED = 2


def _rule_win_update(
    payload: dict[str, Any], now: datetime
) -> "tuple[Status, str, dict[str, Any]] | None":
    recent = _dicts(payload.get("recent"))
    by_kb: dict[str, dict[str, Any]] = {}
    for u in recent:
        if str(u.get("result", "")).lower() != "failed":
            continue
        key = str(u.get("kb") or u.get("title") or "?")
        at = _parse_ts(u.get("installed_at"))
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        row = by_kb.setdefault(
            key,
            {"kb": key, "title": str(u.get("title") or ""), "attempts": 0, "days": set(),
             "first_failed": None, "last_failed": None},
        )
        row["attempts"] += 1
        if at is not None:
            row["days"].add(at.date().isoformat())
            if row["first_failed"] is None or at < row["first_failed"]:
                row["first_failed"] = at
            if row["last_failed"] is None or at > row["last_failed"]:
                row["last_failed"] = at

    failed = sorted(by_kb.values(), key=lambda r: (-r["attempts"], r["kb"]))
    details = {
        "failed": [
            {
                "kb": r["kb"],
                "title": r["title"],
                "attempts": r["attempts"],
                "days": len(r["days"]),
                "first_failed": r["first_failed"].isoformat() if r["first_failed"] else None,
                "last_failed": r["last_failed"].isoformat() if r["last_failed"] else None,
            }
            for r in failed
        ]
    }
    repeating = [
        r
        for r in failed
        if r["attempts"] >= _WIN_UPDATE_REPEAT_ATTEMPTS_CRIT
        and len(r["days"]) >= _WIN_UPDATE_REPEAT_DAYS_CRIT
    ]

    last_check = _parse_ts(payload.get("last_check"))
    if last_check is not None and last_check.tzinfo is None:
        last_check = last_check.replace(tzinfo=timezone.utc)
    stale_days = (now - last_check).days if last_check is not None else None

    def _describe(r: dict[str, Any]) -> str:
        text = f"{r['kb']} failed {r['attempts']}×"
        if r["first_failed"]:
            text += f" since {r['first_failed'].date().isoformat()}"
        if r["last_failed"]:
            hours = max(0.0, (now - r["last_failed"]).total_seconds() / 3600)
            text += f" (last {_age_label(hours)} ago)"
        return text

    if failed:
        named = (repeating or failed)[:_WIN_UPDATE_NAMED]
        reason = ", ".join(_describe(r) for r in named)
        extra = len(failed) - len(named)
        if extra > 0:
            reason += f", +{extra} more"
        status: Status = "crit" if repeating else "warn"
        return status, reason, details
    if stale_days is not None and stale_days >= _WIN_UPDATE_STALE_CHECK_DAYS:
        return "warn", f"no update check for {stale_days}d", details
    return "ok", "Updates healthy", details


def _rule_reboot_pending(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    if payload.get("pending") is True:
        # A truthy non-list (e.g. a string) would otherwise iterate char-by-char below.
        reasons = payload.get("reasons")
        reasons = reasons if isinstance(reasons, list) else []
        why = ", ".join(str(r) for r in reasons) if reasons else "unknown"
        return "warn", f"Reboot pending ({why})"
    return "ok", "No reboot pending"


def _rule_battery(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    health = _number(payload.get("health_percent"))
    if health is not None:
        if health < 50:
            return "crit", f"Battery health {health:.0f}% (<50%)"
        if health < 70:
            return "warn", f"Battery health {health:.0f}% (<70%)"
    return None


def _rule_memory(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    pct = _number(payload.get("percent_used"))
    if pct is not None:
        if pct > 95:
            return "crit", f"Memory {pct:.0f}% used (>95%)"
        if pct > 85:
            return "warn", f"Memory {pct:.0f}% used (>85%)"
        return "ok", f"Memory {pct:.0f}% used"
    return None


def _rule_thermals(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    sensors = _dicts(payload.get("sensors"))
    temps = [t for s in sensors if (t := _number(s.get("temperature_c"))) is not None]
    if not temps:
        return None  # no sensors reported -> defer to agent status
    hottest = max(temps)
    if hottest >= 95:
        return "crit", f"Hottest sensor {hottest:.0f}°C (>=95°C)"
    if hottest >= 85:
        return "warn", f"Hottest sensor {hottest:.0f}°C (>=85°C)"
    return "ok", f"Hottest {hottest:.0f}°C"


# =============================================================================
# disk_smart  (plan Step 1)
# -----------------------------------------------------------------------------
# The server's SMART / NVMe health judgement. Constants and helpers private to
# this rule belong in this block, between this banner and the next one.
# Each rule below owns its block; do not edit another rule's block.
# =============================================================================


# Keys that only a 0.22+ agent puts on a ``disk_smart`` row. A payload where no
# row carries any of them has the pre-0.22 shape, and the rule defers.
_SMART_NEW_ROW_KEYS = ("nvme", "smart_attributes", "bus_type")
# NVMe ``critical_warning`` bits. Bit 1 (temperature) alone is a warn; the rest
# mean the drive itself says it can no longer be trusted with data.
_NVME_CRIT_BITS: tuple[tuple[int, str], ...] = (
    (0, "has used up its spare capacity"),
    (2, "reports that its reliability is degraded"),
    (3, "has switched to read-only to protect its data"),
    (4, "reports that its power-loss memory backup has failed"),
)
_NVME_TEMPERATURE_BIT = 1
# ``percentage_used`` at or above this is a standing fact (the drive is near
# its rated end of life), not an event.
_SMART_ENDURANCE_POSTURE_PCT = 90
# Lifetime ATA counters that mean something on a spinning disk. SSD vendors
# scale and use these attributes differently, so they are not judged there.
_SMART_HDD_LIFETIME_ATTRS: tuple[tuple[str, str], ...] = (
    ("5", "has reallocated damaged sectors in its lifetime"),
    ("187", "has recorded unrecoverable read errors in its lifetime"),
    ("198", "has recorded unrecoverable read errors in its lifetime"),
)
_SMART_PENDING_SECTORS_SYMPTOM = "has sectors waiting to be reallocated"
_SMART_NVME_ERROR_MAX = 80
_SMART_RANK = {"ok": 0, "posture": 1, "warn": 2, "crit": 3}
_SMART_REASON_MAX_FINDINGS = 3
_SMART_DETAILS_MAX_FINDINGS = 20


def _smart_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _smart_positive(value: Any) -> bool:
    n = _number(value)
    return n is not None and n > 0


def _smart_disk_findings(row: dict[str, Any]) -> list[tuple[Status, str]]:
    """``(status, symptom)`` findings for one internal disk, most severe first."""

    crit: list[str] = []
    warn: list[str] = []
    posture: list[str] = []

    def add(bucket: list[str], symptom: str) -> None:
        if symptom not in crit and symptom not in warn and symptom not in posture:
            bucket.append(symptom)

    # A paused row (anti-cheat coexistence) only lacks the raw NVMe health log;
    # the WMI-sourced SMART flag, the attribute table, the OS health status and
    # the reliability counters are read as always and are judged as always.
    paused = row.get("paused") is True
    health = (_smart_text(row.get("health_status")) or "").casefold()
    nvme = {} if paused else _as_dict(row.get("nvme"))
    attrs = _as_dict(row.get("smart_attributes"))
    is_hdd = _smart_text(row.get("media_type")) == "HDD"

    if row.get("predictive_failure") is True:
        add(crit, "reports that it is failing")
    if health == "unhealthy":
        add(crit, "reports that it is failing")
    elif health == "warning":
        add(warn, "reports a health warning")

    warning_bits = _number(nvme.get("critical_warning"))
    bits = int(warning_bits) if warning_bits is not None and warning_bits >= 0 else 0
    for bit, symptom in _NVME_CRIT_BITS:
        if bits >> bit & 1:
            add(crit, symptom)
    if bits >> _NVME_TEMPERATURE_BIT & 1:
        add(warn, "reports a temperature warning")
    if _smart_positive(attrs.get("197")):
        add(warn, _SMART_PENDING_SECTORS_SYMPTOM)

    if _smart_positive(nvme.get("media_errors")) or _smart_positive(
        row.get("read_errors_uncorrected")
    ):
        add(posture, "has recorded unrecoverable read errors in its lifetime")
    if _smart_positive(row.get("write_errors_uncorrected")):
        add(posture, "has recorded unrecoverable write errors in its lifetime")
    if is_hdd:
        for attr, symptom in _SMART_HDD_LIFETIME_ATTRS:
            if _smart_positive(attrs.get(attr)):
                add(posture, symptom)
    used = _number(nvme.get("percentage_used"))
    if used is not None and used >= _SMART_ENDURANCE_POSTURE_PCT:
        add(posture, f"is at {used:.0f}% of its rated write endurance")

    # An NVMe disk whose health log could not be read is unjudged, never healthy.
    nvme_error = _smart_text(row.get("nvme_error"))
    if (
        not paused
        and nvme_error
        and _smart_text(row.get("bus_type")) == "NVMe"
        and not isinstance(row.get("nvme"), dict)
    ):
        add(
            posture,
            f"health log could not be read ({nvme_error[:_SMART_NVME_ERROR_MAX]})",
        )

    return (
        [("crit", s) for s in crit]
        + [("warn", s) for s in warn]
        + [("posture", s) for s in posture]
    )


def _rule_disk_smart(
    payload: dict[str, Any], now: datetime
) -> "tuple[Status, str] | tuple[Status, str, dict[str, Any]] | None":
    """Judge ``disk_smart``: the drive's own failure signals, per internal disk.

    Reports crit for ``predictive_failure``, ``health_status`` Unhealthy or
    NVMe ``critical_warning`` bits 0/2/3/4; warn for ``health_status`` Warning,
    the temperature bit or SMART 197 above zero; and posture for non-zero
    lifetime media / uncorrected counters (SMART 5/187/198 on HDDs only) and
    ``percentage_used`` >= 90, and for an NVMe disk whose health log could not be
    read (``nvme`` null with a ``nvme_error``). Removable, USB and SD disks are
    excluded. A paused row (anti-cheat coexistence) lacks only the NVMe health
    log; its ``predictive_failure``, ``smart_attributes`` and ``health_status``
    are judged like any other row's. Defers (``None``) when no row carries any of the
    0.22 keys (``nvme``, ``smart_attributes``, ``bus_type``) so an old agent's
    own grade stands. Whether a counter is *rising* is the trend layer's job.

    Worst-of across disks; the reason joins up to three findings, most severe
    first, in symptoms (ADR-0065). ``details`` is ``{"disks": [{model, serial,
    status, symptom}, ...]}`` -- a dict, because :func:`evaluate_section` only
    carries dict details.
    """

    rows = _dicts(payload.get("disks"))
    if not rows or not any(k in row for row in rows for k in _SMART_NEW_ROW_KEYS):
        return None

    findings: list[tuple[Status, str, str, str | None, str]] = []
    judged = 0
    for row in rows:
        bus = (_smart_text(row.get("bus_type")) or "").casefold()
        if row.get("removable") is True or bus in _REMOVABLE_BUS_TYPES:
            continue
        judged += 1
        model = (_smart_text(row.get("model")) or "(unknown model)")[:80]
        serial = _smart_text(row.get("serial"))
        for status, symptom in _smart_disk_findings(row):
            findings.append((status, f"Disk {model} {symptom}", model, serial, symptom))
    if not judged:
        return "ok", "No internal disks to judge"
    if not findings:
        return "ok", f"SMART healthy on {judged} disk(s)"

    findings.sort(key=lambda f: -_SMART_RANK[f[0]])  # stable: disk order kept
    status = findings[0][0]
    shown = [f[1] for f in findings[:_SMART_REASON_MAX_FINDINGS]]
    reason = "; ".join(shown)
    if len(findings) > len(shown):
        reason += f" (+{len(findings) - len(shown)} more)"
    details = {
        "disks": [
            {"model": model, "serial": serial, "status": st, "symptom": symptom}
            for st, _, model, serial, symptom in findings[:_SMART_DETAILS_MAX_FINDINGS]
        ]
    }
    return status, reason, details


# =============================================================================
# hardware_errors  (plan Step 2)
# -----------------------------------------------------------------------------
# Component-attributed hardware events. Attribution and severity classes come
# from ``hardware_catalog``; per-group activity from :func:`group_activity`.
# Constants and helpers private to this rule belong in this block.
# =============================================================================

# A graphics-driver reset (a recovered TDR) is only a finding once it has
# happened on this many distinct days of the window; the same count of days is
# what separates "the driver has a bad week" from one bad afternoon.
_HWE_GPU_MIN_DAYS = 3
# Crash diversity -- many unrelated programs dying of memory-access / illegal-
# instruction faults -- suggests unstable RAM or CPU rather than one buggy
# program. Both bars must clear, and a hardware symptom must corroborate.
_HWE_DIVERSITY_MIN_APPS = 4
_HWE_DIVERSITY_MIN_CRASHES = 5
# The reason names at most this many findings, most severe first.
_HWE_NAMED_FINDINGS = 3

_HWE_RANK = {"crit": 2, "warn": 1, "posture": 0}

# Providers whose groups are the machine going down (Kernel-Power 41, bugcheck
# records). They attribute and corroborate; they never escalate on their own,
# because ``reliability`` already scores the crash itself (no double alarm).
_HWE_CRASH_PROVIDERS = frozenset({"kernel-power", "bugcheck", "wer-systemerrorreporting"})
# The Windows providers that report a recovered graphics-driver reset.
_HWE_GPU_RESET_PROVIDERS = frozenset({"display", "nvlddmkm"})

# Who the symptom is about, per component (ADR-0065: name the part, not the
# event).
_HWE_SUBJECT = {
    "cpu": "The processor",
    "memory": "The memory",
    "pcie": "A PCIe link",
    "gpu": "The graphics card",
    "storage": "A disk",
    "power": "The power supply",
    "platform": "The processor interconnect",
}
# The components whose errors make a crash-diversity pattern credible, and how
# the reason names them.
_HWE_DIVERSITY_NOUN = {"cpu": "processor", "memory": "memory", "platform": "processor interconnect"}


def _hwe_span(count: int, days: int) -> str:
    """``"6 times over 4 days"`` -- how often and how widely, in words."""

    times = f"{count} times" if count != 1 else "once"
    if days <= 1:
        return f"{times} in one day"
    return f"{times} over {days} days"


def _hwe_finding(
    component: str,
    severity: str,
    status: Status,
    symptom: str,
    source: str,
    event_id: Any = None,
    act: dict[str, Any] | None = None,
) -> dict[str, Any]:
    age = act["age_hours"] if act else None
    return {
        "component": component,
        "severity": severity,
        "status": status,
        "symptom": symptom,
        "source": source,
        "event_id": event_id if isinstance(event_id, (int, str)) else None,
        "active_days": act["active_days"] if act else None,
        "last_seen_age_hours": round(age, 1) if age is not None else None,
    }


def _hwe_internal_share(details: Any) -> float:
    """The share of a storage group's events that are *not* on a USB or SD disk.

    ``details.disk_bus_type`` says which bus each sampled event's disk is on.
    Retries on a USB drive or an SD / MMC card reader are the user pulling a
    plug, not a failing internal disk. No usable bus information (or
    ``Unknown``) is judged as internal: silence about the bus is not evidence
    of removable media.
    """

    counts = hardware_catalog.detail_counts(details, "disk_bus_type")
    total = sum(counts.values())
    if total <= 0:
        return 1.0
    removable = sum(
        n for bus, n in counts.items() if bus.strip().lower() in _REMOVABLE_BUS_TYPES
    )
    return (total - removable) / total


def _hwe_diversity(payload: dict[str, Any], now: datetime) -> bool:
    """True when ``app_crashes`` shows crashes across many programs, in the
    exception classes that point at hardware (access violation, illegal
    instruction), and still going on."""

    crashes = payload.get("app_crashes")
    if not isinstance(crashes, dict):
        return False
    if (_number(crashes.get("distinct_apps")) or 0) < _HWE_DIVERSITY_MIN_APPS:
        return False
    codes = crashes.get("exception_codes")
    hits = 0.0
    if isinstance(codes, dict):
        for code, n in codes.items():
            value = _number(n)
            if value and str(code).strip().lower() in hardware_catalog.CRASH_EXCEPTION_CODES:
                hits += max(value, 0.0)
    if hits < _HWE_DIVERSITY_MIN_CRASHES:
        return False
    days = sorted(_reliability_by_day(crashes.get("by_day")))
    if days:  # a crash storm that ended days ago is history, not instability
        age = _reliability_day_age_hours(days[-1], now)
        if age is None or age > _RELIABILITY_ACTIVE_MIN_DAYS_MAX_AGE_HOURS:
            return False
    return True


def _hwe_better(new: dict[str, Any], old: dict[str, Any]) -> bool:
    def key(f: dict[str, Any]) -> tuple[int, int]:
        return (_HWE_RANK[f["status"]], f["active_days"] or 0)

    return key(new) > key(old)


def _rule_hardware_errors(
    payload: dict[str, Any], now: datetime
) -> "tuple[Status, str, dict[str, Any]] | None":
    """Judge ``hardware_errors``: "risk rising, component X, symptom Y".

    Escalates only on uncorrected hardware errors and Level-3 precursors, so it
    never double-alarms with ``reliability``: bugcheck / Kernel-Power groups
    attribute and corroborate but never escalate on their own.

    - crit: an uncorrected-error group (WHEA 18, a hardware Xid) that is active
      and recurring; an active failed memory test; EDAC uncorrected counts; PCIe
      uncorrected errors while such a group is also active.
    - warn: one active uncorrected-error group; repeated graphics-driver resets
      with corroboration (a GPU bugcheck or a hardware Xid); active, recurring
      retries on internal disks (USB-only groups are set aside); crash diversity
      alongside a hardware symptom; PCIe uncorrected errors on their own.
    - posture: active corrected-error groups, a stale uncorrected one, graphics
      resets without corroboration. Whether a corrected rate is *rising* is the
      trend layer's job.

    Reasons are symptoms (ADR-0065), never event ids. Defers (``None``) only
    when the payload has none of ``groups`` / ``edac`` / ``aer`` (an old or
    broken agent). Malformed entries are skipped, never raised on.
    """

    if not any(k in payload for k in ("groups", "edac", "aer")):
        return None

    findings: dict[tuple[str, str], dict[str, Any]] = {}

    def add(topic: str, finding: dict[str, Any]) -> None:
        key = (finding["component"], topic)
        old = findings.get(key)
        if old is None or _hwe_better(finding, old):
            findings[key] = finding

    fatal_active = False  # any uncorrected-error group still going on
    gpu_bugcheck = False  # a GPU-strong bugcheck in a crash group
    memory_bugcheck = False  # a memory-class bugcheck in a crash group
    hardware_xid = False
    # (rank, phrase) of active corrected / uncorrected groups on cpu / memory / platform
    diversity_support: list[tuple[int, str]] = []
    gpu_resets: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for g in _dicts(payload.get("groups")):
        source, event_id, details = g.get("source"), g.get("event_id"), g.get("details")
        component = hardware_catalog.component_for(source, event_id, details)
        klass = hardware_catalog.severity_for(source, event_id, details)
        if component is None or klass is None:
            continue
        provider = hardware_catalog.canonical_provider(source)
        src = str(source)[:80]
        act = group_activity(g, now)
        count, days, active = act["count"], act["active_days"], act["active"]
        if hardware_catalog.hardware_xid_count(details) > 0:
            hardware_xid = True

        if klass == hardware_catalog.SUPPORTING:
            if provider in _HWE_CRASH_PROVIDERS:
                hit = hardware_catalog.bugcheck_attribution(details)
                if hit == ("gpu", hardware_catalog.STRONG):
                    gpu_bugcheck = True
                if hit is not None and hit[0] == "memory":
                    memory_bugcheck = True
            continue

        subject = _HWE_SUBJECT.get(component, "The hardware")
        noun = _HWE_DIVERSITY_NOUN.get(component)
        if klass == hardware_catalog.FATAL:
            memtest = provider == "memorydiagnostics-results"
            symptom = (
                "The memory test found errors"
                if memtest
                else f"{subject} reported uncorrectable hardware errors"
            )
            status: Status
            if active and (memtest or act["recurring"]):
                # One failed memory test is a definitive result; anything else
                # has to repeat before it is called critical.
                status = "crit"
            elif active:
                status = "warn"
            else:
                status = "posture"
                symptom += " (not seen recently)"
            if status != "posture":
                fatal_active = True
            if count > 1 and not memtest:
                symptom += f" ({_hwe_span(count, days)})"
            add(
                "memtest" if memtest else "uncorrected",
                _hwe_finding(component, klass, status, symptom, src, event_id, act),
            )
            if active and noun:
                diversity_support.append((2, f"uncorrectable {noun} errors"))
        elif klass == hardware_catalog.CORRECTED:
            if not active:
                continue
            symptom = f"{subject} corrects hardware errors (standing)"
            add("corrected", _hwe_finding(component, klass, "posture", symptom, src, event_id, act))
            if noun:
                diversity_support.append((1, f"corrected {noun} errors"))
        elif provider in _HWE_GPU_RESET_PROVIDERS:
            if active and days >= _HWE_GPU_MIN_DAYS:
                gpu_resets.append((g, act))
        elif component == "storage":
            share = _hwe_internal_share(details)
            if (
                active
                and act["recurring"]
                and count * share >= _RELIABILITY_RECURRING_MIN_COUNT
            ):
                symptom = f"A disk keeps retrying reads and writes ({_hwe_span(count, days)})"
                add("storage", _hwe_finding(component, klass, "warn", symptom, src, event_id, act))
        elif active:
            # Linux journal lines that name a part without saying whether the
            # error was corrected (machine-check banner, amdgpu RAS).
            symptom = f"{subject} reports hardware errors (standing)"
            add("reports", _hwe_finding(component, klass, "posture", symptom, src, event_id, act))

    for g, act in gpu_resets:
        symptom = (
            "The graphics driver crashed and recovered "
            f"{_hwe_span(act['count'], act['active_days'])}"
        )
        status = "warn" if (gpu_bugcheck or hardware_xid) else "posture"
        add(
            "gpu_reset",
            _hwe_finding(
                "gpu",
                hardware_catalog.INSTABILITY,
                status,
                symptom,
                str(g.get("source"))[:80],
                g.get("event_id"),
                act,
            ),
        )

    for entry in _dicts(payload.get("edac")):
        klass = hardware_catalog.edac_severity(entry)
        if klass == hardware_catalog.FATAL:
            fatal_active = True
            symptom = "The memory reported uncorrectable hardware errors"
            add("uncorrected", _hwe_finding("memory", klass, "crit", symptom, "edac"))
        elif klass == hardware_catalog.CORRECTED:
            symptom = "The memory corrects hardware errors (standing)"
            add("corrected", _hwe_finding("memory", klass, "posture", symptom, "edac"))

    for entry in _dicts(payload.get("aer")):
        klass = hardware_catalog.aer_severity(entry)
        if klass == hardware_catalog.FATAL:
            # Uncorrected PCIe errors are common on a flaky link; they only
            # reach crit alongside an uncorrected-error group that is active.
            symptom = "A PCIe link reported uncorrectable hardware errors"
            status = "crit" if fatal_active else "warn"
            add("uncorrected", _hwe_finding("pcie", klass, status, symptom, "aer"))
        elif klass == hardware_catalog.CORRECTED:
            symptom = "A PCIe link corrects hardware errors (standing)"
            add("corrected", _hwe_finding("pcie", klass, "posture", symptom, "aer"))

    if memory_bugcheck:
        diversity_support.append((0, "memory-related system crashes"))
    if diversity_support and _hwe_diversity(payload, now):
        phrase = max(diversity_support)[1]
        symptom = (
            f"Programs crash across the board alongside {phrase} "
            "— possible RAM or CPU instability"
        )
        add(
            "diversity",
            _hwe_finding(
                "platform", hardware_catalog.INSTABILITY, "warn", symptom, "app_crashes", 1000
            ),
        )

    ordered = sorted(
        findings.values(),
        key=lambda f: (-_HWE_RANK[f["status"]], -(f["active_days"] or 0), f["component"]),
    )
    window = int(_number(payload.get("window_days")) or 14)
    if not ordered:
        effective = _number(payload.get("effective_window_days"))
        if effective is not None and 0 < effective < window:
            reason = f"no hardware faults in the {int(effective)}d the event log covers"
        else:
            reason = f"no hardware faults in {window}d"
        return "ok", reason, {"findings": []}

    reason = "; ".join(f["symptom"] for f in ordered[:_HWE_NAMED_FINDINGS])
    extra = len(ordered) - _HWE_NAMED_FINDINGS
    if extra > 0:
        reason += f"; +{extra} more"
    return ordered[0]["status"], reason, {"findings": ordered}


# =============================================================================
# gpu  (plan Step 3)
# -----------------------------------------------------------------------------
# Raw GPU health facts (ECC, retired pages, clock-event reasons). Constants and
# helpers private to this rule belong in this block.
# =============================================================================


# At most this many findings are spelled out in the one-line reason; the rest
# are counted. Every finding stays in ``details``.
_GPU_REASON_NAMED = 3
_GPU_SEVERITY_RANK = {"crit": 0, "warn": 1, "posture": 2}


def _gpu_name(gpu: dict[str, Any]) -> str:
    name = gpu.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else "unnamed graphics card"


def _gpu_findings(gpu: dict[str, Any]) -> list[tuple[Status, str]]:
    """``(status, symptom)`` pairs for one adapter.

    The symptom completes the sentence "The graphics card <name> ..." and says
    what the card is doing, not which counter tripped (ADR-0065).
    """

    found: list[tuple[Status, str]] = []
    ecc = _as_dict(gpu.get("ecc"))
    ras = [_as_dict(block) for block in _as_dict(gpu.get("ras")).values()]
    remap = _as_dict(ecc.get("remapped_rows"))
    throttle = _as_dict(gpu.get("throttle"))

    uncorrected = _number(ecc.get("uncorrected_volatile")) or 0
    if uncorrected > 0 or any((_number(b.get("ue")) or 0) > 0 for b in ras):
        found.append(("crit", "reported uncorrectable memory errors"))
    if remap.get("failure") is True:
        found.append(("crit", "can no longer repair its failing memory"))
    if ecc.get("retired_pages_pending") is True:
        found.append(("crit", "has failing memory waiting to be taken out of use"))

    if throttle.get("hw_power_brake_slowdown") is True:
        found.append(("warn", "is being slowed by a power-delivery signal"))
    if throttle.get("hw_slowdown") is True and throttle.get("hw_thermal_slowdown") is not True:
        found.append(("warn", "is being slowed down by the hardware"))

    if throttle.get("hw_thermal_slowdown") is True:
        found.append(("posture", "is slowing down to stay cool"))
    if any((_number(b.get("ce")) or 0) > 0 for b in ras):
        found.append(("posture", "reported corrected memory errors"))
    if (_number(remap.get("correctable")) or 0) > 0:
        found.append(("posture", "has had memory rows repaired"))
    return found


def _rule_gpu(
    payload: dict[str, Any], now: datetime
) -> "tuple[Status, str, dict[str, Any]] | None":
    """Judge ``gpu``: a failing card or a starved one.

    Reports crit for uncorrected ECC / RAS errors above zero, a row-remap
    failure or pending retired pages; warn for an active
    ``hw_power_brake_slowdown`` (a PSU or cable signal) or a ``hw_slowdown``
    that is not thermal; posture for ``hw_thermal_slowdown`` (cooling-limited)
    and corrected-error counters. PCIe link width is judged against the
    device's own history by the trend layer, never against ``width_max`` here,
    and ``fan_target_percent`` is a target, not a measurement. The verdict is
    the worst across adapters; ``details["findings"]`` lists every finding as
    ``{name, status, symptom}``. Defers when no GPU is reported.
    """

    gpus = _dicts(payload.get("gpus"))
    if not gpus:
        return None
    findings = [
        {"name": _gpu_name(gpu), "status": status, "symptom": symptom}
        for gpu in gpus
        for status, symptom in _gpu_findings(gpu)
    ]
    if not findings:
        return "ok", "Graphics healthy", {"findings": []}
    findings.sort(key=lambda f: _GPU_SEVERITY_RANK[f["status"]])  # stable
    named = findings[:_GPU_REASON_NAMED]
    reason = "; ".join(f"The graphics card {f['name']} {f['symptom']}" for f in named)
    if len(findings) > len(named):
        reason += f"; +{len(findings) - len(named)} more"
    return findings[0]["status"], reason, {"findings": findings}


# =============================================================================
# fans  (plan Step 4)
# -----------------------------------------------------------------------------
# Measured fan speeds in a short burst. Constants and helpers private to this
# rule belong in this block.
# =============================================================================


# A fan the board drives at or above this duty (%) is being asked to spin; a
# fan-stop / zero-RPM mode commands ~0 %, so it never reads as a stall.
_FAN_STALL_MIN_DUTY = 30.0
# Jitter needs a real burst of running samples and a fan fast enough that a few
# RPM of tachometer quantisation is not a large fraction of the mean.
_FAN_JITTER_MIN_SAMPLES = 4
_FAN_JITTER_MIN_MEAN_RPM = 300.0
_FAN_JITTER_CV = 0.15
# Defensive bounds on an unvalidated payload (the contract caps a section at 16
# fans of 5 samples): anything beyond is ignored, and a reading above the
# ceiling is malformed rather than a speed.
_FAN_MAX_FANS = 32
_FAN_MAX_SAMPLES = 64
_FAN_MAX_RPM = 1_000_000.0
_FAN_LABEL_MAX = 64


def _fan_samples(value: Any) -> list[float] | None:
    """The burst as non-negative numbers, or ``None`` when it is unusable.

    One bad entry (a string, ``null``, a negative or absurd reading) makes the
    whole burst unusable: judging the remainder could invent a stall or a
    jitter from a partial read.
    """

    if not isinstance(value, list) or not value:
        return None
    samples: list[float] = []
    for raw in value[:_FAN_MAX_SAMPLES]:
        rpm = _number(raw)
        if rpm is None or rpm < 0 or rpm > _FAN_MAX_RPM:
            return None
        samples.append(rpm)
    return samples


def _fan_name(fan: dict[str, Any]) -> str:
    """The board's label for the fan, falling back to its ``key``."""

    for field in ("label", "key"):
        raw = fan.get(field)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:_FAN_LABEL_MAX]
    return "unnamed fan"


def _judge_fan(fan: dict[str, Any]) -> dict[str, Any]:
    """One fan's verdict: ``{key, label, status, symptom, mean_rpm, cv}``."""

    key = fan.get("key")
    name = _fan_name(fan)
    verdict: dict[str, Any] = {
        "key": key if isinstance(key, str) else name,
        "label": name,
        "status": "ok",
        "symptom": None,
        "mean_rpm": None,
        "cv": None,
    }
    samples = _fan_samples(fan.get("rpm_samples"))
    if samples is None:
        return verdict
    verdict["mean_rpm"] = round(math.fsum(samples) / len(samples), 1)
    duty = _number(fan.get("duty_percent"))
    if all(rpm == 0 for rpm in samples):
        if duty is not None and duty >= _FAN_STALL_MIN_DUTY:
            verdict["status"] = "warn"
            verdict["symptom"] = (
                f"Fan {name} has stopped although the board is driving it at {duty:.0f}%"
            )
        return verdict
    running = [rpm for rpm in samples if rpm > 0]
    if len(running) < _FAN_JITTER_MIN_SAMPLES:
        return verdict
    mean = math.fsum(running) / len(running)
    if mean < _FAN_JITTER_MIN_MEAN_RPM:
        return verdict
    # Population stdev: the burst is the whole population we judge, and the
    # smaller estimate errs toward not alarming on a short burst. The duty is a
    # single value per snapshot, so it is constant by construction.
    cv = math.sqrt(math.fsum((rpm - mean) ** 2 for rpm in running) / len(running)) / mean
    verdict["mean_rpm"] = round(mean, 1)
    verdict["cv"] = round(cv, 3)
    if cv > _FAN_JITTER_CV:
        verdict["status"] = "warn"
        verdict["symptom"] = (
            f"Fan {name} speed is unstable (\u00b1{cv * 100:.0f}%) at a constant "
            "setting \u2014 possible bearing wear"
        )
    return verdict


def _rule_fans(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    """Judge ``fans``: a stalled or unstable fan.

    Warn for a stall (every sample 0 RPM while the commanded duty is at least
    30 %) and for jitter (at least 4 running samples, mean >= 300 RPM, a
    coefficient of variation above 15 % at the snapshot's single duty). Fans
    flagged ``idle_or_absent`` are skipped, and a zero-RPM mode (duty ~0 or
    unreadable) is not a stall. Drift against a fan's own baseline is the trend
    layer's job. Defers when no fan is left to judge. Malformed fans or samples
    are never scored.
    """

    fans = [
        f
        for f in _dicts(payload.get("fans"))[:_FAN_MAX_FANS]
        if f.get("idle_or_absent") is not True
    ]
    if not fans:
        return None
    verdicts = [_judge_fan(f) for f in fans]
    findings = [v for v in verdicts if v["status"] == "warn"]
    details = {"fans": verdicts}
    if findings:
        return "warn", "; ".join(v["symptom"] for v in findings), details
    noun = "fan" if len(verdicts) == 1 else "fans"
    return "ok", f"{len(verdicts)} {noun} spinning normally", details


# =============================================================================
# os_support
# =============================================================================

# The microcode finding is worded as a symptom (ADR-0065): the reader needs to
# know what to do, not which advisory it came from.
_CPU_MICROCODE_REASON = "The CPU needs a BIOS/microcode update to prevent permanent damage"


def _os_support_eol(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
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


def _os_support_microcode(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    if hardware_catalog.raptor_lake_needs_microcode(payload.get("cpu") or {}):
        return "warn", _CPU_MICROCODE_REASON
    return None


def _rule_os_support(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    """Worst of the OS and CPU checks, with every finding's reason joined.

    ``os_support`` carries the OS edition and the host's CPU identity, so a
    finding about either lands here (a rule sees only its own section).
    """

    findings = [
        f
        for check in (_os_support_eol, _os_support_microcode)
        if (f := check(payload, now)) is not None
    ]
    if not findings:
        return None
    findings.sort(key=lambda f: -_ORDER[f[0]])  # stable: ties keep check order
    return findings[0][0], "; ".join(reason for _, reason in findings)


def _number(value: Any) -> float | None:
    """Coerce a JSON number to float, rejecting bools, non-numerics, and anything
    that can't survive being turned into a real ``float`` (an oversized int, or a
    non-finite float).

    Every caller reads this straight off an unvalidated ``telemetry_collect``
    field (same threat model as :func:`_dicts`/:func:`_valid_status`) and then
    either compares it or formats it with ``:.0f``/feeds it to ``int()``. JSON
    allows two shapes that break that unguarded: an int with far more digits
    than a float can represent (``float()`` raises ``OverflowError`` -- e.g. a
    300-digit ``percent_used``) and Python's ``json`` module's ``Infinity``/
    ``-Infinity``/``NaN`` decode extension (formats fine but is never a usable
    reading). Both come back as None, the same "field absent/unusable" path a
    missing field already took, rather than reaching a caller's comparison,
    ``:.0f`` format, or ``int()`` cast and crashing there.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


# -- reliability: scored on user-visible impact ------------------------------
#
# A finding here is something the person at the machine would have noticed.
# Nothing else is.
#
# The Windows Error/Critical log is not a record of user-visible problems; it
# is a record of internal component failures, the overwhelming majority of
# which Windows tolerates, retries or recovers from. CAPI2/4176, DCOM/10010,
# DeviceAssociationService/3503 and the VSS complaints emitted on the way
# through a shutdown have no human-visible counterpart at all. Treating every
# entry as a problem candidate and then scoring it down -- by volume, then by
# severity, then by activity -- produced well-sorted false alarms, because no
# amount of scoring recovers a true finding from a false premise. So the
# premise is inverted: only ``user_impact`` (event_categories.py, asked of the
# classifier in the operator's terms, not the log's) promotes a pattern to a
# finding, and everything else is diagnostic context that colours nothing.
#
# What remains to decide is *how bad* and *is it over*, and for that the
# evidence the agent already sends is enough: ``by_day``, ``last_seen``,
# ``boot_sessions`` and the group's count.
#
# Note what changes about counts. ADR-0041 and ADR-0058 rejected count
# thresholds because a count could not tell "3439 identical harmless lines"
# from "3439 individually relevant errors". Applied to a pattern that has
# *already* been established to have a user-visible impact, that ambiguity is
# gone: two unexpected restarts are two unexpected restarts. A count is the
# direct measure of "did this happen more than once", so recurrence reads it
# rather than approximating it from distinct days alone.

# A pattern is *active* if it was seen within this many hours of ``now`` ...
_RELIABILITY_ACTIVE_WITHIN_HOURS = 48
# ... or on at least this many distinct days, provided its last hit is not
# itself stale (below). The day arm exists so a pattern that fires most days
# stays active even when the latest push lands in a lull -- not so one that
# stopped days ago keeps a host lit for the rest of the window.
_RELIABILITY_ACTIVE_MIN_DAYS = 3
_RELIABILITY_ACTIVE_MIN_DAYS_MAX_AGE_HOURS = 72
# A pattern is *recurring* once it has happened more than once, or on more
# than one day.
_RELIABILITY_RECURRING_MIN_COUNT = 2
_RELIABILITY_RECURRING_MIN_DAYS = 2
# A pattern is a *burst* when one day holds at least this share of its total
# and it is not active any more -- the shape of a reboot storm or a single
# bad afternoon, as opposed to a standing problem. Presentation only.
_RELIABILITY_BURST_SHARE = 0.8
# How many scoring patterns the reason names before folding the rest.
_RELIABILITY_NAMED_PATTERNS = 3

# The Windows Reliability Index (0-10) is an independent, agent-computed
# signal that pattern scoring can't see into, so it always applies on top. It
# is deliberately NOT suppressible -- an operator muting a noisy event pattern
# must never be able to hide a genuinely unstable machine (issue #166 /
# ADR-0041). When it is what decided, the reason says so by name: a status
# whose reason lists nothing the reader can act on is indistinguishable from
# a bug.
_RELIABILITY_SI_CRIT = 3
_RELIABILITY_SI_WARN = 6

_RELIABILITY_SEVERITIES = ("benign", "notable", "serious", "unknown")

# Impacts, least to most consequential; index is the comparison order.
_RELIABILITY_IMPACTS = ("none", "degraded", "crashed", "data_at_risk")
_RELIABILITY_IMPACT_UNKNOWN = "unknown"
# The impacts that make a pattern a finding at all.
_RELIABILITY_SCORING_IMPACTS = frozenset({"degraded", "crashed", "data_at_risk"})

# Why a group does or does not carry a verdict (event_categories.mark). Any
# other value -- including none at all, from a read path that skipped the
# classification annotator -- normalizes to ``unclassified``.
_RELIABILITY_CLASSIFICATION_STATES = frozenset({"classified", "pending", "unavailable"})

# The machine went down. A closed, three-entry set of Windows events that say
# so unambiguously, applied as a floor under whatever the classifier returned
# -- including when it returned nothing, which is what keeps this section
# working on a deployment with no API key.
#
# This is a hand-maintained table, which ADR-0026 and ADR-0041 both rejected
# for this space, so the distinction matters: those rejected enumerating the
# open-ended set of event sources in order to classify them. This enumerates
# one unambiguous *event*, the one whose absence would otherwise be read as
# health. It does not grow with the fleet, and a pattern the operator has
# explicitly suppressed is exempt (ADR-0041: explicit intent overrides an
# automatic escalation, or a suppressed marker could never be muted).
_RELIABILITY_CRASH_MARKERS = frozenset(
    {
        ("kernel-power", 41),
        ("bugcheck", 1001),
        ("wer-systemerrorreporting", 1001),
    }
)
_RELIABILITY_CRASH_SYMPTOM = "The PC shut down unexpectedly"


def _reliability_marker_key(source: Any, event_id: Any) -> tuple[str, int]:
    """Normalize a group's identity for the crash-marker lookup.

    ``Get-WinEvent`` reports the full provider name
    (``Microsoft-Windows-Kernel-Power``) while the event viewer, most
    documentation and this repo's own fixtures use the short one
    (``Kernel-Power``). Both name the same provider, so the prefix is stripped
    rather than both spellings being listed.
    """

    src = str(source or "").strip().lower()
    prefix = "microsoft-windows-"
    if src.startswith(prefix):
        src = src[len(prefix) :]
    return (src, int(_number(event_id) or -1))


def _reliability_impact_rank(impact: str) -> int:
    """Order an impact for comparison; ``unknown`` sorts below every real one."""

    try:
        return _RELIABILITY_IMPACTS.index(impact)
    except ValueError:
        return -1


def _reliability_by_day(value: Any) -> dict[str, int]:
    """Coerce an untrusted ``by_day`` histogram to ``{YYYY-MM-DD: count>0}``."""

    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for day, count in value.items():
        n = _number(count)
        if isinstance(day, str) and n is not None and n > 0:
            out[day] = int(n)
    return out


def _reliability_day_age_hours(day: str, now: datetime) -> float | None:
    """Hours from the *end* of calendar day ``day`` (UTC) to ``now``."""

    try:
        end = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        return None
    return max(0.0, (now - end).total_seconds() / 3600)


def group_activity(group: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Activity of one event group (``count``, ``by_day``, ``last_seen``) as of ``now``.

    Shared by every rule that judges a ``reliability``-shaped group, so "recent",
    "active" and "recurring" mean one thing everywhere. Pure, tolerant of
    malformed input, and every value JSON-safe. Fields:

    ``count`` -- the group's event count (``0`` when absent or unusable);
    ``active_days``, ``first_day``, ``last_day`` -- from ``by_day`` (UTC calendar
    dates); ``age_hours`` -- from ``last_seen`` against ``now``, falling back to
    the end of ``last_day``, ``None`` when neither is usable; ``recent``,
    ``active``, ``recurring``, ``burst`` -- defined by the ``_RELIABILITY_*``
    constants above.
    """

    count = int(_number(group.get("count")) or 0)
    by_day = _reliability_by_day(group.get("by_day"))
    days = sorted(by_day)
    first_day = days[0] if days else None
    last_day = days[-1] if days else None

    last_seen = _parse_ts(group.get("last_seen"))
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age: float | None
    if last_seen is not None:
        age = max(0.0, (now - last_seen).total_seconds() / 3600)
    elif last_day is not None:
        age = _reliability_day_age_hours(last_day, now)
    else:
        age = None

    recent = age is not None and age <= _RELIABILITY_ACTIVE_WITHIN_HOURS
    not_stale = age is not None and age <= _RELIABILITY_ACTIVE_MIN_DAYS_MAX_AGE_HOURS
    active_days = len(days)
    peak = max(by_day.values(), default=0)
    return {
        "count": count,
        "active_days": active_days,
        "first_day": first_day,
        "last_day": last_day,
        "age_hours": age,
        "recent": recent,
        "active": recent or (not_stale and active_days >= _RELIABILITY_ACTIVE_MIN_DAYS),
        "recurring": (
            count >= _RELIABILITY_RECURRING_MIN_COUNT
            or active_days >= _RELIABILITY_RECURRING_MIN_DAYS
        ),
        "burst": count > 0 and peak / count >= _RELIABILITY_BURST_SHARE and not recent,
    }


def reliability_patterns(payload: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Derive one impact/activity record per reliability event group.

    Pure and non-mutating: the ``events`` list is shared with the dashboard's
    heatmap and the fleet aggregation, which must keep seeing the raw groups
    the agent sent. Every field is JSON-safe so the result can travel in a
    section's ``details`` (see :func:`evaluate_section`). Fields:

    ``source``, ``event_id``, ``level``, ``count``, ``category``, ``cause``,
    ``severity``, ``suppressed``, ``classification_state`` -- copied from the
    group (ADR-0026 / ADR-0041 read-path annotations); ``severity`` degrades
    to ``unknown`` when absent or unrecognized and is presentation only since
    the impact model.

    ``user_impact`` -- what the person at the machine would have noticed, the
    only field the verdict reads. ``unknown`` when the classifier has not
    reached this pattern or cannot run at all, floored at ``crashed`` for the
    closed set of events that say the machine went down, unless the operator
    suppressed that exact pattern. ``symptom`` -- that impact in one plain
    sentence, which is what the reason line says out loud. ``scores`` --
    whether this pattern is a finding at all.

    ``active_days``, ``first_day``, ``last_day`` -- from ``by_day`` (UTC
    calendar dates, per ``docs/protocol.md``); ``last_seen_age_hours`` -- from
    ``last_seen`` against ``now`` (falls back to the end of ``last_day``);
    ``active``, ``recurring``, ``burst`` -- defined by the module constants
    above. All are relative to ``now``: the same payload judged well after its
    window has passed is history, which is why history reads evaluate "as of"
    the snapshot's own ``collected_at``.
    """

    events_raw = payload.get("events")
    events = [e for e in events_raw if isinstance(e, dict)] if isinstance(events_raw, list) else []
    out: list[dict[str, Any]] = []
    for e in events:
        severity = e.get("severity")
        if severity not in _RELIABILITY_SEVERITIES:
            severity = "unknown"
        suppressed = bool(e.get("suppressed"))
        count = int(_number(e.get("count")) or 0)

        impact = e.get("user_impact")
        if impact not in _RELIABILITY_IMPACTS:
            impact = _RELIABILITY_IMPACT_UNKNOWN
        state = e.get("classification_state")
        if state not in _RELIABILITY_CLASSIFICATION_STATES:
            # No annotation reached this group at all -- a read path that
            # skipped the classification annotator, or a payload from before
            # the field existed. It is not classified, and the rule must not
            # infer from that that nothing is wrong.
            state = "unclassified"
        symptom = str(e.get("symptom") or "")
        marker = (
            _reliability_marker_key(e.get("source"), e.get("event_id"))
            in _RELIABILITY_CRASH_MARKERS
        )
        if marker and not suppressed and _reliability_impact_rank(impact) < _reliability_impact_rank("crashed"):
            # The floor, not an override: a classifier that called this worse
            # than "crashed" keeps its verdict.
            impact = "crashed"
            symptom = symptom or _RELIABILITY_CRASH_SYMPTOM

        act = group_activity(e, now)
        age = act["age_hours"]
        active = act["active"]
        out.append(
            {
                "source": e.get("source"),
                "event_id": e.get("event_id"),
                "level": e.get("level"),
                "count": count,
                "severity": severity,
                "category": e.get("category"),
                "cause": e.get("suspected_cause"),
                "user_impact": impact,
                "symptom": symptom,
                "classification_state": state,
                "suppressed": suppressed,
                "active_days": act["active_days"],
                "first_day": act["first_day"],
                "last_day": act["last_day"],
                "last_seen_age_hours": round(age, 1) if age is not None else None,
                "active": active,
                "recurring": act["recurring"],
                "burst": act["burst"],
                "scores": (
                    not suppressed and active and impact in _RELIABILITY_SCORING_IMPACTS
                ),
            }
        )
    return out


def _age_label(hours: float) -> str:
    if hours < 1:
        return "<1h"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def _reliability_describe(p: dict[str, Any], window_days: int) -> str:
    """One finding in the operator's words.

    Deliberately names no provider, event id or error code: a reason a reader
    has to look up is a reason that delegates the work back to them, which is
    the thing this section exists not to do. The technical identity stays on
    ``details.patterns`` for the host page.
    """

    symptom = str(p.get("symptom") or "").strip().rstrip(".")
    if not symptom:
        symptom = str(p.get("cause") or "").strip().rstrip(".") or "Something is wrong"
    bits = []
    if p["count"] > 1:
        bits.append(f"{p['count']}×")
    if p["active_days"] > 1:
        bits.append(f"on {p['active_days']} of {window_days} days")
    if p["last_seen_age_hours"] is not None:
        bits.append(f"last {_age_label(p['last_seen_age_hours'])} ago")
    return f"{symptom}" + (f" ({', '.join(bits)})" if bits else "")


def _reliability_scoring(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The patterns that drive the verdict, most important first.

    Ordered by how much of the machine is at stake, then by whether it is a
    standing problem, then by how often it happened. Suppressed patterns, and
    anything with no user-visible impact, never score.
    """

    scoring = [p for p in patterns if p["scores"]]
    scoring.sort(
        key=lambda p: (
            -_reliability_impact_rank(p["user_impact"]),
            not p["recurring"],
            -p["count"],
        )
    )
    return scoring


def _reliability_reason(
    patterns: list[dict[str, Any]],
    scoring: list[dict[str, Any]],
    window_days: int,
    *,
    stability_index: float | None = None,
    si_status: Status | None = None,
) -> str:
    """What is wrong with this machine, said the way its owner would say it.

    Never leads with a raw event total -- "3675 error/critical events" is a
    number with no decision in it, and on the fleet that motivated this it was
    92% one muted pattern. Names no provider, event id or error code either:
    those are the vocabulary of the log, not of the person who has to act.

    Every clause that is *not* a finding is still counted, so a quiet section
    can be told apart from a blind one: patterns the operator suppressed
    (ADR-0041), patterns still awaiting a verdict, and a deployment where the
    classifier cannot run at all. When the Windows stability index is what
    decided the status, it is named -- a red status whose reason lists nothing
    is indistinguishable from a bug.
    """

    suppressed = [p for p in patterns if p["suppressed"]]
    # "No verdict yet" and "no verdict ever on this deployment" are different
    # facts about the same silence, and both have to be visible: a section
    # that reports nothing because nothing looked must not read like one that
    # reports nothing because nothing happened.
    unclassified = [
        p
        for p in patterns
        if not p["suppressed"] and p["classification_state"] != "classified"
    ]
    unavailable = any(p["classification_state"] == "unavailable" for p in patterns)

    tail: list[str] = []
    if suppressed:
        tail.append(f"{len(suppressed)} suppressed")
    if unavailable:
        tail.append("classification unavailable (AI off or no API key)")
    elif unclassified:
        tail.append(f"{len(unclassified)} awaiting classification")

    si_clause = ""
    if si_status is not None and stability_index is not None:
        si_clause = f"Windows stability index {stability_index:.1f}/10"

    if scoring:
        named = scoring[:_RELIABILITY_NAMED_PATTERNS]
        parts = [_reliability_describe(p, window_days) for p in named]
        extra = len(scoring) - len(named)
        if extra > 0:
            parts.append(f"+{extra} more")
        if si_clause:
            parts.append(si_clause)
        head = "; ".join(parts)
    elif si_clause:
        head = si_clause
    else:
        # Quiet. Say what was looked at, so "nothing to report" is visibly a
        # conclusion rather than an absence of one.
        quiet = [p for p in patterns if not p["suppressed"] and not p["scores"]]
        past = [p for p in quiet if p["user_impact"] in _RELIABILITY_SCORING_IMPACTS]
        if past:
            quiet_since = max((p["last_day"] for p in past if p["last_day"]), default=None)
            head = f"no current problems; {len(past)} resolved"
            if quiet_since:
                head += f" since {quiet_since}"
        elif not patterns:
            head = f"no errors logged in {window_days}d"
        elif unclassified and len(unclassified) == len([p for p in patterns if not p["suppressed"]]):
            # Nothing here has been judged, so there is nothing to report as
            # checked. The tail clause carries the whole story.
            head = f"no verdict yet on {len(unclassified)} pattern(s) in {window_days}d"
        else:
            head = f"nothing user-visible in {window_days}d ({len(quiet)} pattern(s) checked)"

    return head + (f" [{', '.join(tail)}]" if tail else "")


def _rule_reliability(
    payload: dict[str, Any], now: datetime
) -> "tuple[Status, str, dict[str, Any]] | None":
    # `events` is the grouped Error/Critical breakdown; `stability_index` is the
    # Windows Reliability Index (0-10). Scoring reads `user_impact` -- what a
    # person would have noticed -- from the ADR-0026 annotation (persisted
    # server-side, ADR-0058), so every consumer reaches the same verdict.
    #
    # The shape of the verdict: an impact has to have happened more than once
    # before it is critical -- one unexpected restart on an otherwise healthy
    # machine is worth saying once, not worth paging about, and `warn` already
    # notifies. Without any annotation nothing scores except the closed
    # crash-marker set, and the reason says so rather than reporting silence
    # as health.
    events_raw = payload.get("events")
    si = _number(payload.get("stability_index"))
    total = _number(payload.get("recent_crashes"))
    if events_raw is None and total is None and si is None:
        return None

    patterns = reliability_patterns(payload, now)
    window_days = int(_number(payload.get("window_days")) or 7)
    boot_sessions = payload.get("boot_sessions")
    boots = len(boot_sessions) if isinstance(boot_sessions, list) else None

    scoring = _reliability_scoring(patterns)
    # One rule for every impact: critical means it is *still happening*, which
    # a single occurrence cannot establish. An earlier version of this exempted
    # `data_at_risk` from recurrence, on the reasoning that the second
    # occurrence of data loss is the loss. Replaying the live fleet through it
    # turned one shadow-copy cleanup -- 33 hours old, already self-corrected,
    # and caused by a disk the `disk` section was already reporting at 87% --
    # into a red host. That is the exact failure this section was being
    # rebuilt to remove, with a better sentence attached to it.
    #
    # What a single data-risk event still gets is a warn, which notifies. A
    # disk that is actually failing logs again well inside one window, and
    # `disk_smart` carries the hardware signal independently of the event log.
    #
    # `degraded` additionally never crits however long it persists: the
    # machine works, and a standing annoyance that goes red every day is how
    # this section became ignorable in the first place.
    crit_finding = any(
        p["recurring"] and p["user_impact"] in ("crashed", "data_at_risk") for p in scoring
    )

    si_status: Status | None = None
    if si is not None and si < _RELIABILITY_SI_CRIT:
        si_status = "crit"
    elif si is not None and si < _RELIABILITY_SI_WARN:
        si_status = "warn"

    if crit_finding or si_status == "crit":
        status: Status = "crit"
    elif scoring or si_status == "warn":
        status = "warn"
    else:
        status = "ok"

    # The index is named whenever it is not ok, not only when it is the sole
    # driver: it always contributes to the status, and a reader who cannot see
    # it cannot tell why a host with no named finding is red.
    reason = _reliability_reason(
        patterns, scoring, window_days, stability_index=si, si_status=si_status
    )
    details = {"patterns": patterns, "window_days": window_days}
    if boots is not None:
        details["boot_sessions"] = boots
    return status, reason, details


_WEB_ACTIVITY_SERIOUS = {"custom", "seed", "external_adult"}


def _rule_web_activity(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    # `flagged` is a server-internal annotation added at insert time (ADR-0024).
    # Absent => the host is not configured for parental controls; defer.
    flagged = payload.get("flagged")
    if flagged is None:
        return None
    recent = [
        f
        for f in _dicts(flagged)
        if (age := _age_days(f.get("last_seen"), now=now)) is not None and age <= 1.0
    ]
    serious = [
        f
        for f in recent
        if isinstance(f.get("category"), str) and f["category"] in _WEB_ACTIVITY_SERIOUS
    ]
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
        for p in _dicts(payload.get("ports"))
        # `port` is an unvalidated wire value: a list/dict is unhashable and would
        # raise TypeError on the set lookup, so only an int is ever a candidate.
        if isinstance(p.get("port"), int) and p["port"] in _REMOTE_ACCESS_PORTS
        and not str(p.get("address", "")).startswith(("127.", "::1"))
    ]
    if exposed:
        # A remote-access listener is a standing fact about how the machine is
        # set up -- RDP on an admin PC, sshd on a server -- not an event. It is
        # listed and aged as posture; a port that *appears* is caught by the
        # inventory diff (diffs.py) as a change notification.
        e = exposed[0]
        example = f"{e.get('proto', '?')}/{e.get('port', '?')} {e.get('process', '?')}"
        return "posture", f"{len(exposed)} remote-access port(s) listening (e.g. {example})"
    return None


def _rule_local_accounts(
    payload: dict[str, Any], now: datetime, agent_os: str = "windows"
) -> "tuple[Status, str] | None":
    is_windows = _is_windows(agent_os)
    warns: list[str] = []
    for account in _dicts(payload.get("accounts")):
        if not account.get("enabled"):
            continue
        # `password_required is False` reflects the Windows UF_PASSWD_NOTREQD flag
        # ("a blank password is permitted"), NOT "this account has no password".
        # OEM/sysprep'd machines set it on accounts that do have a password, so we
        # only crit when the account has ALSO genuinely never had a password set
        # (`password_last_set is None`). A real password means this is a benign
        # OEM flag. Auth-probing to be certain is deliberately out of scope
        # (account-lockout risk). See ADR-0028.
        if (
            account.get("is_admin")
            and account.get("password_required") is False
            and account.get("password_last_set") is None
        ):
            return "crit", f"admin '{account.get('name', '?')}' requires no password"
        # An *enabled* built-in administrator is a finding on Windows, where RID 500
        # ships disabled and something must have turned it on. On Linux the same
        # flag marks root, which is enabled by definition — scoring it would put
        # every Linux host at a permanent warn for being a Linux host (ADR-0043).
        if is_windows and account.get("builtin_admin"):
            warns.append("built-in Administrator enabled")
        if account.get("builtin_guest"):
            warns.append("Guest account enabled")
        # A governance contradiction: an account holding local administrator rights
        # while also being denied logon types. Both were set deliberately, so one of
        # them is stale — most often a demotion that was reverted, or deny rights
        # left on an account that has since been promoted back. Worth a look rather
        # than an alarm, since neither state is dangerous on its own (ADR-0042).
        if account.get("is_admin") and account.get("deny_logon"):
            warns.append(
                f"'{account.get('name', '?')}' is an admin with denied logon rights"
            )
    if warns:
        return "warn", "; ".join(warns)
    return None


# Failed sign-ins per account within the section's window before this looks like
# something other than a mistyped password. A family PC produces a handful a week;
# a spray or a child working through guesses produces dozens.
LOGON_FAILURES_WARN = 15


def _rule_logon_failures(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    """Warn on a burst of failed sign-ins against a single account.

    Deliberately never ``crit``: a failed logon is not, by itself, a compromised
    machine, and kenny reports rather than judges here (the ADR-0029 stance). The
    per-account threshold matters more than the total — twenty failures spread over
    five accounts is a household forgetting passwords, twenty against one account is
    someone working at it.
    """
    hours = payload.get("window_hours") or 24
    worst: dict[str, Any] | None = None
    for account in _dicts(payload.get("accounts")):
        count = _number(account.get("count")) or 0
        if count >= LOGON_FAILURES_WARN and (worst is None or count > worst["count"]):
            worst = {"name": account.get("name", "?"), "count": count}
    if worst:
        return (
            "warn",
            f"{worst['count']} failed sign-ins for '{worst['name']}' in {hours}h",
        )
    # Attempts against names that are not accounts here: password spraying or a
    # scanner, never a household member mistyping their own name.
    unmatched = _number(payload.get("unmatched_count")) or 0
    if unmatched >= LOGON_FAILURES_WARN:
        return "warn", f"{unmatched} failed sign-ins for unknown usernames in {hours}h"
    return None


def _rule_backup_status(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    restore = _as_dict(payload.get("restore_points"))
    file_history = _as_dict(payload.get("file_history"))
    onedrive = _as_dict(payload.get("onedrive"))
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
    reference = _as_dict(payload.get("reference"))
    ref_loss = _number(reference.get("loss_percent"))
    if ref_loss is not None and ref_loss >= 60:
        return "crit", f"internet degraded ({ref_loss:.0f}% loss to {reference.get('host', '?')})"
    gateway = _as_dict(payload.get("gateway"))
    latency = _number(gateway.get("latency_ms"))
    loss = _number(gateway.get("loss_percent"))
    slow = latency is not None and latency > 100
    lossy = loss is not None and loss > 20
    if slow or lossy:
        parts = []
        if slow:
            parts.append(f"{latency:.0f}ms latency")
        if lossy:
            parts.append(f"{loss:.0f}% loss")
        return "warn", f"gateway link poor ({', '.join(parts)})"
    return None


# -- sections the agent used to grade itself (ADR-0058) ------------------------
#
# ``services``, ``encryption``, ``printers``, ``time_sync`` and ``uptime`` carried
# only the collector's own verdict, which this module could never lower -- the
# bug class ``reliability`` was cured of first. Each now has a rule here and
# the collectors report without grading. Most of what they report is posture:
# a fact about how the machine is configured that is true today exactly as it
# was yesterday, and that no operator wants to be told about every day.

_SERVICES_NAMED = 3


def _rule_services(
    payload: dict[str, Any], now: datetime, agent_os: str = "windows"
) -> "tuple[Status, str] | None":
    services = _dicts(payload.get("services"))
    if not services:
        return None  # nothing reported (probe failed, or no failed units on Linux)
    if _is_windows(agent_os):
        # Auto-start services that are not running are, on a real PC, almost
        # always trigger-start and updater services idling by design (Edge and
        # Google updaters, sppsvc, gpsvc, MapsBroker ...). That is posture: worth
        # a list, never an alarm. A service that *fails* announces itself as
        # Service Control Manager events in `reliability`, scored by activity.
        stalled = [
            str(svc.get("name") or svc.get("display") or "?")
            for svc in services
            if str(svc.get("start") or "").lower().startswith("auto")
            and str(svc.get("status") or "").lower() != "running"
        ]
        if not stalled:
            return "ok", f"{len(services)} services, all auto-start running"
        example = ", ".join(stalled[:_SERVICES_NAMED])
        extra = len(stalled) - _SERVICES_NAMED
        suffix = f", +{extra} more" if extra > 0 else ""
        return "posture", f"{len(stalled)} auto-start service(s) not running (e.g. {example}{suffix})"
    # Linux reports failed systemd units only; a failed unit is an incident.
    failed = [
        str(svc.get("name") or "?")
        for svc in services
        if str(svc.get("status") or "").lower() == "failed"
    ]
    if failed:
        example = ", ".join(failed[:_SERVICES_NAMED])
        return "warn", f"{len(failed)} failed unit(s) (e.g. {example})"
    return "ok", "all units healthy"


def _rule_encryption(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    volumes = _dicts(payload.get("volumes"))
    if not volumes:
        return None  # BitLocker state unavailable -> defer, never "encrypted"
    system = next(
        (v for v in volumes if str(v.get("mount") or "").rstrip("\\").upper() == "C:"),
        volumes[0],
    )
    mount = str(system.get("mount") or "C:").rstrip("\\").upper()
    if system.get("protection_status") == 1:
        return "ok", "system drive encrypted"
    return "posture", f"{mount} not BitLocker-protected"


def _rule_printers(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    printers = _dicts(payload.get("printers"))
    if not printers:
        return None
    bad = [
        str(p.get("name") or "?")
        for p in printers
        if any(word in str(p.get("status") or "").lower() for word in ("error", "offline"))
    ]
    if not bad:
        return "ok", f"{len(printers)} printer(s) OK"
    # An offline printer is neither an incident nor posture: it is a fact
    # about a peripheral that is switched off, visible in the section, and
    # not a finding about the machine.
    return "ok", f"{len(bad)} of {len(printers)} printer(s) offline/error ({', '.join(bad[:2])})"


_TIME_SYNC_MAX_OFFSET_SECS = 5.0


def _rule_time_sync(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    synchronized = payload.get("synchronized")
    offset = _number(payload.get("offset_secs"))
    if synchronized is None and offset is None:
        # No reading (time service not queryable, or a platform without one):
        # the agent's summary says which, and an unknown is not a finding.
        return None
    if offset is not None and abs(offset) > _TIME_SYNC_MAX_OFFSET_SECS:
        return "warn", f"clock offset {offset:.2f}s"
    if synchronized is False:
        return "warn", "clock not network-synchronized"
    return "ok", "clock synchronized"


_UPTIME_POSTURE_SECS = 30 * 24 * 3600


def _rule_uptime(
    payload: dict[str, Any], now: datetime, agent_os: str = "windows"
) -> "tuple[Status, str] | None":
    secs = _number(payload.get("uptime_secs"))
    if secs is None:
        return None
    days = int(secs // 86_400)
    if _is_windows(agent_os) and secs >= _UPTIME_POSTURE_SECS:
        # Windows applies updates on reboot; a month without one means
        # patches are waiting. Standing fact, not an event: posture.
        return "posture", f"up {days}d — Windows updates need a reboot to apply"
    # Long uptime on a Linux server is not a finding.
    return "ok", f"up {days}d"


# Section name -> rule. Easy to extend: add an entry.
RULES: dict[str, Rule | OsAwareRule] = {
    "disk": _rule_disk,
    "defender": _rule_defender,
    "win_update": _rule_win_update,
    "reboot_pending": _rule_reboot_pending,
    "battery": _rule_battery,
    "memory": _rule_memory,
    "thermals": _rule_thermals,
    "disk_smart": _rule_disk_smart,
    "hardware_errors": _rule_hardware_errors,
    "gpu": _rule_gpu,
    "fans": _rule_fans,
    "os_support": _rule_os_support,
    "web_activity": _rule_web_activity,
    "reliability": _rule_reliability,
    "listening_ports": _rule_listening_ports,
    "local_accounts": _rule_local_accounts,
    "logon_failures": _rule_logon_failures,
    "backup_status": _rule_backup_status,
    "net_quality": _rule_net_quality,
    "services": _rule_services,
    "encryption": _rule_encryption,
    "printers": _rule_printers,
    "time_sync": _rule_time_sync,
    "uptime": _rule_uptime,
}


# Rules whose section is a Windows-only concept (Microsoft Defender, Windows
# Update / KB numbers, the registry reboot-pending flags, and System Restore /
# File History / OneDrive backup evidence). A non-Windows agent emits an
# "n/a on this platform" stub for these; scoring them would mislead. They are
# skipped for agents whose OS is not Windows (see ADR-0031).
#
# ``logon_failures`` was in this set until ADR-0043 gave it a real Linux arm
# (sshd/PAM failures from the journal). Its thresholds are OS-neutral, so it is
# now scored everywhere.
WINDOWS_ONLY_SECTIONS: frozenset[str] = frozenset(
    {"defender", "win_update", "reboot_pending", "backup_status", "encryption"}
)

# Sections whose *rule* needs to know the agent's OS, as opposed to sections that
# are skipped wholesale. Kept as a separate registry rather than widening every
# rule's signature: an explicit list is greppable in a way an inspected
# signature is not.
OS_AWARE_RULES: frozenset[str] = frozenset({"local_accounts", "services", "uptime"})

# Sections whose incident must be confirmed by a *second* collection before it
# notifies (see ``alerting._health_transitions``).
#
# The alert loop runs every 60s over a snapshot that changes every ~900s, so a
# single evaluation over a single snapshot was enough to page someone and open
# a ticket. For most sections that is right: a disk does not un-fill itself,
# and a Defender that is off is off. ``reliability`` is different -- it reads a
# rolling 7-day event window whose contents shift as events age out of it, so a
# finding can appear and vanish between two pushes without anything having
# happened to the machine. Requiring a newer ``collected_at`` to still agree
# costs one push interval of latency and removes that whole class of alarm.
# ``hardware_errors`` (the same rolling event window), ``gpu`` (clock-event
# reasons that flicker with load) and ``fans`` (a burst of five samples) are
# transient in the same way.
#
# The policy lives here, next to the thresholds it belongs with
# (``kenny-server/CLAUDE.md``: health thresholds live only in this module);
# ``alerting`` reads the set and stays free of per-section knowledge.
CONFIRM_BEFORE_ALARM: frozenset[str] = frozenset(
    {"reliability", "hardware_errors", "gpu", "fans"}
)


def _is_windows(agent_os: str | None) -> bool:
    return str(agent_os or "windows").lower() == "windows"


def _agent_verdict(reported: Status, summary: str) -> dict[str, Any]:
    """The section dict when this module has nothing to say and the agent's
    own ``status`` stands (no rule, or a rule that deferred)."""

    return {
        "status": reported,
        "summary": summary,
        "attention": reported in INCIDENT_STATUSES,
        "tier": tier_of(reported),
    }


def evaluate_section(
    name: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    agent_os: str = "windows",
) -> dict[str, Any]:
    """Return ``{status, summary, attention, tier, reason?, details?}`` for
    one section after applying rules.

    ``attention`` is ``status in {warn, crit}`` and ``tier`` is
    ``incident``/``posture``/``none`` — computed here, alongside ``status``,
    and nowhere else (``kenny-server/CLAUDE.md``: "health thresholds live only
    in health_rules.py"). Every consumer (``tools.build_health``,
    ``fleet_stats``, the dashboard's ``_overview``, the MCP ``agent_health``
    tool) reads them straight off this dict rather than re-deriving them from
    ``status``. A ``posture`` section is not ``attention``: it is listed and
    aged, never alarmed on (ADR-0058).

    **A rule's verdict is final, not a floor over the agent's own.** When a
    section has a rule and that rule reaches a verdict, that verdict *is* the
    status — the ``status`` the agent put in the payload is not folded in. The
    agent computes its own status from a handful of local constants it cannot
    change without being redeployed, which is exactly the judgement this module
    exists to own; letting it raise a verdict it can never lower means a
    server-side threshold change (or an operator suppression, ADR-0041) can
    only ever tighten a section, never relax one. ``reliability`` showed what
    that costs: the collector reports ``warn`` at 20 error events in 7 days, a
    bar every real Windows PC clears, so no amount of server-side scoring could
    put the section back to ``ok``.

    The agent's ``status`` still stands alone where this module has nothing to
    say — a section with no rule, or a rule that defers by returning ``None``
    (a payload missing the fields it scores). There the agent is the only
    judgement available, and it is used unchanged.
    """

    now = now or datetime.now(timezone.utc)
    reported = _valid_status(payload.get("status", "ok"))
    summary = payload.get("summary", "")
    rule = RULES.get(name)
    if rule is None:
        return _agent_verdict(reported, summary)
    outcome = (
        rule(payload, now, agent_os) if name in OS_AWARE_RULES else rule(payload, now)
    )
    if outcome is None:
        return _agent_verdict(reported, summary)
    rule_status, reason = outcome[0], outcome[1]
    # A ruled section carries the *rule's* line and only that one. The agent's
    # `summary` is written from constants baked into the shipped binary and
    # knows nothing of suppression, classification or the operator's rules, so
    # showing it beside the reason contradicts it: `reliability` read
    # "3675 error/critical events in 7d" under a reason that had carefully
    # excluded 3373 muted ones. It is still on the raw snapshot for
    # `agent_snapshot` and the section body; it just stops competing with the
    # verdict. Sections with no rule keep it -- there it is the only line
    # available.
    result: dict[str, Any] = {
        "status": rule_status,
        "summary": "",
        "attention": rule_status in INCIDENT_STATUSES,
        "tier": tier_of(rule_status),
        "reason": reason,
    }
    if len(outcome) > 2 and isinstance(outcome[2], dict):
        result["details"] = outcome[2]
    return result


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
    ``n/a`` stubs (see ADR-0031). Portable sections (e.g. ``listening_ports``,
    ``local_accounts``) apply for every OS.

    Returns ``{"overall": status, "sections": {name: {status, summary, reason?}}}``.
    """

    now = now or datetime.now(timezone.utc)
    is_windows = _is_windows(agent_os)
    sections: dict[str, Any] = {}
    for name, payload in snapshot.items():
        if not is_windows and name in WINDOWS_ONLY_SECTIONS:
            continue
        # `payload` comes straight off an unvalidated stored snapshot -- a pushed
        # `telemetry` frame's `Section` is pydantic-validated (always a dict), but a
        # `telemetry_collect` request/response round trip stores its
        # `Response.result` (`dict[str, Any]`, unvalidated) the same way, so a
        # compromised/buggy agent can make a top-level section value anything JSON
        # allows -- a string, a list, `None`. `dict(payload)` raised `TypeError`/
        # `ValueError` on all of those instead of the "treat as unusable" path every
        # other field on this module already takes (see `_as_dict`/`_dicts`).
        sections[name] = evaluate_section(
            name, _as_dict(payload), now=now, agent_os=agent_os
        )
    overall = worst(*(s["status"] for s in sections.values())) if sections else "ok"
    return {"overall": overall, "sections": sections}

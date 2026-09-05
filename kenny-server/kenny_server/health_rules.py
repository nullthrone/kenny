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
from datetime import datetime, timezone
from typing import Any, Callable

Status = str  # "ok" | "warn" | "crit"

_ORDER = {"ok": 0, "warn": 1, "crit": 2}


def worst(*statuses: Status) -> Status:
    """Return the most severe of the given statuses (crit > warn > ok)."""

    return max((s for s in statuses if s), key=lambda s: _ORDER.get(s, 0), default="ok")


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


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Accept trailing Z (UTC) and offset forms.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
Rule = Callable[[dict[str, Any], datetime], "tuple[Status, str] | None"]
# A rule that also needs the agent's OS. Listed in :data:`OS_AWARE_RULES` and
# called with the extra argument by :func:`evaluate_section`; the OS parameter is
# keyword-defaulted so such a rule still satisfies :data:`Rule`.
OsAwareRule = Callable[[dict[str, Any], datetime, str], "tuple[Status, str] | None"]


def _rule_disk(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    worst_pct = -1.0
    worst_mount = ""
    for vol in _dicts(payload.get("volumes")):
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
    recent = _dicts(payload.get("recent"))
    failed = [u for u in recent if str(u.get("result", "")).lower() == "failed"]
    if failed:
        return "warn", f"{len(failed)} update(s) failed"
    return "ok", "Updates healthy"


def _rule_reboot_pending(payload: dict[str, Any], now: datetime) -> "tuple[Status, str] | None":
    if payload.get("pending") is True:
        # A truthy non-list (e.g. a string) would otherwise iterate char-by-char below.
        reasons = payload.get("reasons")
        reasons = reasons if isinstance(reasons, list) else []
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
    sensors = _dicts(payload.get("sensors"))
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
    """Coerce a JSON number to float, rejecting bools, non-numerics, and non-finite floats.

    Every caller either compares the result or feeds it to ``int()`` (a count, a
    threshold check). Python's ``json`` module accepts the ``NaN``/``Infinity``/
    ``-Infinity`` extension on decode, so a telemetry section field like
    ``recent_crashes`` or an event's ``count`` -- unvalidated ``dict[str, Any]``
    fields on the ``telemetry_collect`` round trip, same threat model as
    :func:`_valid_status` -- can carry one of those. ``int(float("inf"))`` raises
    ``OverflowError`` and ``int(float("nan"))`` raises ``ValueError``, so treating
    them as "not a usable number" here (like a bool or a string) keeps that crash
    out of every caller instead of guarding each ``int()`` call site individually.

    No threshold or status semantics change here — a non-finite value now takes
    the same "field absent/unusable" path a missing field already took.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


# -- reliability: volume-based fallback (no severity annotation present) ----
#
# Used only when events carry no `severity` field at all — i.e. the read-path
# LLM categorization (ADR-0026) has never run over this payload (a raw agent
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
# genuinely low reliability index (issue #166 / ADR-0041).
_RELIABILITY_SI_CRIT = 3
_RELIABILITY_SI_WARN = 6


def _reliability_reason(events: list[Any], total: int) -> str:
    """A content reason: the biggest problem groups by category (or raw source).

    Used for the volume-based fallback path, where there is no severity/cause
    to name — see :func:`_reliability_pattern_reason` for the annotated path.
    Suppressed groups (ADR-0041) are excluded from the tally — a muted pattern
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

    Suppressed patterns (ADR-0041 / issue #166) are never named here — that is
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

    Operator suppression (ADR-0041 / issue #166) still applies on this path.
    Unlike the ADR-0026 LLM categorization, matching a suppression rule needs
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
    (ADR-0026 read-path categorization). Distinct *patterns* drive escalation,
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
            # explicitly suppressed this exact pattern (ADR-0041 / issue #166),
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
    # group with a `severity` (ADR-0026 categorization),
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
        for p in _dicts(payload.get("ports"))
        if p.get("port") in _REMOTE_ACCESS_PORTS
        and not str(p.get("address", "")).startswith(("127.", "::1"))
    ]
    if exposed:
        e = exposed[0]
        example = f"{e.get('proto', '?')}/{e.get('port', '?')} {e.get('process', '?')}"
        return "warn", f"{len(exposed)} remote-access port(s) listening (e.g. {example})"
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
    ref_loss = reference.get("loss_percent")
    if isinstance(ref_loss, (int, float)) and ref_loss >= 60:
        return "crit", f"internet degraded ({ref_loss:.0f}% loss to {reference.get('host', '?')})"
    gateway = _as_dict(payload.get("gateway"))
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
RULES: dict[str, Rule | OsAwareRule] = {
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
    "logon_failures": _rule_logon_failures,
    "backup_status": _rule_backup_status,
    "net_quality": _rule_net_quality,
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
    {"defender", "win_update", "reboot_pending", "backup_status"}
)

# Sections whose *rule* needs to know the agent's OS, as opposed to sections that
# are skipped wholesale. Kept as a separate registry rather than widening every
# rule's signature: exactly one rule needs this today, and an explicit list is
# greppable in a way an inspected signature is not.
OS_AWARE_RULES: frozenset[str] = frozenset({"local_accounts"})


def _is_windows(agent_os: str | None) -> bool:
    return str(agent_os or "windows").lower() == "windows"


def evaluate_section(
    name: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    agent_os: str = "windows",
) -> dict[str, Any]:
    """Return ``{status, summary, attention, reason?}`` for one section after
    applying rules.

    ``attention`` is ``status != "ok"`` — computed here, alongside ``status``,
    and nowhere else (``kenny-server/CLAUDE.md``: "health thresholds live only
    in health_rules.py"). Every consumer (``tools.build_health``,
    ``fleet_stats``, the dashboard's ``_overview``, the MCP ``agent_health``
    tool) reads it straight off this dict rather than re-deriving it from
    ``status``.

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
        return {"status": reported, "summary": summary, "attention": reported != "ok"}
    outcome = (
        rule(payload, now, agent_os) if name in OS_AWARE_RULES else rule(payload, now)
    )
    if outcome is None:
        return {"status": reported, "summary": summary, "attention": reported != "ok"}
    rule_status, reason = outcome
    return {
        "status": rule_status,
        "summary": summary,
        "attention": rule_status != "ok",
        "reason": reason,
    }


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
        sections[name] = evaluate_section(
            name, dict(payload), now=now, agent_os=agent_os
        )
    overall = worst(*(s["status"] for s in sections.values())) if sections else "ok"
    return {"overall": overall, "sections": sections}

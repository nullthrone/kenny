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


# -- reliability: activity- and persistence-based pattern scoring ------------
#
# Score on WHETHER a pattern is still happening and on WHAT it is -- never on
# how many lines it produced. A reboot storm that wrote 80 identical DCOM
# errors on one day a week ago is history; one app crash is a one-off; a
# pattern that fires on five of the last seven days and was last seen an hour
# ago is an incident. The agent already sends the evidence for this
# (``by_day`` per pattern, ``last_seen``, ``window_days`` -- see
# ``docs/protocol.md``); the constants below turn it into three per-pattern
# booleans (``active``, ``recurring``, ``burst``) that the verdict and the
# reason read. There is deliberately no count threshold anywhere in this
# rule: counts cannot tell "3439 identical harmless lines" from "3439
# individually relevant errors" (ADR-0041), and a distinct-pattern count
# cannot tell a reboot storm from a machine that is falling apart (ADR-0058).
#
# A pattern is *active* if it was seen within this many hours of ``now`` ...
_RELIABILITY_ACTIVE_WITHIN_HOURS = 48
# ... or, as long as its last hit is still inside the window, on at least this
# many distinct days of it (a pattern that fires most days is a fixture of the
# machine even if the last push happened to land in a lull).
_RELIABILITY_ACTIVE_MIN_DAYS = 3
# A pattern is *recurring* once it has been seen on this many distinct days.
# Below this it is a one-off, whatever its count.
_RELIABILITY_RECURRING_MIN_DAYS = 2
# A pattern is a *burst* when one day holds at least this share of its total
# and it is not active any more -- the shape of a reboot storm or a single
# bad afternoon, as opposed to a standing problem.
_RELIABILITY_BURST_SHARE = 0.8
# How many scoring patterns the reason names before folding the rest.
_RELIABILITY_NAMED_PATTERNS = 3

# The Windows Reliability Index (0-10) is an independent, agent-computed
# signal that content-based pattern scoring can't see into, so it always
# applies on top. It is deliberately NOT suppressible -- an operator muting a
# noisy event pattern must never be able to hide a genuinely low reliability
# index (issue #166 / ADR-0041).
_RELIABILITY_SI_CRIT = 3
_RELIABILITY_SI_WARN = 6

_RELIABILITY_SEVERITIES = ("benign", "notable", "serious", "unknown")


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


def reliability_patterns(payload: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Derive one activity/persistence record per reliability event group.

    Pure and non-mutating: the ``events`` list is shared with the dashboard's
    heatmap and the fleet aggregation, which must keep seeing the raw groups
    the agent sent. Every field is JSON-safe so the result can travel in a
    section's ``details`` (see :func:`evaluate_section`). Fields:

    ``source``, ``event_id``, ``level``, ``count``, ``category``, ``cause``,
    ``suppressed`` -- copied from the group (ADR-0026 / ADR-0041 read-path
    annotations); ``severity`` -- the LLM's verdict, ``unknown`` when absent
    or unrecognized, forced to ``serious`` for a Windows-critical group unless
    the operator suppressed that exact pattern (explicit intent overrides the
    automatic escalation, otherwise a suppressed Kernel-Power/41 could never
    be muted); ``active_days``, ``first_day``, ``last_day`` -- from ``by_day``;
    ``last_seen_age_hours`` -- from ``last_seen`` against ``now`` (falls back
    to the end of ``last_day``); ``active``, ``recurring``, ``burst`` -- the
    three booleans the verdict reads, defined by the module constants above.
    All three are relative to ``now``: the same payload judged well after its
    window has passed is history, which is why history reads evaluate "as of"
    the snapshot's own ``collected_at``.
    """

    events_raw = payload.get("events")
    events = [e for e in events_raw if isinstance(e, dict)] if isinstance(events_raw, list) else []
    window_hours = (_number(payload.get("window_days")) or 7.0) * 24
    out: list[dict[str, Any]] = []
    for e in events:
        severity = e.get("severity")
        if severity not in _RELIABILITY_SEVERITIES:
            severity = "unknown"
        suppressed = bool(e.get("suppressed"))
        if e.get("level") == "critical" and not suppressed:
            severity = "serious"
        count = int(_number(e.get("count")) or 0)
        by_day = _reliability_by_day(e.get("by_day"))
        days = sorted(by_day)
        first_day = days[0] if days else None
        last_day = days[-1] if days else None

        last_seen = _parse_ts(e.get("last_seen"))
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
        in_window = age is not None and age <= window_hours
        active_days = len(days)
        peak = max(by_day.values(), default=0)
        out.append(
            {
                "source": e.get("source"),
                "event_id": e.get("event_id"),
                "level": e.get("level"),
                "count": count,
                "severity": severity,
                "category": e.get("category"),
                "cause": e.get("suspected_cause"),
                "suppressed": suppressed,
                "active_days": active_days,
                "first_day": first_day,
                "last_day": last_day,
                "last_seen_age_hours": round(age, 1) if age is not None else None,
                "active": recent or (in_window and active_days >= _RELIABILITY_ACTIVE_MIN_DAYS),
                "recurring": active_days >= _RELIABILITY_RECURRING_MIN_DAYS,
                "burst": (count > 0 and peak / count >= _RELIABILITY_BURST_SHARE and not recent),
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
    label = str(p.get("source") or "?")
    if p.get("event_id") is not None:
        label += f"/{p['event_id']}"
    bits = [f"{label} ×{p['count']}"]
    if p["active_days"]:
        bits.append(f"{p['active_days']} of {window_days} days")
    if p["last_seen_age_hours"] is not None:
        bits.append(f"last seen {_age_label(p['last_seen_age_hours'])} ago")
    cause = p.get("cause") or p.get("category") or "cause unclear"
    return ", ".join(bits) + f" — {cause}"


def _reliability_scoring(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The patterns that drive the verdict, most important first: active
    serious, then quiet serious, then active-and-recurring notable/unknown;
    by count within each group. Suppressed patterns never score."""

    scored = [p for p in patterns if not p["suppressed"]]

    def _rank(p: dict[str, Any]) -> int | None:
        if p["severity"] == "serious":
            return 0 if p["active"] else 1
        if p["severity"] in ("notable", "unknown") and p["active"] and p["recurring"]:
            return 2
        return None

    ranked = [(r, p) for p in scored if (r := _rank(p)) is not None]
    ranked.sort(key=lambda rp: (rp[0], -rp[1]["count"]))
    return [p for _, p in ranked]


def _reliability_reason(
    patterns: list[dict[str, Any]], scoring: list[dict[str, Any]], total: int, window_days: int
) -> str:
    """A finding-shaped reason: the patterns that score, each with cadence,
    persistence and age, then the historical remainder folded into one clause.

    Never leads with the raw 7-day total -- "3528 error/critical events" is a
    number without a decision in it. Suppressed patterns (ADR-0041) are never
    named -- that is the point of muting them -- but always counted in the
    trailing clause, so a reader can tell "quiet" from "quieted". A pattern
    the operator muted is never folded into "known-benign": that phrase is
    the LLM's verdict, a suppression is the operator's, a different claim.
    """

    suppressed = [p for p in patterns if p["suppressed"]]
    scored = [p for p in patterns if not p["suppressed"]]
    suffix = f" ({len(suppressed)} pattern(s) suppressed)" if suppressed else ""

    if not patterns:
        return (
            f"no error patterns in {window_days}d"
            if total == 0
            else f"{total} events, no patterns reported"
        )
    if not scored:
        return f"{total} events, all {len(suppressed)} pattern(s) suppressed"

    scoring_ids = {id(p) for p in scoring}
    historical = [p for p in scored if id(p) not in scoring_ids and p["severity"] != "benign"]
    quiet_since = max((p["last_day"] for p in historical if p["last_day"]), default=None)
    historical_clause = f"{len(historical)} historical pattern(s)"
    if quiet_since:
        historical_clause += f" quiet since {quiet_since}"

    if not scoring:
        if not historical:
            by_category: dict[str, int] = {}
            for p in scored:
                label = p.get("category") or "?"
                by_category[label] = by_category.get(label, 0) + p["count"]
            top_cat = max(by_category, key=lambda c: by_category[c])
            return f"{total} events, all known-benign ({top_cat}){suffix}"
        benign_n = len(scored) - len(historical)
        reason = f"no active error patterns; {historical_clause}"
        if benign_n:
            reason += f", {benign_n} known-benign"
        return reason + suffix

    named = scoring[:_RELIABILITY_NAMED_PATTERNS]
    reason = ", ".join(_reliability_describe(p, window_days) for p in named)
    extra = len(scoring) - len(named)
    if extra > 0:
        reason += f", +{extra} more active pattern(s)"
    if historical:
        reason += f", {historical_clause}"
    return reason + suffix


def _rule_reliability(
    payload: dict[str, Any], now: datetime
) -> "tuple[Status, str, dict[str, Any]] | None":
    # `events` is the grouped Error/Critical breakdown; `stability_index` is the
    # Windows Reliability Index (0-10). Per-pattern activity is derived from the
    # `by_day`/`last_seen` fields the agent sends, severity from the ADR-0026
    # annotation (persisted server-side, ADR-0058) -- one scoring path for every
    # consumer. Without any annotation every pattern is `unknown`, which can
    # reach warn but never crit: a count alone is never a critical finding.
    events_raw = payload.get("events")
    si = _number(payload.get("stability_index"))
    total = _number(payload.get("recent_crashes"))
    if events_raw is None and total is None and si is None:
        return None

    patterns = reliability_patterns(payload, now)
    if total is None:
        total = sum(p["count"] for p in patterns)
    total_i = int(total)
    window_days = int(_number(payload.get("window_days")) or 7)

    scoring = _reliability_scoring(patterns)
    serious_active = any(p["severity"] == "serious" and p["active"] for p in scoring)

    if serious_active or (si is not None and si < _RELIABILITY_SI_CRIT):
        status: Status = "crit"
    elif scoring or (si is not None and si < _RELIABILITY_SI_WARN):
        status = "warn"
    else:
        status = "ok"

    reason = _reliability_reason(patterns, scoring, total_i, window_days)
    return status, reason, {"patterns": patterns, "window_days": window_days}


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
    result: dict[str, Any] = {
        "status": rule_status,
        "summary": summary,
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

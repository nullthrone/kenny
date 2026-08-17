"""Fleet-wide telemetry aggregation for the high-level Overview dashboard.

Pure, side-effect-free functions: the web layer loads the latest snapshot and
rolled-up health for every agent (from ``store.py`` + ``health_rules``) and
hands them here to be verdichtet into chart-ready aggregates. Keeping this
module I/O-free makes it directly unit-testable against synthetic snapshots.

Every aggregated quantity carries a ``members`` list — the individual host
observations that produced it — so the dashboard can drill down from any
chart element into a table of the underlying PCs. A *member* is
``{"agent_id": str, "value": <number|str|None>, "detail": str}``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# An agent record as assembled by the web layer.
#   {"agent_id", "online", "meta", "snapshot" | None, "health", "collected_at"}
Agent = dict[str, Any]

_SECTION_ORDER = {"crit": 2, "warn": 1, "ok": 0, "unknown": -1}


def _member(agent_id: str, value: Any, detail: str) -> dict[str, Any]:
    return {"agent_id": agent_id, "value": value, "detail": detail}


def _section(snapshot: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    """Return one section payload from a snapshot, or None if absent."""

    if not snapshot:
        return None
    payload = snapshot.get(name)
    return payload if isinstance(payload, dict) else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _agent_os(agent: Agent) -> str:
    """The agent's OS family from its ``meta`` (registry ``Agent.os``), lower-cased.

    Legacy agents that never reported an OS default to ``windows`` (see ADR-0031).
    """

    return str((agent.get("meta") or {}).get("os") or "windows").lower()


# Display labels for an OS family with no ``os_support`` telemetry to name it.
_OS_FAMILY_LABEL = {"windows": "Windows", "linux": "Linux", "macos": "macOS"}


# --------------------------------------------------------------------------
# Overview (single-snapshot fleet aggregates)
# --------------------------------------------------------------------------


def aggregate_overview(
    agents: list[Agent],
    *,
    now: datetime | None = None,
    disk_forecasts: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Verdichte the latest per-agent snapshots into chart-ready aggregates.

    Returns a JSON-serializable dict with one entry per Overview widget, each
    carrying ``members`` for drill-down. ``agents`` is the list assembled by the
    web layer (see :data:`Agent`). ``disk_forecasts`` is the optional per-agent
    ``trends.disk_forecast`` output (this module stays pure — the web layer does
    the store reads).
    """

    now = now or datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "agent_count": len(agents),
        "online_count": sum(1 for a in agents if a.get("online")),
        "kpis": _kpis(agents, disk_forecasts or {}),
        "health": _health_mix(agents),
        "sections": _section_severity(agents),
        "os": _os_inventory(agents),
        "device": _device_mix(agents),
        "posture": _security_posture(agents),
        "top": _top_metrics(agents),
        "sankey": _sankey(agents),
        "reliability_categories": _reliability_categories(agents),
    }


def _reliability_categories(agents: list[Agent]) -> dict[str, Any]:
    """Heatmap grid: how many error/critical events of each friendly category each
    host has. Reads the server-annotated ``category`` on the reliability events
    (see ADR-0026); an unannotated event falls back to its raw ``source``.

    Returns ``{agents, categories, cells:[{agent_id, category, count, crit,
    suppressed, detail, members}]}`` — cells sum event counts per host+category
    (raw, including anything suppressed — the heatmap must keep showing real
    volume). ``crit`` flags any critical-level group, OR any group the
    read-path LLM classified as ``severity="serious"`` — a pattern can be
    worth flagging even when the agent didn't mark the Windows event itself
    "critical" — but a pattern the operator has suppressed (ADR-0041 / issue
    #166) never sets ``crit``, matching the same exclusion the health rule
    applies. ``suppressed`` is the portion of ``count`` behind muted patterns,
    surfaced in the tooltip so a hot-but-not-crit cell is explained rather than
    silently downgraded. ``detail`` names the loudest sources for the tooltip.
    """

    # (agent_id, category) -> {count, crit, suppressed, sources: {source: count}}
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    categories: set[str] = set()
    host_set: set[str] = set()
    for a in agents:
        aid = a["agent_id"]
        rel = _section(a.get("snapshot"), "reliability")
        events = rel.get("events") if rel else None
        for e in events or []:
            if not isinstance(e, dict):
                continue
            category = str(e.get("category") or e.get("source") or "Other")
            count = int(_num(e.get("count")) or 0)
            if count <= 0:
                continue
            host_set.add(aid)
            categories.add(category)
            cell = cells.setdefault(
                (aid, category), {"count": 0, "crit": False, "suppressed": 0, "sources": {}}
            )
            cell["count"] += count
            suppressed = bool(e.get("suppressed"))
            if suppressed:
                cell["suppressed"] += count
            elif e.get("level") == "critical" or e.get("severity") == "serious":
                cell["crit"] = True
            src = str(e.get("source") or "?")
            cell["sources"][src] = cell["sources"].get(src, 0) + count

    out_cells: list[dict[str, Any]] = []
    for (aid, category), c in cells.items():
        top_src = sorted(c["sources"].items(), key=lambda kv: kv[1], reverse=True)[:3]
        detail = ", ".join(f"{s} ×{n}" for s, n in top_src)
        out_cells.append(
            {
                "agent_id": aid,
                "category": category,
                "count": c["count"],
                "crit": c["crit"],
                "suppressed": c["suppressed"],
                "detail": detail,
                "members": [_member(aid, c["count"], detail)],
            }
        )

    return {
        "agents": sorted(host_set),
        "categories": sorted(categories),
        "cells": out_cells,
    }


def _kpis(
    agents: list[Agent], disk_forecasts: dict[str, list[dict[str, Any]]] | None = None
) -> list[dict[str, Any]]:
    """Actionable fleet totals — each a single number not shown by any chart."""

    from .trends import DISK_FULL_KPI_DAYS

    online = [a for a in agents if a.get("online")]
    disk_forecasts = disk_forecasts or {}

    filling_members: list[dict[str, Any]] = []
    for aid, forecasts in sorted(disk_forecasts.items()):
        filling = [
            f
            for f in forecasts
            if f.get("days_until_full") is not None
            and f["days_until_full"] < DISK_FULL_KPI_DAYS
        ]
        if filling:
            detail = ", ".join(f"{f['mount']} ~{f['days_until_full']:.0f}d" for f in filling)
            filling_members.append(_member(aid, len(filling), f"filling up: {detail}"))

    reboot_members: list[dict[str, Any]] = []
    updates_members: list[dict[str, Any]] = []
    updates_total = 0
    failed_members: list[dict[str, Any]] = []
    failed_total = 0
    quarantine_members: list[dict[str, Any]] = []
    quarantine_total = 0
    eol_members: list[dict[str, Any]] = []

    for a in agents:
        aid, snap, health = a["agent_id"], a.get("snapshot"), a.get("health") or {}
        sections = health.get("sections", {})

        rb = _section(snap, "reboot_pending")
        if rb and rb.get("pending") is True:
            reasons = ", ".join(str(r) for r in (rb.get("reasons") or [])) or "unknown"
            reboot_members.append(_member(aid, True, f"reboot pending ({reasons})"))

        au = _section(snap, "app_updates")
        if au:
            avail = int(_num(au.get("available")) or 0)
            if avail > 0:
                updates_total += avail
                updates_members.append(_member(aid, avail, f"{avail} upgrade(s) available"))

        wu = _section(snap, "win_update")
        if wu:
            failed = [u for u in (wu.get("recent") or []) if str(u.get("result", "")).lower() == "failed"]
            if failed:
                failed_total += len(failed)
                kbs = ", ".join(str(u.get("kb") or "?") for u in failed)
                failed_members.append(_member(aid, len(failed), f"{len(failed)} failed ({kbs})"))

        dq = _section(snap, "defender_quarantine")
        if dq:
            items = dq.get("items") or []
            if items:
                quarantine_total += len(items)
                quarantine_members.append(_member(aid, len(items), f"{len(items)} quarantined item(s)"))

        os_health = sections.get("os_support")
        if os_health and os_health.get("status") in ("warn", "crit"):
            eol_members.append(
                _member(aid, os_health["status"], os_health.get("reason") or os_health.get("summary") or "")
            )

    total = len(agents)
    online_count = len(online)
    # ``severity`` is a display-only "is this count worth flagging" label, not a
    # new health threshold: it says whether the KPI's own number is nonzero
    # (and, for a couple of counts that are worth treating as more than a
    # routine nudge, "crit"). It never re-derives anything `health_rules.py`
    # already scores — those thresholds stay there.
    return [
        {
            "key": "online",
            "label": "Hosts online",
            "value": online_count,
            "suffix": f"/ {total}",
            "severity": "warn" if online_count < total else "ok",
            "members": [
                _member(a["agent_id"], a.get("online", False), "online" if a.get("online") else "offline")
                for a in agents
            ],
        },
        {
            "key": "reboot",
            "label": "Reboots pending",
            "value": len(reboot_members),
            "severity": "warn" if reboot_members else "ok",
            "members": reboot_members,
        },
        {
            "key": "app_updates",
            "label": "Open app updates",
            "value": updates_total,
            "severity": "warn" if updates_total else "ok",
            "members": updates_members,
        },
        {
            "key": "failed_updates",
            "label": "Failed updates",
            "value": failed_total,
            "severity": "crit" if failed_total else "ok",
            "members": failed_members,
        },
        {
            "key": "quarantine",
            "label": "Quarantined threats",
            "value": quarantine_total,
            "severity": "crit" if quarantine_total else "ok",
            "members": quarantine_members,
        },
        {
            "key": "os_eol",
            "label": "OS end-of-life",
            "value": len(eol_members),
            "severity": "warn" if eol_members else "ok",
            "members": eol_members,
        },
        {
            "key": "disk_forecast",
            "label": "Disks filling <30d",
            "value": len(filling_members),
            "severity": "warn" if filling_members else "ok",
            "members": filling_members,
        },
    ]


def _health_mix(agents: list[Agent]) -> dict[str, Any]:
    """Donut: distribution of agents over their rolled-up overall status."""

    buckets: dict[str, list[dict[str, Any]]] = {"ok": [], "warn": [], "crit": [], "unknown": []}
    for a in agents:
        overall = (a.get("health") or {}).get("overall", "unknown")
        overall = overall if overall in buckets else "unknown"
        buckets[overall].append(_member(a["agent_id"], overall, overall))
    labels = {"ok": "OK", "warn": "Warning", "crit": "Critical", "unknown": "No data"}
    return {
        "segments": [
            {"key": k, "label": labels[k], "value": len(m), "members": m}
            for k, m in buckets.items()
            if m
        ]
    }


def _section_severity(agents: list[Agent]) -> dict[str, Any]:
    """Stacked bars: how many hosts are warn/crit per telemetry section."""

    rows: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for a in agents:
        aid = a["agent_id"]
        for name, s in ((a.get("health") or {}).get("sections", {})).items():
            status = s.get("status")
            if status not in ("warn", "crit"):
                continue
            row = rows.setdefault(name, {"warn": [], "crit": []})
            row[status].append(_member(aid, status, s.get("reason") or s.get("summary") or status))

    out = [
        {
            "section": name,
            "warn": len(r["warn"]),
            "crit": len(r["crit"]),
            "members_warn": r["warn"],
            "members_crit": r["crit"],
        }
        for name, r in rows.items()
    ]
    out.sort(key=lambda r: (r["crit"], r["warn"], r["section"]), reverse=True)
    return {"rows": out}


def _os_inventory(agents: list[Agent]) -> dict[str, Any]:
    """Pie: what OS versions make up the fleet."""

    buckets: dict[str, list[dict[str, Any]]] = {}
    for a in agents:
        family = _agent_os(a)
        os = _section(a.get("snapshot"), "os_support")
        if os and (os.get("name") or os.get("version")):
            # Never assume "Windows": if os_support carries no name, fall back to
            # the agent's declared OS family, not a hardcoded Windows label.
            name = str(os.get("name") or _OS_FAMILY_LABEL.get(family, "")).strip()
            version = str(os.get("version") or "").strip()
            label = f"{name} {version}".strip() or _OS_FAMILY_LABEL.get(family, "Unknown")
            build = os.get("build")
            detail = f"build {build}" if build else (os.get("summary") or label)
        elif family in _OS_FAMILY_LABEL:
            # No os_support telemetry (e.g. an early or stubbed agent): bucket by
            # the OS family it registered with rather than "Unknown"/"Windows".
            label = _OS_FAMILY_LABEL[family]
            detail = f"{label} agent (no os_support telemetry)"
        else:
            label, detail = "Unknown", "no os_support telemetry"
        buckets.setdefault(label, []).append(_member(a["agent_id"], label, detail))
    segments = [
        {"key": label, "label": label, "value": len(m), "members": m}
        for label, m in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
    ]
    return {"segments": segments}


def _device_mix(agents: list[Agent]) -> dict[str, Any]:
    """Pie: laptops (have a battery) vs desktops."""

    buckets: dict[str, list[dict[str, Any]]] = {"Laptop": [], "Desktop": [], "Server": []}
    for a in agents:
        bat = _section(a.get("snapshot"), "battery")
        if bat and bat.get("present") is True:
            charge = _num(bat.get("charge_percent"))
            detail = f"battery {charge:.0f}%" if charge is not None else "battery present"
            buckets["Laptop"].append(_member(a["agent_id"], "laptop", detail))
        elif _agent_os(a) == "linux":
            # A batteryless Linux host is a server/appliance, not a desktop PC.
            buckets["Server"].append(_member(a["agent_id"], "server", "Linux host, no battery"))
        else:
            buckets["Desktop"].append(_member(a["agent_id"], "desktop", "no battery"))
    return {
        "segments": [
            {"key": k, "label": k, "value": len(m), "members": m} for k, m in buckets.items() if m
        ]
    }


def _security_posture(agents: list[Agent]) -> dict[str, Any]:
    """Compliance ratios for three security controls (small-multiple donuts)."""

    metrics = {
        "encryption": {"label": "System drive encrypted", "compliant": [], "noncompliant": [], "unknown": [], "na": []},
        "defender_realtime": {"label": "Defender real-time on", "compliant": [], "noncompliant": [], "unknown": [], "na": []},
        "firewall": {"label": "Firewall fully on", "compliant": [], "noncompliant": [], "unknown": [], "na": []},
    }

    for a in agents:
        aid, snap = a["agent_id"], a.get("snapshot")

        # These three controls are Windows concepts (BitLocker, Microsoft
        # Defender, Windows Firewall profiles). For a non-Windows agent they do
        # not apply — mark them "n/a"/excluded rather than counting them as
        # "unknown", which would dilute the Windows compliance ratios (ADR-0031).
        if _agent_os(a) == "linux":
            for m in metrics.values():
                m["na"].append(_member(aid, "n/a", "not applicable on Linux"))
            continue

        enc = _section(snap, "encryption")
        sysvol = None
        for vol in (enc.get("volumes") if enc else []) or []:
            if str(vol.get("mount", "")).upper().startswith("C"):
                sysvol = vol
                break
        if sysvol is None:
            metrics["encryption"]["unknown"].append(_member(aid, None, "no encryption telemetry"))
        elif sysvol.get("protection_status") == 1:
            pct = _num(sysvol.get("encryption_percent"))
            metrics["encryption"]["compliant"].append(
                _member(aid, "on", f"{sysvol.get('mount')} encrypted" + (f" ({pct:.0f}%)" if pct is not None else ""))
            )
        else:
            metrics["encryption"]["noncompliant"].append(
                _member(aid, "off", f"{sysvol.get('mount')} not protected")
            )

        dfn = _section(snap, "defender")
        rt = dfn.get("realtime_protection") if dfn else None
        if rt is True:
            metrics["defender_realtime"]["compliant"].append(_member(aid, "on", "real-time protection on"))
        elif rt is False:
            metrics["defender_realtime"]["noncompliant"].append(_member(aid, "off", "real-time protection off"))
        else:
            metrics["defender_realtime"]["unknown"].append(_member(aid, None, "no defender telemetry"))

        fw = _section(snap, "firewall")
        profiles = (fw.get("profiles") if fw else None) or []
        if not profiles:
            metrics["firewall"]["unknown"].append(_member(aid, None, "no firewall telemetry"))
        elif all(p.get("enabled") is True for p in profiles):
            metrics["firewall"]["compliant"].append(_member(aid, "on", "all profiles enabled"))
        else:
            off = [str(p.get("name")) for p in profiles if p.get("enabled") is not True]
            metrics["firewall"]["noncompliant"].append(_member(aid, "off", f"disabled: {', '.join(off)}"))

    out = []
    for key, m in metrics.items():
        out.append(
            {
                "key": key,
                "label": m["label"],
                "compliant": len(m["compliant"]),
                "noncompliant": len(m["noncompliant"]),
                "unknown": len(m["unknown"]),
                "na": len(m["na"]),
                "members_compliant": m["compliant"],
                "members_noncompliant": m["noncompliant"],
                "members_unknown": m["unknown"],
                "members_na": m["na"],
            }
        )
    return {"metrics": out}


def _top_metrics(agents: list[Agent]) -> dict[str, Any]:
    """Per-host continuous metrics, each ranked desc with full drill-down detail."""

    disk, mem, crashes, uptime, updates = [], [], [], [], []
    for a in agents:
        aid, snap = a["agent_id"], a.get("snapshot")

        d = _section(snap, "disk")
        worst_pct, worst_mount = -1.0, ""
        for vol in (d.get("volumes") if d else []) or []:
            pct = _num(vol.get("percent_used"))
            if pct is not None and pct > worst_pct:
                worst_pct, worst_mount = pct, str(vol.get("mount", "?"))
        if worst_pct >= 0:
            disk.append(_member(aid, round(worst_pct, 1), f"{worst_mount} {worst_pct:.0f}% full"))

        m = _section(snap, "memory")
        mpct = _num(m.get("percent_used")) if m else None
        if mpct is not None:
            disk_detail = f"{mpct:.0f}% used"
            total = _num(m.get("total_bytes"))
            if total:
                disk_detail += f" of {total / 1024 ** 3:.0f} GB"
            mem.append(_member(aid, round(mpct, 1), disk_detail))

        r = _section(snap, "reliability")
        rc = _num(r.get("recent_crashes")) if r else None
        if rc is not None:
            si = _num(r.get("stability_index"))
            crashes.append(
                _member(aid, int(rc), f"{int(rc)} crash(es) / 7d" + (f", stability {si:.1f}" if si is not None else ""))
            )

        u = _section(snap, "uptime")
        secs = _num(u.get("uptime_secs")) if u else None
        if secs is not None:
            uptime.append(_member(aid, round(secs / 86400, 1), f"up {secs / 86400:.1f} days"))

        au = _section(snap, "app_updates")
        avail = _num(au.get("available")) if au else None
        if avail is not None:
            updates.append(_member(aid, int(avail), f"{int(avail)} upgrade(s) available"))

    def ranked(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(members, key=lambda x: x["value"], reverse=True)

    return {
        "metrics": [
            {"key": "disk", "label": "Disk usage", "unit": "%", "entries": ranked(disk)},
            {"key": "memory", "label": "Memory usage", "unit": "%", "entries": ranked(mem)},
            {"key": "crashes", "label": "Recent crashes (7d)", "unit": "", "entries": ranked(crashes)},
            {"key": "uptime", "label": "Uptime", "unit": "d", "entries": ranked(uptime)},
            {"key": "app_updates", "label": "Pending app updates", "unit": "", "entries": ranked(updates)},
        ]
    }


def _sankey(agents: list[Agent]) -> dict[str, Any]:
    """Sankey flow: host → severity (warn/crit) → responsible section.

    Nodes are tagged with ``kind`` so the frontend can colour them; every link
    carries the ``members`` (host/section pairs) behind its width for drill-down.
    """

    host_sev: dict[tuple[str, str], list[dict[str, Any]]] = {}
    sev_section: dict[tuple[str, str], list[dict[str, Any]]] = {}
    hosts: set[str] = set()
    sections: set[str] = set()
    sev_used: set[str] = set()

    for a in agents:
        aid = a["agent_id"]
        for name, s in ((a.get("health") or {}).get("sections", {})).items():
            status = s.get("status")
            if status not in ("warn", "crit"):
                continue
            reason = s.get("reason") or s.get("summary") or status
            hosts.add(aid)
            sections.add(name)
            sev_used.add(status)
            host_sev.setdefault((aid, status), []).append(_member(aid, name, reason))
            sev_section.setdefault((status, name), []).append(_member(aid, name, reason))

    sev_label = {"warn": "Warning", "crit": "Critical"}
    nodes = (
        [{"name": h, "kind": "host"} for h in sorted(hosts)]
        + [{"name": sev_label[s], "kind": s} for s in ("warn", "crit") if s in sev_used]
        + [{"name": n, "kind": "section"} for n in sorted(sections)]
    )

    links: list[dict[str, Any]] = []
    for (aid, status), members in host_sev.items():
        links.append({"source": aid, "target": sev_label[status], "value": len(members), "members": members})
    for (status, name), members in sev_section.items():
        links.append({"source": sev_label[status], "target": name, "value": len(members), "members": members})

    return {"nodes": nodes, "links": links}


# --------------------------------------------------------------------------
# Trend (time-series fleet health)
# --------------------------------------------------------------------------


def aggregate_trend(
    points_by_agent: dict[str, list[dict[str, Any]]],
    days: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bucket per-agent overall-health points into daily fleet status counts.

    ``points_by_agent`` maps an agent id to a list of
    ``{"collected_at": iso, "overall": status}`` points (the web layer computes
    ``overall`` via the health rules). For each calendar day each agent counts
    once, using its last status that day. Returns ``{"days": [...]}`` ascending,
    each day carrying status counts and ``members`` for drill-down.
    """

    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).date()

    # (date -> {agent_id -> overall}) keeping the last point per agent per day.
    per_day: dict[str, dict[str, str]] = {}
    for aid, points in points_by_agent.items():
        for p in points:
            collected = p.get("collected_at")
            if not isinstance(collected, str) or len(collected) < 10:
                continue
            date = collected[:10]
            try:
                if datetime.fromisoformat(date).date() < cutoff:
                    continue
            except ValueError:
                continue
            overall = p.get("overall", "unknown")
            per_day.setdefault(date, {})[aid] = overall if overall in ("ok", "warn", "crit") else "unknown"

    out = []
    for date in sorted(per_day):
        members: dict[str, list[dict[str, Any]]] = {"ok": [], "warn": [], "crit": [], "unknown": []}
        for aid, overall in per_day[date].items():
            members[overall].append(_member(aid, overall, overall))
        out.append(
            {
                "date": date,
                "ok": len(members["ok"]),
                "warn": len(members["warn"]),
                "crit": len(members["crit"]),
                "unknown": len(members["unknown"]),
                "members": members,
            }
        )
    return {"days": out}

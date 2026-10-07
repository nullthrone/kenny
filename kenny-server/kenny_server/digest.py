"""Weekly fleet digest: a short overview over existing data (ADR-0027).

``build_digest`` reduces the operator's week to a coarse overview: the fleet
health mix, one line per host that needs attention (section names only, no rule
reasons), 7-day alert and change counts, pending maintenance, posture findings
per host (ADR-0058) and screen time (ADR-0029). The detail behind every line is
on the dashboard, so when the dashboard's public URL is known each host links to
its host page and the message ends with a link into the fleet view.

Everything is derived from data already in the stores; the digest adds no
collection and no storage beyond its last-sent timestamp (``alert_state`` scope
``digest``, owned by the scheduler in ``alerting.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from .health_rules import _dicts, evaluate_snapshot
from .registry import AgentRegistry
from .store import EventStore, TelemetryStore
from .trends import DISK_FULL_KPI_DAYS, battery_trend, disk_forecast

_MAX_HOSTS = 8
_MAX_LIST = 6
_MAX_SECTIONS = 3
_MARKERS = {"crit": "\U0001F534", "warn": "\U0001F7E0"}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_~|\[\]()<>#])")
_EMPTY = "No agents have reported telemetry yet."


@dataclass(frozen=True)
class Digest:
    title: str
    # Plain text for every channel; links only as a trailing dashboard URL.
    body: str
    # The same overview with each host linked, for channels that render
    # markdown (Discord).
    markdown: str


@dataclass
class _Fleet:
    hosts: int = 0
    online: int = 0
    health: dict[str, int] = field(default_factory=lambda: {"ok": 0, "warn": 0, "crit": 0})
    # (agent_id, overall severity, section names at that severity)
    attention: list[tuple[str, str, list[str]]] = field(default_factory=list)
    posture: list[tuple[str, int]] = field(default_factory=list)
    alerts: int = 0
    crit_alerts: int = 0
    changes: int = 0
    reboots: int = 0
    failed_updates: int = 0
    failed_update_hosts: int = 0
    eol: list[str] = field(default_factory=list)
    # agent_id -> soonest days-until-full across its volumes
    disk_soonest: dict[str, float] = field(default_factory=dict)
    battery_wear: list[str] = field(default_factory=list)
    screen_hours: list[tuple[str, float]] = field(default_factory=list)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _capped(items: list[str], limit: int, sep: str = " · ") -> str:
    shown = sep.join(items[:limit])
    return f"{shown} +{len(items) - limit}" if len(items) > limit else shown


def _escape(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


async def _collect(
    store: TelemetryStore, event_store: EventStore, registry: AgentRegistry, now: datetime
) -> _Fleet:
    week_ago = now - timedelta(days=7)
    forecast_since = (now - timedelta(days=30)).date().isoformat()
    fleet = _Fleet()

    agents = await store.known_agents()
    fleet.hosts = len(agents)
    for agent_id in agents:
        agent = registry.get(agent_id)
        if agent is not None and agent.online:
            fleet.online += 1
        latest = await store.latest(agent_id)
        if latest is None:
            continue
        snapshot = latest["snapshot"]
        agent_os = getattr(agent, "os", "windows")
        evaluation = evaluate_snapshot(snapshot, agent_os=agent_os, now=now)
        sections = evaluation["sections"]
        overall = evaluation["overall"]
        fleet.health[overall] = fleet.health.get(overall, 0) + 1
        if overall != "ok":
            worst = [name for name, sec in sections.items() if sec["status"] == overall]
            fleet.attention.append((agent_id, overall, worst))
        standing = sum(1 for sec in sections.values() if sec.get("tier") == "posture")
        if standing:
            fleet.posture.append((agent_id, standing))

        if (snapshot.get("reboot_pending") or {}).get("pending") is True:
            fleet.reboots += 1
        recent = _dicts((snapshot.get("win_update") or {}).get("recent"))
        failed = {
            str(u.get("kb") or u.get("title") or "?")
            for u in recent
            if str(u.get("result", "")).lower() == "failed"
        }
        if failed:
            fleet.failed_updates += len(failed)
            fleet.failed_update_hosts += 1
        if sections.get("os_support", {}).get("status") in ("warn", "crit"):
            fleet.eol.append(agent_id)

        daily = await store.daily_latest(agent_id, forecast_since)
        soonest = [
            f["days_until_full"]
            for f in disk_forecast(daily)
            if f["days_until_full"] is not None and f["days_until_full"] < DISK_FULL_KPI_DAYS
        ]
        if soonest:
            fleet.disk_soonest[agent_id] = min(soonest)
        battery = battery_trend(daily)
        if battery and battery["percent_per_30d"] is not None and battery["percent_per_30d"] < -1:
            fleet.battery_wear.append(agent_id)

        days = _dicts((snapshot.get("screen_time") or {}).get("days"))
        if days:
            minutes = sum(
                d.get("active_minutes", 0)
                for d in days
                if isinstance(d.get("active_minutes"), (int, float))
            )
            fleet.screen_hours.append((agent_id, minutes / 60))

    alert_events = [
        e
        for e in await event_store.query(kind="alert", limit=1000)
        if (ts := _parse_ts(e.get("at"))) is not None
        and ts >= week_ago
        and (e.get("fields") or {}).get("kind") != "digest"
    ]
    fleet.changes = sum(1 for e in alert_events if (e.get("fields") or {}).get("kind") == "change")
    fleet.alerts = len(alert_events) - fleet.changes
    fleet.crit_alerts = sum(
        1
        for e in alert_events
        if e.get("level") == "crit" and (e.get("fields") or {}).get("kind") != "change"
    )
    fleet.attention.sort(key=lambda a: (a[1] != "crit", a[0]))
    return fleet


def _render(fleet: _Fleet, base_url: str, *, markdown: bool) -> str:
    """One renderer for both outputs, so plain text and markdown cannot drift."""

    esc: Callable[[str], str] = _escape if markdown else (lambda s: s)

    def page(path: str) -> str:
        return f"{base_url}/#/{path}"

    def link(text: str, path: str) -> str:
        if markdown and base_url:
            return f"[{text}]({page(path)})"
        return text

    def host(agent_id: str, section: str = "") -> str:
        path = f"fleet/{quote(agent_id, safe='')}"
        if section:
            path += f"?section={quote(section, safe='')}"
        return link(esc(agent_id), path)

    if not fleet.hosts:
        return _EMPTY

    h = fleet.health
    lines = [
        f"{_plural(fleet.hosts, 'host')} · {fleet.online} online · "
        f"{h.get('crit', 0)} crit · {h.get('warn', 0)} warn · {h.get('ok', 0)} ok"
    ]

    if fleet.attention:
        lines += ["", "**Needs attention**" if markdown else "Needs attention:"]
        for agent_id, severity, sections in fleet.attention[:_MAX_HOSTS]:
            names = _capped([esc(s) for s in sections], _MAX_SECTIONS, ", ")
            first = sections[0] if sections else ""
            lines.append(f"{_MARKERS.get(severity, '')} {host(agent_id, first)}: {names}".strip())
        if len(fleet.attention) > _MAX_HOSTS:
            lines.append(link(f"+{len(fleet.attention) - _MAX_HOSTS} more hosts", "fleet"))

    lines.append("")
    alerts = f"{_plural(fleet.alerts, 'alert')} ({fleet.crit_alerts} crit)"
    lines.append(f"This week: {link(alerts, 'log')} · {_plural(fleet.changes, 'change')}")

    todo: list[str] = []
    if fleet.reboots:
        todo.append(f"{_plural(fleet.reboots, 'reboot')} pending")
    if fleet.failed_updates:
        todo.append(
            f"{_plural(fleet.failed_updates, 'failed update')} "
            f"on {_plural(fleet.failed_update_hosts, 'host')}"
        )
    if fleet.eol:
        todo.append("OS end of life: " + _capped([host(a, "os_support") for a in fleet.eol], _MAX_LIST, ", "))
    if fleet.disk_soonest:
        filling = sorted(fleet.disk_soonest.items(), key=lambda kv: kv[1])
        todo.append(
            "disk filling: "
            + _capped([f"{host(a, 'disk')} (~{d:.0f}d)" for a, d in filling], _MAX_LIST, ", ")
        )
    if fleet.battery_wear:
        todo.append(
            "battery wear: " + _capped([host(a, "battery") for a in fleet.battery_wear], _MAX_LIST, ", ")
        )
    if todo:
        lines.append("To do: " + " · ".join(todo))

    if fleet.posture:
        # Standing facts, once a week and nowhere else (ADR-0058); the
        # findings themselves are on each host page.
        lines.append(
            "Posture: " + _capped([f"{host(a)} ({n})" for a, n in fleet.posture], _MAX_LIST)
        )
    if fleet.screen_hours:
        lines.append(
            "Screen time (7d): "
            + _capped([f"{host(a, 'screen_time')} {hrs:.1f}h" for a, hrs in fleet.screen_hours], _MAX_LIST)
        )

    if base_url:
        lines += ["", f"[Open kenny]({page('fleet')})" if markdown else f"Details: {page('fleet')}"]
    return "\n".join(lines)


async def build_digest(
    store: TelemetryStore,
    event_store: EventStore,
    registry: AgentRegistry,
    *,
    now: datetime | None = None,
    base_url: str = "",
) -> Digest:
    """Render the weekly digest.

    ``base_url`` is the dashboard origin links point at; empty means no links.
    """

    now = now or datetime.now(timezone.utc)
    fleet = await _collect(store, event_store, registry, now)
    base = base_url.rstrip("/")
    return Digest(
        title=f"kenny weekly digest - {now.date().isoformat()}",
        body=_render(fleet, base, markdown=False),
        markdown=_render(fleet, base, markdown=True),
    )

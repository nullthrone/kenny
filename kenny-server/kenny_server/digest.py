"""Weekly fleet digest: a plain-text summary over existing data (ADR-0029).

``build_digest`` renders the operator's week — fleet health, alert and change
counts (read back from the events table the alert loop writes), disk/battery
forecasts (ADR-0030), pending maintenance, and screen time (ADR-0032) — into a
short plain-text body that fits an ntfy notification. Everything is derived
from data already in the stores; the digest adds no collection and no storage
beyond its last-sent timestamp (``alert_state`` scope ``digest``, owned by the
scheduler in ``alerting.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .health_rules import evaluate_snapshot
from .registry import AgentRegistry
from .store import EventStore, TelemetryStore
from .trends import DISK_FULL_KPI_DAYS, battery_trend, disk_forecast

_MAX_LIST_LINES = 6


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def build_digest(
    store: TelemetryStore,
    event_store: EventStore,
    registry: AgentRegistry,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return ``(title, body)`` for the weekly digest."""

    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    forecast_since = (now - timedelta(days=30)).date().isoformat()

    agents = await store.known_agents()
    online = 0
    health_counts = {"ok": 0, "warn": 0, "crit": 0}
    degraded: list[str] = []
    reboots = 0
    failed_updates = 0
    eol_hosts: list[str] = []
    forecast_lines: list[str] = []
    battery_lines: list[str] = []
    screen_lines: list[str] = []

    for agent_id in agents:
        agent = registry.get(agent_id)
        if agent is not None and agent.online:
            online += 1
        latest = await store.latest(agent_id)
        if latest is None:
            continue
        snapshot = latest["snapshot"]
        evaluation = evaluate_snapshot(snapshot, now=now)
        overall = evaluation["overall"]
        health_counts[overall] = health_counts.get(overall, 0) + 1
        if overall != "ok":
            worst = [
                f"{name} {sec['status']}"
                for name, sec in evaluation["sections"].items()
                if sec["status"] == overall
            ]
            degraded.append(f"{agent_id} ({overall}: {', '.join(worst[:3])})")

        if (snapshot.get("reboot_pending") or {}).get("pending") is True:
            reboots += 1
        recent = (snapshot.get("win_update") or {}).get("recent") or []
        failed_updates += sum(
            1 for u in recent if str(u.get("result", "")).lower() == "failed"
        )
        if evaluation["sections"].get("os_support", {}).get("status") in ("warn", "crit"):
            eol_hosts.append(agent_id)

        daily = await store.daily_latest(agent_id, forecast_since)
        for f in disk_forecast(daily):
            if f["days_until_full"] is not None and f["days_until_full"] < DISK_FULL_KPI_DAYS:
                forecast_lines.append(
                    f"{agent_id} {f['mount']} full in ~{f['days_until_full']:.0f}d"
                )
        battery = battery_trend(daily)
        if battery and battery["percent_per_30d"] is not None and battery["percent_per_30d"] < -1:
            battery_lines.append(
                f"{agent_id} health {battery['current_percent']:.0f}% "
                f"({battery['percent_per_30d']:+.1f}%/30d)"
            )

        screen = snapshot.get("screen_time") or {}
        days = screen.get("days") or []
        minutes = sum(
            d.get("active_minutes", 0)
            for d in days
            if isinstance(d.get("active_minutes"), (int, float))
        )
        if days:
            screen_lines.append(f"{agent_id} {minutes / 60:.1f}h")

    alert_events = [
        e
        for e in await event_store.query(kind="alert", limit=1000)
        if (ts := _parse_ts(e.get("at"))) is not None
        and ts >= week_ago
        and (e.get("fields") or {}).get("kind") != "digest"
    ]
    changes = sum(1 for e in alert_events if (e.get("fields") or {}).get("kind") == "change")
    alerts = len(alert_events) - changes
    crit_alerts = sum(
        1
        for e in alert_events
        if e.get("level") == "crit" and (e.get("fields") or {}).get("kind") != "change"
    )

    lines = [
        f"Fleet: {len(agents)} host(s), {online} online. "
        f"Health: {health_counts['ok']} ok / {health_counts['warn']} warn / {health_counts['crit']} crit.",
    ]
    if degraded:
        lines.append("Degraded: " + "; ".join(degraded[:_MAX_LIST_LINES]))
    lines.append(f"Alerts (7d): {alerts} ({crit_alerts} crit), changes: {changes}.")
    if forecast_lines:
        lines.append("Disks filling: " + "; ".join(forecast_lines[:_MAX_LIST_LINES]))
    if battery_lines:
        lines.append("Batteries: " + "; ".join(battery_lines[:_MAX_LIST_LINES]))
    pending = []
    if reboots:
        pending.append(f"{reboots} reboot(s) pending")
    if failed_updates:
        pending.append(f"{failed_updates} failed update(s)")
    if eol_hosts:
        pending.append(f"OS EOL: {', '.join(eol_hosts[:_MAX_LIST_LINES])}")
    if pending:
        lines.append("Pending: " + "; ".join(pending) + ".")
    if screen_lines:
        lines.append("Screen time (7d): " + "; ".join(screen_lines[:_MAX_LIST_LINES]) + ".")
    if not agents:
        lines = ["No agents have reported telemetry yet."]

    title = f"kenny weekly digest - {now.date().isoformat()}"
    return title, "\n".join(lines)

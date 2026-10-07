"""The I/O around the long-lived hardware history (ADR-0070).

Two jobs the pure modules cannot do: :func:`rollup_agent` / :func:`rollup_all`
reduce stored snapshots to ``hw_metrics`` rows, and :func:`load_forecasts` reads
them back and runs the cross-day judgements. The reductions themselves live in
``hardware_metrics`` and ``trends``; this module only moves data between them and
the stores.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import hardware_metrics, trends
from .store import HardwareHistoryStore, TelemetryStore

logger = logging.getLogger("kenny.hardware_history")

#: What the dashboard's device series cover.
WINDOW_DAYS = 180

#: ``since_day`` that selects every stored day of an agent, whatever the retention is.
ALL_TIME = "0001-01-01"


def _day_of(stamp: str) -> date | None:
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


async def rollup_agent(
    store: TelemetryStore,
    history: HardwareHistoryStore,
    agent_id: str,
    *,
    now: datetime | None = None,
) -> int:
    """Roll one agent's snapshots up to today's UTC day; returns rows written.

    Starts the day after ``hw_rollup_state.last_day`` -- or at the agent's oldest
    stored snapshot on the first run, which backfills whatever the snapshot
    retention still holds -- and walks to today inclusive. Days up to yesterday
    are final; today is a provisional upsert that the next run rewrites, because
    ``last_day`` is left at yesterday. One transaction per agent, idempotent.
    """

    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    last = await history.last_day(agent_id)
    start: date | None
    if last is not None:
        previous = _day_of(last)
        start = previous + timedelta(days=1) if previous is not None else None
    else:
        first = (await store.first_collected_many([agent_id])).get(agent_id)
        start = _day_of(first) if first else None
    if start is None or start > today:
        return 0

    rows: list[tuple[str, str, str, float]] = []
    day = start
    while day <= today:
        records = await store.snapshots_for_day(agent_id, day.isoformat(), hardware_metrics.SECTIONS)
        snapshots = [r["snapshot"] for r in records]
        if snapshots:
            rows.extend(
                (key, metric, day.isoformat(), value)
                for key, metric, value in hardware_metrics.extract(snapshots, day.isoformat())
            )
        day += timedelta(days=1)
    return await history.record(agent_id, rows, last_day=(today - timedelta(days=1)).isoformat())


async def rollup_all(
    store: TelemetryStore, history: HardwareHistoryStore, *, now: datetime | None = None
) -> int:
    """Roll up every known agent; one failing agent never stops the rest."""

    total = 0
    for agent_id in await store.known_agents():
        try:
            total += await rollup_agent(store, history, agent_id, now=now)
        except Exception:  # noqa: BLE001 - best-effort maintenance
            logger.exception("hardware history rollup failed for %s", agent_id)
    return total


async def load_forecasts(
    store: TelemetryStore,
    history: HardwareHistoryStore,
    agent_id: str,
    *,
    now: datetime | None = None,
    series: dict[str, dict[str, list[tuple[str, float]]]] | None = None,
    labels: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """The hardware at risk on one host, from its whole stored history.

    The forecasts see the full retention window, not just what the dashboard
    shows: a fan's baseline is the first 30 days of its history. ``series`` and
    ``labels`` may be passed in by a caller that has already read them.
    """

    now = now or datetime.now(timezone.utc)
    if series is None:
        series = await history.series(agent_id, ALL_TIME)
    if not series:
        return []
    if labels is None:
        latest = await store.latest(agent_id)
        labels = hardware_metrics.device_labels(latest["snapshot"] if latest else None)
    return trends.hardware_forecasts(series, labels, now.astimezone(timezone.utc).date())


def empty_payload() -> dict[str, Any]:
    """The ``hardware`` object for a server or host with no history."""

    return {"window_days": WINDOW_DAYS, "devices": [], "forecasts": []}


def api_payload(
    series: dict[str, dict[str, list[tuple[str, float]]]],
    labels: dict[str, tuple[str, str]],
    forecasts: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The ``hardware`` object of ``/api/agent/{id}/trends``.

    Series are limited to the last ``WINDOW_DAYS`` days; a device appears when it
    has any series in the window.
    """

    now = now or datetime.now(timezone.utc)
    since = (now.astimezone(timezone.utc).date() - timedelta(days=WINDOW_DAYS)).isoformat()
    devices = []
    for key in sorted(series):
        windowed = {
            metric: [{"day": day, "value": value} for day, value in points if day >= since]
            for metric, points in sorted(series[key].items())
        }
        windowed = {metric: points for metric, points in windowed.items() if points}
        if not windowed:
            continue
        kind, label = labels.get(key) or (
            hardware_metrics.kind_of(key),
            hardware_metrics.fallback_label(key),
        )
        devices.append({"device_key": key, "kind": kind, "label": label, "series": windowed})
    return {
        "window_days": WINDOW_DAYS,
        "devices": devices,
        "forecasts": [
            {
                "device_key": f["device_key"],
                "kind": f["kind"],
                "label": f["label"],
                "reason": f["reason"],
                "symptom": f["symptom"],
                "days_until": f["days_until"],
            }
            for f in forecasts
        ],
    }

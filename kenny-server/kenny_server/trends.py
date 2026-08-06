"""Cross-snapshot trend analysis over the daily history.

Pure, I/O-free functions fed with ``TelemetryStore.daily_latest()`` output
(one representative snapshot per UTC day, oldest first). An ordinary
least-squares fit over the daily points powers a "disk full in ~N days"
forecast and a battery-degradation trend. Forecasts are deliberately shy:
no forecast without at least ``MIN_POINTS`` days, a rising slope and a
reasonable fit (r²), so a noisy series yields ``None`` instead of a scary
made-up number.

Cross-snapshot thresholds live *here*, which is the one deliberate exception to
"thresholds only in ``health_rules.py``": that module is evaluated against a
single snapshot by design and has nowhere to put a judgement that spans days.
``DISK_FULL_ALERT_DAYS`` is the alert loop's forecast threshold. Keep the
exception small — a rule that *can* be expressed per-snapshot belongs in
``health_rules.py``, not here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

MIN_POINTS = 5
MIN_R2 = 0.5
# A volume forecast below this many days-until-full raises an alert (24 h cooldown).
DISK_FULL_ALERT_DAYS = 14.0
# The Overview KPI counts hosts with any volume forecast under this horizon.
DISK_FULL_KPI_DAYS = 30.0


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """OLS fit; returns ``(slope, r2)`` or None for degenerate input."""

    n = len(points)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    ss_xx = sum((x - mean_x) ** 2 for x, _ in points)
    if ss_xx == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / ss_xx
    ss_tot = sum((y - mean_y) ** 2 for _, y in points)
    if ss_tot == 0:
        return slope, 1.0  # perfectly flat series fits its own (zero-slope) line
    ss_res = sum((y - (mean_y + slope * (x - mean_x))) ** 2 for x, y in points)
    return slope, 1.0 - ss_res / ss_tot


def _daily_series(
    daily: list[dict[str, Any]], section: str, extract: Any
) -> dict[str, list[tuple[float, float]]]:
    """Build per-key ``(day_index, value)`` series from daily snapshots."""

    series: dict[str, list[tuple[float, float]]] = {}
    day0: date | None = None
    for entry in daily:
        day = _parse_day(entry.get("collected_at"))
        payload = (entry.get("snapshot") or {}).get(section)
        if day is None or not isinstance(payload, dict):
            continue
        if day0 is None:
            day0 = day
        for key, value in extract(payload):
            if isinstance(value, (int, float)):
                series.setdefault(key, []).append(((day - day0).days, float(value)))
    return series


def disk_forecast(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Days-until-full estimate per volume from the daily ``percent_used`` series."""

    def volumes(payload: dict[str, Any]):
        for vol in payload.get("volumes") or []:
            if isinstance(vol, dict) and vol.get("mount"):
                yield str(vol["mount"]), vol.get("percent_used")

    out: list[dict[str, Any]] = []
    for mount, points in sorted(_daily_series(daily, "disk", volumes).items()):
        current = points[-1][1]
        fit = _fit(points)
        days_until_full: float | None = None
        slope = 0.0
        if fit is not None:
            slope, r2 = fit
            if slope > 0 and len(points) >= MIN_POINTS and r2 >= MIN_R2:
                days_until_full = max(0.0, (100.0 - current) / slope)
        out.append(
            {
                "mount": mount,
                "current_percent": current,
                "slope_percent_per_day": round(slope, 3),
                "days_until_full": round(days_until_full, 1) if days_until_full is not None else None,
                "points": len(points),
            }
        )
    return out


def battery_trend(daily: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Battery health drift as percent per 30 days, or None without a battery."""

    def health(payload: dict[str, Any]):
        yield "battery", payload.get("health_percent")

    points = _daily_series(daily, "battery", health).get("battery")
    if not points:
        return None
    fit = _fit(points)
    return {
        "current_percent": points[-1][1],
        "percent_per_30d": round(fit[0] * 30.0, 2) if fit is not None else None,
        "points": len(points),
    }

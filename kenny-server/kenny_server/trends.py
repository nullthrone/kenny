"""Cross-snapshot trend analysis over the daily history.

Pure, I/O-free functions fed with ``TelemetryStore.daily_latest()`` output
(one representative snapshot per UTC day, oldest first). An ordinary
least-squares fit over the daily points powers a "disk full in ~N days"
forecast and a battery-degradation trend. Forecasts are deliberately shy:
no forecast without at least ``MIN_POINTS`` days, a rising slope and a
reasonable fit (r²), so a noisy series yields ``None`` instead of a scary
made-up number.

The hardware half (``hardware_forecasts`` and the counter, wear, PCIe, fan and
error-rate functions under it) reads the long-lived per-device history instead
(``HardwareHistoryStore.series``, ADR-0070), because wear and bearing drift
unfold over months.

Cross-snapshot thresholds live *here*, which is the one deliberate exception to
"thresholds only in ``health_rules.py``": that module is evaluated against a
single snapshot by design and has nowhere to put a judgement that spans days.
``DISK_FULL_ALERT_DAYS`` is the alert loop's forecast threshold. Keep the
exception small — a rule that *can* be expressed per-snapshot belongs in
``health_rules.py``, not here.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from . import hardware_metrics

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

    try:
        fit = _fit_unchecked(points)
    except OverflowError:
        # Finite but huge wire values (e.g. 1e308) overflow when squared; such a
        # series has no usable trend line.
        return None
    if fit is None or not all(math.isfinite(v) for v in fit):
        return None
    return fit


def _fit_unchecked(points: list[tuple[float, float]]) -> tuple[float, float] | None:
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
            # `value` is an unvalidated wire extra (protocol.Section allows any
            # extra field) -- reject bools (isinstance(True, int) is True) and
            # anything that can't survive becoming a real float: an oversized
            # int raises OverflowError, and a non-finite float is never a
            # usable trend point.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                as_float = float(value)
            except OverflowError:
                continue
            if not math.isfinite(as_float):
                continue
            series.setdefault(key, []).append(((day - day0).days, as_float))
    return series


def disk_forecast(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Days-until-full estimate per volume from the daily ``percent_used`` series."""

    def volumes(payload: dict[str, Any]):
        # `disk.volumes` is an unvalidated wire extra (protocol.Section allows any
        # extra field) -- a malfunctioning/malicious agent can push a non-list value.
        raw = payload.get("volumes")
        for vol in raw if isinstance(raw, list) else []:
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


# =============================================================================
# Hardware history (ADR-0070)
#
# Everything below is fed with ``HardwareHistoryStore.series`` output: per device,
# per metric, ``[(UTC day, value)]`` ascending. A series is an ordered list of
# days *with data*; a missing day is unknown, not zero, except where a function
# says otherwise.
# =============================================================================

Series = list[tuple[str, float]]

# -- thresholds (cross-day judgements live here, see the module docstring) -----
#: A wear-out projection under this many days raises a hardware forecast.
WEAR_OUT_ALERT_DAYS = 180.0
#: A spare-capacity projection under this many days raises a hardware forecast.
SPARE_DECLINE_ALERT_DAYS = 90.0
#: Window the wear and spare fits look at.
WEAR_FIT_DAYS = 180
#: A counter that first left zero longer ago than this, and has not moved since,
#: is history, not news.
FIRST_ERROR_RECENT_DAYS = 30
#: A device with no data for this long is gone (replaced, removed), not failing.
STALE_DEVICE_DAYS = 14

PCIE_WINDOW_DAYS = 7
PCIE_MIN_DAYS = 3

FAN_BASELINE_DAYS = 30
FAN_BASELINE_MIN_DAYS = 5
FAN_REBASELINE_GAP_DAYS = 30
FAN_RECENT_DAYS = 7
FAN_RECENT_MIN_DAYS = 3
FAN_SUSTAINED_DAYS = 5
FAN_MIN_DROP = 0.12

RATE_RECENT_DAYS = 7
RATE_PRIOR_DAYS = 21
RATE_FACTOR = 2.0
RATE_MIN_EVENTS = 3

#: Which telemetry section a forecast about each device kind belongs to; what the
#: alert names so the ticket merges with that section's own finding.
FORECAST_SECTION: dict[str, str] = {
    "disk": "disk_smart",
    "gpu": "gpu",
    "fan": "fans",
    "component": "hardware_errors",
}

#: Disk counters whose first non-zero value is worth a forecast, most telling
#: first.
FIRST_ERROR_METRICS: tuple[str, ...] = (
    "media_errors",
    "smart_197",
    "smart_198",
    "smart_5",
    "read_errors_uncorrected",
    "write_errors_uncorrected",
)
_FIRST_ERROR_SYMPTOM = {
    "media_errors": "{label} reported its first media or data-integrity error",
    "smart_197": "{label} has sectors it cannot read reliably, waiting to be remapped",
    "smart_198": "{label} found sectors it cannot read during its own scan",
    "smart_5": "{label} began replacing damaged sectors",
    "read_errors_uncorrected": (
        "{label} failed to read data it could not recover, for the first time"
    ),
    "write_errors_uncorrected": (
        "{label} failed to write data it could not recover, for the first time"
    ),
}
#: Per-component event series, and the counters whose daily growth stands in for one.
_EVENT_METRICS: tuple[str, ...] = ("fatal_events", "instability_events", "corrected_events")
_COUNTER_METRICS: tuple[str, ...] = ("edac_ue", "aer_uncorrected", "edac_ce", "aer_correctable")
_RATE_KIND = {
    "fatal_events": "fatal",
    "edac_ue": "fatal",
    "aer_uncorrected": "fatal",
    "instability_events": "instability",
    "corrected_events": "corrected",
    "edac_ce": "corrected",
    "aer_correctable": "corrected",
}
_RATE_SYMPTOM = {
    "fatal": (
        "{label} is reporting uncorrectable hardware errors more often "
        "({recent:.0f} in the last 7 days, up from about {prior:.1f} a week)"
    ),
    "instability": (
        "{label} faults are becoming more frequent "
        "({recent:.0f} in the last 7 days, up from about {prior:.1f} a week)"
    ),
    "corrected": (
        "{label} is correcting hardware errors more often "
        "({recent:.0f} in the last 7 days, up from about {prior:.1f} a week)"
    ),
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError:
        return None
    return out if math.isfinite(out) else None


def _points(series: Any) -> list[tuple[date, float]]:
    """Valid ``(day, value)`` points of a series, oldest first, one per day."""

    by_day: dict[date, float] = {}
    for item in series if isinstance(series, (list, tuple)) else []:
        try:
            raw_day, raw_value = item
        except (TypeError, ValueError):
            continue
        day, value = _parse_day(raw_day), _finite(raw_value)
        if day is not None and value is not None:
            by_day[day] = value
    return sorted(by_day.items())


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _parse_day(value)


def counter_deltas(series: Series) -> Series:
    """Growth of a lifetime counter between consecutive days with data.

    One ``(day, delta)`` per point after the first, dated at the later point. A
    counter that goes *down* was reset (firmware, driver reinstall) -- it did not
    recover -- so what it shows since the reset is the delta.
    """

    points = _points(series)
    out: Series = []
    for (_, before), (day, now) in zip(points, points[1:]):
        out.append((day.isoformat(), now - before if now >= before else now))
    return out


def first_nonzero(series: Series) -> str | None:
    """The first day a counter read above zero, or ``None``.

    Requires an earlier zero reading: a series that starts non-zero was already
    in that state when observation began, which is a standing fact, not a first.
    """

    points = _points(series)
    for index, (day, value) in enumerate(points):
        if value > 0:
            return day.isoformat() if index > 0 else None
    return None


def rate_per_day(series: Series) -> float | None:
    """Mean daily growth of a lifetime counter over the series, reset-safe."""

    points = _points(series)
    if len(points) < 2:
        return None
    span = (points[-1][0] - points[0][0]).days
    if span <= 0:
        return None
    return sum(delta for _, delta in counter_deltas(series)) / span


def _linear_days(
    series: Series, target: float, *, rising: bool, window_days: int = WEAR_FIT_DAYS
) -> float | None:
    """Days until a fitted series reaches ``target`` -- the shared gate of the
    disk forecast: enough points, a slope in the right direction, a decent fit."""

    points = _points(series)
    if not points:
        return None
    last_day, current = points[-1]
    if (current >= target) if rising else (current <= target):
        return 0.0
    recent = [p for p in points if (last_day - p[0]).days <= window_days]
    if len(recent) < MIN_POINTS:
        return None
    fit = _fit([(float((d - recent[0][0]).days), v) for d, v in recent])
    if fit is None:
        return None
    slope, r2 = fit
    if r2 < MIN_R2 or (slope <= 0 if rising else slope >= 0):
        return None
    return round(abs((target - current) / slope), 1)


def wearout_forecast(percentage_used: Series) -> float | None:
    """Days until an SSD's ``percentage_used`` reaches 100, or ``None``.

    Gated like :func:`disk_forecast`: at least ``MIN_POINTS`` days in the last
    180, a rising slope and an r² of at least ``MIN_R2``. A drive already at 100
    returns 0.
    """

    return _linear_days(percentage_used, 100.0, rising=True)


def spare_decline(available_spare: Series, threshold: Series | float | None) -> float | None:
    """Days until the NVMe spare capacity falls to its threshold, or ``None``.

    ``threshold`` is the drive's own ``available_spare_threshold`` (a series, of
    which the newest value counts, or a number). A drive already at or under it
    returns 0.
    """

    if isinstance(threshold, (list, tuple)):
        points = _points(threshold)
        limit = points[-1][1] if points else None
    else:
        limit = _finite(threshold)
    if limit is None:
        return None
    return _linear_days(available_spare, limit, rising=False)


def _pcie_regression(
    loaded_max: Series, width_max: Series | None
) -> tuple[float, float] | None:
    """``(historic best loaded width, newest loaded width)`` when it regressed."""

    points = _points(loaded_max)
    if not points:
        return None
    best = max(v for _, v in points)
    ceiling = [v for _, v in _points(width_max)] if width_max else []
    if ceiling:
        # A loaded width above the card's own maximum is a bad reading.
        best = min(best, max(ceiling))
    recent = points[-PCIE_WINDOW_DAYS:]
    if sum(1 for _, v in recent if v < best) >= PCIE_MIN_DAYS:
        return best, recent[-1][1]
    return None


def pcie_width_regression(loaded_max: Series, width_max: Series | None = None) -> bool:
    """Whether a GPU's link under load is narrower than it has been.

    ``loaded_max`` is the day's widest link seen at load. The baseline is the
    device's *own* historic best, never its ``width_max``: an x8 card or an x4
    slot is by design. True when the width was below that best on at least three
    of the last seven days with data. ``width_max`` only caps a bad baseline.
    """

    return _pcie_regression(loaded_max, width_max) is not None


def fan_drift(
    bands: dict[str, Series], today: date | str | None = None
) -> dict[str, Any] | None:
    """A fan slowing at the same duty, or ``None``.

    Per duty band, the last seven days' median RPM is compared with the fan's own
    baseline: the median of the first 30 days of its history, taken again after
    a gap of more than 30 days and only from at least five days that precede the
    recent window. A band takes part with three or more days in the last seven
    and a baseline. Drift needs every participating band down by 12 % or more,
    and the drop visible on at least five of the last seven days. Returns
    ``{"drop_percent", "bands"}`` (the median drop and the bands that took part).

    Bands, not raw RPM, so a change of fan curve -- which only moves *where* the
    fan runs -- leaves the medians alone; it empties one band and starts another
    without a baseline, so nothing fires.
    """

    series = {
        name: _points(values)
        for name, values in (bands or {}).items()
        if isinstance(name, str) and name.startswith("rpm_duty_")
    }
    last_days = [pts[-1][0] for pts in series.values() if pts]
    end = _as_date(today) or (max(last_days) if last_days else None)
    if end is None:
        return None
    recent_from = end - timedelta(days=FAN_RECENT_DAYS - 1)

    drops: list[float] = []
    taking_part: list[str] = []
    dropped_days: set[date] = set()
    for name, points in sorted(series.items()):
        points = [p for p in points if p[0] <= end and p[1] > 0]
        start = 0
        for i in range(1, len(points)):
            if (points[i][0] - points[i - 1][0]).days > FAN_REBASELINE_GAP_DAYS:
                start = i
        epoch = points[start:]
        if not epoch:
            continue
        recent = [p for p in epoch if p[0] >= recent_from]
        baseline_until = epoch[0][0] + timedelta(days=FAN_BASELINE_DAYS)
        baseline = [p for p in epoch if p[0] < baseline_until and p[0] < recent_from]
        if len(recent) < FAN_RECENT_MIN_DAYS or len(baseline) < FAN_BASELINE_MIN_DAYS:
            continue
        base = statistics.median(v for _, v in baseline)
        now = statistics.median(v for _, v in recent)
        drops.append(1.0 - now / base)
        taking_part.append(name)
        dropped_days.update(d for d, v in recent if v <= base * (1.0 - FAN_MIN_DROP))
    if not drops or min(drops) < FAN_MIN_DROP or len(dropped_days) < FAN_SUSTAINED_DAYS:
        return None
    return {
        "drop_percent": round(100.0 * statistics.median(drops), 1),
        "bands": taking_part,
    }


def _error_rate(counts: Series, today: date | str | None) -> tuple[float, float] | None:
    """``(events in the last 7 days, typical events per week before)`` if rising."""

    points = _points(counts)
    if not points:
        return None
    end = _as_date(today) or points[-1][0]
    by_day = {d: max(v, 0.0) for d, v in points}
    # Rising needs a before to compare with: at least a week of history ahead
    # of the recent window.
    if (end - points[0][0]).days < 2 * RATE_RECENT_DAYS:
        return None

    def window(first_back: int, last_back: int) -> float:
        return math.fsum(
            by_day.get(end - timedelta(days=back), 0.0)
            for back in range(last_back, first_back + 1)
        )

    recent = window(RATE_RECENT_DAYS - 1, 0)
    prior = window(RATE_RECENT_DAYS + RATE_PRIOR_DAYS - 1, RATE_RECENT_DAYS)
    if recent < RATE_MIN_EVENTS:
        return None
    if recent / RATE_RECENT_DAYS < RATE_FACTOR * prior / RATE_PRIOR_DAYS:
        return None
    return recent, prior / RATE_PRIOR_DAYS * 7.0


def error_rate_rising(counts: Series, today: date | str | None = None) -> bool:
    """Whether a daily event count is climbing.

    The last seven days' mean is at least twice the mean of the 21 days before
    them, with at least three events in the last seven. Days without a row count
    as zero (the section was collected, nothing was logged); a series with less
    than two weeks of history cannot be called rising.
    """

    return _error_rate(counts, today) is not None


# -- forecasts ------------------------------------------------------------------


def _span(days: float) -> str:
    if days < 60:
        return f"{max(1, round(days))} days"
    return f"{round(days / 30)} months"


def _forecast(
    key: str, kind: str, label: str, reason: str, symptom: str, days: float | None = None
) -> dict[str, Any]:
    return {
        "device_key": key,
        "kind": kind,
        "label": label,
        "reason": reason,
        "symptom": symptom,
        "days_until": days,
    }


def _disk_forecasts(
    key: str, label: str, metrics: dict[str, Series], today: date
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    used = metrics.get("percentage_used")
    days = wearout_forecast(used) if used else None
    if days is not None and days <= WEAR_OUT_ALERT_DAYS:
        pct = _points(used)[-1][1]
        symptom = (
            f"{label} has used all of its rated write endurance"
            if days <= 0
            else f"{label} has used {pct:.0f}% of its rated write endurance and may "
            f"reach it in about {_span(days)}"
        )
        out.append(_forecast(key, "disk", label, "wear_out", symptom, days))

    spare = metrics.get("available_spare")
    days = spare_decline(spare, metrics.get("available_spare_threshold")) if spare else None
    if days is not None and days <= SPARE_DECLINE_ALERT_DAYS:
        now = _points(spare)[-1][1]
        symptom = (
            f"{label}'s spare capacity has reached its safety threshold"
            if days <= 0
            else f"{label}'s spare capacity is shrinking ({now:.0f}% left) and should "
            f"reach its safety threshold in about {_span(days)}"
        )
        out.append(_forecast(key, "disk", label, "spare_decline", symptom, days))

    for metric in FIRST_ERROR_METRICS:
        series = metrics.get(metric)
        if not series or first_nonzero(series) is None:
            continue
        grew = [d for d, delta in counter_deltas(series) if delta > 0]
        newest = _as_date(grew[-1]) if grew else None
        if newest is not None and (today - newest).days <= FIRST_ERROR_RECENT_DAYS:
            # One per device: the first counter in FIRST_ERROR_METRICS order.
            out.append(
                _forecast(
                    key, "disk", label, "first_error",
                    _FIRST_ERROR_SYMPTOM[metric].format(label=label),
                )
            )
            break
    return out


def _gpu_forecasts(
    key: str, label: str, metrics: dict[str, Series], today: date
) -> list[dict[str, Any]]:
    hit = _pcie_regression(
        metrics.get("pcie_width_loaded_max") or [], metrics.get("pcie_width_max")
    )
    if hit is None:
        return []
    best, now = hit
    return [
        _forecast(
            key, "gpu", label, "pcie_width_regression",
            f"{label} now runs its PCIe link at x{now:.0f} under load, "
            f"where it used to reach x{best:.0f}",
        )
    ]


def _fan_forecasts(
    key: str, label: str, metrics: dict[str, Series], today: date
) -> list[dict[str, Any]]:
    drift = fan_drift(metrics, today)
    if drift is None:
        return []
    return [
        _forecast(
            key, "fan", label, "fan_drift",
            f"Fan {label} is slowing at the same power setting "
            f"({drift['drop_percent']:.0f}% below its usual speed) — bearing wear likely",
        )
    ]


def _component_forecasts(
    key: str, label: str, metrics: dict[str, Series], today: date
) -> list[dict[str, Any]]:
    order = {"corrected": 0, "instability": 1, "fatal": 2}
    worst: tuple[int, str, tuple[float, float]] | None = None
    for metric in _EVENT_METRICS + _COUNTER_METRICS:
        series = metrics.get(metric)
        if not series:
            continue
        counts = counter_deltas(series) if metric in _COUNTER_METRICS else series
        rate = _error_rate(counts, today)
        if rate is None:
            continue
        kind = _RATE_KIND[metric]
        if worst is None or order[kind] > worst[0]:
            worst = (order[kind], kind, rate)
    if worst is None:
        return []
    recent, prior = worst[2]
    symptom = _RATE_SYMPTOM[worst[1]].format(label=label, recent=recent, prior=prior)
    return [_forecast(key, "component", label, "error_rate_rising", symptom)]


_PRODUCERS = {
    "disk": _disk_forecasts,
    "gpu": _gpu_forecasts,
    "fan": _fan_forecasts,
    "component": _component_forecasts,
}


def hardware_forecasts(
    series_by_device: dict[str, dict[str, Series]],
    labels: dict[str, tuple[str, str]],
    today: date | str | datetime,
) -> list[dict[str, Any]]:
    """The hardware at risk, as ``[{device_key, kind, label, reason, symptom,
    days_until}]`` sorted by device and reason.

    ``labels`` names the devices (``hardware_metrics.device_labels``); a device
    it does not know is labelled from its key. Devices with no data for
    ``STALE_DEVICE_DAYS`` are skipped. ``reason`` is one of ``wear_out``,
    ``spare_decline``, ``first_error``, ``error_rate_rising``,
    ``pcie_width_regression`` and ``fan_drift``; ``days_until`` is a number for
    the two projections and ``None`` otherwise. Never raises on malformed series.
    """

    day = _as_date(today)
    if day is None:
        return []
    out: list[dict[str, Any]] = []
    for key in sorted(series_by_device):
        metrics = series_by_device[key]
        if not isinstance(metrics, dict) or not isinstance(key, str):
            continue
        newest = [pts[-1][0] for s in metrics.values() if (pts := _points(s))]
        if not newest or (day - max(newest)).days > STALE_DEVICE_DAYS:
            continue
        kind = hardware_metrics.kind_of(key)
        label = (labels.get(key) or (kind, ""))[1] or hardware_metrics.fallback_label(key)
        try:
            out.extend(_PRODUCERS[kind](key, label, metrics, day))
        except Exception:  # noqa: BLE001 - one odd series must not hide the rest
            continue
    out.sort(key=lambda f: (f["device_key"], f["reason"]))
    return out

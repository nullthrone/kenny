"""Tests for :mod:`kenny_server.trends` (OLS forecasts, ADR-0029)."""

from __future__ import annotations

from kenny_server.trends import battery_trend, disk_forecast


def _daily_disk(points: list[tuple[str, float]]) -> list[dict]:
    return [
        {
            "collected_at": f"{day}T20:00:00+00:00",
            "snapshot": {
                "disk": {
                    "status": "ok",
                    "summary": "",
                    "volumes": [{"mount": "C:", "percent_used": pct}],
                }
            },
        }
        for day, pct in points
    ]


def test_linear_fill_series_forecasts_days_until_full() -> None:
    # +2 %/day at 80 % now -> full in ~10 days.
    daily = _daily_disk(
        [(f"2026-06-{d:02d}", 70.0 + 2.0 * i) for i, d in enumerate(range(10, 16))]
    )
    (forecast,) = disk_forecast(daily)
    assert forecast["mount"] == "C:"
    assert forecast["current_percent"] == 80.0
    assert forecast["slope_percent_per_day"] == 2.0
    assert forecast["days_until_full"] == 10.0


def test_flat_series_has_no_forecast() -> None:
    daily = _daily_disk([(f"2026-06-{d:02d}", 50.0) for d in range(10, 16)])
    (forecast,) = disk_forecast(daily)
    assert forecast["days_until_full"] is None


def test_shrinking_usage_has_no_forecast() -> None:
    daily = _daily_disk(
        [(f"2026-06-{d:02d}", 90.0 - 2.0 * i) for i, d in enumerate(range(10, 16))]
    )
    (forecast,) = disk_forecast(daily)
    assert forecast["days_until_full"] is None


def test_too_few_points_has_no_forecast() -> None:
    daily = _daily_disk([("2026-06-10", 70.0), ("2026-06-11", 80.0)])
    (forecast,) = disk_forecast(daily)
    assert forecast["days_until_full"] is None


def test_noisy_series_below_r2_has_no_forecast() -> None:
    daily = _daily_disk(
        [
            ("2026-06-10", 50.0),
            ("2026-06-11", 90.0),
            ("2026-06-12", 45.0),
            ("2026-06-13", 88.0),
            ("2026-06-14", 51.0),
            ("2026-06-15", 60.0),
        ]
    )
    (forecast,) = disk_forecast(daily)
    assert forecast["days_until_full"] is None


def test_battery_trend_reports_drift_per_30d() -> None:
    daily = [
        {
            "collected_at": f"2026-06-{d:02d}T20:00:00+00:00",
            "snapshot": {"battery": {"status": "ok", "summary": "", "health_percent": 90.0 - 0.5 * i}},
        }
        for i, d in enumerate(range(10, 16))
    ]
    trend = battery_trend(daily)
    assert trend is not None
    assert trend["current_percent"] == 87.5
    assert trend["percent_per_30d"] == -15.0


def test_battery_trend_none_without_battery_section() -> None:
    assert battery_trend([{"collected_at": "2026-06-10T20:00:00+00:00", "snapshot": {}}]) is None

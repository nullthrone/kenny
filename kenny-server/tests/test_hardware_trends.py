"""Cross-day hardware trends (ADR-0070): counters, wear, spare, PCIe, fans, rates."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from kenny_server import trends

START = date(2026, 1, 1)


def series(values, start: date = START, step: int = 1):
    """``[(day, value)]`` one point every ``step`` days; ``None`` skips a day."""

    return [
        ((start + timedelta(days=i * step)).isoformat(), float(v))
        for i, v in enumerate(values)
        if v is not None
    ]


def at(offset: int) -> date:
    return START + timedelta(days=offset)


# -- counters -----------------------------------------------------------------------


def test_counter_deltas_are_dated_at_the_later_point() -> None:
    assert trends.counter_deltas(series([0, 0, 2, 5])) == [
        ("2026-01-02", 0.0),
        ("2026-01-03", 2.0),
        ("2026-01-04", 3.0),
    ]


def test_a_decreasing_counter_is_a_reset_not_a_recovery() -> None:
    deltas = trends.counter_deltas(series([10, 12, 3, 4]))
    assert [d for _, d in deltas] == [2.0, 3.0, 1.0]  # never negative
    assert all(d >= 0 for _, d in deltas)


def test_counter_deltas_span_gaps_and_ignore_junk() -> None:
    messy = [("2026-01-01", 1.0), ("2026-01-05", 4.0), ("bad", 5.0), ("2026-01-06", "x"), None]
    assert trends.counter_deltas(messy) == [("2026-01-05", 3.0)]
    assert trends.counter_deltas([]) == []
    assert trends.counter_deltas(series([7])) == []


def test_first_nonzero_needs_an_earlier_zero() -> None:
    assert trends.first_nonzero(series([0, 0, 0, 1, 1, 2])) == "2026-01-04"
    assert trends.first_nonzero(series([0, 1])) == "2026-01-02"
    assert trends.first_nonzero(series([3, 3, 4])) is None  # already non-zero on arrival
    assert trends.first_nonzero(series([0, 0, 0])) is None
    assert trends.first_nonzero([]) is None
    # a reset back to zero and up again is not the first time
    assert trends.first_nonzero(series([0, 2, 0, 1])) == "2026-01-02"


def test_rate_per_day_is_reset_safe() -> None:
    assert trends.rate_per_day(series([0, 2, 4, 6])) == pytest.approx(2.0)
    assert trends.rate_per_day(series([0, 5, 1, 3])) == pytest.approx((5 + 1 + 2) / 3)
    assert trends.rate_per_day(series([5])) is None
    assert trends.rate_per_day([]) is None
    assert trends.rate_per_day([("2026-01-01", 1.0), ("2026-01-01", 2.0)]) is None


# -- wear-out ----------------------------------------------------------------------


def test_wearout_forecast_projects_a_steady_climb() -> None:
    # 1 % every 2 days, 60 % now -> 40 more points = 80 days
    values = [30 + i * 0.5 for i in range(61)]
    assert trends.wearout_forecast(series(values)) == pytest.approx(80.0, abs=0.5)


def test_wearout_forecast_follows_integer_steps() -> None:
    values = [40 + i // 4 for i in range(100)]  # +1 point every 4 days, stair-stepped
    days = trends.wearout_forecast(series(values))
    assert days is not None and 130 < days < 160  # 64 % now, +0.25 / day


def test_wearout_forecast_is_none_for_flat_falling_short_or_noisy_series() -> None:
    assert trends.wearout_forecast(series([5] * 60)) is None
    assert trends.wearout_forecast(series([20 - i * 0.1 for i in range(60)])) is None
    assert trends.wearout_forecast(series([10, 11, 12, 13])) is None  # under MIN_POINTS
    rng = random.Random(7)
    noisy = [50 + rng.choice([-20, 20]) for _ in range(60)]
    assert trends.wearout_forecast(series(noisy)) is None


def test_wearout_forecast_needs_more_than_a_few_days_of_whole_percent_steps() -> None:
    assert trends.wearout_forecast(series([85, 85, 85, 85, 86])) is None
    # A month of data is still short of 30 calendar days / 21 points.
    assert trends.wearout_forecast(series([85] * 20 + [86] * 9)) is None
    # Enough points, but spread over too few calendar days.
    assert trends.wearout_forecast(series([80, 80, 81, 81, 82, 82] * 4)) is None


def test_wearout_forecast_needs_a_real_movement_in_a_long_series() -> None:
    assert trends.wearout_forecast(series([85] * 60 + [86])) is None  # one step of one point
    # two steps (or two points) are enough once the series is long
    assert trends.wearout_forecast(series([85] * 30 + [86] * 20 + [87] * 11)) is not None
    assert trends.wearout_forecast(series([85] * 30 + [87] * 31)) is not None


def test_a_genuine_slow_trend_over_two_months_still_forecasts() -> None:
    values = [80 + i // 15 for i in range(61)]  # +1 point every 15 days, whole percent
    days = trends.wearout_forecast(series(values))
    assert days is not None and 200 < days < 400
    spare = [100 - i // 6 for i in range(61)]  # -1 point every 6 days
    assert trends.spare_decline(series(spare), 10) is not None


def test_spare_decline_has_the_same_evidence_floor() -> None:
    assert trends.spare_decline(series([100, 100, 100, 100, 99]), 10) is None


def test_wearout_forecast_is_zero_for_a_drive_at_its_rated_endurance() -> None:
    assert trends.wearout_forecast(series([97, 98, 100])) == 0.0
    assert trends.wearout_forecast(series([100])) == 0.0
    assert trends.wearout_forecast([]) is None


def test_wearout_forecast_only_fits_the_recent_window() -> None:
    old = [5.0] * 400  # flat for ages, then six months of steady climb
    recent = [5 + i * 0.2 for i in range(1, 181)]
    assert trends.wearout_forecast(series(old + recent)) == pytest.approx(
        (100 - 41) / 0.2, abs=1.0
    )


# -- spare decline -----------------------------------------------------------------


def test_spare_decline_projects_to_the_threshold() -> None:
    # 100 -> 70 in 60 days (-0.5/day); threshold 10 -> 120 more days
    values = [100 - i * 0.5 for i in range(61)]
    assert trends.spare_decline(series(values), 10) == pytest.approx(120.0, abs=0.5)


def test_spare_decline_takes_the_threshold_as_a_series_too() -> None:
    values = series([100 - i * 0.5 for i in range(61)])
    assert trends.spare_decline(values, series([10] * 61)) == pytest.approx(120.0, abs=0.5)


def test_spare_decline_is_none_without_a_decline_or_a_threshold() -> None:
    assert trends.spare_decline(series([100] * 40), 10) is None
    assert trends.spare_decline(series([90 + i * 0.1 for i in range(40)]), 10) is None
    assert trends.spare_decline(series([100 - i for i in range(40)]), None) is None
    assert trends.spare_decline(series([100 - i for i in range(40)]), []) is None


def test_spare_decline_is_zero_at_or_under_the_threshold() -> None:
    assert trends.spare_decline(series([12, 11, 10]), 10) == 0.0
    assert trends.spare_decline(series([5]), 10) == 0.0


# -- PCIe width --------------------------------------------------------------------


def test_pcie_regression_needs_three_of_the_last_seven_days_below_the_best() -> None:
    assert not trends.pcie_width_regression(series([16] * 30))
    assert trends.pcie_width_regression(series([16] * 30 + [8, 8, 8]))
    assert not trends.pcie_width_regression(series([16] * 30 + [8, 8]))
    # two dips and a recovery do not make three
    assert not trends.pcie_width_regression(series([16] * 30 + [8, 16, 8, 16, 16]))
    # three anywhere in the last seven days with data
    assert trends.pcie_width_regression(series([16] * 30 + [8, 16, 8, 16, 8, 16, 16]))


def test_pcie_regression_compares_with_the_devices_own_best_never_its_max() -> None:
    # an x8 card in an x8 slot, advertised x16 maximum: by design, not a regression
    assert not trends.pcie_width_regression(series([8] * 40), series([16] * 40))
    # an x4 slot likewise
    assert not trends.pcie_width_regression(series([4] * 40), series([16] * 40))


def test_pcie_regression_counts_days_with_data_not_calendar_days() -> None:
    sparse = series([16, 16, 16, 8, 8, 8], step=3)
    assert trends.pcie_width_regression(sparse)


def test_pcie_width_max_only_caps_a_bad_baseline() -> None:
    # a single bogus x32 reading must not turn every x16 day into a regression
    loaded = series([16] * 20 + [32] + [16] * 6)
    assert trends.pcie_width_regression(loaded)
    assert not trends.pcie_width_regression(loaded, series([16] * 27))


def test_pcie_regression_handles_empty_and_junk() -> None:
    assert not trends.pcie_width_regression([])
    assert not trends.pcie_width_regression([("x", 1.0), None])  # type: ignore[list-item]


# -- fan drift ---------------------------------------------------------------------

BAND = "rpm_duty_30_50"


def band_series(baseline: float, recent: float, *, base_days: int = 30, recent_days: int = 10,
                gap: int = 0):
    """``baseline`` for ``base_days``, then ``recent`` for ``recent_days``."""

    values = [baseline] * base_days + [None] * gap + [recent] * recent_days
    return series(values)


def test_fan_drift_flags_a_gradual_decline_in_every_band() -> None:
    bands = {}
    for name, rpm in (("rpm_duty_30_50", 1000), ("rpm_duty_50_70", 1600)):
        values = [rpm] * 30 + [rpm * (1 - 0.02 * i) for i in range(1, 21)]
        bands[name] = series(values)
    drift = trends.fan_drift(bands)
    assert drift is not None
    assert drift["bands"] == ["rpm_duty_30_50", "rpm_duty_50_70"]
    assert 20 < drift["drop_percent"] < 40


def test_fan_drift_ignores_a_flat_fan() -> None:
    assert trends.fan_drift({BAND: series([1000] * 60)}) is None
    rng = random.Random(3)
    jitter = {BAND: series([1000 + rng.randint(-30, 30) for _ in range(60)])}
    assert trends.fan_drift(jitter) is None


def test_fan_drift_needs_a_drop_of_twelve_percent() -> None:
    assert trends.fan_drift({BAND: band_series(1000, 900)}) is None  # 10 %
    assert trends.fan_drift({BAND: band_series(1000, 870)}) is not None  # 13 %
    assert trends.fan_drift({BAND: band_series(1000, 1200)}) is None  # faster is not drift


def test_a_curve_change_that_moves_the_fan_between_bands_is_not_drift() -> None:
    # The BIOS curve changed on day 30: the fan now sits at a higher duty, so
    # the old band goes quiet and the new one has no baseline.
    old = series([1000] * 30)
    new = series([1500] * 15, start=at(30))
    assert trends.fan_drift({"rpm_duty_30_50": old, "rpm_duty_50_70": new}) is None


def test_a_step_in_every_band_is_visible_as_drift_in_the_series() -> None:
    # A change that lowers RPM at the same duty everywhere looks like wear; the
    # series keeps the step so a reader can tell it from a gradual decline.
    bands = {
        "rpm_duty_30_50": band_series(1000, 700),
        "rpm_duty_50_70": band_series(1600, 1100),
    }
    drift = trends.fan_drift(bands)
    assert drift is not None and drift["drop_percent"] > 25


def test_fan_drift_needs_every_participating_band_to_drop() -> None:
    bands = {
        "rpm_duty_30_50": band_series(1000, 700),
        "rpm_duty_50_70": band_series(1600, 1600),
    }
    assert trends.fan_drift(bands) is None


def test_fan_drift_needs_three_recent_days_in_a_band_and_five_sustained() -> None:
    # only two recent days in the band -> it does not take part
    assert trends.fan_drift({BAND: series([1000] * 30 + [700] * 2)}) is None
    # three or four low days in the band cannot be "sustained on five days"
    assert trends.fan_drift({BAND: series([1000] * 30 + [None] * 3 + [700] * 4)}) is None
    # five of the last seven
    assert trends.fan_drift({BAND: series([1000] * 30 + [None] * 2 + [700] * 5)}) is not None


def test_fan_drift_needs_a_five_day_baseline_before_the_recent_window() -> None:
    assert trends.fan_drift({BAND: series([1000] * 4 + [700] * 7)}) is None
    assert trends.fan_drift({BAND: series([1000] * 5 + [700] * 7)}) is not None


def test_fan_drift_rebaselines_after_a_gap_longer_than_thirty_days() -> None:
    # The fan ran at 1000, vanished for 40 days (a rebuilt PC), and came back at
    # 700 -- the new baseline is 700, so nothing has drifted.
    bands = {BAND: band_series(1000, 700, base_days=30, recent_days=40, gap=40)}
    assert trends.fan_drift(bands) is None
    # a gap of exactly thirty days keeps the old baseline
    kept = {BAND: band_series(1000, 700, base_days=30, recent_days=10, gap=29)}
    assert trends.fan_drift(kept) is not None


def test_fan_drift_baseline_is_the_first_thirty_days_not_the_latest() -> None:
    # 1000 for the first 30 days, a slow slide to 800 over the next 60: the
    # baseline stays 1000 even when the window is long.
    values = [1000] * 30 + [1000 - i * (200 / 60) for i in range(1, 61)]
    drift = trends.fan_drift({BAND: series(values)})
    assert drift is not None and 17 < drift["drop_percent"] < 21


def test_fan_drift_uses_the_supplied_today_and_ignores_other_metrics() -> None:
    bands = {BAND: band_series(1000, 700), "stall_seen": series([0] * 40)}
    assert trends.fan_drift(bands) is not None
    assert trends.fan_drift(bands, today=at(200)) is None  # data is long stale
    assert trends.fan_drift({}) is None
    assert trends.fan_drift({"stall_seen": series([1] * 40)}) is None


# -- error rates -------------------------------------------------------------------


def test_error_rate_rising_when_the_week_doubles_the_month_before() -> None:
    counts = series([1] * 21 + [0] * 7 + [1, 1, 1, 2, 2, 2, 3])
    assert trends.error_rate_rising(counts)
    assert trends.error_rate_rising(counts, today=at(34))


def test_error_rate_needs_three_events_and_twice_the_prior_mean() -> None:
    quiet = series([0] * 28 + [1, 0, 0, 1, 0, 0, 0])
    assert not trends.error_rate_rising(quiet)  # only two events
    steady = series([2] * 35)
    assert not trends.error_rate_rising(steady)
    barely = series([2] * 28 + [3] * 7)
    assert not trends.error_rate_rising(barely)  # 1.5x
    assert trends.error_rate_rising(series([2] * 28 + [4] * 7))  # exactly 2x


def test_error_rate_rising_from_a_clean_history() -> None:
    assert trends.error_rate_rising(series([0] * 28 + [0, 0, 1, 0, 1, 1, 0]))


def test_error_rate_treats_missing_days_as_zero_but_needs_history() -> None:
    sparse = [(at(0).isoformat(), 0.0)] + [(at(i).isoformat(), 1.0) for i in range(28, 35)]
    assert trends.error_rate_rising(sparse)
    young = series([0, 0, 0, 5, 5, 5])
    assert not trends.error_rate_rising(young)  # nothing before the recent week
    assert not trends.error_rate_rising([])


def test_error_rate_ignores_a_burst_that_is_already_over() -> None:
    old_burst = series([0] * 5 + [9] * 5 + [0] * 25)
    assert not trends.error_rate_rising(old_burst)


# -- hardware_forecasts ------------------------------------------------------------


TODAY = at(60)


def disk_metrics(**overrides):
    base = {
        "percentage_used": series([40] * 61),
        "available_spare": series([100] * 61),
        "available_spare_threshold": series([10] * 61),
        "media_errors": series([0] * 61),
        "smart_197": series([0] * 61),
    }
    base.update(overrides)
    return base


def forecast(devices, labels=None, today=TODAY):
    return trends.hardware_forecasts(devices, labels or {}, today)


def test_a_healthy_fleet_has_no_forecasts() -> None:
    assert forecast({"disk:S1": disk_metrics()}) == []
    assert forecast({}) == []


def test_wear_out_within_180_days_fires_with_days_until() -> None:
    wearing = disk_metrics(percentage_used=series([60 + i * 0.5 for i in range(61)]))
    (f,) = forecast({"disk:S1": wearing}, {"disk:S1": ("disk", "WD_BLACK SN850X")})
    assert f["reason"] == "wear_out" and f["kind"] == "disk"
    assert f["device_key"] == "disk:S1" and f["label"] == "WD_BLACK SN850X"
    assert f["days_until"] == pytest.approx(20.0, abs=1.0)
    assert "WD_BLACK SN850X" in f["symptom"] and "90%" in f["symptom"]


def test_wear_out_beyond_180_days_does_not_fire() -> None:
    slow = disk_metrics(percentage_used=series([20 + i * 0.1 for i in range(61)]))
    assert forecast({"disk:S1": slow}) == []


def test_spare_decline_within_90_days_fires() -> None:
    declining = disk_metrics(available_spare=series([100 - i for i in range(61)]))
    (f,) = forecast({"disk:S1": declining})
    assert f["reason"] == "spare_decline" and f["days_until"] == pytest.approx(30.0, abs=1.0)
    slow = disk_metrics(available_spare=series([100 - i * 0.1 for i in range(61)]))
    assert forecast({"disk:S1": slow}) == []


def test_the_first_error_fires_once_per_device_for_the_most_telling_counter() -> None:
    both = disk_metrics(
        media_errors=series([0] * 55 + [1] * 6),
        smart_197=series([0] * 58 + [4] * 3),
    )
    (f,) = forecast({"disk:S1": both}, {"disk:S1": ("disk", "Seagate")})
    assert f["reason"] == "first_error" and f["days_until"] is None
    assert "Seagate" in f["symptom"] and "media" in f["symptom"]


@pytest.mark.parametrize(
    "metric,words",
    [
        ("smart_5", "damaged sectors"),
        ("smart_197", "waiting to be remapped"),
        ("smart_198", "cannot read"),
        ("read_errors_uncorrected", "read"),
        ("write_errors_uncorrected", "write"),
        ("media_errors", "media"),
    ],
)
def test_every_first_error_counter_has_its_own_symptom(metric, words) -> None:
    (f,) = forecast({"disk:S1": disk_metrics(**{metric: series([0] * 58 + [2] * 3)})})
    assert f["reason"] == "first_error" and words in f["symptom"]


def test_a_counter_that_started_non_zero_or_went_quiet_long_ago_is_not_news() -> None:
    standing = disk_metrics(media_errors=series([3] * 61))
    assert forecast({"disk:S1": standing}) == []
    old = disk_metrics(media_errors=series([0] * 5 + [2] * 56))  # first error 56 days ago
    assert forecast({"disk:S1": old}) == []


def test_a_later_rise_of_an_already_non_zero_counter_is_not_a_first_error() -> None:
    # The first error was 56 days ago; it rose again 15 days ago. That is not a
    # "first" error, and re-announcing it as one would misdescribe it.
    rising = disk_metrics(media_errors=series([0] * 5 + [2] * 40 + [3] * 16))
    assert forecast({"disk:S1": rising}) == []


def test_a_first_error_inside_the_window_fires_even_if_it_rose_again() -> None:
    fresh = disk_metrics(media_errors=series([0] * 50 + [1] * 5 + [4] * 6))
    (f,) = forecast({"disk:S1": fresh})
    assert f["reason"] == "first_error"
    outside = disk_metrics(media_errors=series([0] * 20 + [1] * 41))  # first move 41 days ago
    assert forecast({"disk:S1": outside}) == []


def test_a_reset_counter_is_not_a_first_error_by_itself() -> None:
    reset = disk_metrics(media_errors=series([0] * 10 + [9] * 40 + [0] * 11))
    assert forecast({"disk:S1": reset}) == []


def test_gpu_pcie_regression_fires() -> None:
    loaded = series([16] * 54 + [8] * 7)
    (f,) = forecast(
        {"gpu:G1": {"pcie_width_loaded_max": loaded, "pcie_width_max": series([16] * 61)}},
        {"gpu:G1": ("gpu", "RTX 4080")},
    )
    assert f["reason"] == "pcie_width_regression" and f["kind"] == "gpu"
    assert "x8" in f["symptom"] and "x16" in f["symptom"]
    assert forecast({"gpu:G1": {"pcie_width_loaded_max": series([8] * 61)}}) == []


def test_fan_drift_fires_as_a_forecast() -> None:
    drifting = {BAND: series([1000] * 30 + [1000 - i * 10 for i in range(1, 32)])}
    (f,) = forecast({"fan:nct.fan1": drifting}, {"fan:nct.fan1": ("fan", "CPU_FAN")})
    assert f["reason"] == "fan_drift" and f["kind"] == "fan" and f["days_until"] is None
    assert "CPU_FAN" in f["symptom"] and "bearing" in f["symptom"]


def test_a_component_with_a_rising_error_rate_fires() -> None:
    counts = series([1] * 21 + [0] * 7 + [2, 2, 3, 3, 3, 4, 4])
    (f,) = forecast({"host:memory": {"corrected_events": counts}}, today=at(34))
    assert f["reason"] == "error_rate_rising" and f["kind"] == "component"
    assert f["label"] == "Memory"  # from the key: no snapshot names it
    assert "correcting" in f["symptom"]
    (fatal,) = forecast({"host:cpu": {"fatal_events": counts}}, today=at(34))
    assert fatal["label"] == "Processor" and "uncorrectable" in fatal["symptom"]


def test_cumulative_counters_stand_in_for_event_counts() -> None:
    cumulative = [0.0]
    for gained in [1] * 28 + [0, 0, 3, 3, 3, 4, 4]:
        cumulative.append(cumulative[-1] + gained)
    metrics = {"edac_ce": series(cumulative)}
    today = at(len(cumulative) - 1)
    (f,) = forecast({"host:memory": metrics}, today=today)
    assert f["reason"] == "error_rate_rising"


def test_the_worst_class_names_the_component_symptom() -> None:
    rising = series([0] * 28 + [3] * 7)
    (f,) = forecast(
        {"host:gpu": {"corrected_events": rising, "instability_events": rising}},
        today=at(34),
    )
    assert "faults" in f["symptom"]


def test_a_device_not_seen_for_two_weeks_is_gone_not_failing() -> None:
    wearing = disk_metrics(percentage_used=series([60 + i * 0.5 for i in range(61)]))
    assert forecast({"disk:S1": wearing}, today=at(60 + 14)) != []
    assert forecast({"disk:S1": wearing}, today=at(60 + 15)) == []


def test_forecasts_are_sorted_and_never_raise_on_junk() -> None:
    wearing = disk_metrics(
        percentage_used=series([60 + i * 0.5 for i in range(61)]),
        available_spare=series([100 - i for i in range(61)]),
    )
    got = forecast(
        {
            "disk:B": wearing,
            "disk:A": wearing,
            "disk:junk": {"percentage_used": "x", "media_errors": [None, 3, ("a",)]},  # type: ignore[dict-item]
            "gpu:g": None,  # type: ignore[dict-item]
            "weird": {"x": series([1] * 61)},
        }
    )
    assert [(f["device_key"], f["reason"]) for f in got] == [
        ("disk:A", "spare_decline"),
        ("disk:A", "wear_out"),
        ("disk:B", "spare_decline"),
        ("disk:B", "wear_out"),
    ]
    assert trends.hardware_forecasts({"disk:A": wearing}, {}, "not a date") == []
    for f in got:
        assert set(f) == {"device_key", "kind", "label", "reason", "symptom", "days_until"}


def test_forecast_sections_cover_every_kind() -> None:
    from kenny_server import ticket_rules

    assert set(trends.FORECAST_SECTION) == {"disk", "gpu", "fan", "component"}
    assert set(trends.FORECAST_SECTION.values()) == ticket_rules.KNOWN_SECTIONS["hardware_forecast"]

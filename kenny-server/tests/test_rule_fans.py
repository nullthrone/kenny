"""``fans`` health rule: stall, jitter, idle, worst-of and malformed input."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kenny_server import health_rules

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
NOW = datetime(2026, 6, 4, 18, 30, tzinfo=timezone.utc)


def _fan(
    samples: Any,
    duty: Any = 45.0,
    *,
    key: str = "nct6798.fan1",
    label: Any = "CPU_FAN",
    idle: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "source": "hwmon",
        "rpm_samples": samples,
        "duty_percent": duty,
        "mode": "pwm",
        "idle_or_absent": idle,
    }


def _section(*fans: Any) -> dict[str, Any]:
    return {"status": "ok", "summary": "fans", "fans": list(fans)}


def _judge(*fans: Any) -> dict[str, Any]:
    return health_rules.evaluate_section("fans", _section(*fans), now=NOW, agent_os="linux")


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["key"]: v for v in result["details"]["fans"]}


STEADY = [1180, 1176, 1182, 1179, 1181]


# --- registration -----------------------------------------------------------


def test_rule_is_registered_and_confirmed_before_alarm() -> None:
    assert health_rules.RULES["fans"] is health_rules._rule_fans
    assert "fans" in health_rules.CONFIRM_BEFORE_ALARM


# --- deferral ---------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"fans": []},
        {"fans": None},
        {"fans": "CPU_FAN"},
        {"fans": {"key": "x"}},
        {"fans": ["a", 1, None]},
    ],
)
def test_defers_without_fans(payload: dict[str, Any]) -> None:
    assert health_rules._rule_fans(payload, NOW) is None


def test_deferred_section_keeps_the_agents_grade() -> None:
    result = health_rules.evaluate_section("fans", {"status": "warn", "fans": []}, now=NOW)
    assert result["status"] == "warn"


def test_only_idle_fans_defers() -> None:
    assert health_rules._rule_fans(_section(_fan([0] * 5, None, idle=True)), NOW) is None


# --- stall ------------------------------------------------------------------


def test_stall_with_duty_warns_with_symptom() -> None:
    result = _judge(_fan([0, 0, 0, 0, 0], 45.0))
    assert result["status"] == "warn"
    assert result["reason"] == "Fan CPU_FAN has stopped although the board is driving it at 45%"
    (v,) = result["details"]["fans"]
    assert v["status"] == "warn" and v["mean_rpm"] == 0 and v["cv"] is None


@pytest.mark.parametrize("duty", [30, 30.0, 100, 255])
def test_stall_at_or_above_threshold(duty: float) -> None:
    assert _judge(_fan([0] * 5, duty))["status"] == "warn"


@pytest.mark.parametrize("duty", [None, 0, 20, 29.9, -5, "45", True, float("nan")])
def test_zero_rpm_without_a_real_demand_is_not_a_stall(duty: Any) -> None:
    # duty unreadable, a zero-RPM mode (~0 %), or below the threshold
    assert _judge(_fan([0] * 5, duty))["status"] == "ok"


def test_one_running_sample_is_not_a_stall() -> None:
    assert _judge(_fan([0, 0, 0, 0, 900], 60.0))["status"] == "ok"


# --- jitter -----------------------------------------------------------------


def test_unstable_fan_warns_with_percentage() -> None:
    samples = [900, 1500, 800, 1600, 1000]
    result = _judge(_fan(samples, 50.0, key="sys2", label="SYS_FAN2"))
    assert result["status"] == "warn"
    v = _verdicts(result)["sys2"]
    assert v["cv"] is not None and v["cv"] > 0.15
    assert result["reason"].startswith("Fan SYS_FAN2 speed is unstable (±")
    assert result["reason"].endswith("%) at a constant setting — possible bearing wear")


def test_jitter_applies_when_duty_is_unreadable() -> None:
    assert _judge(_fan([900, 1500, 800, 1600, 1000], None))["status"] == "warn"


def test_cv_threshold_boundary() -> None:
    # Population CV of [m-d, m+d, m-d, m+d] is d/m.
    just_over = [1000 - 151, 1000 + 151, 1000 - 151, 1000 + 151]
    exactly = [1000 - 150, 1000 + 150, 1000 - 150, 1000 + 150]
    under = [1000 - 100, 1000 + 100, 1000 - 100, 1000 + 100]
    assert _judge(_fan(just_over))["status"] == "warn"
    assert _judge(_fan(exactly))["status"] == "ok"
    assert _judge(_fan(under))["status"] == "ok"


def test_jitter_needs_four_running_samples() -> None:
    wild = [500, 1500, 500, 1500]
    assert _judge(_fan(wild))["status"] == "warn"
    assert _judge(_fan(wild[:3]))["status"] == "ok"
    # A zero sample is not a running sample.
    assert _judge(_fan([0, 500, 1500, 500]))["status"] == "ok"


def test_jitter_needs_a_mean_of_300_rpm() -> None:
    slow = [150, 450, 150, 450, 150]  # mean 270, cv ~0.5
    assert _judge(_fan(slow))["status"] == "ok"
    just_fast_enough = [200, 400, 200, 400, 300]  # mean 300, cv ~0.27
    assert _judge(_fan(just_fast_enough))["status"] == "warn"


def test_steady_fan_is_ok() -> None:
    result = _judge(_fan(STEADY))
    assert result["status"] == "ok"
    assert result["reason"] == "1 fan spinning normally"
    (v,) = result["details"]["fans"]
    assert v["symptom"] is None and v["mean_rpm"] == pytest.approx(1179.6)
    assert v["cv"] is not None and v["cv"] < 0.01


# --- idle, labels, worst-of -------------------------------------------------


def test_idle_fans_are_skipped() -> None:
    result = _judge(_fan([0] * 5, None, key="fan3", idle=True), _fan(STEADY))
    assert result["status"] == "ok"
    assert [v["key"] for v in result["details"]["fans"]] == ["nct6798.fan1"]
    assert result["reason"] == "1 fan spinning normally"


def test_idle_flag_wins_over_a_stall_shape() -> None:
    assert health_rules._rule_fans(_section(_fan([0] * 5, 80.0, idle=True)), NOW) is None


def test_worst_of_across_fans_lists_every_finding() -> None:
    result = _judge(
        _fan(STEADY, key="a", label="A"),
        _fan([0] * 5, 60.0, key="b", label="B"),
        _fan([500, 1500, 500, 1500, 500], key="c", label="C"),
    )
    assert result["status"] == "warn"
    v = _verdicts(result)
    assert (v["a"]["status"], v["b"]["status"], v["c"]["status"]) == ("ok", "warn", "warn")
    assert "Fan B has stopped" in result["reason"] and "Fan C speed is unstable" in result["reason"]
    assert "Fan A" not in result["reason"]
    assert result["attention"] is True


def test_label_falls_back_to_key() -> None:
    result = _judge(_fan([0] * 5, 50.0, key="nct6798.fan4", label=None))
    assert result["reason"].startswith("Fan nct6798.fan4 has stopped")
    (v,) = result["details"]["fans"]
    assert v["label"] == "nct6798.fan4"


def test_rule_overrides_the_agents_status() -> None:
    section = _section(_fan(STEADY))
    section["status"] = "crit"
    result = health_rules.evaluate_section("fans", section, now=NOW)
    assert result["status"] == "ok"


# --- golden fixtures --------------------------------------------------------


def test_golden_linux_fixture_fans_are_ok() -> None:
    frame = json.loads((FIXTURES_DIR / "telemetry_snapshot_linux.json").read_text())
    fans = frame["snapshot"]["fans"]
    assert fans["fans"], "fixture should carry fans"
    result = health_rules.evaluate_section("fans", fans, now=NOW, agent_os="linux")
    assert result["status"] == "ok"
    assert len(result["details"]["fans"]) == len(fans["fans"])
    assert all(v["status"] == "ok" for v in result["details"]["fans"])


def test_golden_windows_fixture_has_no_fans_to_judge() -> None:
    frame = json.loads((FIXTURES_DIR / "telemetry_snapshot.json").read_text())
    fans = frame["snapshot"]["fans"]
    assert health_rules._rule_fans(fans, NOW) is None
    assert health_rules.evaluate_section("fans", fans, now=NOW)["status"] == "ok"


# --- malformed input never raises -------------------------------------------


@pytest.mark.parametrize(
    "samples",
    [
        None,
        "1180,1176",
        1180,
        {"0": 0},
        [],
        ["a", "b", "c", "d"],
        [None] * 5,
        [-1, -1, -1, -1, -1],
        [0, 0, "x", 0, 0],
        [[1], [2], [3], [4]],
        [True, True, True, True],
        [float("inf")] * 5,
        [float("nan")] * 5,
        [10**400] * 5,
        [10**9] * 5,
        [1000, 1000, 1000, 1000, -400],
    ],
)
def test_malformed_samples_are_not_judged(samples: Any) -> None:
    result = _judge(_fan(samples, 80.0))
    assert result["status"] == "ok"
    assert result["details"]["fans"][0]["status"] == "ok"


@pytest.mark.parametrize(
    "fan",
    [
        None,
        "fan",
        42,
        [],
        {},
        {"rpm_samples": [0] * 5, "duty_percent": 50},
        {"key": 7, "label": 9, "rpm_samples": [0] * 5, "duty_percent": 50},
        {"key": "k", "label": "x" * 500, "rpm_samples": [0] * 5, "duty_percent": 50},
        {"key": "k", "idle_or_absent": "yes", "rpm_samples": None},
    ],
)
def test_malformed_fan_entries_do_not_raise(fan: Any) -> None:
    result = health_rules.evaluate_section("fans", _section(fan, _fan(STEADY)), now=NOW)
    assert result["status"] in {"ok", "warn"}
    json.dumps(result)


def test_oversized_label_is_truncated() -> None:
    result = _judge(_fan([0] * 5, 50.0, label="x" * 500))
    assert len(result["reason"]) < 150


def test_many_fans_are_capped() -> None:
    result = _judge(*[_fan(STEADY, key=f"f{i}") for i in range(500)])
    assert len(result["details"]["fans"]) == health_rules._FAN_MAX_FANS

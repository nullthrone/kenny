"""``_rule_gpu``: every branch, worst-of, the golden fixtures, malformed input."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kenny_server import health_rules
from kenny_server.webui import _SECTION_ACTION

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
NOW = datetime(2026, 6, 4, 18, 30, tzinfo=timezone.utc)
NAME = "NVIDIA GeForce RTX 4080"


def _gpu(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": NAME,
        "vendor": "nvidia",
        "fan_target_percent": 30,
        "pcie": {"gen_current": 1, "gen_max": 4, "width_current": 16, "width_max": 16},
        "throttle": {
            "hw_slowdown": False,
            "hw_thermal_slowdown": False,
            "hw_power_brake_slowdown": False,
            "sw_thermal_slowdown": False,
        },
        "ecc": None,
        "ras": None,
    }
    base.update(overrides)
    return base


def _judge(*gpus: Any) -> dict[str, Any]:
    return health_rules.evaluate_section(
        "gpu", {"status": "ok", "summary": "", "gpus": list(gpus)}, now=NOW
    )


def _symptoms(result: dict[str, Any]) -> list[str]:
    return [f["symptom"] for f in result["details"]["findings"]]


def test_missing_or_empty_gpus_defer() -> None:
    assert health_rules._rule_gpu({}, NOW) is None
    assert health_rules._rule_gpu({"gpus": []}, NOW) is None
    assert health_rules._rule_gpu({"gpus": None}, NOW) is None
    assert _judge()["status"] == "ok"  # agent's own grade stands


def test_healthy_gpu_is_ok() -> None:
    result = _judge(_gpu())
    assert result["status"] == "ok"
    assert result["details"] == {"findings": []}


@pytest.mark.parametrize(
    "overrides",
    [
        {"ecc": {"uncorrected_volatile": 3, "retired_pages_pending": False, "remapped_rows": None}},
        {"ecc": {"uncorrected_volatile": 0, "remapped_rows": {"failure": True}}},
        {"ecc": {"uncorrected_volatile": 0, "retired_pages_pending": True}},
        {"ras": {"gfx": {"ue": 0, "ce": 0}, "umc": {"ue": 1, "ce": 0}}},
    ],
)
def test_crit_branches(overrides: dict[str, Any]) -> None:
    result = _judge(_gpu(**overrides))
    assert result["status"] == "crit"
    assert result["attention"] is True
    assert all(f["name"] == NAME for f in result["details"]["findings"])
    assert result["reason"].startswith(f"The graphics card {NAME} ")


def test_uncorrectable_reason_is_a_symptom() -> None:
    result = _judge(_gpu(ecc={"uncorrected_volatile": 2}))
    assert result["reason"] == f"The graphics card {NAME} reported uncorrectable memory errors"
    assert result["details"]["findings"] == [
        {"name": NAME, "status": "crit", "symptom": "reported uncorrectable memory errors"}
    ]


def test_power_brake_is_warn() -> None:
    throttle = {"hw_slowdown": True, "hw_thermal_slowdown": False, "hw_power_brake_slowdown": True}
    result = _judge(_gpu(throttle=throttle))
    assert result["status"] == "warn"
    assert result["reason"].startswith(
        f"The graphics card {NAME} is being slowed by a power-delivery signal"
    )


def test_hw_slowdown_without_thermal_is_warn() -> None:
    result = _judge(_gpu(throttle={"hw_slowdown": True, "hw_thermal_slowdown": False}))
    assert result["status"] == "warn"
    assert _symptoms(result) == ["is being slowed down by the hardware"]


def test_hw_slowdown_caused_by_thermal_is_posture_only() -> None:
    result = _judge(_gpu(throttle={"hw_slowdown": True, "hw_thermal_slowdown": True}))
    assert result["status"] == "posture"
    assert result["attention"] is False
    assert result["tier"] == "posture"
    assert result["reason"] == f"The graphics card {NAME} is slowing down to stay cool"


def test_hw_slowdown_with_unreported_thermal_is_warn() -> None:
    result = _judge(_gpu(throttle={"hw_slowdown": True, "hw_thermal_slowdown": None}))
    assert result["status"] == "warn"


@pytest.mark.parametrize(
    "overrides",
    [
        {"throttle": {"hw_thermal_slowdown": True}},
        {"ras": {"umc": {"ue": 0, "ce": 2}}},
        {"ecc": {"uncorrected_volatile": 0, "remapped_rows": {"correctable": 4, "failure": False}}},
    ],
)
def test_posture_branches(overrides: dict[str, Any]) -> None:
    result = _judge(_gpu(**overrides))
    assert result["status"] == "posture"
    assert result["attention"] is False
    assert health_rules.worst(result["status"]) == "ok"


def test_not_judged_signals_stay_ok() -> None:
    # PCIe downshift is the trend layer's call; fan target is not a measurement.
    result = _judge(
        _gpu(
            pcie={"gen_current": 1, "gen_max": 4, "width_current": 1, "width_max": 16},
            fan_target_percent=100,
            throttle={"hw_slowdown": False, "sw_thermal_slowdown": True},
            ecc={"uncorrected_volatile": 0, "retired_pages_pending": False, "remapped_rows": None},
        )
    )
    assert result["status"] == "ok"


def test_worst_of_across_gpus_names_each_card() -> None:
    other = "AMD Radeon RX 7800 XT"
    result = _judge(
        _gpu(throttle={"hw_thermal_slowdown": True}),
        _gpu(name=other, ras={"umc": {"ue": 1, "ce": 0}}),
    )
    assert result["status"] == "crit"
    findings = result["details"]["findings"]
    assert [(f["name"], f["status"]) for f in findings] == [(other, "crit"), (NAME, "posture")]
    # Worst first in the reason too.
    assert result["reason"].startswith(f"The graphics card {other} ")


def test_reason_caps_named_findings() -> None:
    gpus = [_gpu(name=f"GPU {i}", ras={"umc": {"ue": 1}}) for i in range(5)]
    result = _judge(*gpus)
    assert result["reason"].endswith("; +2 more")
    assert len(result["details"]["findings"]) == 5


@pytest.mark.parametrize(
    "gpus",
    [
        "not a list",
        ["a string", 5, None, ["nested"]],
        [{}],
        [{"name": 7, "ecc": "x", "ras": [1, 2], "throttle": "hot"}],
        [{"ecc": {"uncorrected_volatile": "many", "remapped_rows": "bad"}, "ras": {"gfx": 3}}],
        [{"ecc": {"uncorrected_volatile": float("inf")}, "ras": {"gfx": {"ue": True, "ce": "2"}}}],
        [{"ecc": {"uncorrected_volatile": 10**400}}],
    ],
)
def test_malformed_input_never_raises(gpus: Any) -> None:
    outcome = health_rules._rule_gpu({"gpus": gpus}, NOW)
    assert outcome is None or outcome[0] in ("ok", "posture", "warn", "crit")
    health_rules.evaluate_section("gpu", {"gpus": gpus}, now=NOW)


def test_oversized_uncorrected_count_is_treated_as_unreported() -> None:
    # An int too large for a float is unusable, so it is treated as unreported.
    assert _judge(_gpu(ecc={"uncorrected_volatile": 10**400}))["status"] == "ok"


def _fixture_gpu(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())["snapshot"]["gpu"]


def test_windows_fixture_gpu_is_ok() -> None:
    result = health_rules.evaluate_section("gpu", _fixture_gpu("telemetry_snapshot.json"), now=NOW)
    assert result["status"] == "ok"
    assert result["attention"] is False


def test_linux_fixture_gpu_is_not_an_incident() -> None:
    # The fixture's amdgpu umc block carries two corrected errors: a standing
    # fact (posture), never an alarm.
    result = health_rules.evaluate_section(
        "gpu", _fixture_gpu("telemetry_snapshot_linux.json"), now=NOW
    )
    assert result["status"] == "posture"
    assert result["attention"] is False
    assert health_rules.worst(result["status"]) == "ok"


def test_fixture_is_not_mutated() -> None:
    payload = _fixture_gpu("telemetry_snapshot_linux.json")
    before = copy.deepcopy(payload)
    health_rules.evaluate_section("gpu", payload, now=NOW)
    assert payload == before


def test_gpu_is_confirmed_before_alarm() -> None:
    assert "gpu" in health_rules.CONFIRM_BEFORE_ALARM


def test_hardware_sections_have_dashboard_action_labels() -> None:
    for section in ("disk_smart", "hardware_errors", "gpu", "fans"):
        assert section in _SECTION_ACTION
        assert _SECTION_ACTION[section] != "REVIEW"

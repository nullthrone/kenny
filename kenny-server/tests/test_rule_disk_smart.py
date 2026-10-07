"""The ``disk_smart`` health rule: SMART / NVMe judgement per internal disk."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kenny_server import health_rules

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
NOW = datetime(2026, 6, 4, 18, 30, tzinfo=timezone.utc)


def _nvme(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "critical_warning": 0, "available_spare": 100, "available_spare_threshold": 10,
        "percentage_used": 2, "media_errors": 0, "unsafe_shutdowns": 14,
        "error_log_entries": 0, "data_units_written": 1, "power_on_hours": 10,
        "temperature_c": 40,
    }
    base.update(over)
    return base


def _nvme_disk(model: str = "WD_BLACK SN850X", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "model": model, "serial": "SN1", "health_status": "Healthy",
        "predictive_failure": False, "read_errors_uncorrected": 0,
        "write_errors_uncorrected": 0, "bus_type": "NVMe", "media_type": "SSD",
        "removable": False, "smart_attributes": None, "nvme": _nvme(),
        "nvme_error": None, "paused": False,
    }
    row.update(over)
    return row


def _sata_disk(model: str = "ST2000DM008", media: str = "HDD", **attrs: int) -> dict[str, Any]:
    smart = {"5": 0, "187": 0, "188": 0, "197": 0, "198": 0, "199": 0}
    smart.update(attrs)
    return {
        "model": model, "serial": "SA1", "health_status": "Healthy",
        "predictive_failure": False, "read_errors_uncorrected": 0,
        "write_errors_uncorrected": 0, "bus_type": "SATA", "media_type": media,
        "removable": False, "smart_attributes": smart, "nvme": None,
        "nvme_error": None, "paused": False,
    }


def _rule(*disks: Any) -> Any:
    return health_rules._rule_disk_smart({"status": "ok", "disks": list(disks)}, NOW)


# -- deferral -----------------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"disks": []}, {"disks": None}, {"disks": "x"}, {"disks": ["x", 3]}])
def test_defers_without_rows(payload: dict) -> None:
    assert health_rules._rule_disk_smart(payload, NOW) is None


def test_defers_on_pre_0_22_row_shape() -> None:
    old = {
        "model": "Samsung SSD 870", "health_status": "Unhealthy", "predictive_failure": True,
        "wear": 3, "temperature_c": 34, "power_on_hours": 5,
        "read_errors_total": 0, "read_errors_uncorrected": 9, "write_errors_uncorrected": 0,
    }
    assert _rule(old, dict(old)) is None
    # The agent's own grade then stands through evaluate_section.
    out = health_rules.evaluate_section(
        "disk_smart", {"status": "crit", "summary": "agent line", "disks": [old]}, now=NOW
    )
    assert out["status"] == "crit" and out["summary"] == "agent line"


@pytest.mark.parametrize("key", ["nvme", "smart_attributes", "bus_type"])
def test_any_new_key_on_any_row_makes_the_rule_authoritative(key: str) -> None:
    old = {"model": "A", "health_status": "Healthy", "predictive_failure": False}
    new = {"model": "B", "health_status": "Healthy", key: None}
    assert _rule(old, new)[0] == "ok"


# -- crit ---------------------------------------------------------------------


def test_predictive_failure_is_crit() -> None:
    status, reason, *_ = _rule(_sata_disk("Samsung SSD 870 EVO", "SSD", ) | {"predictive_failure": True})
    assert status == "crit"
    assert reason == "Disk Samsung SSD 870 EVO reports that it is failing"


def test_predictive_failure_unknown_is_not_a_finding() -> None:
    assert _rule(_sata_disk() | {"predictive_failure": None})[0] == "ok"


@pytest.mark.parametrize("health", ["Unhealthy", "unhealthy", " UNHEALTHY "])
def test_unhealthy_is_crit_case_insensitively(health: str) -> None:
    status, reason, *_ = _rule(_sata_disk("D1") | {"health_status": health})
    assert (status, reason) == ("crit", "Disk D1 reports that it is failing")


@pytest.mark.parametrize(
    "bit, symptom",
    [
        (0, "has used up its spare capacity"),
        (2, "reports that its reliability is degraded"),
        (3, "has switched to read-only to protect its data"),
        (4, "reports that its power-loss memory backup has failed"),
    ],
)
def test_nvme_critical_warning_bits_are_crit(bit: int, symptom: str) -> None:
    status, reason, *_ = _rule(_nvme_disk("SN850X", nvme=_nvme(critical_warning=1 << bit)))
    assert (status, reason) == ("crit", f"Disk SN850X {symptom}")


def test_nvme_spare_wording_matches_the_spec_example() -> None:
    reason = _rule(_nvme_disk("WD_BLACK SN850X", nvme=_nvme(critical_warning=1)))[1]
    assert reason == "Disk WD_BLACK SN850X has used up its spare capacity"


# -- warn ---------------------------------------------------------------------


def test_health_status_warning_is_warn() -> None:
    status, reason, *_ = _rule(_sata_disk("D1") | {"health_status": "Warning"})
    assert (status, reason) == ("warn", "Disk D1 reports a health warning")


def test_temperature_bit_alone_is_warn() -> None:
    status, reason, *_ = _rule(_nvme_disk("N1", nvme=_nvme(critical_warning=0b10)))
    assert (status, reason) == ("warn", "Disk N1 reports a temperature warning")


def test_temperature_bit_with_a_crit_bit_is_crit_and_lists_both() -> None:
    status, reason, *_ = _rule(_nvme_disk("N1", nvme=_nvme(critical_warning=0b11)))
    assert status == "crit"
    assert reason == (
        "Disk N1 has used up its spare capacity; Disk N1 reports a temperature warning"
    )


def test_pending_sectors_are_warn_on_hdd_and_ssd() -> None:
    for media in ("HDD", "SSD"):
        status, reason, *_ = _rule(_sata_disk("ST2000DM008", media, **{"197": 8}))
        assert (status, reason) == (
            "warn", "Disk ST2000DM008 has sectors waiting to be reallocated"
        )


def test_pending_sectors_zero_is_ok() -> None:
    assert _rule(_sata_disk(**{"197": 0}))[0] == "ok"


# -- posture ------------------------------------------------------------------


def test_nvme_media_errors_are_posture() -> None:
    status, reason, *_ = _rule(_nvme_disk("N1", nvme=_nvme(media_errors=3)))
    assert status == "posture"
    assert reason == "Disk N1 has recorded unrecoverable read errors in its lifetime"
    assert health_rules.tier_of(status) == "posture"


def test_uncorrected_counters_are_posture() -> None:
    read = _rule(_nvme_disk("N1", read_errors_uncorrected=2))
    write = _rule(_sata_disk("S1", "SSD") | {"write_errors_uncorrected": 1})
    assert read[0] == "posture" and "read errors" in read[1]
    assert write[0] == "posture" and "write errors" in write[1]


def test_nvme_media_errors_and_mirrored_read_counter_are_one_finding() -> None:
    out = _rule(_nvme_disk(nvme=_nvme(media_errors=3), read_errors_uncorrected=3))
    assert len(out[2]["disks"]) == 1


@pytest.mark.parametrize("attr", ["5", "187", "198"])
def test_hdd_lifetime_attributes_are_posture(attr: str) -> None:
    status, reason, *_ = _rule(_sata_disk("ST2000DM008", "HDD", **{attr: 12}))
    assert status == "posture"
    assert reason.startswith("Disk ST2000DM008 has ")
    assert "in its lifetime" in reason


@pytest.mark.parametrize("attr", ["5", "187", "198"])
def test_ssd_lifetime_attributes_are_not_judged(attr: str) -> None:
    assert _rule(_sata_disk("S1", "SSD", **{attr: 12}))[0] == "ok"


@pytest.mark.parametrize("attr", ["188", "199"])
def test_timeout_and_crc_counters_never_grade(attr: str) -> None:
    assert _rule(_sata_disk("S1", "HDD", **{attr: 500}))[0] == "ok"


@pytest.mark.parametrize("used, expected", [(89, "ok"), (90, "posture"), (93, "posture"), (140, "posture")])
def test_percentage_used_threshold(used: int, expected: str) -> None:
    out = _rule(_nvme_disk("N1", nvme=_nvme(percentage_used=used)))
    assert out[0] == expected
    if expected == "posture":
        assert out[1] == f"Disk N1 is at {used}% of its rated write endurance"


def test_posture_never_rolls_up_into_the_host() -> None:
    snapshot = {
        "disk_smart": {"status": "ok", "disks": [_nvme_disk(nvme=_nvme(media_errors=1))]}
    }
    result = health_rules.evaluate_snapshot(snapshot, now=NOW)
    section = result["sections"]["disk_smart"]
    assert (section["status"], section["tier"], section["attention"]) == ("posture", "posture", False)
    assert result["overall"] == "ok"


# -- ignored disks ------------------------------------------------------------


def test_sd_card_readers_are_ignored_like_usb() -> None:
    sd = _sata_disk("Card", "SSD") | {"bus_type": "SD", "predictive_failure": True}
    assert _rule(sd) == ("ok", "No internal disks to judge")
    sd_nvme = _nvme_disk("Card2", nvme=None, nvme_error="x") | {"bus_type": "SD"}
    assert _rule(sd_nvme) == ("ok", "No internal disks to judge")


def test_usb_and_removable_disks_are_ignored_entirely() -> None:
    usb = _sata_disk("Stick", "SSD") | {"bus_type": "USB", "predictive_failure": True}
    removable = _sata_disk("Card", "SSD") | {"removable": True, "health_status": "Unhealthy"}
    assert _rule(usb, removable) == ("ok", "No internal disks to judge")
    out = _rule(usb, removable, _sata_disk("Internal"))
    assert out[0] == "ok" and "1 disk" in out[1]


# -- paused -------------------------------------------------------------------


def test_paused_nvme_row_has_no_log_but_its_health_status_still_counts() -> None:
    paused = _nvme_disk("P1", paused=True, nvme=None, predictive_failure=False)
    assert _rule(paused)[0] == "ok"
    warning = paused | {"health_status": "Warning"}
    assert _rule(warning)[0] == "warn"
    unhealthy = paused | {"health_status": "Unhealthy"}
    assert _rule(unhealthy)[0] == "crit"


def test_paused_row_still_judges_the_smart_flag_and_attributes() -> None:
    # The SMART WMI classes are not paused: a failing disk must not look healthy
    # because a protected game is running.
    failing = _sata_disk("P2", "HDD", **{"197": 5, "5": 5}) | {
        "paused": True, "predictive_failure": True, "read_errors_uncorrected": 4,
    }
    status, reason, *_ = _rule(failing)
    assert status == "crit"
    assert reason.startswith("Disk P2 reports that it is failing")
    assert "has sectors waiting to be reallocated" in reason
    attrs_only = _sata_disk("P3", "HDD", **{"197": 2}) | {"paused": True}
    assert _rule(attrs_only)[0] == "warn"
    nvme_failing = _nvme_disk("P4", paused=True, nvme=None, predictive_failure=True)
    assert _rule(nvme_failing)[0] == "crit"


def test_paused_row_ignores_a_stale_nvme_log_it_could_not_have_read() -> None:
    row = _nvme_disk("P5", paused=True, nvme=_nvme(critical_warning=4, media_errors=3))
    assert _rule(row)[0] == "ok"


# -- nvme_error -----------------------------------------------------------------


def test_unreadable_nvme_log_is_a_posture_finding_never_healthy() -> None:
    row = _nvme_disk("U1", nvme=None, nvme_error="unsupported by driver")
    status, reason, details = _rule(row)
    assert status == "posture"
    assert reason == "Disk U1 health log could not be read (unsupported by driver)"
    assert details["disks"][0]["status"] == "posture"
    assert details["disks"][0]["symptom"] == "health log could not be read (unsupported by driver)"


def test_unreadable_nvme_log_does_not_outrank_a_real_failure() -> None:
    row = _nvme_disk("U2", nvme=None, nvme_error="access denied") | {"health_status": "Warning"}
    status, reason, *_ = _rule(row)
    assert status == "warn"
    assert reason.startswith("Disk U2 reports a health warning; ")
    assert "health log could not be read (access denied)" in reason


@pytest.mark.parametrize(
    "row",
    [
        _nvme_disk("N1", nvme=_nvme(), nvme_error="access denied"),  # log was read
        _nvme_disk("N2", nvme=None, nvme_error=None),  # nothing to explain
        _nvme_disk("N3", nvme=None, nvme_error="  "),
        _nvme_disk("N4", nvme=None, nvme_error="access denied", paused=True),
        _nvme_disk("N5", nvme=None, nvme_error="access denied", bus_type="SATA"),
        _nvme_disk("N6", nvme=None, nvme_error="access denied", bus_type="USB"),
        _nvme_disk("N7", nvme=None, nvme_error="access denied", removable=True),
    ],
    ids=["log-read", "no-error", "blank-error", "paused", "not-nvme", "usb", "removable"],
)
def test_no_unreadable_log_finding_without_an_unread_nvme_log(row: dict[str, Any]) -> None:
    assert _rule(row)[0] == "ok"


def test_oversized_nvme_error_is_truncated() -> None:
    reason = _rule(_nvme_disk("U3", nvme=None, nvme_error="x" * 5_000))[1]
    assert len(reason) < 200


# -- aggregation --------------------------------------------------------------


def test_worst_of_across_disks_and_most_severe_first() -> None:
    posture = _nvme_disk("P", nvme=_nvme(percentage_used=95))
    warn = _sata_disk("W", **{"197": 1})
    crit = _sata_disk("C") | {"predictive_failure": True}
    status, reason, details = _rule(posture, warn, crit)
    assert status == "crit"
    assert reason == (
        "Disk C reports that it is failing; "
        "Disk W has sectors waiting to be reallocated; "
        "Disk P is at 95% of its rated write endurance"
    )
    assert [d["status"] for d in details["disks"]] == ["crit", "warn", "posture"]


def test_reason_joins_at_most_three_findings() -> None:
    disks = [_sata_disk(f"D{i}") | {"health_status": "Warning"} for i in range(5)]
    status, reason, details = _rule(*disks)
    assert status == "warn"
    assert reason.count("; ") == 2
    assert reason.endswith("(+2 more)")
    assert len(details["disks"]) == 5


def test_details_shape_survives_evaluate_section() -> None:
    row = _sata_disk("ST2000DM008", **{"197": 2})
    out = health_rules.evaluate_section(
        "disk_smart", {"status": "ok", "summary": "agent", "disks": [row]}, now=NOW
    )
    assert out["status"] == "warn" and out["attention"] is True
    assert out["summary"] == ""
    assert out["details"] == {
        "disks": [
            {
                "model": "ST2000DM008", "serial": "SA1", "status": "warn",
                "symptom": "has sectors waiting to be reallocated",
            }
        ]
    }


def test_reasons_use_symptoms_not_codes() -> None:
    row = _sata_disk("D", **{"197": 2, "5": 1})
    reason = _rule(row)[1]
    for banned in ("197", "SMART", "attribute", "0x"):
        assert banned not in reason


def test_rule_verdict_replaces_the_agent_grade() -> None:
    # Agent says crit (pre-0.22 grading of a non-zero uncorrected count); the
    # rule sees a standing fact and the verdict is posture, not crit.
    row = _nvme_disk(read_errors_uncorrected=1, nvme=_nvme(media_errors=1))
    out = health_rules.evaluate_section(
        "disk_smart", {"status": "crit", "summary": "x", "disks": [row]}, now=NOW
    )
    assert out["status"] == "posture"


# -- golden fixtures ----------------------------------------------------------


@pytest.mark.parametrize(
    "fixture, agent_os",
    [("telemetry_snapshot.json", "windows"), ("telemetry_snapshot_linux.json", "linux")],
)
def test_golden_fixture_disk_smart_is_ok(fixture: str, agent_os: str) -> None:
    section = json.loads((FIXTURES_DIR / fixture).read_text())["snapshot"]["disk_smart"]
    assert section["disks"]
    assert health_rules._rule_disk_smart(section, NOW) is not None  # authoritative
    out = health_rules.evaluate_section("disk_smart", section, now=NOW, agent_os=agent_os)
    assert out["status"] == "ok", out
    assert "details" not in out


def test_golden_windows_fixture_goes_crit_when_a_disk_predicts_failure() -> None:
    section = copy.deepcopy(
        json.loads((FIXTURES_DIR / "telemetry_snapshot.json").read_text())["snapshot"]["disk_smart"]
    )
    section["disks"][0]["predictive_failure"] = True
    out = health_rules.evaluate_section("disk_smart", section, now=NOW)
    assert out["status"] == "crit"
    assert out["reason"] == "Disk Samsung SSD 870 EVO 1TB reports that it is failing"


# -- malformed input ----------------------------------------------------------

_MALFORMED_ROWS: list[Any] = [
    None, "disk", 7, [], [1, 2],
    {"nvme": "oops"}, {"nvme": [1]}, {"nvme": {"critical_warning": "3"}},
    {"nvme": {"critical_warning": None, "percentage_used": "99", "media_errors": "1"}},
    {"nvme": {"critical_warning": -1}}, {"nvme": {"critical_warning": 1.7}},
    {"nvme": {"critical_warning": float("inf"), "percentage_used": float("nan")}},
    {"nvme": {"critical_warning": 10**400, "percentage_used": 10**400}},
    {"nvme": {"critical_warning": True, "media_errors": True}},
    {"nvme": {"critical_warning": [1], "media_errors": {"a": 1}}},
    {"smart_attributes": [1]}, {"smart_attributes": "x"},
    {"smart_attributes": {"197": "3", "5": None, "187": [1], "198": {}}},
    {"smart_attributes": {"197": float("nan")}, "media_type": "HDD"},
    {"smart_attributes": {5: 1, "197": 10**400}, "media_type": "HDD"},
    {"bus_type": ["USB"]}, {"bus_type": {"a": 1}}, {"removable": "yes", "bus_type": 3},
    {"model": 5, "serial": [1], "health_status": 9, "bus_type": "NVMe"},
    {"model": None, "serial": None, "health_status": ["Unhealthy"], "nvme": None},
    {"predictive_failure": "true", "paused": "yes", "nvme": None},
    {"media_type": ["HDD"], "read_errors_uncorrected": "9", "nvme": None},
    {"model": "x" * 10_000, "health_status": "Unhealthy", "nvme": None},
]


@pytest.mark.parametrize("row", _MALFORMED_ROWS, ids=range(len(_MALFORMED_ROWS)))
def test_malformed_row_never_raises(row: Any) -> None:
    out = health_rules.evaluate_section(
        "disk_smart", {"status": "ok", "summary": "", "disks": [row, _sata_disk()]}, now=NOW
    )
    assert out["status"] in ("ok", "posture", "warn", "crit")


@pytest.mark.parametrize("disks", [None, 5, "x", {"a": 1}, [None], [[]], [{}], [{"nvme": None}]])
def test_malformed_disks_container_never_raises(disks: Any) -> None:
    out = health_rules.evaluate_section(
        "disk_smart", {"status": "warn", "summary": "agent", "disks": disks}, now=NOW
    )
    assert out["status"] in ("ok", "posture", "warn", "crit")


def test_oversized_model_is_truncated_in_the_reason() -> None:
    reason = _rule(_sata_disk("x" * 10_000) | {"health_status": "Unhealthy"})[1]
    assert len(reason) < 200

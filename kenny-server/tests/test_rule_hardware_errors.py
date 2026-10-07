"""``_rule_hardware_errors``: every branch of the hardware-event judgement.

Groups are synthesized relative to a fixed ``NOW`` so activity (recent, active,
recurring) is deterministic. The rule's contract: crit/warn/posture/ok per the
spec in its docstring, bugchecks only corroborate, USB retries are set aside,
reasons name symptoms rather than event ids, and nothing raises on malformed
input.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kenny_server import health_rules
from kenny_server.health_rules import _rule_hardware_errors as rule

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

WHEA = "Microsoft-Windows-WHEA-Logger"
KPOWER = "Microsoft-Windows-Kernel-Power"


def grp(
    source: str,
    event_id: int,
    ages_h: list[float],
    details: dict[str, Any] | None = None,
    level: str = "warning",
) -> dict[str, Any]:
    """A group with one event per entry of ``ages_h`` (hours before ``NOW``)."""

    stamps = [NOW - timedelta(hours=h) for h in ages_h]
    by_day: dict[str, int] = {}
    for t in stamps:
        by_day[t.date().isoformat()] = by_day.get(t.date().isoformat(), 0) + 1
    out: dict[str, Any] = {
        "source": source,
        "event_id": event_id,
        "level": level,
        "count": len(stamps),
        "last_seen": max(stamps).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "by_day": by_day,
        "sample": "x",
    }
    if details is not None:
        out["details"] = details
    return out


def section(*groups: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"status": "ok", "window_days": 14, "groups": list(groups), "edac": [], "aer": [], **extra}


def judge(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    out = rule(payload, NOW)
    assert out is not None
    status, reason, details = out
    return status, reason, details["findings"]


ACTIVE = [2.0, 26.0]  # two events within 48 h, on two days
STALE = [240.0, 264.0]  # ten days ago
THREE_DAYS = [2.0, 26.0, 50.0]  # three calendar days, last 2 h ago


def whea18(ages: list[float] = ACTIVE) -> dict[str, Any]:
    return grp(WHEA, 18, ages, {"error_source": {"Machine Check Exception": len(ages)}}, "error")


def whea19(ages: list[float] = ACTIVE, error_type: str = "Cache Hierarchy Error") -> dict[str, Any]:
    return grp(WHEA, 19, ages, {"error_type": {error_type: len(ages)}})


def bugcheck_41(code: str | None, ages: list[float] = (30.0,)) -> dict[str, Any]:  # type: ignore[assignment]
    details = {"bugcheck_code": {code: len(ages)}} if code else None
    return grp(KPOWER, 41, list(ages), details, "critical")


def display(ages: list[float] = THREE_DAYS) -> dict[str, Any]:
    return grp("Display", 4101, ages, {"driver": {"nvlddmkm": len(ages)}})


def disk153(ages: list[float] = ACTIVE, bus: dict[str, int] | None = None) -> dict[str, Any]:
    return grp("disk", 153, ages, {"disk_bus_type": bus if bus is not None else {"SATA": len(ages)}})


CRASHES = {
    "total": 17,
    "distinct_apps": 5,
    "distinct_modules": 4,
    "exception_codes": {"0xc0000005": 4, "0xc000001d": 2, "0xc0000409": 11},
    "by_day": {"2026-06-09": 9, "2026-06-10": 8},
}


# -- deferral and quiet ---------------------------------------------------------


@pytest.mark.parametrize("payload", [{}, {"status": "ok"}, {"summary": "x", "errors": []}])
def test_defers_without_groups_edac_or_aer_keys(payload: dict[str, Any]) -> None:
    assert rule(payload, NOW) is None


@pytest.mark.parametrize("key", ["groups", "edac", "aer"])
def test_any_one_of_the_keys_is_enough_to_judge(key: str) -> None:
    out = rule({key: []}, NOW)
    assert out is not None and out[0] == "ok"


def test_empty_is_ok_and_says_what_was_covered() -> None:
    status, reason, findings = judge(section())
    assert (status, findings) == ("ok", [])
    assert reason == "no hardware faults in 14d"


def test_short_log_is_not_read_as_a_quiet_window() -> None:
    _, reason, _ = judge(section(effective_window_days=3))
    assert "3d" in reason and "14d" not in reason


def test_unattributed_groups_are_ignored() -> None:
    noise = grp("Some-Other-Provider", 7, ACTIVE)
    unqueried = grp(WHEA, 99, ACTIVE)
    assert judge(section(noise, unqueried))[0] == "ok"


# -- uncorrected hardware errors ------------------------------------------------


def test_recurring_active_uncorrected_cpu_error_is_crit() -> None:
    status, reason, findings = judge(section(whea18()))
    assert status == "crit"
    assert reason.startswith("The processor reported uncorrectable hardware errors")
    assert "2 times over 2 days" in reason
    f = findings[0]
    assert (f["component"], f["severity"], f["status"], f["source"], f["event_id"]) == (
        "cpu", "fatal", "crit", WHEA, 18,
    )
    assert f["active_days"] == 2 and f["last_seen_age_hours"] == 2.0


def test_single_active_uncorrected_error_is_warn() -> None:
    status, reason, findings = judge(section(whea18([2.0])))
    assert status == "warn"
    assert reason == "The processor reported uncorrectable hardware errors"
    assert findings[0]["status"] == "warn"


def test_stale_uncorrected_error_is_posture_not_an_alarm() -> None:
    status, reason, _ = judge(section(whea18(STALE)))
    assert status == "posture"
    assert "not seen recently" in reason


def test_hardware_xid_repeating_is_crit_on_the_gpu() -> None:
    g = grp("nvlddmkm", 153, ACTIVE, {"xid": {"79": 2}})
    status, reason, findings = judge(section(g))
    assert status == "crit"
    assert findings[0]["component"] == "gpu"
    assert reason.startswith("The graphics card reported uncorrectable hardware errors")


def test_linux_hardware_xid_group_is_crit() -> None:
    g = grp("nvrm_xid", 0, ACTIVE, {"xid": {"79": 2}}, "error")
    assert judge(section(g))[0] == "crit"


def test_software_xid_is_not_a_hardware_finding() -> None:
    g = grp("nvlddmkm", 13, THREE_DAYS, {"xid": {"31": 3}})
    assert judge(section(g))[0] == "ok"


def test_memory_test_errors_active_is_crit_on_a_single_event() -> None:
    g = grp("Microsoft-Windows-MemoryDiagnostics-Results", 1202, [5.0], level="error")
    status, reason, findings = judge(section(g))
    assert status == "crit"
    assert reason == "The memory test found errors"
    assert findings[0]["component"] == "memory"


def test_old_memory_test_result_is_posture() -> None:
    g = grp("Microsoft-Windows-MemoryDiagnostics-Results", 1202, [300.0], level="error")
    status, reason, _ = judge(section(g))
    assert status == "posture" and reason.startswith("The memory test found errors")


# -- EDAC / AER -------------------------------------------------------------------


def test_edac_uncorrected_is_crit() -> None:
    status, reason, findings = judge(
        section(edac=[{"controller": "mc0", "ce_count": 0, "ue_count": 1}])
    )
    assert status == "crit"
    assert reason == "The memory reported uncorrectable hardware errors"
    assert findings[0]["source"] == "edac" and findings[0]["active_days"] is None


def test_edac_corrected_only_is_posture() -> None:
    status, reason, _ = judge(section(edac=[{"controller": "mc0", "ce_count": 3, "ue_count": 0}]))
    assert status == "posture"
    assert reason == "The memory corrects hardware errors (standing)"


def test_aer_nonfatal_alone_stays_warn() -> None:
    status, reason, findings = judge(
        section(aer=[{"device": "0000:01:00.0", "correctable": 0, "nonfatal": 4, "fatal": 0}])
    )
    assert status == "warn"
    assert findings[0]["component"] == "pcie" and findings[0]["severity"] == "fatal"
    assert reason == "A PCIe link reported uncorrectable hardware errors"


def test_aer_fatal_escalates_with_an_active_uncorrected_group() -> None:
    aer = [{"device": "0000:01:00.0", "correctable": 0, "nonfatal": 0, "fatal": 1}]
    status, _, findings = judge(section(whea18([2.0]), aer=aer))
    assert [f["status"] for f in findings if f["source"] == "aer"] == ["crit"]
    # ... but a stale uncorrected group does not escalate the link errors
    status, _, findings = judge(section(whea18(STALE), aer=aer))
    assert [f["status"] for f in findings if f["source"] == "aer"] == ["warn"]


def test_aer_correctable_is_posture() -> None:
    status, _, _ = judge(section(aer=[{"device": "d", "correctable": 12, "nonfatal": 0, "fatal": 0}]))
    assert status == "posture"


# -- corrected errors -------------------------------------------------------------


def test_active_corrected_errors_are_posture() -> None:
    status, reason, findings = judge(section(whea19()))
    assert status == "posture"
    assert reason == "The processor corrects hardware errors (standing)"
    assert findings[0]["severity"] == "corrected"


def test_bus_interconnect_corrected_errors_name_the_platform() -> None:
    _, reason, findings = judge(section(whea19(error_type="Bus/Interconnect Error")))
    assert findings[0]["component"] == "platform"
    assert reason == "The processor interconnect corrects hardware errors (standing)"


def test_stale_corrected_errors_are_ok() -> None:
    assert judge(section(whea19(STALE)))[0] == "ok"


def test_posture_is_not_an_incident_through_evaluate_section() -> None:
    result = health_rules.evaluate_section("hardware_errors", section(whea19()), now=NOW)
    assert (result["status"], result["tier"], result["attention"]) == ("posture", "posture", False)
    assert result["details"]["findings"][0]["component"] == "cpu"


def test_linux_journal_machine_check_is_posture() -> None:
    g = grp("mce", 0, ACTIVE, None, "error")
    status, reason, findings = judge(section(g))
    assert status == "posture" and findings[0]["component"] == "cpu"
    assert reason == "The processor reports hardware errors (standing)"


# -- GPU instability --------------------------------------------------------------


def test_gpu_resets_without_corroboration_are_posture() -> None:
    status, reason, findings = judge(section(display()))
    assert status == "posture"
    assert reason == "The graphics driver crashed and recovered 3 times over 3 days"
    assert findings[0]["component"] == "gpu" and findings[0]["severity"] == "instability"


@pytest.mark.parametrize(
    "corroboration",
    [
        bugcheck_41("0x116"),
        bugcheck_41("0x00000119"),
        grp("BugCheck", 1001, [30.0], {"bugcheck_code": {"0x00000116": 1}}, "error"),
        grp("Microsoft-Windows-WER-SystemErrorReporting", 1001, [30.0], {"bugcheck_code": {"0x116": 1}}, "error"),
        grp("nvlddmkm", 153, [200.0], {"xid": {"79": 1}}),  # a hardware Xid, even an old one
    ],
    ids=["kp41-116", "kp41-119-padded", "bugcheck-1001", "wer-1001", "hardware-xid"],
)
def test_gpu_resets_with_corroboration_are_warn(corroboration: dict[str, Any]) -> None:
    status, reason, findings = judge(section(display(), corroboration))
    gpu = [f for f in findings if f["event_id"] == 4101]
    assert gpu and gpu[0]["status"] == "warn"
    assert status == "warn"
    assert reason.startswith("The graphics driver crashed and recovered 3 times over 3 days")


@pytest.mark.parametrize(
    "not_corroborating",
    [bugcheck_41("0x124"), bugcheck_41("0x117"), bugcheck_41("0x1a"), bugcheck_41(None)],
    ids=["cpu-bugcheck", "live-dump-117", "memory-bugcheck", "no-code"],
)
def test_gpu_resets_stay_posture_without_a_gpu_bugcheck(not_corroborating: dict[str, Any]) -> None:
    assert judge(section(display(), not_corroborating))[0] == "posture"


def test_gpu_resets_on_fewer_than_three_days_are_not_a_finding() -> None:
    assert judge(section(display([2.0, 3.0, 26.0]), bugcheck_41("0x116")))[0] == "ok"


def test_stale_gpu_resets_are_not_a_finding() -> None:
    old = display([200.0, 224.0, 248.0])
    assert judge(section(old, bugcheck_41("0x116")))[0] == "ok"


def test_display_and_nvlddmkm_resets_collapse_into_one_finding() -> None:
    nv = grp("nvlddmkm", 14, THREE_DAYS, {})
    status, _, findings = judge(section(display(), nv, bugcheck_41("0x116")))
    assert status == "warn"
    assert [f["component"] for f in findings] == ["gpu"]


# -- storage ----------------------------------------------------------------------


def test_recurring_active_internal_disk_retries_are_warn() -> None:
    status, reason, findings = judge(section(disk153()))
    assert status == "warn"
    assert reason == "A disk keeps retrying reads and writes (2 times over 2 days)"
    assert findings[0]["component"] == "storage"


@pytest.mark.parametrize(
    "provider,event_id",
    [("disk", 51), ("storahci", 129), ("stornvme", 129), ("stornvme", 11), ("disk", 7)],
)
def test_other_storage_providers_are_judged_the_same(provider: str, event_id: int) -> None:
    assert judge(section(grp(provider, event_id, ACTIVE)))[0] == "warn"


def test_usb_only_storage_retries_are_ignored() -> None:
    assert judge(section(disk153(bus={"USB": 2})))[0] == "ok"
    assert judge(section(disk153(bus={"SD": 2})))[0] == "ok"
    assert judge(section(disk153(bus={"SD": 1, "USB": 1})))[0] == "ok"
    assert judge(section(disk153(bus={"usb": 2})))[0] == "ok"


def test_mixed_bus_storage_retries_are_judged() -> None:
    ages = [2.0, 26.0, 28.0, 30.0]
    assert judge(section(disk153(ages, bus={"USB": 2, "NVMe": 2})))[0] == "warn"


def test_mostly_usb_with_a_stray_internal_sample_is_not_recurring() -> None:
    ages = [2.0 + 3 * i for i in range(10)]
    assert judge(section(disk153(ages, bus={"USB": 9, "SATA": 1})))[0] == "ok"


def test_unresolved_bus_type_is_judged_as_internal() -> None:
    assert judge(section(disk153(bus={"Unknown": 2})))[0] == "warn"
    assert judge(section(grp("disk", 153, ACTIVE)))[0] == "warn"  # no disk_bus_type at all


def test_single_or_stale_storage_retries_are_not_a_finding() -> None:
    assert judge(section(disk153([2.0])))[0] == "ok"
    assert judge(section(disk153(STALE)))[0] == "ok"


# -- crash diversity --------------------------------------------------------------


def test_crash_diversity_with_a_corrected_cpu_error_is_warn() -> None:
    status, reason, findings = judge(section(whea19(), app_crashes=CRASHES))
    assert status == "warn"
    assert reason.startswith(
        "Programs crash across the board alongside corrected processor errors"
        " — possible RAM or CPU instability"
    )
    assert findings[0]["source"] == "app_crashes" and findings[0]["component"] == "platform"


def test_crash_diversity_with_a_memory_bugcheck_is_warn() -> None:
    status, reason, _ = judge(section(bugcheck_41("0x1a"), app_crashes=CRASHES))
    assert status == "warn" and "memory-related system crashes" in reason


def test_crash_diversity_never_fires_on_diversity_alone() -> None:
    assert judge(section(app_crashes=CRASHES))[0] == "ok"
    assert judge(section(bugcheck_41(None), app_crashes=CRASHES))[0] == "ok"


def test_crash_diversity_needs_more_than_a_cpu_bugcheck_or_stale_support() -> None:
    assert judge(section(bugcheck_41("0x124"), app_crashes=CRASHES))[0] == "ok"
    assert judge(section(whea19(STALE), app_crashes=CRASHES))[0] == "ok"


@pytest.mark.parametrize(
    "crashes",
    [
        {**CRASHES, "distinct_apps": 3},
        {**CRASHES, "exception_codes": {"0xc0000005": 4, "0xc0000409": 20}},  # only 4 hardware-class
        {**CRASHES, "exception_codes": {"0xc0000409": 30}},
        {**CRASHES, "by_day": {"2026-06-02": 17}},  # the storm ended eight days ago
        None,
        [],
    ],
    ids=["apps<4", "codes<5", "wrong-codes", "stale", "null", "list"],
)
def test_crash_diversity_thresholds(crashes: Any) -> None:
    assert judge(section(whea19(), app_crashes=crashes))[0] == "posture"


def test_crash_diversity_exactly_at_the_bars_fires() -> None:
    crashes = {**CRASHES, "distinct_apps": 4, "exception_codes": {"0xC0000005": 3, "0xc000001d": 2}}
    assert judge(section(whea19(), app_crashes=crashes))[0] == "warn"


def test_absent_app_crashes_is_fine() -> None:
    assert judge(section(whea19()))[0] == "posture"


# -- no double alarm with reliability ---------------------------------------------


@pytest.mark.parametrize("code", [None, "0x124", "0x116", "0x1a", "0x7a", "0x0"])
def test_crash_groups_never_escalate_on_their_own(code: str | None) -> None:
    kp = bugcheck_41(code, ages=[2.0, 26.0, 50.0, 74.0])
    bc = grp("BugCheck", 1001, [2.0, 26.0], {"bugcheck_code": {code or "0x0": 2}}, "error")
    assert judge(section(kp, bc))[0] == "ok"


# -- reasons and ordering ---------------------------------------------------------


def test_most_severe_first_and_at_most_three_findings() -> None:
    status, reason, findings = judge(
        section(
            whea19(),  # posture, cpu
            disk153(),  # warn, storage
            whea18(),  # crit, cpu
            grp(WHEA, 47, ACTIVE, {}),  # posture, memory
            grp(WHEA, 17, ACTIVE, {"error_source": {"PCI Express": 2}}),  # posture, pcie
        )
    )
    assert status == "crit"
    assert [f["status"] for f in findings] == ["crit", "warn", "posture", "posture", "posture"][
        : len(findings)
    ]
    parts = reason.split("; ")
    assert len(parts) == 4 and parts[3] == "+2 more"
    assert parts[0].startswith("The processor reported uncorrectable")
    assert parts[1].startswith("A disk keeps retrying")


def test_reasons_never_carry_event_ids_or_codes() -> None:
    payload = section(
        whea18(), whea19(), display(), bugcheck_41("0x116"), disk153(),
        grp("Microsoft-Windows-MemoryDiagnostics-Results", 1202, [5.0]),
        app_crashes=CRASHES,
        edac=[{"ce_count": 2, "ue_count": 1}],
        aer=[{"fatal": 1}],
    )
    _, reason, findings = judge(payload)
    for text in [reason] + [f["symptom"] for f in findings]:
        assert not re.search(r"0x[0-9a-f]+|\b(4101|1202|153|WHEA|Xid|nvlddmkm)\b", text, re.I), text
        assert not re.search(r"\b(17|18|19|41|47|51|129)\b", text), text


def test_details_findings_have_the_documented_keys_and_are_json_safe() -> None:
    payload = section(whea18(), disk153(), app_crashes=CRASHES)
    out = rule(payload, NOW)
    assert out is not None
    findings = out[2]["findings"]
    assert findings
    for f in findings:
        assert set(f) == {
            "component", "severity", "status", "symptom", "source", "event_id",
            "active_days", "last_seen_age_hours",
        }
        assert f["status"] in ("crit", "warn", "posture")
    json.dumps(out[2])


# -- golden fixtures ----------------------------------------------------------------


def _fixture_section(name: str) -> dict[str, Any]:
    frame = json.loads((FIXTURES_DIR / name).read_text())
    return frame["snapshot"]["hardware_errors"]


def _by(findings: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    return {(f["component"], f["source"]): f["status"] for f in findings}


def test_windows_fixture_near_its_timestamps() -> None:
    """Judged a few hours after the fixture's newest event (2026-06-04 11:52Z).

    * disk 153 (4 retries on a SATA disk, 2 days, newest 2026-06-03 08:15) is
      active and recurring on an internal disk: warn.
    * Display 4101 (6 resets on 3 days) has no GPU bugcheck and no hardware Xid
      in the window: uncorroborated, so posture -- the reliability section owns
      any crash.
    * WHEA 19 "Bus/Interconnect" (3 corrected errors) is a standing platform
      fact: posture.
    * app_crashes: 5 apps, 13 hardware-class exceptions, and an active corrected
      platform error corroborates: warn.
    Overall: warn.
    """

    now = datetime(2026, 6, 4, 18, 30, tzinfo=timezone.utc)
    out = rule(_fixture_section("telemetry_snapshot.json"), now)
    assert out is not None
    status, reason, details = out
    assert status == "warn"
    by = _by(details["findings"])
    assert by[("storage", "disk")] == "warn"
    assert by[("gpu", "Display")] == "posture"
    assert by[("platform", WHEA)] == "posture"
    assert by[("platform", "app_crashes")] == "warn"
    assert len(by) == 4
    assert "A disk keeps retrying reads and writes (4 times over 2 days)" in reason
    assert "Programs crash across the board alongside corrected processor interconnect errors" in reason


def test_windows_fixture_goes_quiet_once_it_is_history() -> None:
    now = datetime(2026, 7, 4, 18, 30, tzinfo=timezone.utc)
    out = rule(_fixture_section("telemetry_snapshot.json"), now)
    assert out is not None and out[0] == "ok"


def test_linux_fixture_near_its_timestamps() -> None:
    """The fixture's NVRM Xid 79 ("fell off the bus", a hardware Xid) hit on two
    days, newest 2026-07-29 22:41: active and recurring, so crit on the GPU. EDAC
    corrected-only and AER correctable-only counters are standing posture."""

    now = datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc)
    out = rule(_fixture_section("telemetry_snapshot_linux.json"), now)
    assert out is not None
    status, reason, details = out
    assert status == "crit"
    by = _by(details["findings"])
    assert by[("gpu", "nvrm_xid")] == "crit"
    assert by[("memory", "edac")] == "posture"
    assert by[("pcie", "aer")] == "posture"
    assert reason.startswith("The graphics card reported uncorrectable hardware errors")


def test_linux_fixture_xid_decays_to_posture_but_counters_stand() -> None:
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    out = rule(_fixture_section("telemetry_snapshot_linux.json"), now)
    assert out is not None and out[0] == "posture"
    assert _by(out[2]["findings"])[("gpu", "nvrm_xid")] == "posture"


# -- malformed input ----------------------------------------------------------------

JUNK: list[Any] = [None, "x", 7, 1.5, True, [], [1, "a", None], {}, {"a": [1]}, float("nan"), 10**400]


@pytest.mark.parametrize("junk", JUNK, ids=repr)
def test_junk_at_every_level_never_raises(junk: Any) -> None:
    good = whea18()
    cases = [
        {"groups": junk, "edac": junk, "aer": junk},
        {"groups": [junk], "edac": [junk], "aer": [junk], "app_crashes": junk},
        {"groups": [{**good, "by_day": junk}], "edac": [], "aer": []},
        {"groups": [{**good, "last_seen": junk, "count": junk}]},
        {"groups": [{**good, "details": junk}]},
        {"groups": [{**good, "source": junk, "event_id": junk}]},
        {"groups": [{"source": "disk", "event_id": 153, "details": {"disk_bus_type": junk}, "count": 5,
                     "by_day": {"2026-06-10": 5}, "last_seen": "2026-06-10T11:00:00Z"}]},
        {"groups": [], "app_crashes": {"distinct_apps": junk, "exception_codes": junk, "by_day": junk},
         "window_days": junk, "effective_window_days": junk},
        {"groups": [whea19()], "app_crashes": {"distinct_apps": 9, "exception_codes": {junk if isinstance(junk, (str, int)) else "k": junk}}},
        {"edac": [{"ce_count": junk, "ue_count": junk}], "aer": [{"fatal": junk, "nonfatal": junk, "correctable": junk}]},
    ]
    for payload in cases:
        out = rule(payload, NOW)
        assert out is not None
        assert out[0] in ("ok", "posture", "warn", "crit")
        health_rules.evaluate_section("hardware_errors", payload, now=NOW)


def test_hostile_values_do_not_become_findings() -> None:
    assert judge({"groups": "not a list", "edac": "x", "aer": 3})[0] == "ok"
    bad = [{"source": "Display"}, {"event_id": 4101}, {}, "str", 5]
    assert judge({"groups": bad})[0] == "ok"

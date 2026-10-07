"""``hardware_metrics.extract`` / ``device_labels``: one day of snapshots to rows."""

from __future__ import annotations

import json
from pathlib import Path

from support.hardware import WHEA, disk, disk_smart, fan, fans_section, gpu, gpu_section
from support.hardware import group, hardware_errors, snapshot

from kenny_server import hardware_metrics as hm

DAY = "2026-07-01"
FIXTURES = Path(__file__).resolve().parents[2] / "docs" / "fixtures"


def rows(snaps, day=DAY):
    return {(key, metric): value for key, metric, value in hm.extract(snaps, day)}


# -- disks ------------------------------------------------------------------------


def test_a_disk_yields_the_last_non_null_value_of_the_day() -> None:
    morning = snapshot(disk_smart=disk_smart(disk(pct=10, spare=100, media_errors=0, unsafe=3)))
    evening = snapshot(
        disk_smart=disk_smart(
            disk(pct=11, spare=99, media_errors=2, unsafe=4, read_unc=1, write_unc=2,
                 smart={"5": 7, "187": 1, "197": 2, "198": 3, "199": 9})
        )
    )
    got = rows([morning, evening])
    key = "disk:SN-1"
    assert got[(key, "percentage_used")] == 11.0
    assert got[(key, "available_spare")] == 99.0
    assert got[(key, "available_spare_threshold")] == 10.0
    assert got[(key, "media_errors")] == 2.0
    assert got[(key, "unsafe_shutdowns")] == 4.0
    assert got[(key, "read_errors_uncorrected")] == 1.0
    assert got[(key, "write_errors_uncorrected")] == 2.0
    assert [got[(key, f"smart_{n}")] for n in (5, 187, 197, 198)] == [7.0, 1.0, 2.0, 3.0]
    assert (key, "smart_199") not in got  # only the documented four


def test_a_null_later_in_the_day_does_not_erase_an_earlier_value() -> None:
    first = snapshot(disk_smart=disk_smart(disk(pct=12, spare=98)))
    later = snapshot(disk_smart=disk_smart(disk(pct=None, spare=None)))
    got = rows([first, later])
    assert got[("disk:SN-1", "percentage_used")] == 12.0
    assert got[("disk:SN-1", "available_spare")] == 98.0


def test_wear_stands_in_for_percentage_used_without_an_nvme_log() -> None:
    got = rows([snapshot(disk_smart=disk_smart(disk(bus_type="SATA", wear=7)))])
    assert got[("disk:SN-1", "percentage_used")] == 7.0
    assert ("disk:SN-1", "available_spare") not in got


def test_disks_without_a_serial_removable_or_usb_are_not_tracked() -> None:
    snap = snapshot(
        disk_smart=disk_smart(
            disk(serial=None, pct=1),
            disk(serial="  ", pct=1),
            disk(serial="STICK", pct=1, removable=True),
            disk(serial="BRIDGE", pct=1, bus_type="USB"),
            disk(serial="OK", pct=1),
        )
    )
    assert {k for k, _ in rows([snap])} == {"disk:OK"}


def test_a_paused_disk_contributes_nothing_but_other_snapshots_still_count() -> None:
    paused = snapshot(disk_smart=disk_smart(disk(pct=None, spare=None, paused=True, read_unc=0)))
    live = snapshot(disk_smart=disk_smart(disk(pct=20)))
    assert rows([paused]) == {}
    assert rows([live, paused])[("disk:SN-1", "percentage_used")] == 20.0


def test_a_replaced_disk_starts_its_own_series() -> None:
    snap = snapshot(disk_smart=disk_smart(disk("OLD", pct=90), disk("NEW", pct=1)))
    got = rows([snap])
    assert got[("disk:OLD", "percentage_used")] == 90.0
    assert got[("disk:NEW", "percentage_used")] == 1.0


# -- gpus -------------------------------------------------------------------------


def test_gpu_loaded_width_is_the_widest_link_seen_at_load() -> None:
    snaps = [
        snapshot(gpu=gpu_section(gpu(util=5, width=1))),  # idle downshift: ignored
        snapshot(gpu=gpu_section(gpu(util=60, width=8))),
        snapshot(gpu=gpu_section(gpu(util=90, width=16))),
        snapshot(gpu=gpu_section(gpu(util=40, width=8))),
    ]
    got = rows(snaps)
    assert got[("gpu:GPU-1", "pcie_width_loaded_max")] == 16.0
    assert got[("gpu:GPU-1", "pcie_width_max")] == 16.0
    assert got[("gpu:GPU-1", "hw_slowdown_seen")] == 0.0


def test_gpu_with_no_loaded_sample_has_no_loaded_width() -> None:
    got = rows([snapshot(gpu=gpu_section(gpu(util=2, width=1)))])
    assert ("gpu:GPU-1", "pcie_width_loaded_max") not in got
    assert got[("gpu:GPU-1", "pcie_width_max")] == 16.0


def test_gpu_hardware_slowdown_or_power_brake_is_seen_if_any_snapshot_had_it() -> None:
    clean = snapshot(gpu=gpu_section(gpu()))
    braked = snapshot(gpu=gpu_section(gpu(power_brake=True)))
    assert rows([clean, braked])[("gpu:GPU-1", "hw_slowdown_seen")] == 1.0
    assert rows([braked, clean])[("gpu:GPU-1", "hw_slowdown_seen")] == 1.0
    slowed = snapshot(gpu=gpu_section(gpu(slowdown=True)))
    assert rows([slowed])[("gpu:GPU-1", "hw_slowdown_seen")] == 1.0


def test_gpu_slowdown_is_unknown_when_the_driver_reports_no_reasons() -> None:
    got = rows([snapshot(gpu=gpu_section(gpu(slowdown=None, power_brake=None)))])
    assert ("gpu:GPU-1", "hw_slowdown_seen") not in got


def test_gpu_identity_falls_back_from_uuid_to_bus_id_to_pci_id() -> None:
    snap = snapshot(
        gpu=gpu_section(
            gpu(uuid=None, bus_id="0000:02:00.0"),
            gpu(uuid=None, bus_id=None, pci_id="1002:744c"),
            gpu(uuid=None, bus_id=None, pci_id=None),
        )
    )
    assert {k for k, _ in rows([snap])} == {"gpu:0000:02:00.0", "gpu:1002:744c"}


# -- fans -------------------------------------------------------------------------


def test_fan_band_medians_use_all_nonzero_samples_taken_in_the_band() -> None:
    snaps = [
        snapshot(fans=fans_section(fan(samples=[1000, 1010, 990, 1000, 0], duty=40))),
        snapshot(fans=fans_section(fan(samples=[1100, 1100, 1100, 1100, 1100], duty=45))),
        snapshot(fans=fans_section(fan(samples=[2000, 2010, 1990, 2000, 2000], duty=60))),
    ]
    got = rows(snaps)
    key = "fan:nct6798.fan1"
    # eight non-zero samples at 40-45 %: 990 1000 1000 1010 | 1100 x5 -> median 1100
    assert got[(key, "rpm_duty_30_50")] == 1100.0
    assert got[(key, "rpm_duty_50_70")] == 2000.0
    assert (key, "rpm_duty_70_90") not in got


def test_a_band_needs_three_samples() -> None:
    got = rows([snapshot(fans=fans_section(fan(samples=[1500, 1500, 0, 0, 0], duty=80)))])
    assert ("fan:nct6798.fan1", "rpm_duty_70_90") not in got


def test_band_edges_belong_to_the_upper_band_and_100_is_inclusive() -> None:
    def band_of(duty):
        got = rows([snapshot(fans=fans_section(fan(samples=[900] * 5, duty=duty)))])
        return sorted(m for (_, m) in got if m.startswith("rpm_duty"))

    assert band_of(29.9) == []
    assert band_of(30) == ["rpm_duty_30_50"]
    assert band_of(50) == ["rpm_duty_50_70"]
    assert band_of(70) == ["rpm_duty_70_90"]
    assert band_of(90) == ["rpm_duty_90_100"]
    assert band_of(100) == ["rpm_duty_90_100"]
    assert band_of(None) == []


def test_fan_stall_needs_an_all_zero_burst_while_driven() -> None:
    key = "fan:nct6798.fan1"
    stalled = rows([snapshot(fans=fans_section(fan(samples=[0] * 5, duty=45)))])
    assert stalled[(key, "stall_seen")] == 1.0
    zero_rpm_mode = rows([snapshot(fans=fans_section(fan(samples=[0] * 5, duty=0)))])
    assert zero_rpm_mode[(key, "stall_seen")] == 0.0
    unreadable = rows([snapshot(fans=fans_section(fan(samples=[0] * 5, duty=None)))])
    assert unreadable[(key, "stall_seen")] == 0.0
    healthy = rows([snapshot(fans=fans_section(fan(duty=45)))])
    assert healthy[(key, "stall_seen")] == 0.0
    # one stalled burst in the day is enough
    both = rows(
        [
            snapshot(fans=fans_section(fan(duty=45))),
            snapshot(fans=fans_section(fan(samples=[0] * 5, duty=45))),
        ]
    )
    assert both[(key, "stall_seen")] == 1.0


def test_fan_jitter_is_the_worst_per_snapshot_coefficient_of_variation() -> None:
    steady = snapshot(fans=fans_section(fan(samples=[1000] * 5)))
    jittery = snapshot(fans=fans_section(fan(samples=[800, 1200, 800, 1200, 1000])))
    got = rows([steady, jittery, steady])
    assert abs(got[("fan:nct6798.fan1", "rpm_cv_max")] - 0.1789) < 1e-3
    assert rows([steady])[("fan:nct6798.fan1", "rpm_cv_max")] == 0.0


def test_idle_or_absent_fans_and_garbage_samples_are_skipped() -> None:
    snap = snapshot(
        fans=fans_section(
            fan("a", [0] * 5, None, idle=True),
            fan("b", ["x", None, True, float("nan"), -5], 40),
            fan("c", None, 40),
        )
    )
    snap["fans"]["fans"][2]["rpm_samples"] = "not a list"
    assert {k for k, _ in rows([snap])} == set()


# -- component counts -------------------------------------------------------------


def test_component_counts_come_from_the_latest_snapshots_by_day() -> None:
    early = snapshot(
        hardware_errors=hardware_errors(group(WHEA, 17, {DAY: 1, "2026-06-30": 5}))
    )
    late = snapshot(
        hardware_errors=hardware_errors(
            group(WHEA, 17, {DAY: 4, "2026-06-30": 5}),  # pcie, corrected
            group(WHEA, 18, {DAY: 1}, level="error"),  # cpu, fatal
            group("Display", 4101, {DAY: 2, "2026-06-29": 9}),  # gpu, instability
            group("Display", 4101, {"2026-06-29": 9}),  # nothing today
        )
    )
    got = rows([early, late])
    assert got[("host:pcie", "corrected_events")] == 4.0  # not 1 + 4
    assert got[("host:cpu", "fatal_events")] == 1.0
    assert got[("host:gpu", "instability_events")] == 2.0


def test_a_component_with_groups_but_nothing_today_reads_zero() -> None:
    snap = snapshot(hardware_errors=hardware_errors(group(WHEA, 17, {"2026-06-25": 3})))
    assert rows([snap])[("host:pcie", "corrected_events")] == 0.0


def test_supporting_and_unattributed_groups_are_not_counted() -> None:
    snap = snapshot(
        hardware_errors=hardware_errors(
            group("Microsoft-Windows-Kernel-Power", 41, {DAY: 3}, level="critical"),
            group("SomethingElse", 7, {DAY: 3}),
        )
    )
    assert rows([snap]) == {}


def test_edac_and_aer_totals_are_the_latest_cumulative_values() -> None:
    first = snapshot(
        hardware_errors=hardware_errors(
            edac=[{"controller": "mc0", "ce_count": 1, "ue_count": 0}],
            aer=[{"device": "0000:01:00.0", "correctable": 2, "nonfatal": 0, "fatal": 0}],
            sources=["journal", "edac", "aer"],
        )
    )
    last = snapshot(
        hardware_errors=hardware_errors(
            edac=[
                {"controller": "mc0", "ce_count": 3, "ue_count": 1},
                {"controller": "mc1", "ce_count": 4, "ue_count": 0},
            ],
            aer=[
                {"device": "0000:01:00.0", "correctable": 12, "nonfatal": 1, "fatal": 2},
                {"device": "0000:02:00.0", "correctable": 1, "nonfatal": 0, "fatal": 0},
            ],
            sources=["journal", "edac", "aer"],
        )
    )
    got = rows([first, last])
    assert got[("host:memory", "edac_ce")] == 7.0
    assert got[("host:memory", "edac_ue")] == 1.0
    assert got[("host:pcie", "aer_correctable")] == 13.0
    assert got[("host:pcie", "aer_uncorrected")] == 3.0


def test_an_empty_edac_or_aer_list_is_zero_only_when_the_agent_read_that_source() -> None:
    read = snapshot(hardware_errors=hardware_errors(sources=["journal", "edac", "aer"]))
    got = rows([read])
    assert got[("host:memory", "edac_ce")] == 0.0
    assert got[("host:pcie", "aer_uncorrected")] == 0.0
    windows = snapshot(hardware_errors=hardware_errors(sources=["event_log"]))
    assert rows([windows]) == {}


def test_the_day_is_read_from_the_newest_snapshot_when_not_given() -> None:
    snap = snapshot(hardware_errors=hardware_errors(group(WHEA, 18, {DAY: 2}, level="error")))
    snap["collected_at"] = f"{DAY}T23:00:00Z"
    got = {(k, m): v for k, m, v in hm.extract([snap])}
    assert got[("host:cpu", "fatal_events")] == 2.0
    no_stamp = snapshot(hardware_errors=hardware_errors(group(WHEA, 18, {DAY: 2}, level="error")))
    assert hm.extract([no_stamp]) == []  # no day, no per-day counts


# -- robustness -------------------------------------------------------------------


def test_malformed_input_never_raises_and_loses_only_its_own_rows() -> None:
    junk = [
        None,
        "x",
        42,
        {},
        {"disk_smart": None, "gpu": [], "fans": "x", "hardware_errors": 3},
        {"disk_smart": {"disks": "x"}, "gpu": {"gpus": [1, None, {"uuid": 5}]}},
        {"fans": {"fans": [{"key": None}, {"key": "k", "rpm_samples": [1] * 500, "duty_percent": "x"}]}},
        {"hardware_errors": {"groups": [None, {"source": [], "event_id": {}, "by_day": 5}], "edac": [5, {}],
                             "aer": "x", "sources": 7}},
        {"disk_smart": {"disks": [{"serial": "S", "nvme": "x", "smart_attributes": [1],
                                    "wear": float("inf"), "read_errors_uncorrected": True}]}},
    ]
    out = hm.extract(junk, DAY)
    assert isinstance(out, list)
    assert hm.extract("not a list") == []  # type: ignore[arg-type]
    assert hm.extract([], DAY) == []
    assert hm.device_labels(None) == {}
    assert hm.device_labels({"disk_smart": 7, "gpu": {"gpus": "x"}}) == {}
    # the good disk row of a half-broken snapshot is still read
    mixed = [{"disk_smart": {"disks": [disk(pct=3), "junk", None]}, "fans": 5}]
    assert rows(mixed)[("disk:SN-1", "percentage_used")] == 3.0


def test_huge_and_non_finite_values_are_dropped() -> None:
    snap = snapshot(
        disk_smart=disk_smart(disk(pct=10**400, spare=float("nan"), media_errors=float("inf")))
    )
    assert rows([snap]) == {
        ("disk:SN-1", "read_errors_uncorrected"): 0.0,
        ("disk:SN-1", "unsafe_shutdowns"): 0.0,
        ("disk:SN-1", "write_errors_uncorrected"): 0.0,
        ("disk:SN-1", "available_spare_threshold"): 10.0,
    }


# -- labels -----------------------------------------------------------------------


def test_device_labels_name_every_kind() -> None:
    snap = snapshot(
        disk_smart=disk_smart(disk("S1", "WD_BLACK SN850X 2000GB"), disk("USB1", bus_type="USB")),
        gpu=gpu_section(gpu("GPU-9", "RTX 4080")),
        fans=fans_section(fan("nct.fan1", label="CPU_FAN"), fan("nct.fan2", label=None)),
        hardware_errors=hardware_errors(
            group(WHEA, 17, {DAY: 1}),
            group(WHEA, 18, {DAY: 1}, level="error"),
            edac=[{"controller": "mc0", "ce_count": 1, "ue_count": 0}],
        ),
    )
    assert hm.device_labels(snap) == {
        "disk:S1": ("disk", "WD_BLACK SN850X 2000GB"),
        "gpu:GPU-9": ("gpu", "RTX 4080"),
        "fan:nct.fan1": ("fan", "CPU_FAN"),
        "fan:nct.fan2": ("fan", "nct.fan2"),
        "host:pcie": ("component", "PCIe"),
        "host:cpu": ("component", "Processor"),
        "host:memory": ("component", "Memory"),
    }


def test_kind_and_fallback_label_work_from_the_key_alone() -> None:
    assert [hm.kind_of(k) for k in ("disk:a", "gpu:b", "fan:c", "host:memory", "weird")] == [
        "disk", "gpu", "fan", "component", "component",
    ]
    assert hm.fallback_label("host:memory") == "Memory"
    assert hm.fallback_label("host:cpu") == "Processor"
    assert hm.fallback_label("disk:SN1") == "SN1"


# -- against the contract fixtures ---------------------------------------------------


def test_the_contract_fixtures_yield_the_documented_metrics() -> None:
    for name in ("telemetry_snapshot.json", "telemetry_snapshot_linux.json"):
        frame = json.loads((FIXTURES / name).read_text())
        out = hm.extract([frame["snapshot"]], frame["collected_at"][:10])
        assert out, name
        for key, metric, value in out:
            assert key.split(":")[0] in {"disk", "gpu", "fan", "host"}
            assert isinstance(metric, str) and isinstance(value, float)
        labels = hm.device_labels(frame["snapshot"])
        assert labels, name
        # every device the rows name is one a reader can label
        assert {k for k, _, _ in out if k.startswith(("disk:", "gpu:", "fan:"))} <= set(labels)

"""The server's closed hardware attribution tables (hardware_catalog.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kenny_server import hardware_catalog as hc

VECTORS = Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "vectors"


# -- the seam with the agent's query set --------------------------------------


def test_event_query_equals_the_contract_vector() -> None:
    """The literal is a copy of the contract file; any drift fails here."""

    assert hc.EVENT_QUERY == json.loads((VECTORS / "hardware_event_query.json").read_text())


def test_every_attributed_event_is_queried() -> None:
    """An event the server attributes must be an event the agent queries."""

    assert hc.ATTRIBUTED_EVENTS <= hc.queried_events()
    assert hc.ATTRIBUTED_LINUX_KEYS <= hc.queried_linux_keys()


def test_every_queried_event_is_attributed_except_the_crash_aggregate() -> None:
    """The other direction: a queried event nobody attributes is wasted agent work."""

    unattributed = hc.queried_events() - hc.ATTRIBUTED_EVENTS
    assert unattributed == {("application error", 1000)}
    assert hc.queried_linux_keys() == hc.ATTRIBUTED_LINUX_KEYS


def test_every_attributed_event_resolves_to_a_component_or_a_documented_none() -> None:
    """Each attributed event yields a component for at least one plausible detail."""

    for provider, eid in sorted(hc.ATTRIBUTED_EVENTS):
        if provider == "kernel-power":
            details = None
        elif provider in ("bugcheck", "wer-systemerrorreporting"):
            details = {"bugcheck_code": {"0x124": 1}}
        else:
            details = {}
        assert hc.component_for(provider, eid, details) in hc.COMPONENTS, (provider, eid)
        assert hc.severity_for(provider, eid, details) in hc.SEVERITIES, (provider, eid)


def test_linux_keys_all_have_a_component_and_severity() -> None:
    for key in sorted(hc.queried_linux_keys()):
        details = {"xid": {"79": 1}} if key == "nvrm_xid" else None
        assert hc.component_for(key, 0, details) in hc.COMPONENTS
        assert hc.severity_for(key, 0, details) in hc.SEVERITIES


# -- provider and event id normalisation --------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Microsoft-Windows-WHEA-Logger", "WHEA-Logger", "microsoft-windows-whea-logger", " WHEA-Logger "],
)
def test_provider_spellings_are_one_provider(name: str) -> None:
    assert hc.component_for(name, 47) == "memory"


@pytest.mark.parametrize("eid", [47, "47", 47.0])
def test_event_id_spellings(eid: object) -> None:
    assert hc.component_for("Microsoft-Windows-WHEA-Logger", eid) == "memory"


@pytest.mark.parametrize("source", [None, 3, "", "SomethingElse", ["x"]])
def test_unknown_or_malformed_sources_attribute_nothing(source: object) -> None:
    assert hc.component_for(source, 17) is None
    assert hc.severity_for(source, 17) is None


@pytest.mark.parametrize("eid", [None, "x", True, [], {}, 99999])
def test_unknown_or_malformed_event_ids_attribute_nothing(eid: object) -> None:
    assert hc.component_for("Microsoft-Windows-WHEA-Logger", eid) is None
    assert hc.severity_for("Microsoft-Windows-WHEA-Logger", eid) is None


# -- WHEA ---------------------------------------------------------------------

WHEA = "Microsoft-Windows-WHEA-Logger"


def _d(**keys: dict[str, int]) -> dict[str, dict[str, int]]:
    return dict(keys)


def test_whea_17_is_pcie_corrected() -> None:
    assert hc.component_for(WHEA, 17) == "pcie"
    assert hc.severity_for(WHEA, 17) == hc.CORRECTED


def test_whea_18_is_fatal_cpu_by_default() -> None:
    assert hc.component_for(WHEA, 18) == "cpu"
    assert hc.severity_for(WHEA, 18) == hc.FATAL
    cache = _d(error_type={"Cache Hierarchy Error": 2})
    assert hc.component_for(WHEA, 18, cache) == "cpu"


def test_whea_18_bus_interconnect_is_platform() -> None:
    bus = _d(error_type={"Bus/Interconnect Error": 1})
    assert hc.component_for(WHEA, 18, bus) == "platform"
    assert hc.severity_for(WHEA, 18, bus) == hc.FATAL


def test_whea_19_is_cpu_but_bus_interconnect_is_platform() -> None:
    assert hc.component_for(WHEA, 19) == "cpu"
    assert hc.component_for(WHEA, 19, _d(error_type={"TLB Error": 3})) == "cpu"
    bus = _d(error_source={"Corrected Machine Check": 3}, error_type={"Bus/Interconnect Error": 3})
    assert hc.component_for(WHEA, 19, bus) == "platform"
    assert hc.severity_for(WHEA, 19, bus) == hc.CORRECTED


def test_whea_19_mixed_types_follow_the_majority() -> None:
    mostly_bus = _d(error_type={"Bus/Interconnect Error": 4, "TLB Error": 1})
    mostly_core = _d(error_type={"Bus/Interconnect Error": 1, "TLB Error": 4})
    even = _d(error_type={"Bus/Interconnect Error": 2, "TLB Error": 2})
    assert hc.component_for(WHEA, 19, mostly_bus) == "platform"
    assert hc.component_for(WHEA, 19, mostly_core) == "cpu"
    assert hc.component_for(WHEA, 19, even) == "cpu"


def test_whea_pci_express_source_is_pcie_whatever_the_id() -> None:
    pcie = _d(error_source={"PCI Express Root Port AER": 2})
    for eid in (17, 18, 19):
        assert hc.component_for(WHEA, eid, pcie) == "pcie"
    assert hc.severity_for(WHEA, 18, pcie) == hc.FATAL


def test_whea_47_is_memory_even_with_a_cpu_looking_type() -> None:
    assert hc.component_for(WHEA, 47, _d(error_type={"Bus/Interconnect Error": 1})) == "memory"
    assert hc.severity_for(WHEA, 47) == hc.CORRECTED


@pytest.mark.parametrize(
    "details",
    [None, "x", 5, [], {"error_type": "oops"}, {"error_type": ["Bus/Interconnect Error"]},
     {"error_type": {"Bus": "many"}}, {"error_type": {1: None}}],
)
def test_whea_malformed_details_never_crash(details: object) -> None:
    assert hc.component_for(WHEA, 19, details) in ("cpu", "platform")


# -- the rest of the Windows events -------------------------------------------


def test_gpu_events() -> None:
    assert hc.component_for("Display", 4101) == "gpu"
    assert hc.severity_for("Display", 4101) == hc.INSTABILITY
    for eid in (13, 14, 153):
        assert hc.component_for("nvlddmkm", eid) == "gpu"
        assert hc.severity_for("nvlddmkm", eid) == hc.INSTABILITY


def test_nvlddmkm_with_xid_details_is_judged_by_the_xid() -> None:
    hardware = _d(xid={"79": 2})
    software = _d(xid={"13": 3})
    mixed = _d(xid={"13": 3, "79": 1})
    assert hc.component_for("nvlddmkm", 14, hardware) == "gpu"
    assert hc.severity_for("nvlddmkm", 14, hardware) == hc.FATAL
    assert hc.component_for("nvlddmkm", 13, software) is None
    assert hc.severity_for("nvlddmkm", 13, software) is None
    assert hc.component_for("nvlddmkm", 14, mixed) == "gpu"
    assert hc.hardware_xid_count(mixed) == 1


@pytest.mark.parametrize(
    "source, eid",
    [("disk", 7), ("disk", 11), ("disk", 51), ("disk", 153), ("storahci", 129),
     ("stornvme", 11), ("stornvme", 129)],
)
def test_storage_events_are_storage_instability(source: str, eid: int) -> None:
    assert hc.component_for(source, eid) == "storage"
    assert hc.severity_for(source, eid) == hc.INSTABILITY


def test_storage_event_ids_are_per_provider() -> None:
    assert hc.component_for("storahci", 11) is None  # only stornvme has 11
    assert hc.component_for("disk", 129) is None


def test_memory_diagnostics_1202_is_fatal_memory() -> None:
    provider = "Microsoft-Windows-MemoryDiagnostics-Results"
    assert hc.component_for(provider, 1202) == "memory"
    assert hc.severity_for(provider, 1202) == hc.FATAL


def test_application_error_names_no_component() -> None:
    assert hc.component_for("Application Error", 1000) is None
    assert hc.severity_for("Application Error", 1000) is None


# -- bugchecks and power loss -------------------------------------------------

KP = "Microsoft-Windows-Kernel-Power"


def test_bugcheck_codes_normalise_case_and_padding() -> None:
    for spelling in ("0x124", "0X124", "0x00000124", " 0x0124 ", 0x124):
        assert hc.normalize_bugcheck(spelling) == "0x124"
    assert hc.bugcheck_component("0x00000124") == hc.bugcheck_component("0x124") == ("cpu", "strong")
    assert hc.bugcheck_component("0x0000009C") == ("cpu", "strong")
    assert hc.normalize_bugcheck("0x0000000A") == "0xa"


@pytest.mark.parametrize("bad", [None, True, "", "0x", "124", "0xzz", "0x-1", -4, 1.5, [], {}])
def test_bugcheck_normalisation_rejects_garbage(bad: object) -> None:
    assert hc.normalize_bugcheck(bad) is None
    assert hc.bugcheck_component(bad) is None


def test_bugcheck_table_is_canonical_and_complete() -> None:
    assert all(hc.normalize_bugcheck(k) == k for k in hc.BUGCHECK_COMPONENTS)
    assert all(c in hc.COMPONENTS and s in ("strong", "supporting") for c, s in hc.BUGCHECK_COMPONENTS.values())
    assert {k: v for k, v in hc.BUGCHECK_COMPONENTS.items() if v[1] == "strong"} == {
        "0x124": ("cpu", "strong"), "0x9c": ("cpu", "strong"), "0x101": ("cpu", "strong"),
        "0x116": ("gpu", "strong"), "0x119": ("gpu", "strong"),
        "0x7a": ("storage", "strong"), "0x77": ("storage", "strong"),
    }
    assert {k for k, v in hc.BUGCHECK_COMPONENTS.items() if v[1] == "supporting"} == {
        "0x1a", "0x50", "0x3b", "0xa",
    }
    assert all(hc.BUGCHECK_COMPONENTS[k][0] == "memory" for k in ("0x1a", "0x50", "0x3b", "0xa"))


def test_0x117_is_a_recovered_live_dump_and_attributes_nothing() -> None:
    assert "0x117" in hc.BUGCHECK_IGNORED
    assert hc.bugcheck_component("0x117") is None
    assert hc.component_for("BugCheck", 1001, _d(bugcheck_code={"0x00000117": 1})) is None
    assert hc.severity_for("BugCheck", 1001, _d(bugcheck_code={"0x00000117": 1})) is None


@pytest.mark.parametrize("source", ["BugCheck", "Microsoft-Windows-WER-SystemErrorReporting"])
def test_bugcheck_1001_is_attributed_by_code(source: str) -> None:
    cpu = _d(bugcheck_code={"0x00000124": 1})
    mem = _d(bugcheck_code={"0x0000001a": 1})
    assert hc.component_for(source, 1001, cpu) == "cpu"
    assert hc.component_for(source, 1001, mem) == "memory"
    assert hc.severity_for(source, 1001, cpu) == hc.SUPPORTING
    assert hc.component_for(source, 1001) is None
    assert hc.component_for(source, 1001, _d(bugcheck_code={"0x000000d1": 1})) is None


def test_kernel_power_41_is_power_unless_a_bugcheck_attributes_it() -> None:
    assert hc.component_for(KP, 41) == "power"
    assert hc.severity_for(KP, 41) == hc.SUPPORTING
    assert hc.component_for(KP, 41, _d(bugcheck_code={"0x0": 1})) == "power"
    assert hc.component_for(KP, 41, _d(bugcheck_code={"0x00000000": 1})) == "power"
    assert hc.component_for(KP, 41, _d(bugcheck_code={"0x00000124": 1})) == "cpu"
    assert hc.component_for(KP, 41, _d(bugcheck_code={"0x00000116": 1})) == "gpu"
    assert hc.component_for(KP, 41, _d(bugcheck_code={"0x0000001a": 1})) == "memory"
    assert hc.severity_for(KP, 41, _d(bugcheck_code={"0x00000124": 1})) == hc.SUPPORTING


def test_kernel_power_41_with_an_unmapped_bugcheck_is_not_a_power_loss() -> None:
    driver_bug = _d(bugcheck_code={"0x000000d1": 2})
    assert hc.component_for(KP, 41, driver_bug) is None
    assert hc.severity_for(KP, 41, driver_bug) is None
    mixed = _d(bugcheck_code={"0x000000d1": 2, "0x0": 1})
    assert hc.component_for(KP, 41, mixed) == "power"


def test_mixed_bugchecks_pick_the_most_frequent_then_the_strongest() -> None:
    assert hc.bugcheck_attribution(_d(bugcheck_code={"0x124": 1, "0x1a": 3})) == ("memory", "supporting")
    assert hc.bugcheck_attribution(_d(bugcheck_code={"0x124": 2, "0x1a": 2})) == ("cpu", "strong")
    assert hc.bugcheck_attribution(None) is None


# -- Xids and the crash heuristic ---------------------------------------------


def test_hardware_xid_set() -> None:
    assert hc.HARDWARE_XIDS == {48, 63, 64, 79, 92, 93, 94, 95, 119, 120}
    for xid in (13, 31, 43, 45):
        assert xid not in hc.HARDWARE_XIDS
    assert hc.is_hardware_xid("79") and hc.is_hardware_xid(48)
    assert not hc.is_hardware_xid("13") and not hc.is_hardware_xid(None) and not hc.is_hardware_xid(True)


def test_linux_nvrm_xid_attributes_only_hardware_xids() -> None:
    assert hc.component_for("nvrm_xid", 0, _d(xid={"79": 2})) == "gpu"
    assert hc.severity_for("nvrm_xid", 0, _d(xid={"79": 2})) == hc.FATAL
    assert hc.component_for("nvrm_xid", 0, _d(xid={"13": 2})) is None
    assert hc.severity_for("nvrm_xid", 0, _d(xid={"31": 1})) is None
    assert hc.component_for("nvrm_xid", 0, None) is None


def test_crash_exception_codes() -> None:
    assert hc.CRASH_EXCEPTION_CODES == {"0xc0000005", "0xc000001d"}


# -- Linux --------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, component",
    [("mce", "cpu"), ("edac", "memory"), ("amdgpu_ras", "gpu"), ("block_io", "storage"),
     ("nvme", "storage"), ("ata", "storage"), ("pcie_aer", "pcie")],
)
def test_linux_keys(key: str, component: str) -> None:
    assert hc.component_for(key, 0) == component
    assert hc.severity_for(key, 0) in hc.SEVERITIES


def test_linux_journal_edac_and_aer_cannot_say_corrected_so_they_only_support() -> None:
    assert hc.severity_for("edac", 0) == hc.SUPPORTING
    assert hc.severity_for("pcie_aer", 0) == hc.SUPPORTING


def test_edac_entries() -> None:
    assert hc.edac_severity({"controller": "mc0", "ce_count": 3, "ue_count": 0}) == hc.CORRECTED
    assert hc.edac_severity({"controller": "mc0", "ce_count": 3, "ue_count": 1}) == hc.FATAL
    assert hc.edac_severity({"controller": "mc0", "ce_count": 0, "ue_count": 0}) is None
    for bad in (None, "x", [], {"ce_count": "many", "ue_count": True}, {"ue_count": float("inf")}):
        assert hc.edac_severity(bad) is None


def test_aer_entries() -> None:
    assert hc.aer_severity({"device": "0000:01:00.0", "correctable": 12, "nonfatal": 0, "fatal": 0}) == hc.CORRECTED
    assert hc.aer_severity({"correctable": 12, "nonfatal": 1, "fatal": 0}) == hc.FATAL
    assert hc.aer_severity({"correctable": 0, "nonfatal": 0, "fatal": 2}) == hc.FATAL
    assert hc.aer_severity({"correctable": 0}) is None
    assert hc.aer_severity("x") is None


def test_a_windows_provider_is_not_mistaken_for_a_linux_key() -> None:
    # Linux keys only apply with event id 0.
    assert hc.component_for("nvme", 129) is None


# -- Raptor Lake microcode ----------------------------------------------------


def _cpu(**overrides: object) -> dict:
    cpu: dict = {
        "vendor": "GenuineIntel",
        "brand": "13th Gen Intel(R) Core(TM) i7-13700K",
        "family": 6, "model": 183, "stepping": 1,
        "microcode": "0x123", "microcode_bios": None,
    }
    cpu.update(overrides)
    return cpu


def test_affected_cpu_on_old_microcode_needs_the_update() -> None:
    assert hc.raptor_lake_needs_microcode(_cpu())
    assert hc.raptor_lake_needs_microcode(_cpu(microcode="0x129"))
    assert hc.raptor_lake_needs_microcode(_cpu(microcode="0X12A"))
    assert hc.raptor_lake_needs_microcode(_cpu(microcode=0x112))


def test_fixed_microcode_is_not_a_finding() -> None:
    assert not hc.raptor_lake_needs_microcode(_cpu(microcode="0x12b"))
    assert not hc.raptor_lake_needs_microcode(_cpu(microcode="0x12B"))
    assert not hc.raptor_lake_needs_microcode(_cpu(microcode="0x130"))
    assert not hc.raptor_lake_needs_microcode(_cpu(microcode=0x12B))


@pytest.mark.parametrize("microcode", [None, "", "unknown", "0xzz", True, [], 1.5, -1])
def test_unknown_microcode_is_not_a_finding(microcode: object) -> None:
    assert not hc.raptor_lake_needs_microcode(_cpu(microcode=microcode))


@pytest.mark.parametrize(
    "brand",
    [
        "13th Gen Intel(R) Core(TM) i5-13600K",
        "13th Gen Intel(R) Core(TM) i5-13600KF",
        "13th Gen Intel(R) Core(TM) i5-13500",
        "13th Gen Intel(R) Core(TM) i7-13700",
        "13th Gen Intel(R) Core(TM) i7-13700KF",
        "13th Gen Intel(R) Core(TM) i7-13790F",
        "13th Gen Intel(R) Core(TM) i9-13900K",
        "13th Gen Intel(R) Core(TM) i9-13900KS",
        "13th Gen Intel(R) Core(TM) i9-13900F",
        "14th Gen Intel(R) Core(TM) i5-14600K",
        "14th Gen Intel(R) Core(TM) i5-14500",
        "14th Gen Intel(R) Core(TM) i7-14700K",
        "14th Gen Intel(R) Core(TM) i9-14900KS",
        "Intel(R) Core(TM) i9-14900",
    ],
)
def test_desktop_65w_plus_brands_are_affected(brand: str) -> None:
    for model in (183, 191):
        assert hc.raptor_lake_needs_microcode(_cpu(brand=brand, model=model))


@pytest.mark.parametrize(
    "brand",
    [
        # mobile
        "13th Gen Intel(R) Core(TM) i9-13900HX",
        "13th Gen Intel(R) Core(TM) i7-13700H",
        "13th Gen Intel(R) Core(TM) i7-1370P",
        "13th Gen Intel(R) Core(TM) i7-1365U",
        "13th Gen Intel(R) Core(TM) i5-13420H",
        # 35 W
        "13th Gen Intel(R) Core(TM) i7-13700T",
        "14th Gen Intel(R) Core(TM) i5-14500T",
        # Alder Lake silicon under a 13th/14th gen name
        "13th Gen Intel(R) Core(TM) i3-13100",
        "13th Gen Intel(R) Core(TM) i3-13100F",
        "13th Gen Intel(R) Core(TM) i5-13400",
        "13th Gen Intel(R) Core(TM) i5-13400F",
        "14th Gen Intel(R) Core(TM) i5-14400",
        "14th Gen Intel(R) Core(TM) i5-14400F",
        "14th Gen Intel(R) Core(TM) i3-14100",
        # other generations / brands
        "12th Gen Intel(R) Core(TM) i9-12900K",
        "11th Gen Intel(R) Core(TM) i7-11700K",
        "Intel(R) Core(TM) Ultra 9 285K",
        "AMD Ryzen 9 7950X 16-Core Processor",
        "Intel(R) Xeon(R) E-2488",
        "",
    ],
)
def test_other_brands_are_not_affected(brand: str) -> None:
    for model in (183, 191):
        assert not hc.raptor_lake_needs_microcode(_cpu(brand=brand, model=model))


def test_vendor_family_and_model_must_all_match() -> None:
    assert not hc.raptor_lake_needs_microcode(_cpu(vendor="AuthenticAMD"))
    assert not hc.raptor_lake_needs_microcode(_cpu(vendor=None))
    assert not hc.raptor_lake_needs_microcode(_cpu(family=25))
    assert not hc.raptor_lake_needs_microcode(_cpu(family="6"))
    assert not hc.raptor_lake_needs_microcode(_cpu(model=151))  # Alder Lake-S
    assert not hc.raptor_lake_needs_microcode(_cpu(model=186))  # Raptor Lake-P (mobile)
    assert not hc.raptor_lake_needs_microcode(_cpu(model=True))
    assert not hc.raptor_lake_needs_microcode(_cpu(model=None))
    assert hc.raptor_lake_needs_microcode(_cpu(model=191))


@pytest.mark.parametrize("cpu", [None, {}, [], "cpu", 3, {"vendor": "GenuineIntel"}, _cpu(brand=None), _cpu(brand=7)])
def test_malformed_cpu_is_not_a_finding(cpu: object) -> None:
    assert not hc.raptor_lake_needs_microcode(cpu)

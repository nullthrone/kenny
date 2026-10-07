"""Closed attribution tables for the hardware-failure signals (server judgement).

The agent reports raw facts (``hardware_errors``, ``os_support.cpu``, ...) and
never says which component is failing (ADR-0007, ADR-0058). This module is the
server's side of that split: which event points at which component, how bad a
class of event is, what a bugcheck code implies, which NVIDIA Xids are hardware
and which CPUs need a microcode update. None of it is part of the wire contract.

It is deliberately *closed*. ADR-0026 and ADR-0041 rejected hand tables that grow
with the fleet (enumerating the open-ended set of event sources in order to
classify them). Every table here is bounded by something that does not grow with
the fleet -- the fixed list of events the agent queries (:data:`EVENT_QUERY`),
the fixed set of bugchecks that mean a hardware class, one vendor advisory --
the same justification as ``health_rules._RELIABILITY_CRASH_MARKERS``. An event
the agent does not query is never attributed here; ``tests/test_hardware_catalog``
fails when the two sets diverge.

The result is a *component* (:data:`COMPONENTS`) and a *severity class*
(:data:`SEVERITIES`) per event group, so a rule can tell an uncorrected error
(``fatal``) from a corrected one (``corrected``), from driver-level
``instability`` and from evidence that only ``supporting`` other evidence.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# -- the agent's query set ----------------------------------------------------

# A Python literal equal to ``docs/fixtures/vectors/hardware_event_query.json``.
# The server ships without the repository docs, so it cannot read the file at
# runtime; ``tests/test_hardware_catalog.py`` asserts the two are equal, and the
# agent's Rust constant is tested against the same file, so an event this module
# attributes is always an event the agent queries.
EVENT_QUERY: dict[str, Any] = {
    "window_days": 14,
    "windows": [
        {"log": "System", "provider": "Microsoft-Windows-WHEA-Logger", "event_ids": [17, 18, 19, 47], "max_level": 4},
        {"log": "System", "provider": "Display", "event_ids": [4101], "max_level": 3},
        {"log": "System", "provider": "nvlddmkm", "event_ids": [13, 14, 153], "max_level": 3},
        {"log": "System", "provider": "storahci", "event_ids": [129], "max_level": 3},
        {"log": "System", "provider": "stornvme", "event_ids": [11, 129], "max_level": 3},
        {"log": "System", "provider": "disk", "event_ids": [7, 11, 51, 153], "max_level": 3},
        {"log": "System", "provider": "Microsoft-Windows-MemoryDiagnostics-Results", "event_ids": [1202], "max_level": 4},
        {"log": "System", "provider": "Microsoft-Windows-Kernel-Power", "event_ids": [41], "max_level": 1},
        {"log": "System", "provider": "BugCheck", "event_ids": [1001], "max_level": 2},
        {"log": "System", "provider": "Microsoft-Windows-WER-SystemErrorReporting", "event_ids": [1001], "max_level": 2},
        {"log": "Application", "provider": "Application Error", "event_ids": [1000], "max_level": 2, "aggregate": "app_crashes"},
    ],
    "linux_kernel_patterns": [
        {"key": "mce", "regex": "mce: \\[Hardware Error\\]"},
        {"key": "edac", "regex": "EDAC .*(CE|UE)"},
        {"key": "nvrm_xid", "regex": "NVRM: Xid \\(.*\\): (\\d+)"},
        {"key": "amdgpu_ras", "regex": "amdgpu.*(RAS|ras).*(error|uncorrectable|correctable)"},
        {"key": "block_io", "regex": "I/O error, dev (\\S+)"},
        {"key": "nvme", "regex": "nvme\\S*: .*(timeout|reset|I/O error)"},
        {"key": "ata", "regex": "ata\\d+(\\.\\d+)?: failed command"},
        {"key": "pcie_aer", "regex": "AER: .*(Corrected|Uncorrected)"},
    ],
}

# -- vocabulary ---------------------------------------------------------------

COMPONENTS: tuple[str, ...] = ("cpu", "memory", "pcie", "gpu", "storage", "power", "platform")

# Severity classes, as a rule consumes them:
#   fatal       an uncorrected hardware error (data or execution was lost)
#   corrected   the hardware fixed it itself (ECC, retried transfer) -- a rate to watch
#   instability a recovered fault with no hardware verdict (a TDR, a storage retry)
#   supporting  evidence that only corroborates other evidence (a bugcheck, a power loss)
FATAL = "fatal"
CORRECTED = "corrected"
INSTABILITY = "instability"
SUPPORTING = "supporting"
SEVERITIES: tuple[str, ...] = (FATAL, CORRECTED, INSTABILITY, SUPPORTING)

STRONG = "strong"

# -- provider names -----------------------------------------------------------

_PROVIDER_PREFIX = "microsoft-windows-"


def canonical_provider(source: Any) -> str:
    """Normalize an event provider name for lookup.

    ``Get-WinEvent`` reports ``Microsoft-Windows-Kernel-Power`` while the event
    viewer and most documentation use ``Kernel-Power``; both name one provider
    (same rule as ``health_rules._reliability_marker_key``).
    """

    src = str(source or "").strip().lower()
    return src[len(_PROVIDER_PREFIX) :] if src.startswith(_PROVIDER_PREFIX) else src


def _event_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def queried_events() -> frozenset[tuple[str, int]]:
    """The ``(canonical provider, event_id)`` pairs the agent queries on Windows."""

    return frozenset(
        (canonical_provider(w["provider"]), int(i))
        for w in EVENT_QUERY["windows"]
        for i in w["event_ids"]
    )


def queried_linux_keys() -> frozenset[str]:
    """The matcher keys the agent reports for the Linux kernel journal."""

    return frozenset(p["key"] for p in EVENT_QUERY["linux_kernel_patterns"])


# -- details helpers ----------------------------------------------------------


def detail_counts(details: Any, key: str) -> dict[str, int]:
    """``details[key]`` as ``{value: count}``, tolerating malformed input.

    ``details`` is ``{key: {value: count}}`` on the wire. A bare string or list
    under a key is read as count 1 per value; anything else yields ``{}``.
    """

    if not isinstance(details, Mapping):
        return {}
    raw = details.get(key)
    out: dict[str, int] = {}
    if isinstance(raw, Mapping):
        for value, count in raw.items():
            n = count if isinstance(count, int) and not isinstance(count, bool) else 1
            out[str(value)] = max(n, 0)
        return out
    if isinstance(raw, str):
        return {raw: 1}
    if isinstance(raw, (list, tuple)):
        for value in raw:
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                out[str(value)] = out.get(str(value), 0) + 1
    return out


# -- removable media -----------------------------------------------------------

#: Bus types of media the user plugs in and out: USB drives and SD / MMC card
#: readers. Neither is judged as a disk, and storage retries on them are set aside.
REMOVABLE_BUS_TYPES: frozenset[str] = frozenset({"usb", "sd"})


def is_removable_bus(bus: Any) -> bool:
    """Whether a ``bus_type`` / ``disk_bus_type`` value names removable media."""

    return isinstance(bus, str) and bus.strip().casefold() in REMOVABLE_BUS_TYPES


def is_removable_disk(row: Any) -> bool:
    """Whether a ``disk_smart`` row is removable media (a USB drive, an SD card).

    The one definition the ``disk_smart`` rule (which does not judge such a disk)
    and the history rollup (which does not track it) share.
    """

    if not isinstance(row, Mapping):
        return False
    return row.get("removable") is True or is_removable_bus(row.get("bus_type"))


def internal_share(details: Any) -> float:
    """The share of a storage group's events that are *not* on removable media.

    ``details.disk_bus_type`` says which bus each sampled event's disk is on.
    Retries on a USB drive or an SD / MMC card reader are the user pulling a
    plug, not a failing internal disk. No usable bus information (or
    ``Unknown``) is judged as internal: silence about the bus is not evidence
    of removable media.
    """

    counts = detail_counts(details, "disk_bus_type")
    total = sum(counts.values())
    if total <= 0:
        return 1.0
    removable = sum(n for bus, n in counts.items() if is_removable_bus(bus))
    return (total - removable) / total


# -- WHEA ---------------------------------------------------------------------

_WHEA = "whea-logger"
_BUS_WORDS = ("bus", "interconnect")


def _whea_component(event_id: int, details: Any) -> str | None:
    """Attribute a WHEA-Logger event by what it says failed, not only by id.

    ``error_source`` / ``error_type`` name the failing block. Event 19 is a
    corrected machine check: normally the core (cache, TLB), but a
    "Bus/Interconnect" error type on a Ryzen is the fabric -- in practice the
    memory-overclock instability signature, not a defective CPU -- so it is
    ``platform``. A mixed group goes to whichever side holds the majority.
    """

    if event_id == 47:
        return "memory"
    sources = " ".join(detail_counts(details, "error_source")).lower()
    if "pci express" in sources or "pcie" in sources:
        return "pcie"
    if event_id == 17:
        return "pcie"
    if event_id not in (18, 19):
        return None
    types = detail_counts(details, "error_type")
    bus = sum(n for t, n in types.items() if any(w in t.lower() for w in _BUS_WORDS))
    total = sum(types.values())
    return "platform" if total and bus * 2 > total else "cpu"


_WHEA_SEVERITY = {17: CORRECTED, 18: FATAL, 19: CORRECTED, 47: CORRECTED}

# -- GPU Xids -----------------------------------------------------------------

# NVIDIA Xids that mean the GPU, its memory or its link failed. Xid 13/31/43/45
# (graphics exception, MMU fault, reset channel, preemptive removal) are
# application or driver faults and are not hardware.
HARDWARE_XIDS: frozenset[int] = frozenset({48, 63, 64, 79, 92, 93, 94, 95, 119, 120})


def is_hardware_xid(xid: Any) -> bool:
    n = _event_id(xid)
    return n is not None and n in HARDWARE_XIDS


def hardware_xid_count(details: Any) -> int:
    """How many events in a group carry a hardware Xid (``details.xid``)."""

    return sum(n for x, n in detail_counts(details, "xid").items() if is_hardware_xid(x))


def _gpu_xid_verdict(details: Any) -> tuple[str | None, str | None]:
    """``(component, severity)`` for a group that carries Xids.

    One hardware Xid makes the group a ``fatal`` GPU finding; a group whose Xids
    are all software faults (13/31/43/45) or unreadable is not one.
    """

    xids = detail_counts(details, "xid")
    if any(is_hardware_xid(x) for x in xids):
        return "gpu", FATAL
    return None, None


# -- bugchecks ----------------------------------------------------------------


def normalize_bugcheck(code: Any) -> str | None:
    """Canonical bugcheck code: lowercase hex, ``0x`` prefix, no zero padding.

    ``"0x124"``, ``"0X00000124"`` and the int ``292`` all give ``"0x124"``.
    A string without the ``0x`` prefix is not read (it would be ambiguous with
    decimal); anything unparseable gives ``None``.
    """

    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return f"0x{code:x}" if code >= 0 else None
    if isinstance(code, str):
        text = code.strip().lower()
        if text.startswith("0x") and len(text) > 2:
            try:
                return f"0x{int(text, 16):x}"
            except ValueError:
                return None
    return None


# Bugcheck code (canonical) -> (component, strength). ``strong`` codes name a
# hardware class by themselves; ``supporting`` ones are usually driver bugs and
# only corroborate other evidence, so a rule must never escalate on them alone.
BUGCHECK_COMPONENTS: dict[str, tuple[str, str]] = {
    "0x124": ("cpu", STRONG),  # WHEA_UNCORRECTABLE_ERROR
    "0x9c": ("cpu", STRONG),  # MACHINE_CHECK_EXCEPTION
    "0x101": ("cpu", STRONG),  # CLOCK_WATCHDOG_TIMEOUT
    "0x116": ("gpu", STRONG),  # VIDEO_TDR_FAILURE
    "0x119": ("gpu", STRONG),  # VIDEO_SCHEDULER_INTERNAL_ERROR
    "0x7a": ("storage", STRONG),  # KERNEL_DATA_INPAGE_ERROR
    "0x77": ("storage", STRONG),  # KERNEL_STACK_INPAGE_ERROR
    "0x1a": ("memory", SUPPORTING),  # MEMORY_MANAGEMENT
    "0x50": ("memory", SUPPORTING),  # PAGE_FAULT_IN_NONPAGED_AREA
    "0x3b": ("memory", SUPPORTING),  # SYSTEM_SERVICE_EXCEPTION
    "0xa": ("memory", SUPPORTING),  # IRQL_NOT_LESS_OR_EQUAL
}

# 0x117 (VIDEO_TDR_TIMEOUT_DETECTED) is a *live* dump of a TDR the driver
# recovered from; the machine did not go down. It carries no attribution.
BUGCHECK_IGNORED: frozenset[str] = frozenset({"0x117"})


def bugcheck_component(code: Any) -> tuple[str, str] | None:
    """``(component, strength)`` for one bugcheck code, ``None`` if it maps to none."""

    key = normalize_bugcheck(code)
    return BUGCHECK_COMPONENTS.get(key) if key is not None else None


def bugcheck_attribution(details: Any) -> tuple[str, str] | None:
    """The ``(component, strength)`` a group's ``details.bugcheck_code`` implies.

    Where several codes are present the most frequent mapped one wins; a strong
    attribution beats a supporting one at equal count.
    """

    best: tuple[int, int, str, str] | None = None
    for code, count in detail_counts(details, "bugcheck_code").items():
        hit = bugcheck_component(code)
        if hit is None:
            continue
        rank = (count, 1 if hit[1] == STRONG else 0)
        if best is None or rank > (best[0], best[1]):
            best = (rank[0], rank[1], hit[0], hit[1])
    return (best[2], best[3]) if best else None


def _bugcheck_codes(details: Any) -> dict[str, int]:
    """Canonical code -> count for every readable ``bugcheck_code``."""

    out: dict[str, int] = {}
    for code, count in detail_counts(details, "bugcheck_code").items():
        key = normalize_bugcheck(code)
        if key is not None:
            out[key] = out.get(key, 0) + count
    return out


# -- Application crash heuristic ----------------------------------------------

# Exception codes whose diversity across unrelated applications suggests bad
# hardware rather than one buggy program: access violation, illegal instruction.
CRASH_EXCEPTION_CODES: frozenset[str] = frozenset({"0xc0000005", "0xc000001d"})

# -- the event table ----------------------------------------------------------

_STORAGE_EVENTS = {
    ("disk", 7),
    ("disk", 11),
    ("disk", 51),
    ("disk", 153),
    ("storahci", 129),
    ("stornvme", 11),
    ("stornvme", 129),
}

# (canonical provider, event_id) pairs this module attributes. A subset of
# :func:`queried_events`; the one queried event it leaves out is Application
# Error 1000, which the agent folds into ``app_crashes`` and which names no
# component.
ATTRIBUTED_EVENTS: frozenset[tuple[str, int]] = frozenset(
    {(_WHEA, i) for i in _WHEA_SEVERITY}
    | {("display", 4101)}
    | {("nvlddmkm", 13), ("nvlddmkm", 14), ("nvlddmkm", 153)}
    | _STORAGE_EVENTS
    | {("memorydiagnostics-results", 1202)}
    | {("kernel-power", 41), ("bugcheck", 1001), ("wer-systemerrorreporting", 1001)}
)

# The Linux matcher keys this module attributes (every key the agent reports).
_LINUX_COMPONENTS: dict[str, str] = {
    "mce": "cpu",
    "edac": "memory",
    "nvrm_xid": "gpu",
    "amdgpu_ras": "gpu",
    "block_io": "storage",
    "nvme": "storage",
    "ata": "storage",
    "pcie_aer": "pcie",
}
ATTRIBUTED_LINUX_KEYS: frozenset[str] = frozenset(_LINUX_COMPONENTS)

# Severity per Linux key for a *journal* group. A journal line cannot say
# whether an EDAC / AER event was corrected, so those groups are only
# ``supporting``; the structured ``edac`` / ``aer`` lists carry the counts and
# are classified by :func:`edac_severity` / :func:`aer_severity`. A machine-check
# banner and an amdgpu RAS line likewise do not say corrected or not.
_LINUX_SEVERITY: dict[str, str] = {
    "mce": INSTABILITY,
    "edac": SUPPORTING,
    "amdgpu_ras": INSTABILITY,
    "block_io": INSTABILITY,
    "nvme": INSTABILITY,
    "ata": INSTABILITY,
    "pcie_aer": SUPPORTING,
}


def component_for(source: Any, event_id: Any, details: Any = None) -> str | None:
    """The component an event group points at, or ``None`` if it names none.

    ``source`` / ``event_id`` are the group's own fields (Windows provider and
    event id, or a Linux matcher key with event id 0); ``details`` is its
    ``{key: {value: count}}`` map. ``None`` also covers events the agent does
    not query, software-fault Xids, ignored bugchecks, and a Kernel-Power 41
    whose bugcheck code is a driver bug rather than a power loss.
    """

    provider = canonical_provider(source)
    eid = _event_id(event_id)
    if provider in _LINUX_COMPONENTS and eid in (0, None):
        if provider == "nvrm_xid":
            return _gpu_xid_verdict(details)[0]
        return _LINUX_COMPONENTS[provider]
    if eid is None or (provider, eid) not in ATTRIBUTED_EVENTS:
        return None
    if provider == _WHEA:
        return _whea_component(eid, details)
    if provider == "display":
        return "gpu"
    if provider == "nvlddmkm":
        xids = detail_counts(details, "xid")
        if xids:
            return _gpu_xid_verdict(details)[0]
        return "gpu"
    if (provider, eid) in _STORAGE_EVENTS:
        return "storage"
    if provider == "memorydiagnostics-results":
        return "memory"
    if provider in ("bugcheck", "wer-systemerrorreporting"):
        hit = bugcheck_attribution(details)
        return hit[0] if hit else None
    if provider == "kernel-power":
        hit = bugcheck_attribution(details)
        if hit:
            return hit[0]
        codes = _bugcheck_codes(details)
        # A recorded non-zero bugcheck that maps to nothing (a driver bug, a
        # recovered live dump) is a crash, not a power loss.
        if codes and "0x0" not in codes:
            return None
        return "power"
    return None


def severity_for(source: Any, event_id: Any, details: Any = None) -> str | None:
    """The severity class of an event group (:data:`SEVERITIES`), or ``None``.

    ``None`` means the group is not attributed (see :func:`component_for`), so
    it must not feed a hardware verdict.
    """

    provider = canonical_provider(source)
    eid = _event_id(event_id)
    if provider in _LINUX_COMPONENTS and eid in (0, None):
        if provider == "nvrm_xid":
            return _gpu_xid_verdict(details)[1]
        return _LINUX_SEVERITY[provider]
    if eid is None or (provider, eid) not in ATTRIBUTED_EVENTS:
        return None
    if provider == _WHEA:
        return _WHEA_SEVERITY[eid]
    if provider == "display":
        return INSTABILITY
    if provider == "nvlddmkm":
        xids = detail_counts(details, "xid")
        if xids:
            return _gpu_xid_verdict(details)[1]
        return INSTABILITY
    if (provider, eid) in _STORAGE_EVENTS:
        return INSTABILITY
    if provider == "memorydiagnostics-results":
        return FATAL
    if component_for(source, event_id, details) is None:
        return None
    return SUPPORTING  # Kernel-Power 41, BugCheck / WER 1001


def edac_severity(entry: Any) -> str | None:
    """Severity of one structured ``edac`` entry: uncorrected is fatal."""

    if not isinstance(entry, Mapping):
        return None
    ue, ce = _count(entry.get("ue_count")), _count(entry.get("ce_count"))
    if ue > 0:
        return FATAL
    return CORRECTED if ce > 0 else None


def aer_severity(entry: Any) -> str | None:
    """Severity of one structured ``aer`` entry: fatal / non-fatal are uncorrected."""

    if not isinstance(entry, Mapping):
        return None
    if _count(entry.get("fatal")) > 0 or _count(entry.get("nonfatal")) > 0:
        return FATAL
    return CORRECTED if _count(entry.get("correctable")) > 0 else None


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return int(value) if value > 0 else 0
    except (OverflowError, ValueError):
        return 0


# -- Raptor Lake microcode ----------------------------------------------------

# Intel 13th / 14th Gen desktop processors with a 65 W or higher base power
# (CPUID family 6, model 0xB7 "Raptor Lake-S" or 0xBF "Raptor Lake refresh")
# can degrade permanently from excessive voltage requests; microcode 0x12B is
# the revision that carries the fix.
#
# VERIFY AGAINST INTEL'S ADVISORY before widening or trusting this list: the
# SKU list, the fixed revision and the model numbers below are from the vendor's
# published guidance (the 0x129 / 0x12B "Vmin Shift Instability" updates) and
# were not re-checked against it from this repository.
#
# This is a hand table, which ADR-0026 rejected for open-ended spaces. It is
# kept because it is a closed set that does not grow with the fleet -- one
# vendor, one defect, one fixed revision -- exactly like
# ``health_rules._RELIABILITY_CRASH_MARKERS``. It errs towards silence: when the
# brand, model or microcode is unknown or unclear, no finding is raised.
RAPTOR_LAKE_FIXED_MICROCODE = 0x12B
_RAPTOR_LAKE_MODELS = frozenset({183, 191})  # 0xB7, 0xBF

# Desktop SKUs only: "i<tier>-<gen><sku>" followed by an optional unlocked /
# no-iGPU suffix and nothing else. A mobile suffix (H, HX, P, U) or a 35 W
# "T" part leaves a letter after the number, so the trailing ``\b`` rejects
# it; 4-digit mobile numbers (i7-1370P) do not match the 3-digit SKU group.
_RAPTOR_LAKE_BRAND = re.compile(r"\bi([579])-(13|14)(\d{3})(?:KS|KF|K|F)?\b", re.IGNORECASE)
# Lowest 3-digit SKU of the affected 65 W+ range per tier: i5-135xx/136xx/145xx/
# 146xx, i7-137xx/147xx(/1379x), i9-139xx/149xx. i3 is out entirely, as are
# the i5-13400 / i5-14400 / i3-13100 / i3-14100 families, whose silicon (and
# the "model 191" parts among them) is Alder Lake, not Raptor Lake.
_RAPTOR_LAKE_SKU_RANGE = {"5": (500, 699), "7": (700, 799), "9": (900, 999)}


def _int_field(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _microcode_revision(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip().lower()
        try:
            return int(text, 16)
        except ValueError:
            return None
    return None


def raptor_lake_needs_microcode(cpu: Any) -> bool:
    """True only for an affected Raptor Lake desktop CPU on an old microcode.

    ``cpu`` is ``os_support.cpu`` (``{vendor, brand, family, model, stepping,
    microcode, ...}``). The running ``microcode`` is the judged revision; an
    unreadable one is not a finding.
    """

    if not isinstance(cpu, Mapping):
        return False
    if cpu.get("vendor") != "GenuineIntel":
        return False
    if _int_field(cpu.get("family")) != 6 or _int_field(cpu.get("model")) not in _RAPTOR_LAKE_MODELS:
        return False
    brand = cpu.get("brand")
    if not isinstance(brand, str):
        return False
    match = _RAPTOR_LAKE_BRAND.search(brand)
    if match is None:
        return False
    low, high = _RAPTOR_LAKE_SKU_RANGE[match.group(1)]
    if not low <= int(match.group(3)) <= high:
        return False
    revision = _microcode_revision(cpu.get("microcode"))
    return revision is not None and revision < RAPTOR_LAKE_FIXED_MICROCODE

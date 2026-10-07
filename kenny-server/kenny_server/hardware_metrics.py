"""Reduce one UTC day of snapshots to compact per-device metrics (ADR-0070).

Pure and I/O-free. :func:`extract` turns the snapshots one host pushed on one day
into ``(device_key, metric, value)`` rows for the ``hw_metrics`` table, and
:func:`device_labels` names the devices a snapshot shows. The metric names below
are a contract with the dashboard and with ``trends.hardware_forecasts``: change
one only together with its consumers.

Device identity decides what counts as one series, so a replaced device starts a
new one:

* ``disk:<serial>`` -- disks without a serial and removable disks (USB, SD;
  ``hardware_catalog.is_removable_disk``, the definition the ``disk_smart`` rule
  uses) are not tracked: a USB stick's counters say nothing about the host's
  hardware;
* ``gpu:<uuid | bus id | pci id>``;
* ``fan:<key>`` -- the agent's stable fan key;
* ``host:<component>`` -- hardware-error counts per component, attributed by
  ``hardware_catalog`` exactly as the ``hardware_errors`` rule attributes them.

Every reading is untrusted wire data (``protocol.Section`` allows any extra
field): a malformed value is skipped, never raised on, and a malformed section
costs only its own metrics.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable

from . import hardware_catalog, health_rules

__all__ = [
    "COMPONENT_LABELS",
    "DISK_METRICS",
    "FAN_BANDS",
    "device_labels",
    "extract",
    "fallback_label",
    "kind_of",
]

#: How a component is named to a reader.
COMPONENT_LABELS: dict[str, str] = {
    "cpu": "Processor",
    "memory": "Memory",
    "pcie": "PCIe",
    "gpu": "Graphics",
    "storage": "Storage",
    "power": "Power",
    "platform": "Platform",
}

#: Disk metric -> where the value is read from.
DISK_METRICS: tuple[str, ...] = (
    "percentage_used",
    "available_spare",
    "available_spare_threshold",
    "media_errors",
    "unsafe_shutdowns",
    "read_errors_uncorrected",
    "write_errors_uncorrected",
    "smart_5",
    "smart_187",
    "smart_197",
    "smart_198",
)

_NVME_FIELDS = (
    "available_spare",
    "available_spare_threshold",
    "media_errors",
    "unsafe_shutdowns",
)
_SMART_IDS = ("5", "187", "197", "198")

#: Fan duty bands: metric name and the half-open ``[low, high)`` percent range
#: (the last band includes 100 %).
FAN_BANDS: tuple[tuple[str, float, float], ...] = (
    ("rpm_duty_30_50", 30.0, 50.0),
    ("rpm_duty_50_70", 50.0, 70.0),
    ("rpm_duty_70_90", 70.0, 90.0),
    ("rpm_duty_90_100", 90.0, 100.0 + 1e-9),
)
# A band's median is only worth storing from this many non-zero samples.
_MIN_BAND_SAMPLES = 3
_MAX_SAMPLES = 64

_SEVERITY_METRIC = {
    hardware_catalog.CORRECTED: "corrected_events",
    hardware_catalog.FATAL: "fatal_events",
    hardware_catalog.INSTABILITY: "instability_events",
}

#: Top-level snapshot sections the rollup reads (see ``TelemetryStore.snapshots_for_day``).
SECTIONS: tuple[str, ...] = ("disk_smart", "gpu", "fans", "hardware_errors")

Row = tuple[str, str, float]


# -- helpers --------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """A finite float, or ``None`` for bools, non-numbers and unusable values."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError:
        return None
    return out if math.isfinite(out) else None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _section(snapshot: Any, name: str) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    section = snapshot.get(name)
    return section if isinstance(section, dict) else {}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def kind_of(device_key: str) -> str:
    """``disk`` / ``gpu`` / ``fan`` / ``component`` for a device key."""

    prefix = device_key.partition(":")[0]
    return {"disk": "disk", "gpu": "gpu", "fan": "fan"}.get(prefix, "component")


def fallback_label(device_key: str) -> str:
    """A label for a device no current snapshot names (removed, renamed)."""

    prefix, _, rest = device_key.partition(":")
    if prefix == "host":
        return COMPONENT_LABELS.get(rest, rest.capitalize())
    return rest or device_key


# -- device keys ----------------------------------------------------------------


def _disk_key(row: dict[str, Any]) -> str | None:
    serial = _text(row.get("serial"))
    if serial is None or hardware_catalog.is_removable_disk(row):
        return None
    return f"disk:{serial}"


def _gpu_key(gpu: dict[str, Any]) -> str | None:
    for field in ("uuid", "bus_id", "pci_id"):
        value = _text(gpu.get(field))
        if value is not None:
            return f"gpu:{value}"
    return None


def _fan_key(fan: dict[str, Any]) -> str | None:
    key = _text(fan.get("key"))
    return f"fan:{key}" if key is not None else None


def _attributed(group: dict[str, Any]) -> tuple[str, str] | None:
    """``(component, severity)`` of a ``hardware_errors`` group, if attributed."""

    args = (group.get("source"), group.get("event_id"), group.get("details"))
    component = hardware_catalog.component_for(*args)
    severity = hardware_catalog.severity_for(*args)
    if component is None or severity is None:
        return None
    return component, severity


# -- labels ---------------------------------------------------------------------


def device_labels(snapshot: Any) -> dict[str, tuple[str, str]]:
    """``{device_key: (kind, label)}`` for the devices one snapshot shows.

    Component entries cover the components the snapshot's ``hardware_errors``
    section attributes anything to. Never raises.
    """

    out: dict[str, tuple[str, str]] = {}
    try:
        for row in _dicts(_section(snapshot, "disk_smart").get("disks")):
            key = _disk_key(row)
            if key is not None:
                out[key] = ("disk", _text(row.get("model")) or key.removeprefix("disk:"))
        for gpu in _dicts(_section(snapshot, "gpu").get("gpus")):
            key = _gpu_key(gpu)
            if key is not None:
                out[key] = ("gpu", _text(gpu.get("name")) or key.removeprefix("gpu:"))
        for fan in _dicts(_section(snapshot, "fans").get("fans")):
            key = _fan_key(fan)
            if key is not None:
                label = _text(fan.get("label")) or key.removeprefix("fan:")
                out[key] = ("fan", label)
        errors = _section(snapshot, "hardware_errors")
        components: set[str] = set()
        for group in _dicts(errors.get("groups")):
            hit = _attributed(group)
            if hit is not None:
                components.add(hit[0])
        if _dicts(errors.get("edac")):
            components.add("memory")
        if _dicts(errors.get("aer")):
            components.add("pcie")
        for component in components:
            out[f"host:{component}"] = (
                "component",
                COMPONENT_LABELS.get(component, component.capitalize()),
            )
    except Exception:  # noqa: BLE001 - labels are cosmetic, never fatal
        pass
    return out


# -- extraction -----------------------------------------------------------------


def _extract_disks(snapshots: Iterable[Any]) -> list[Row]:
    last: dict[tuple[str, str], float] = {}
    for snapshot in snapshots:
        for row in _dicts(_section(snapshot, "disk_smart").get("disks")):
            key = _disk_key(row)
            # A paused read (anti-cheat coexistence) carries no raw counters.
            if key is None or row.get("paused") is True:
                continue
            nvme = row.get("nvme") if isinstance(row.get("nvme"), dict) else {}
            attrs = row.get("smart_attributes")
            attrs = attrs if isinstance(attrs, dict) else {}
            values: dict[str, float | None] = {}
            used = _num(nvme.get("percentage_used"))
            values["percentage_used"] = used if used is not None else _num(row.get("wear"))
            for field in _NVME_FIELDS:
                values[field] = _num(nvme.get(field))
            values["read_errors_uncorrected"] = _num(row.get("read_errors_uncorrected"))
            values["write_errors_uncorrected"] = _num(row.get("write_errors_uncorrected"))
            for attr in _SMART_IDS:
                values[f"smart_{attr}"] = _num(attrs.get(attr))
            for metric, value in values.items():
                if value is not None:
                    last[(key, metric)] = value
    return [(key, metric, value) for (key, metric), value in last.items()]


def _extract_gpus(snapshots: Iterable[Any]) -> list[Row]:
    loaded: dict[str, float] = {}
    width_max: dict[str, float] = {}
    slowdown: dict[str, bool] = {}
    for snapshot in snapshots:
        for gpu in _dicts(_section(snapshot, "gpu").get("gpus")):
            key = _gpu_key(gpu)
            if key is None:
                continue
            pcie = gpu.get("pcie") if isinstance(gpu.get("pcie"), dict) else {}
            current, maximum = _num(pcie.get("width_current")), _num(pcie.get("width_max"))
            util = _num(gpu.get("utilization_percent"))
            loaded_from = health_rules.GPU_LOADED_UTILIZATION_PERCENT
            if current is not None and util is not None and util >= loaded_from:
                loaded[key] = max(loaded.get(key, current), current)
            if maximum is not None:
                width_max[key] = max(width_max.get(key, maximum), maximum)
            throttle = gpu.get("throttle") if isinstance(gpu.get("throttle"), dict) else {}
            flags = [throttle.get("hw_slowdown"), throttle.get("hw_power_brake_slowdown")]
            if any(isinstance(f, bool) for f in flags):
                slowdown[key] = slowdown.get(key, False) or any(f is True for f in flags)
    rows: list[Row] = [(k, "pcie_width_loaded_max", v) for k, v in loaded.items()]
    rows += [(k, "pcie_width_max", v) for k, v in width_max.items()]
    # Reported only when the driver said anything about the reasons: 0 means
    # "looked and saw none", not "unknown".
    rows += [(k, "hw_slowdown_seen", 1.0 if seen else 0.0) for k, seen in slowdown.items()]
    return rows


def _fan_samples(fan: dict[str, Any]) -> list[float]:
    raw = fan.get("rpm_samples")
    if not isinstance(raw, list):
        return []
    out = [_num(v) for v in raw[:_MAX_SAMPLES]]
    return [v for v in out if v is not None and v >= 0]


def _population_cv(running: list[float]) -> float | None:
    mean = math.fsum(running) / len(running)
    if mean <= 0:
        return None
    return math.sqrt(math.fsum((v - mean) ** 2 for v in running) / len(running)) / mean


def _extract_fans(snapshots: Iterable[Any]) -> list[Row]:
    bands: dict[str, dict[str, list[float]]] = {}
    stalled: dict[str, bool] = {}
    cv_max: dict[str, float] = {}
    for snapshot in snapshots:
        for fan in _dicts(_section(snapshot, "fans").get("fans")):
            key = _fan_key(fan)
            if key is None or fan.get("idle_or_absent") is True:
                continue
            samples = _fan_samples(fan)
            if not samples:
                continue
            duty = _num(fan.get("duty_percent"))
            running = [v for v in samples if v > 0]
            stalled[key] = stalled.get(key, False) or (
                not running and duty is not None and duty >= health_rules.FAN_STALL_MIN_DUTY
            )
            if duty is not None:
                for name, low, high in FAN_BANDS:
                    if low <= duty < high:
                        bands.setdefault(key, {}).setdefault(name, []).extend(running)
                        break
            if len(running) >= 2:
                cv = _population_cv(running)
                if cv is not None:
                    cv_max[key] = max(cv_max.get(key, cv), cv)
    rows: list[Row] = [(k, "stall_seen", 1.0 if seen else 0.0) for k, seen in stalled.items()]
    rows += [(k, "rpm_cv_max", round(v, 4)) for k, v in cv_max.items()]
    for key, per_band in bands.items():
        for name, values in per_band.items():
            if len(values) >= _MIN_BAND_SAMPLES:
                rows.append((key, name, float(statistics.median(values))))
    return rows


def _extract_components(snapshots: list[Any], day: str | None) -> list[Row]:
    # `by_day` is a rolling histogram, so the day's count comes from the latest
    # snapshot that carries the section; summing across snapshots would count
    # the same event once per push.
    latest: dict[str, Any] | None = None
    for snapshot in snapshots:
        section = _section(snapshot, "hardware_errors")
        if section:
            latest = section
    if latest is None:
        return []
    totals: dict[tuple[str, str], float] = {}
    if day is not None:
        for group in _dicts(latest.get("groups")):
            hit = _attributed(group)
            metric = _SEVERITY_METRIC.get(hit[1]) if hit else None
            if hit is None or metric is None:
                continue
            by_day = group.get("by_day")
            count = _num(by_day.get(day)) if isinstance(by_day, dict) else None
            count = max(count or 0.0, 0.0)
            if hit[0] == "storage" and hit[1] == hardware_catalog.INSTABILITY:
                # Retries on a USB drive or SD card are the user pulling a plug;
                # the rule sets them aside by the same share (see ``internal_share``).
                count *= hardware_catalog.internal_share(group.get("details"))
            key = (f"host:{hit[0]}", metric)
            totals[key] = totals.get(key, 0.0) + count
    sources = latest.get("sources")
    sources = sources if isinstance(sources, list) else []

    edac, aer = _dicts(latest.get("edac")), _dicts(latest.get("aer"))
    # An empty list is a real zero only when the agent says it read that source
    # (Linux lists `aer` entries only when non-zero).
    if edac or "edac" in sources:
        totals[("host:memory", "edac_ce")] = math.fsum(_num(e.get("ce_count")) or 0.0 for e in edac)
        totals[("host:memory", "edac_ue")] = math.fsum(_num(e.get("ue_count")) or 0.0 for e in edac)
    if aer or "aer" in sources:
        totals[("host:pcie", "aer_correctable")] = math.fsum(
            _num(e.get("correctable")) or 0.0 for e in aer
        )
        totals[("host:pcie", "aer_uncorrected")] = math.fsum(
            (_num(e.get("nonfatal")) or 0.0) + (_num(e.get("fatal")) or 0.0) for e in aer
        )
    return [(key, metric, value) for (key, metric), value in totals.items()]


def extract(snapshots: list[Any], day: str | None = None) -> list[Row]:
    """The day's ``(device_key, metric, value)`` rows from its snapshots.

    ``snapshots`` are the bare snapshot dicts of one UTC day, oldest first.
    ``day`` (``YYYY-MM-DD``) selects the ``by_day`` bucket of the hardware-error
    counts; when omitted it is read from the newest snapshot's ``collected_at``
    if it carries one, and the per-component event counts are skipped otherwise.
    Rows are sorted. Never raises: a malformed snapshot or section only loses
    its own rows.
    """

    snaps = [s for s in snapshots if isinstance(s, dict)] if isinstance(snapshots, list) else []
    if day is None and snaps:
        stamp = snaps[-1].get("collected_at")
        day = stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else None
    rows: list[Row] = []
    for part in (
        lambda: _extract_disks(snaps),
        lambda: _extract_gpus(snaps),
        lambda: _extract_fans(snaps),
        lambda: _extract_components(snaps, day),
    ):
        try:
            rows.extend(part())
        except Exception:  # noqa: BLE001 - a bad section must not cost the others
            continue
    return sorted(rows)

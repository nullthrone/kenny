"""Snapshot-section builders for the hardware-history tests (ADR-0070).

Each builder returns the shape ``docs/protocol.md`` documents for the section, with
only the fields the tests vary.
"""

from __future__ import annotations

from typing import Any

WHEA = "Microsoft-Windows-WHEA-Logger"


def disk(
    serial: str | None = "SN-1",
    model: str = "WD_BLACK SN850X 2000GB",
    *,
    pct: float | None = None,
    spare: float | None = None,
    spare_threshold: float | None = 10,
    media_errors: float | None = 0,
    unsafe: float | None = 0,
    read_unc: float | None = 0,
    write_unc: float | None = 0,
    smart: dict[str, Any] | None = None,
    bus_type: str = "NVMe",
    removable: bool = False,
    paused: bool = False,
    wear: float | None = None,
) -> dict[str, Any]:
    nvme = None
    if pct is not None or spare is not None:
        nvme = {
            "critical_warning": 0,
            "available_spare": spare,
            "available_spare_threshold": spare_threshold,
            "percentage_used": pct,
            "media_errors": media_errors,
            "unsafe_shutdowns": unsafe,
        }
    return {
        "model": model,
        "serial": serial,
        "bus_type": bus_type,
        "removable": removable,
        "paused": paused,
        "health_status": "Healthy",
        "wear": wear,
        "read_errors_uncorrected": read_unc,
        "write_errors_uncorrected": write_unc,
        "smart_attributes": smart,
        "nvme": nvme,
    }


def disk_smart(*disks: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "summary": "SMART healthy", "disks": list(disks)}


def gpu(
    uuid: str | None = "GPU-1",
    name: str = "NVIDIA GeForce RTX 4080",
    *,
    util: float | None = 60,
    width: float | None = 16,
    width_max: float | None = 16,
    slowdown: bool | None = False,
    power_brake: bool | None = False,
    bus_id: str | None = "0000:01:00.0",
    pci_id: str | None = "10de:2704",
) -> dict[str, Any]:
    throttle = (
        None
        if slowdown is None and power_brake is None
        else {
            "hw_slowdown": slowdown,
            "hw_thermal_slowdown": False,
            "hw_power_brake_slowdown": power_brake,
            "sw_thermal_slowdown": False,
        }
    )
    return {
        "name": name,
        "vendor": "nvidia",
        "pci_id": pci_id,
        "bus_id": bus_id,
        "uuid": uuid,
        "utilization_percent": util,
        "pcie": {"gen_current": 4, "gen_max": 4, "width_current": width, "width_max": width_max},
        "throttle": throttle,
    }


def gpu_section(*gpus: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "summary": f"{len(gpus)} GPU(s)", "gpus": list(gpus), "errors": []}


def fan(
    key: str = "nct6798.fan1",
    samples: list[float] | None = None,
    duty: float | None = 40.0,
    *,
    label: str | None = "CPU_FAN",
    idle: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "source": "hwmon",
        "rpm_samples": samples if samples is not None else [1000, 1000, 1000, 1000, 1000],
        "duty_percent": duty,
        "mode": "pwm",
        "idle_or_absent": idle,
    }


def fans_section(*fans: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "summary": f"{len(fans)} fans read", "fans": list(fans), "errors": []}


def group(
    source: str,
    event_id: int,
    by_day: dict[str, int],
    *,
    level: str = "warning",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "event_id": event_id,
        "level": level,
        "count": sum(by_day.values()),
        "last_seen": "2026-07-01T00:00:00Z",
        "by_day": by_day,
        "sample": "x",
        "details": details or {},
    }


def hardware_errors(
    *groups: dict[str, Any],
    edac: list[dict[str, Any]] | None = None,
    aer: list[dict[str, Any]] | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": "",
        "sources": sources if sources is not None else ["event_log"],
        "groups": list(groups),
        "edac": edac if edac is not None else [],
        "aer": aer if aer is not None else [],
        "errors": [],
    }


def snapshot(**sections: dict[str, Any]) -> dict[str, Any]:
    return dict(sections)

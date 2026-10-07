"""Build a demo fleet of ~6 family PCs from the golden telemetry fixture.

The screenshot tooling renders the *real* dashboard against *mock* data. This
module is the mock-data source: it deep-copies ``docs/fixtures/telemetry_snapshot.json``
and mutates section statuses/values so the fleet exercises every dashboard
widget with a documented health mix (see ``docs/dashboard.md``):

* ``papa-pc``        — all-green desktop baseline
* ``mama-laptop``    — laptop with a battery (device-mix + battery trend)
* ``kid-pc``         — flagged ``web_activity`` (parental controls + Flagged view); a
  graphics card whose PCIe link narrowed under load
* ``study-pc``       — disk critical + a <30-day disk-full forecast; an NVMe drive near
  its rated endurance and a CPU fan slowing at the same duty (hardware forecasts)
* ``living-room-pc`` — reboot pending + a failed/ pending Windows update; an unstable
  chassis fan and corrected processor-interconnect errors
* ``grandpa-pc``     — Defender real-time OFF + an end-of-life OS; a hard disk that began
  reallocating sectors and a Raptor Lake CPU on old microcode

Everything is derived from one *base clock* captured once per run, so relative
timestamps (scan ages, "last seen", the daily trend) are internally consistent.
The functions are pure: they return plain dicts, and :mod:`seed` writes them
into a running app's stores.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "telemetry_snapshot.json"
)

# Number of daily history points seeded per host (drives the fleet health trend
# and the per-agent sparkline / disk-fill + battery forecasts).
HISTORY_DAYS = 30

# Reliability (source, event_id) -> classification (category/severity/cause).
# Pre-seeded into the server's categorization cache by :mod:`seed` so the
# heatmaps *and* the health rule's pattern-severity scoring show
# varied, deliberate results without an Anthropic API key (which would
# otherwise coerce every group to category="Other", severity="unknown"; see
# event_categories.categorize_events). Severities are chosen to preserve each
# host's documented health mix (above) while still demonstrating the range:
# an occasional app crash and background update-client noise are benign
# nuisances (kept off the crit/warn path regardless of count), a disk I/O
# error and a recurring critical-level power event are serious.
RELIABILITY_CLASSIFICATIONS: dict[tuple[str, int], dict[str, str]] = {
    ("Application Error", 1000): {
        "category": "App crash / hang",
        "severity": "benign",
        "cause": "an occasional app crash, not a systemic pattern",
    },
    ("disk", 51): {
        "category": "Disk & storage",
        "severity": "serious",
        "cause": "possible failing sectors on the disk",
    },
    ("Microsoft-Windows-Kernel-Power", 41): {
        "category": "Power & boot",
        "severity": "serious",
        "cause": "unexpected shutdown, possible power or hardware issue",
    },
    ("Service Control Manager", 7034): {
        "category": "Windows service",
        "severity": "benign",
        "cause": "a service restarting itself, usually self-resolves",
    },
    ("Microsoft-Windows-WindowsUpdateClient", 20): {
        "category": "Windows Update",
        "severity": "benign",
        "cause": "background update-client retry noise",
    },
    # The issue #166 pattern: a well-known, harmless CryptSvc quirk that can
    # dominate a host's reliability scoring by sheer volume. Left "unknown"
    # (not "benign") on purpose, mirroring the real classifier's genuine
    # uncertainty about it — demonstrating that suppression, not a benign
    # verdict, is what tames it (see RELIABILITY_SUPPRESSIONS below).
    ("Microsoft-Windows-CAPI2", 4176): {
        "category": "Windows service",
        "severity": "unknown",
        "cause": "undocumented AuthSafes count quirk in CryptSvc; no known fix",
    },
}

# Seeded reliability alarm suppression rules (ADR-0041 / issue #166) — applied
# by scripts/screenshots/seed.py via the app's SuppressionService, so the demo
# fleet's Reliability card shows the suppressed badge and the panel populated
# without any manual dashboard interaction.
RELIABILITY_SUPPRESSIONS: list[dict[str, Any]] = [
    {
        "event_id": 4176,
        "source": "Microsoft-Windows-CAPI2",
        "note": "known CryptSvc AuthSafes quirk; Microsoft has no fix (issue #166)",
    },
]


@dataclass
class DemoHost:
    """One mock agent: identity metadata plus its latest full snapshot."""

    agent_id: str
    meta: dict[str, Any]
    latest: dict[str, Any]
    online: bool = True
    # Per-day history overrides (start value -> latest value across HISTORY_DAYS).
    disk_series: tuple[float, float] | None = None
    battery_series: tuple[float, float] | None = None
    # Parental-controls state for the web_activity list editor (kid-pc only).
    webfilter: dict[str, Any] | None = None
    # Rewrites a copy of ``latest`` into how the host looked ``age`` days ago
    # (the hardware sections whose drift the long-lived history shows: NVMe
    # endurance, a counter that left zero, a fan's speed, a PCIe link's width).
    evolve: Callable[[dict[str, Any], int], None] | None = None


def _load_base_snapshot() -> dict[str, Any]:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    snap = copy.deepcopy(data["snapshot"])
    # The fixture exposes RDP (3389) on 0.0.0.0, which the listening-ports health
    # rule flags on every host. Bind it to loopback so a clean host stays green
    # (grandpa/kid/etc. get their warns/crits from their intended sections).
    snap["listening_ports"] = {
        "status": "ok",
        "summary": "10 listening ports",
        "ports": [
            {"proto": "tcp", "port": 445, "address": "0.0.0.0", "pid": 4, "process": "System"},
            {"proto": "tcp", "port": 3389, "address": "127.0.0.1", "pid": 1204, "process": "svchost"},
        ],
        "count": 10,
        "truncated": False,
    }
    return snap


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reliability(events: list[dict[str, Any]], stability: float) -> dict[str, Any]:
    total = sum(int(e.get("count", 0)) for e in events)
    return {
        "status": "warn" if total >= 15 else "ok",
        "summary": f"{total} error/critical events in 7d",
        "stability_index": stability,
        "recent_crashes": total,
        "window_days": 7,
        "events": events,
        "truncated": False,
    }


def _rel_event(source: str, event_id: int, level: str, count: int, sample: str) -> dict[str, Any]:
    return {
        "source": source,
        "event_id": event_id,
        "level": level,
        "count": count,
        "sample": sample,
    }


def _os_support(
    name: str,
    version: str,
    build: str,
    *,
    eol: bool = False,
    cpu: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "crit" if eol else "ok",
        "summary": f"{name} {version} (build {build})" + (" — end of life" if eol else ""),
        "name": name,
        "version": version,
        "build": build,
        "eol": eol,
        "arch": "x86_64",
        "channel": "stable",
    }
    if cpu is not None:
        out["cpu"] = cpu
    return out


def _cpu(vendor: str, brand: str, family: int, model: int, stepping: int, microcode: str) -> dict[str, Any]:
    return {
        "vendor": vendor,
        "brand": brand,
        "family": family,
        "model": model,
        "stepping": stepping,
        "microcode": microcode,
    }


def _encryption(protected: bool) -> dict[str, Any]:
    return {
        "status": "ok" if protected else "warn",
        "summary": "C: BitLocker on" if protected else "C: not encrypted",
        "volumes": [
            {
                "mount": "C:",
                "protection_status": 1 if protected else 0,
                "encryption_percent": 100 if protected else 0,
            }
        ],
    }


def _firewall(all_on: bool) -> dict[str, Any]:
    profiles = [
        {"name": "Domain", "enabled": True},
        {"name": "Private", "enabled": True},
        {"name": "Public", "enabled": all_on},
    ]
    return {
        "status": "ok" if all_on else "warn",
        "summary": "all profiles on" if all_on else "Public profile off",
        "profiles": profiles,
    }


def _memory(percent: float, total_gb: int) -> dict[str, Any]:
    return {
        "status": "warn" if percent > 85 else "ok",
        "summary": f"{percent:.0f}% of {total_gb} GB used",
        "percent_used": percent,
        "total_bytes": total_gb * 1024**3,
    }


def _uptime(days: float) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": f"up {days:.1f} days",
        "uptime_secs": int(days * 86400),
    }


def _battery(charge: int, health: float) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": f"{charge}% charged, health {health:.0f}%",
        "present": True,
        "charge_percent": charge,
        "health_percent": health,
    }


def _app_updates(available: int) -> dict[str, Any]:
    # Informational only — there is no app_updates health rule, so the reported
    # status is authoritative; keep it "ok" (the Overview KPI / Top-hosts chart
    # read the `available` count directly).
    return {
        "status": "ok",
        "summary": f"{available} app update(s) available",
        "available": available,
    }


def _disk(mount: str, percent: float, total_gb: int = 512) -> dict[str, Any]:
    total = total_gb * 1024**3
    free = int(total * (100 - percent) / 100)
    status = "crit" if percent >= 95 else "warn" if percent > 80 else "ok"
    return {
        "status": status,
        "summary": f"{mount} {percent:.0f}% full",
        "volumes": [
            {
                "mount": mount,
                "total_bytes": total,
                "free_bytes": free,
                "percent_used": round(percent),
            }
        ],
        "top_dirs": [{"path": f"{mount}\\Users\\family\\Videos", "bytes": 120000000000}],
    }


def _defender(realtime: bool, base: datetime) -> dict[str, Any]:
    return {
        "status": "crit" if not realtime else "ok",
        "summary": "Real-time protection OFF" if not realtime else "Defender healthy",
        "enabled": realtime,
        "realtime_protection": realtime,
        "last_scan": _iso(base - timedelta(days=2)),
        "last_scan_type": "quick",
        "last_signature_update": _iso(base - timedelta(days=1)),
        "threats_found": 0,
        "action_needed": not realtime,
    }


def _no_reboot() -> dict[str, Any]:
    return {"status": "ok", "summary": "no reboot pending", "pending": False, "reasons": []}


def _win_update_ok(base: datetime) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": "up to date",
        "last_check": _iso(base - timedelta(hours=6)),
        "recent": [
            {
                "kb": "KB5037853",
                "title": "2026-06 Cumulative Update",
                "result": "succeeded",
                "installed_at": _iso(base - timedelta(days=12)),
            }
        ],
    }


# -- hardware-health sections (protocol 0.22) ----------------------------------
#
# ``disk_smart``, ``hardware_errors``, ``gpu`` and ``fans`` follow the golden
# fixture's shapes. The fixture's own event dates are fixed in the past, so the
# demo builds these sections against the base clock, otherwise every event would
# read as long gone.

_GIB = 1024**3


def _nvme_row(
    model: str, serial: str, size_gb: int, *, used: int, device: int = 0, written: int = 9_000_000
) -> dict[str, Any]:
    return {
        "model": model,
        "health_status": "Healthy",
        "predictive_failure": False,
        "wear": used,
        "temperature_c": 41,
        "power_on_hours": 6420,
        "read_errors_total": None,
        "read_errors_uncorrected": 0,
        "write_errors_uncorrected": 0,
        "device_number": device,
        "serial": serial,
        "bus_type": "NVMe",
        "media_type": "SSD",
        "size_bytes": size_gb * 1000**3,
        "removable": False,
        "temperature_max_c": 74,
        "smart_attributes": None,
        "nvme": {
            "critical_warning": 0,
            "available_spare": 100,
            "available_spare_threshold": 10,
            "percentage_used": used,
            "media_errors": 0,
            "unsafe_shutdowns": 9,
            "error_log_entries": 0,
            "data_units_written": written,
            "power_on_hours": 6420,
            "temperature_c": 41,
        },
        "nvme_error": None,
        "paused": False,
    }


def _sata_row(
    model: str, serial: str, size_gb: int, *, hdd: bool, device: int, reallocated: int = 0
) -> dict[str, Any]:
    return {
        "model": model,
        "health_status": "Healthy",
        "predictive_failure": False,
        "wear": None if hdd else 4,
        "temperature_c": 38 if hdd else 33,
        "power_on_hours": 21457 if hdd else 8123,
        "read_errors_total": 184223611 if hdd else 0,
        "read_errors_uncorrected": 0,
        "write_errors_uncorrected": 0,
        "device_number": device,
        "serial": serial,
        "bus_type": "SATA",
        "media_type": "HDD" if hdd else "SSD",
        "size_bytes": size_gb * 1000**3,
        "removable": False,
        "temperature_max_c": 51 if hdd else 58,
        "smart_attributes": {
            "5": reallocated,
            "187": 0,
            "188": 0,
            "197": 0,
            "198": 0,
            "199": 0,
        },
        "nvme": None,
        "nvme_error": None,
        "paused": False,
    }


def _disk_smart(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": "SMART healthy",
        "disks": rows,
        "truncated": False,
    }


def _gpu_row(
    name: str,
    vendor: str,
    pci_id: str,
    *,
    driver: str,
    uuid: str | None = None,
    bus_id: str | None = None,
    discrete: bool = False,
    width: int = 16,
    utilization: int = 6,
) -> dict[str, Any]:
    """An adapter row. An integrated GPU reports identity and nothing else."""

    row: dict[str, Any] = {
        "name": name,
        "vendor": vendor,
        "pci_id": pci_id,
        "bus_id": bus_id,
        "uuid": uuid,
        "driver_version": driver,
        "sources": ["wmi", "nvidia-smi"] if discrete else ["wmi"],
        "temperature_c": None,
        "utilization_percent": None,
        "power_draw_w": None,
        "power_limit_w": None,
        "fan_target_percent": None,
        "pcie": None,
        "throttle": None,
        "ecc": None,
        "ras": None,
    }
    if discrete:
        row.update(
            {
                "temperature_c": 71 if utilization >= 30 else 46,
                "utilization_percent": utilization,
                "power_draw_w": 112.0 if utilization >= 30 else 21.5,
                "power_limit_w": 115.0,
                "fan_target_percent": 62 if utilization >= 30 else 0,
                "pcie": {"gen_current": 4, "gen_max": 4, "width_current": width, "width_max": 16},
                "throttle": {
                    "hw_slowdown": False,
                    "hw_thermal_slowdown": False,
                    "hw_power_brake_slowdown": False,
                    "sw_thermal_slowdown": False,
                },
            }
        )
    return row


def _gpu(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": f"{len(rows)} GPU(s)",
        "gpus": rows,
        "truncated": False,
        "errors": [],
    }


def _fan(
    key: str, label: str, samples: list[int], duty: float | None, mode: str = "pwm"
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "source": "lhm",
        "rpm_samples": samples,
        "duty_percent": duty,
        "mode": mode,
        "idle_or_absent": False,
    }


def _fans(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": f"{len(rows)} fans read",
        "sources_tried": ["lhm_wmi"],
        "sample_interval_ms": 1000,
        "fans": rows,
        "truncated": False,
        "errors": [],
    }


def _jitter(centre: int, spread: int = 3) -> list[int]:
    """Five tightly grouped samples, the same every time for a given centre."""

    return [centre + d for d in (-spread, spread - 1, 0, -1, spread)]


def _hardware_errors(
    base: datetime, groups: list[dict[str, Any]] | None = None, age: int = 0
) -> dict[str, Any]:
    """A ``hardware_errors`` section as the host reported it ``age`` days ago.

    Each group lists ``days``: ``{days ago: count}`` over the whole demo. The
    section holds the 14 days up to ``age``, as the real collector's rolling
    window does, so the daily rollup rebuilds each day's count from the snapshot
    pushed that day.
    """

    now = base - timedelta(days=age)
    rows: list[dict[str, Any]] = []
    for spec in groups or []:
        seen = {d: n for d, n in spec["days"].items() if d >= age and d - age < 14}
        if not seen:
            continue
        newest = min(seen)
        last = (base - timedelta(days=newest)).replace(hour=11, minute=26, second=0, microsecond=0)
        if newest == 0:
            last = base - timedelta(hours=2)
        rows.append(
            {
                "source": spec["source"],
                "event_id": spec["event_id"],
                "level": spec["level"],
                "count": sum(seen.values()),
                "last_seen": _iso(last),
                "by_day": {
                    (base - timedelta(days=d)).date().isoformat(): n for d, n in sorted(seen.items(), reverse=True)
                },
                "sample": spec["sample"],
                "details": spec["details"],
            }
        )
    total = sum(g["count"] for g in rows)
    return {
        "status": "ok",
        "summary": f"{total} hardware-relevant events in 14d",
        "window_days": 14,
        "effective_window_days": 14,
        "oldest_event_utc": _iso(now - timedelta(days=14)),
        "sources": ["event_log"],
        "groups": rows,
        "truncated": False,
        "truncated_count": 0,
        "app_crashes": {
            "total": 0,
            "distinct_apps": 0,
            "distinct_modules": 0,
            "exception_codes": {},
            "by_day": {},
        },
        "edac": [],
        "aer": [],
        "errors": [],
    }


_WHEA_CORRECTED = {
    "source": "Microsoft-Windows-WHEA-Logger",
    "event_id": 19,
    "level": "warning",
    "sample": "A corrected hardware error has occurred.",
    "details": {
        "error_source": {"Corrected Machine Check": 3},
        "error_type": {"Bus/Interconnect Error": 3},
        "processor_apic_id": {"6": 2, "14": 1},
    },
}
_DISPLAY_TDR = {
    "source": "Display",
    "event_id": 4101,
    "level": "warning",
    "sample": "Display driver nvlddmkm stopped responding and has successfully recovered.",
    "details": {"driver": {"nvlddmkm": 6}},
}


def build_fleet(base: datetime | None = None) -> list[DemoHost]:
    """Return the demo fleet, all timestamps derived from ``base`` (default now)."""

    base = base or datetime.now(timezone.utc)
    hosts: list[DemoHost] = []

    # -- papa-pc: all-green desktop baseline -------------------------------
    papa = _load_base_snapshot()
    papa["disk"] = _disk("C:", 54)
    papa["defender"] = _defender(True, base)
    papa["win_update"] = _win_update_ok(base)
    papa["reboot_pending"] = _no_reboot()
    papa["reliability"] = _reliability(
        [_rel_event("Application Error", 1000, "error", 3, "faulting module chrome.exe")], 8.4
    )
    papa["web_activity"] = _clean_web_activity(base)
    papa["os_support"] = _os_support(
        "Windows 11 Pro", "23H2", "22631",
        cpu=_cpu("GenuineIntel", "13th Gen Intel(R) Core(TM) i7-13700K", 6, 183, 1, "0x12b"),
    )
    papa["hardware_errors"] = _hardware_errors(base)
    # A desktop with LibreHardwareMonitor running: three fans, all steady.
    papa["fans"] = _fans(
        [
            _fan("lhm:/lpc/nct6798d/fan/0", "CPU Fan", _jitter(1180), 45.0),
            _fan("lhm:/lpc/nct6798d/fan/1", "Chassis Fan #1", _jitter(820), None, "auto"),
            _fan("lhm:/lpc/nct6798d/fan/2", "Chassis Fan #2", _jitter(1420), None, "dc"),
        ]
    )
    papa["encryption"] = _encryption(True)
    papa["firewall"] = _firewall(True)
    papa["memory"] = _memory(44, 32)
    papa["uptime"] = _uptime(3.2)
    papa["app_updates"] = _app_updates(0)
    hosts.append(
        DemoHost(
            agent_id="papa-pc",
            meta={"hostname": "papa-pc", "version": "1.4.0", "os": "Windows 11 Pro 23H2"},
            latest=papa,
            disk_series=(52, 54),
        )
    )

    # -- mama-laptop: clean laptop with a battery --------------------------
    mama = _load_base_snapshot()
    mama["disk"] = _disk("C:", 61)
    mama["defender"] = _defender(True, base)
    mama["win_update"] = _win_update_ok(base)
    mama["reboot_pending"] = _no_reboot()
    mama["reliability"] = _reliability(
        [_rel_event("Application Error", 1000, "error", 5, "faulting module Teams.exe")], 7.9
    )
    mama["web_activity"] = _clean_web_activity(base)
    mama["os_support"] = _os_support(
        "Windows 11 Home", "23H2", "22631",
        cpu=_cpu("GenuineIntel", "13th Gen Intel(R) Core(TM) i7-1355U", 6, 186, 3, "0x4121"),
    )
    mama["disk_smart"] = _disk_smart(
        [_nvme_row("KIOXIA XG8 512GB", "Z6RF70AB0A1B", 512, used=6, written=4_200_000)]
    )
    mama["hardware_errors"] = _hardware_errors(base)
    # An integrated GPU reports identity only; a laptop's fans are not readable.
    mama["gpu"] = _gpu(
        [_gpu_row("Intel(R) Iris(R) Xe Graphics", "intel", "8086:a7a1", driver="31.0.101.5186")]
    )
    mama["fans"] = _fans([])
    mama["encryption"] = _encryption(True)
    mama["firewall"] = _firewall(True)
    mama["memory"] = _memory(51, 16)
    mama["uptime"] = _uptime(1.4)
    mama["app_updates"] = _app_updates(2)
    mama["battery"] = _battery(72, 84)
    hosts.append(
        DemoHost(
            agent_id="mama-laptop",
            meta={"hostname": "mama-laptop", "version": "1.4.0", "os": "Windows 11 Home 23H2"},
            latest=mama,
            disk_series=(58, 61),
            battery_series=(88, 84),
        )
    )

    # -- kid-pc: flagged web_activity (parental controls) ------------------
    kid = _load_base_snapshot()
    kid["disk"] = _disk("C:", 66)
    kid["defender"] = _defender(True, base)
    kid["win_update"] = _win_update_ok(base)
    kid["reboot_pending"] = _no_reboot()
    kid["reliability"] = _reliability(
        [_rel_event("Application Error", 1000, "error", 8, "faulting module game.exe")], 7.1
    )
    kid["web_activity"] = _flagged_web_activity(base)
    kid["os_support"] = _os_support(
        "Windows 11 Home", "23H2", "22631",
        cpu=_cpu("AuthenticAMD", "AMD Ryzen 5 5600X 6-Core Processor", 25, 33, 2, "0xa201016"),
    )
    kid["disk_smart"] = _disk_smart(
        [_nvme_row("WD_BLACK SN770 1TB", "23154L802341", 1000, used=9, written=11_800_000)]
    )
    kid["fans"] = _fans(
        [_fan("lhm:/lpc/it8628e/fan/0", "CPU Fan", _jitter(1320), 50.0)]
    )

    def _kid_hardware(snap: dict[str, Any], age: int) -> None:
        # A gaming card that used to hold an x16 link under load and, for the
        # last week, negotiates x8; its driver resets climb with it.
        snap["gpu"] = _gpu(
            [
                _gpu_row(
                    "NVIDIA GeForce RTX 4060", "nvidia", "10de:2882",
                    driver="560.94", uuid="GPU-9a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9",
                    bus_id="0000:01:00.0", discrete=True, width=16 if age >= 7 else 8,
                    utilization=78,
                )
            ]
        )
        snap["hardware_errors"] = _hardware_errors(
            base, [{**_DISPLAY_TDR, "days": {20: 1, 11: 1, 4: 1, 2: 2, 0: 3}}], age
        )

    _kid_hardware(kid, 0)
    kid["encryption"] = _encryption(True)
    kid["firewall"] = _firewall(True)
    kid["memory"] = _memory(62, 16)
    kid["uptime"] = _uptime(0.6)
    kid["app_updates"] = _app_updates(1)
    hosts.append(
        DemoHost(
            agent_id="kid-pc",
            meta={"hostname": "kid-pc", "version": "1.4.0", "os": "Windows 11 Home 23H2"},
            latest=kid,
            disk_series=(64, 66),
            webfilter=_kid_webfilter(base),
            evolve=_kid_hardware,
        )
    )

    # -- study-pc: disk critical + <30d forecast ---------------------------
    study = _load_base_snapshot()
    study["disk"] = _disk("C:", 96)
    study["defender"] = _defender(True, base)
    study["win_update"] = _win_update_ok(base)
    study["reboot_pending"] = _no_reboot()
    study["reliability"] = _reliability(
        [
            _rel_event("Application Error", 1000, "error", 14, "faulting module ntdll.dll"),
            _rel_event("disk", 51, "error", 8, "An error was detected on \\Device\\Harddisk0"),
        ],
        5.6,
    )
    study["web_activity"] = _clean_web_activity(base)
    study["os_support"] = _os_support(
        "Windows 11 Pro", "23H2", "22631",
        cpu=_cpu("GenuineIntel", "12th Gen Intel(R) Core(TM) i5-12400", 6, 151, 2, "0x3c"),
    )
    study["hardware_errors"] = _hardware_errors(base)
    study["gpu"] = _gpu(
        [_gpu_row("Intel(R) UHD Graphics 730", "intel", "8086:4688", driver="31.0.101.4502")]
    )

    def _study_hardware(snap: dict[str, Any], age: int) -> None:
        # The system NVMe drive has been climbing towards its rated endurance, and
        # the CPU fan has slowed at the same duty for the last week.
        used = round(87 + (29 - age) * 4 / 29)
        snap["disk_smart"] = _disk_smart(
            [
                _nvme_row(
                    "Samsung SSD 970 EVO Plus 500GB", "S4EVNX0N123456P", 500,
                    used=used, written=41_000_000,
                ),
                _sata_row("WDC WD10EZEX-08WN4A0", "WD-WCC6Y1234567", 1000, hdd=True, device=1),
            ]
        )
        cpu_fan = 1650 if age >= 7 else 1440 - (6 - age) * 10
        snap["fans"] = _fans(
            [
                _fan("lhm:/lpc/nct6687d/fan/0", "CPU Fan", _jitter(cpu_fan), 60.0),
                _fan("lhm:/lpc/nct6687d/fan/1", "Chassis Fan #1", _jitter(900), None, "auto"),
            ]
        )

    _study_hardware(study, 0)
    study["encryption"] = _encryption(True)
    study["firewall"] = _firewall(True)
    study["memory"] = _memory(88, 8)
    study["uptime"] = _uptime(11.3)
    study["app_updates"] = _app_updates(3)
    hosts.append(
        DemoHost(
            agent_id="study-pc",
            meta={"hostname": "study-pc", "version": "1.4.0", "os": "Windows 11 Pro 23H2"},
            latest=study,
            # Rising disk drives a positive slope -> a <30-day (and <14-day) forecast.
            disk_series=(82, 96),
            evolve=_study_hardware,
        )
    )

    # -- living-room-pc: reboot pending + failed/ pending Windows update ---
    living = _load_base_snapshot()
    living["disk"] = _disk("C:", 73)
    living["defender"] = _defender(True, base)
    living["win_update"] = {
        "status": "warn",
        "summary": "1 update failed in last 30 days",
        "last_check": _iso(base - timedelta(hours=9)),
        "recent": [
            {
                "kb": "KB5037853",
                "title": "2026-05 Cumulative Update",
                "result": "succeeded",
                "installed_at": _iso(base - timedelta(days=20)),
            },
            {
                "kb": "KB5039211",
                "title": "2026-06 Cumulative Update",
                "result": "failed",
                "installed_at": _iso(base - timedelta(days=2)),
            },
        ],
    }
    living["reboot_pending"] = {
        "status": "warn",
        "summary": "Reboot required (Windows Update)",
        "pending": True,
        "reasons": ["WindowsUpdate"],
    }
    living["reliability"] = _reliability(
        [
            _rel_event("Service Control Manager", 7034, "error", 7, "The Print Spooler terminated"),
            _rel_event(
                "Microsoft-Windows-WindowsUpdateClient", 20, "error", 5, "Update failed 0x80070..."
            ),
        ],
        6.3,
    )
    living["web_activity"] = _clean_web_activity(base)
    living["os_support"] = _os_support(
        "Windows 11 Home", "23H2", "22631",
        cpu=_cpu("GenuineIntel", "Intel(R) Core(TM) i3-10100 CPU @ 3.60GHz", 6, 165, 3, "0xf8"),
    )
    living["disk_smart"] = _disk_smart(
        [
            _sata_row("CT500MX500SSD1", "2145E5B4A1C2", 500, hdd=False, device=0),
            _sata_row("ST1000DM010-2EP102", "Z9A0X1QW", 1000, hdd=True, device=1),
        ]
    )
    living["gpu"] = _gpu(
        [_gpu_row("Intel(R) UHD Graphics 630", "intel", "8086:9bc8", driver="31.0.101.2125")]
    )
    # The chassis fan's speed wanders at a constant setting: a warn on `fans`.
    living["fans"] = _fans(
        [
            _fan("lhm:/lpc/it8686e/fan/0", "CPU Fan", _jitter(1250), 50.0),
            _fan("lhm:/lpc/it8686e/fan/1", "Chassis Fan #1", [1210, 830, 1260, 790, 1230], 55.0),
        ]
    )

    def _living_hardware(snap: dict[str, Any], age: int) -> None:
        # Now and then a corrected machine-check on the processor interconnect.
        snap["hardware_errors"] = _hardware_errors(
            base, [{**_WHEA_CORRECTED, "days": {9: 1, 5: 1, 1: 1}}], age
        )

    _living_hardware(living, 0)
    living["encryption"] = _encryption(True)
    living["firewall"] = _firewall(True)
    living["memory"] = _memory(70, 16)
    living["uptime"] = _uptime(29.4)
    living["app_updates"] = _app_updates(7)
    hosts.append(
        DemoHost(
            agent_id="living-room-pc",
            meta={"hostname": "living-room-pc", "version": "1.3.2", "os": "Windows 11 Home 23H2"},
            latest=living,
            disk_series=(71, 73),
            evolve=_living_hardware,
        )
    )

    # -- grandpa-pc: Defender real-time OFF + end-of-life OS ---------------
    grandpa = _load_base_snapshot()
    grandpa["disk"] = _disk("C:", 77)
    grandpa["defender"] = _defender(False, base)
    grandpa["win_update"] = _win_update_ok(base)
    grandpa["reboot_pending"] = _no_reboot()
    grandpa["reliability"] = _reliability(
        [
            _rel_event("Application Error", 1000, "error", 18, "faulting module explorer.exe"),
            _rel_event("Microsoft-Windows-Kernel-Power", 41, "critical", 12, "unexpected shutdown"),
            # issue #166: a single noisy-but-suppressed pattern must not drown
            # out the two events above once RELIABILITY_SUPPRESSIONS is seeded.
            _rel_event(
                "Microsoft-Windows-CAPI2", 4176, "error", 3439,
                "PFX operation failed as AuthSafes count doesn't lie in expected "
                "range. Maximum permissible value: 200. Erroneous value: 202.",
            ),
        ],
        4.2,
    )
    grandpa["web_activity"] = _clean_web_activity(base)
    grandpa["os_support"] = _os_support(
        "Windows 10 Pro", "22H2", "19045", eol=True,
        # An affected Raptor Lake desktop part on microcode older than 0x12B.
        cpu=_cpu("GenuineIntel", "13th Gen Intel(R) Core(TM) i7-13700K", 6, 183, 1, "0x129"),
    )
    grandpa["hardware_errors"] = _hardware_errors(base)
    grandpa["gpu"] = _gpu(
        [
            _gpu_row(
                "NVIDIA GeForce GTX 1660 SUPER", "nvidia", "10de:21c4",
                driver="552.22", uuid="GPU-c7d8e9f0-a1b2-4c3d-8e4f-5a6b7c8d9e0f",
                bus_id="0000:01:00.0", discrete=True, utilization=3,
            )
        ]
    )
    grandpa["fans"] = _fans([])

    def _grandpa_hardware(snap: dict[str, Any], age: int) -> None:
        # The old hard disk began replacing damaged sectors ten days ago.
        reallocated = 0 if age >= 10 else 8 if age >= 4 else 16
        snap["disk_smart"] = _disk_smart(
            [
                _sata_row(
                    "ST1000DM010-2EP102", "Z9A0X1QW", 1000, hdd=True, device=0,
                    reallocated=reallocated,
                )
            ]
        )

    _grandpa_hardware(grandpa, 0)
    grandpa["encryption"] = _encryption(False)
    grandpa["firewall"] = _firewall(False)
    grandpa["memory"] = _memory(77, 8)
    grandpa["uptime"] = _uptime(6.8)
    grandpa["app_updates"] = _app_updates(4)
    grandpa["defender_quarantine"] = {
        "status": "warn",
        "summary": "1 item in quarantine",
        "items": [
            {"name": "Trojan:Win32/Wacatac", "path": "C:\\Users\\opa\\Downloads\\setup.exe"}
        ],
    }
    hosts.append(
        DemoHost(
            agent_id="grandpa-pc",
            meta={"hostname": "grandpa-pc", "version": "1.2.9", "os": "Windows 10 Pro 22H2"},
            latest=grandpa,
            disk_series=(75, 77),
            evolve=_grandpa_hardware,
        )
    )

    return hosts


def _clean_web_activity(base: datetime) -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": "38 domains observed (24h)",
        "window_hours": 24,
        "sources": ["dns_cache", "browser_history"],
        "domains": [
            {
                "domain": "wikipedia.org",
                "first_seen": _iso(base - timedelta(hours=8)),
                "last_seen": _iso(base - timedelta(hours=1)),
                "hits": 12,
                "sources": ["dns_cache", "browser_history"],
            }
        ],
        "truncated": False,
        "browser_profiles_read": 2,
        "errors": [],
    }


def _flagged_web_activity(base: datetime) -> dict[str, Any]:
    """web_activity payload with a ``flagged`` array so the health rule crits.

    ``flagged`` is the server-internal annotation (ADR-0024) the live tunnel
    would add via ``WebFilterService.record_activity``; we bypass the tunnel and
    write it directly. A serious category (``seed``/``custom``) within 24h -> crit.
    """

    return {
        "status": "warn",
        "summary": "51 domains observed (24h), 3 flagged",
        "window_hours": 24,
        "sources": ["dns_cache", "browser_history"],
        "domains": [
            {
                "domain": "roblox.com",
                "first_seen": _iso(base - timedelta(hours=5)),
                "last_seen": _iso(base - timedelta(hours=1)),
                "hits": 34,
                "sources": ["dns_cache", "browser_history"],
            }
        ],
        "flagged": [
            {
                "domain": "adult-example.com",
                "category": "seed",
                "matched_entry": "adult-example.com",
                "first_seen": _iso(base - timedelta(hours=4)),
                "last_seen": _iso(base - timedelta(hours=2)),
            },
            {
                "domain": "proxy-unblock.example",
                "category": "custom",
                "matched_entry": "proxy-unblock.example",
                "first_seen": _iso(base - timedelta(hours=6)),
                "last_seen": _iso(base - timedelta(hours=3)),
            },
            {
                "domain": "warez-example.net",
                "category": "seed",
                "matched_entry": "warez-example.net",
                "first_seen": _iso(base - timedelta(hours=7)),
                "last_seen": _iso(base - timedelta(hours=1)),
            },
        ],
        "flagged_count_24h": 3,
        "truncated": False,
        "browser_profiles_read": 3,
        "errors": [],
    }


def _kid_webfilter(base: datetime) -> dict[str, Any]:
    """Parental-controls store state for kid-pc (config + custom list + events)."""

    return {
        "config": {
            "enabled": True,
            "block_mode": True,
            "use_external_adult": True,
            "use_bypass_protection": True,
            "doh_policy": "disable",
        },
        "domains": [
            {"domain": "proxy-unblock.example", "action": "block", "note": "known VPN bypass"},
            {"domain": "roblox.com", "action": "watch", "note": "time-limit review"},
            {"domain": "khanacademy.org", "action": "allow", "note": "school"},
        ],
        "events": [
            {
                "domain": "adult-example.com",
                "first_seen": _iso(base - timedelta(hours=4)),
                "last_seen": _iso(base - timedelta(hours=2)),
                "hits": 5,
                "sources": ["dns_cache"],
                "flagged": True,
                "category": "seed",
            },
            {
                "domain": "proxy-unblock.example",
                "first_seen": _iso(base - timedelta(hours=6)),
                "last_seen": _iso(base - timedelta(hours=3)),
                "hits": 9,
                "sources": ["dns_cache", "browser_history"],
                "flagged": True,
                "category": "custom",
            },
            {
                "domain": "warez-example.net",
                "first_seen": _iso(base - timedelta(hours=7)),
                "last_seen": _iso(base - timedelta(hours=1)),
                "hits": 3,
                "sources": ["browser_history"],
                "flagged": True,
                "category": "seed",
            },
            {
                "domain": "roblox.com",
                "first_seen": _iso(base - timedelta(hours=5)),
                "last_seen": _iso(base - timedelta(hours=1)),
                "hits": 34,
                "sources": ["dns_cache", "browser_history"],
                "flagged": False,
                "category": None,
            },
        ],
    }


def _lerp(series: tuple[float, float], frac: float) -> float:
    start, end = series
    return start + (end - start) * frac


def history_snapshots(
    host: DemoHost, base: datetime | None = None, days: int = HISTORY_DAYS
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(collected_at_iso, snapshot)`` daily points, oldest-first.

    The final point (today) is the host's full ``latest`` snapshot; earlier days
    are deep copies with only the disk/ battery values walked back along their
    linear series, so the fleet trend, disk-fill forecast, and battery trend all
    have a coherent multi-day history to fit. A host's ``evolve`` hook then
    rewrites the hardware sections the same way for that day.
    """

    base = base or datetime.now(timezone.utc)
    out: list[tuple[str, dict[str, Any]]] = []
    for i in range(days):
        age = days - 1 - i
        day = base - timedelta(days=age)
        collected_at = _iso(day.replace(hour=12, minute=0, second=0, microsecond=0))
        if age == 0:
            out.append((_iso(base), copy.deepcopy(host.latest)))
            continue
        snap = copy.deepcopy(host.latest)
        frac = i / (days - 1) if days > 1 else 1.0
        if host.disk_series is not None and isinstance(snap.get("disk"), dict):
            pct = _lerp(host.disk_series, frac)
            snap["disk"] = _disk("C:", pct)
        if host.battery_series is not None and isinstance(snap.get("battery"), dict):
            health = _lerp(host.battery_series, frac)
            snap["battery"] = _battery(72, health)
        if host.evolve is not None:
            host.evolve(snap, age)
        out.append((collected_at, snap))
    return out

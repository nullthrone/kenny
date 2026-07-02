"""Health-rule assertions against the golden telemetry snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kenny_server import health_rules

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
# Evaluate "as of" a fixed time so age-based rules are deterministic.
NOW = datetime(2026, 6, 4, 18, 30, tzinfo=timezone.utc)


def _snapshot() -> dict:
    frame = json.loads((FIXTURES_DIR / "telemetry_snapshot.json").read_text())
    return frame["snapshot"]


def test_snapshot_section_statuses() -> None:
    result = health_rules.evaluate_snapshot(_snapshot(), now=NOW)
    sections = result["sections"]
    assert sections["disk"]["status"] == "warn"
    assert sections["defender"]["status"] == "crit"
    assert sections["win_update"]["status"] == "warn"
    assert sections["reboot_pending"]["status"] == "warn"
    # reliability: 48 events (>=15) with no critical group -> warn, content reason.
    assert sections["reliability"]["status"] == "warn"
    assert "×" in sections["reliability"]["reason"]


def test_snapshot_overall_is_crit() -> None:
    result = health_rules.evaluate_snapshot(_snapshot(), now=NOW)
    assert result["overall"] == "crit"


def test_disk_thresholds() -> None:
    crit = health_rules.evaluate_section(
        "disk", {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 95}]},
        now=NOW,
    )
    warn = health_rules.evaluate_section(
        "disk", {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 85}]},
        now=NOW,
    )
    ok = health_rules.evaluate_section(
        "disk", {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 50}]},
        now=NOW,
    )
    assert crit["status"] == "crit"
    assert warn["status"] == "warn"
    assert ok["status"] == "ok"


def test_os_support_eol() -> None:
    crit = health_rules.evaluate_section(
        "os_support", {"status": "ok", "summary": "", "eol": True}, now=NOW
    )
    assert crit["status"] == "crit"


def test_thermals_thresholds() -> None:
    def _eval(temps: list[float]) -> dict:
        sensors = [{"label": f"zone{i}", "temperature_c": t} for i, t in enumerate(temps)]
        return health_rules.evaluate_section(
            "thermals", {"status": "ok", "summary": "", "sensors": sensors}, now=NOW
        )

    assert _eval([40.0, 97.0])["status"] == "crit"
    assert _eval([40.0, 88.0])["status"] == "warn"
    assert _eval([40.0, 61.0])["status"] == "ok"


def test_thermals_no_sensors_defers_to_agent() -> None:
    # With no sensors the rule defers, so the agent-reported status passes through.
    result = health_rules.evaluate_section(
        "thermals", {"status": "ok", "summary": "no temperature sensors", "sensors": []}, now=NOW
    )
    assert result["status"] == "ok"
    assert "reason" not in result


def test_reliability_content_reason_and_thresholds() -> None:
    def _eval(payload: dict) -> dict:
        return health_rules.evaluate_section(
            "reliability", {"status": "ok", "summary": "", **payload}, now=NOW
        )

    events = [
        {"source": "Application Error", "event_id": 1000, "level": "error", "count": 30,
         "category": "App crash / hang"},
        {"source": "disk", "event_id": 51, "level": "error", "count": 18,
         "category": "Disk & storage"},
    ]
    warn = _eval({"recent_crashes": 48, "events": events})
    assert warn["status"] == "warn"
    # The reason names the biggest categories by count, not a bare number.
    assert warn["reason"].startswith("App crash / hang ×30")
    assert "Disk & storage ×18" in warn["reason"]

    # A critical-level group escalates to warn even with a small total.
    crit_evt = [{"source": "Kernel-Power", "event_id": 41, "level": "critical", "count": 2,
                 "category": "Power & boot"}]
    assert _eval({"recent_crashes": 2, "events": crit_evt})["status"] == "warn"

    # Large total -> crit; low stability index -> crit.
    assert _eval({"recent_crashes": 60, "events": events})["status"] == "crit"
    assert _eval({"recent_crashes": 3, "stability_index": 2.0})["status"] == "crit"

    # Quiet host -> ok.
    assert _eval({"recent_crashes": 4, "events": []})["status"] == "ok"


def test_reliability_falls_back_to_source_without_category() -> None:
    # Before annotation runs (or with no API key), the reason uses the raw source.
    result = health_rules.evaluate_section(
        "reliability",
        {"status": "ok", "summary": "", "recent_crashes": 20,
         "events": [{"source": "Ntfs", "event_id": 55, "level": "error", "count": 20}]},
        now=NOW,
    )
    assert result["status"] == "warn"
    assert "Ntfs ×20" in result["reason"]


def test_reliability_defers_when_no_fields() -> None:
    result = health_rules.evaluate_section(
        "reliability", {"status": "warn", "summary": "collector unavailable"}, now=NOW
    )
    assert result["status"] == "warn"
    assert "reason" not in result


def test_worst_of() -> None:
    assert health_rules.worst("ok", "warn", "crit") == "crit"
    assert health_rules.worst("ok", "warn") == "warn"
    assert health_rules.worst("ok", "ok") == "ok"


def test_listening_ports_remote_access_warn() -> None:
    exposed = health_rules.evaluate_section(
        "listening_ports",
        {
            "status": "ok",
            "summary": "",
            "ports": [
                {"proto": "tcp", "port": 3389, "address": "0.0.0.0", "pid": 1204, "process": "svchost"},
                {"proto": "tcp", "port": 445, "address": "0.0.0.0", "pid": 4, "process": "System"},
            ],
        },
        now=NOW,
    )
    assert exposed["status"] == "warn"
    assert "3389" in exposed["reason"]

    loopback_only = health_rules.evaluate_section(
        "listening_ports",
        {
            "status": "ok",
            "summary": "",
            "ports": [{"proto": "tcp", "port": 3389, "address": "127.0.0.1", "pid": 1, "process": "x"}],
        },
        now=NOW,
    )
    assert loopback_only["status"] == "ok"


def test_local_accounts_rules() -> None:
    def account(**kw):
        base = {
            "name": "u", "enabled": True, "is_admin": False,
            "password_required": True, "builtin_admin": False, "builtin_guest": False,
        }
        base.update(kw)
        return base

    crit = health_rules.evaluate_section(
        "local_accounts",
        {"status": "ok", "summary": "", "accounts": [account(is_admin=True, password_required=False)]},
        now=NOW,
    )
    assert crit["status"] == "crit"

    warn_admin = health_rules.evaluate_section(
        "local_accounts",
        {"status": "ok", "summary": "", "accounts": [account(name="Administrator", builtin_admin=True)]},
        now=NOW,
    )
    assert warn_admin["status"] == "warn"

    # A disabled built-in Guest is the healthy default.
    ok = health_rules.evaluate_section(
        "local_accounts",
        {"status": "ok", "summary": "", "accounts": [account(name="Guest", enabled=False, builtin_guest=True)]},
        now=NOW,
    )
    assert ok["status"] == "ok"


def test_backup_status_no_evidence_warn() -> None:
    bare = health_rules.evaluate_section(
        "backup_status",
        {
            "status": "ok",
            "summary": "",
            "restore_points": {"enabled": False, "count": 0, "latest": None},
            "file_history": {"service_state": "stopped", "configured": None},
            "onedrive": {"installed": False, "running": False},
        },
        now=NOW,
    )
    assert bare["status"] == "warn"
    assert "no backup evidence" in bare["reason"]

    # Any single living mechanism is enough to defer to the agent status.
    onedrive_ok = health_rules.evaluate_section(
        "backup_status",
        {
            "status": "ok",
            "summary": "",
            "restore_points": {"enabled": False, "count": 0, "latest": None},
            "file_history": {"service_state": "stopped", "configured": None},
            "onedrive": {"installed": True, "running": True},
        },
        now=NOW,
    )
    assert onedrive_ok["status"] == "ok"

    recent_rp = health_rules.evaluate_section(
        "backup_status",
        {
            "status": "ok",
            "summary": "",
            "restore_points": {"enabled": True, "count": 3, "latest": "2026-06-02T11:30:00Z"},
            "file_history": {"service_state": "stopped", "configured": None},
            "onedrive": {"installed": False, "running": False},
        },
        now=NOW,
    )
    assert recent_rp["status"] == "ok"


def test_net_quality_rules() -> None:
    crit = health_rules.evaluate_section(
        "net_quality",
        {
            "status": "ok",
            "summary": "",
            "gateway": {"host": "192.168.1.1", "latency_ms": 2.0, "loss_percent": 0},
            "reference": {"host": "1.1.1.1", "latency_ms": None, "loss_percent": 80},
        },
        now=NOW,
    )
    assert crit["status"] == "crit"
    assert "internet degraded" in crit["reason"]

    warn = health_rules.evaluate_section(
        "net_quality",
        {
            "status": "ok",
            "summary": "",
            "gateway": {"host": "192.168.1.1", "latency_ms": 250.0, "loss_percent": 0},
            "reference": {"host": "1.1.1.1", "latency_ms": 30.0, "loss_percent": 0},
        },
        now=NOW,
    )
    assert warn["status"] == "warn"

    ok = health_rules.evaluate_section(
        "net_quality",
        {
            "status": "ok",
            "summary": "",
            "gateway": {"host": "192.168.1.1", "latency_ms": 2.0, "loss_percent": 0},
            "reference": {"host": "1.1.1.1", "latency_ms": 14.0, "loss_percent": 0},
        },
        now=NOW,
    )
    assert ok["status"] == "ok"

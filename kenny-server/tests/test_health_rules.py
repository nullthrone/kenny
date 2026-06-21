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


def test_worst_of() -> None:
    assert health_rules.worst("ok", "warn", "crit") == "crit"
    assert health_rules.worst("ok", "warn") == "warn"
    assert health_rules.worst("ok", "ok") == "ok"

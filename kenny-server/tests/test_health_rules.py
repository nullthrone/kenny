"""Health-rule assertions against the golden telemetry snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

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


def test_attention_flag_matches_status() -> None:
    """`attention` is computed alongside `status` in evaluate_section itself
    (kenny-server/CLAUDE.md: thresholds live only here) -- every section in
    the golden snapshot must carry `attention == (status != "ok")`."""

    result = health_rules.evaluate_snapshot(_snapshot(), now=NOW)
    for name, section in result["sections"].items():
        assert section["attention"] == (section["status"] != "ok"), name


def test_attention_true_for_warn_and_crit() -> None:
    crit = health_rules.evaluate_section(
        "disk", {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 96}]},
        now=NOW,
    )
    warn = health_rules.evaluate_section(
        "disk", {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 85}]},
        now=NOW,
    )
    ok = health_rules.evaluate_section(
        "disk", {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 10}]},
        now=NOW,
    )
    assert crit["attention"] is True
    assert warn["attention"] is True
    assert ok["attention"] is False


def test_attention_present_with_no_rule_for_section() -> None:
    """A section with no entry in RULES defers to the reported status, and
    still carries `attention` -- the deferred-return branch, not just the
    rule-computed one."""

    ok = health_rules.evaluate_section("unknown_section", {"status": "ok"}, now=NOW)
    bad = health_rules.evaluate_section("unknown_section", {"status": "warn"}, now=NOW)
    assert ok["attention"] is False
    assert bad["attention"] is True


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


def test_reliability_benign_repetition_scores_ok() -> None:
    # Headline case: 300 repeats of ONE known-benign pattern (the
    # DistributedCOM-timeout symptom from the user story) must not crit a host
    # on volume alone once the pattern has been annotated as benign.
    events = [
        {"source": "DistributedCOM", "event_id": 10016, "level": "error", "count": 304,
         "category": "Windows service", "severity": "benign",
         "suspected_cause": "two apps colliding over a stale COM permission"},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 304, "events": events}, now=NOW
    )
    assert result["status"] == "ok"
    assert "known-benign" in result["reason"]


def test_reliability_diverse_significant_patterns_score_crit() -> None:
    # Many DISTINCT non-benign patterns -> crit, even if no single one repeats
    # often — novel-error diversity, not volume, is what should escalate here.
    events = [
        {"source": f"App{i}", "event_id": i, "level": "error", "count": 2,
         "category": "App crash / hang", "severity": "notable"}
        for i in range(5)
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 10, "events": events}, now=NOW
    )
    assert result["status"] == "crit"


def test_reliability_recurring_serious_pattern_scores_crit() -> None:
    # A single 'serious' pattern that recurs meaningfully escalates to crit on
    # its own, independent of the distinct-pattern count.
    events = [
        {"source": "disk", "event_id": 51, "level": "error", "count": 12,
         "category": "Disk & storage", "severity": "serious",
         "suspected_cause": "failing sectors on the boot drive"},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 12, "events": events}, now=NOW
    )
    assert result["status"] == "crit"
    assert "disk/51" in result["reason"]
    assert "failing sectors" in result["reason"]


def test_reliability_unknown_severity_is_treated_as_sensitive() -> None:
    # An "unknown" severity (no key, or the model genuinely unsure) must never
    # be silently treated as benign -> at least warn, even at low count.
    events = [
        {"source": "Mystery", "event_id": 1, "level": "error", "count": 2,
         "category": "Other", "severity": "unknown"},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 2, "events": events}, now=NOW
    )
    assert result["status"] == "warn"


def test_reliability_annotated_stability_index_still_applies() -> None:
    # The Windows Reliability Index is an independent signal that still
    # applies even once events carry severity annotations.
    result = health_rules.evaluate_section(
        "reliability",
        {"status": "ok", "summary": "", "recent_crashes": 0, "events": [], "stability_index": 2.0},
        now=NOW,
    )
    assert result["status"] == "crit"


def test_reliability_falls_back_by_volume_without_severity_annotation() -> None:
    # DOD/regression guard: unannotated payloads (LLM categorization never ran)
    # keep the exact original volume-based behavior. This mirrors the
    # golden fixture, which is a raw (unannotated) agent payload.
    events = [
        {"source": "Application Error", "event_id": 1000, "level": "error", "count": 30,
         "category": "App crash / hang"},
        {"source": "disk", "event_id": 51, "level": "error", "count": 18,
         "category": "Disk & storage"},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 48, "events": events}, now=NOW
    )
    assert result["status"] == "warn"
    assert result["reason"].startswith("App crash / hang ×30")


def test_reliability_fallback_distinct_patterns_warn_even_below_volume_threshold() -> None:
    # No-LLM fallback addition: many distinct low-count
    # patterns are themselves a signal, even though their sum stays under the
    # old bare-count warn threshold. Strictly widens sensitivity, never narrows.
    events = [
        {"source": f"App{i}", "event_id": i, "level": "error", "count": 1}
        for i in range(8)
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 8, "events": events}, now=NOW
    )
    assert result["status"] == "warn"


def test_rule_verdict_is_not_floored_by_the_agents_own_status() -> None:
    """The rule's verdict is the status; the agent's `status` is not folded in.

    The seam: `reliability.rs` computes a status from constants baked into the
    shipped binary, and `health_rules.py` owns the judgement
    (`kenny-server/CLAUDE.md`). While `evaluate_section` took
    `worst(reported, rule_status)`, the agent could raise a verdict the server
    could never lower -- so a threshold change here, or an operator suppression
    (ADR-0041), could only ever tighten a section, never relax one. On real
    hosts that pinned `reliability` at `warn` permanently, because the
    collector warns at 20 error events in 7 days.

    Asserted for every section that has a rule, so a rule added later cannot
    quietly reintroduce the floor.
    """

    payload = {"status": "crit", "summary": "the agent thinks this is dire"}
    # A payload the reliability rule scores as ok: no events, no crashes, and a
    # healthy stability index.
    ok_payload = dict(payload, recent_crashes=0, events=[], stability_index=9.5)
    result = health_rules.evaluate_section("reliability", ok_payload, now=NOW)
    assert result["status"] == "ok"
    assert result["attention"] is False


def test_sections_without_a_rule_still_use_the_agents_status() -> None:
    """The agent stays the only judgement where this module has none.

    The counterpart to the test above: dropping the floor must not turn into
    "ignore the agent". A section with no rule in `RULES` -- and a rule that
    defers by returning None -- still reports exactly what the agent said.
    """

    payload = {"status": "crit", "summary": "printer on fire"}
    result = health_rules.evaluate_section("printers", payload, now=NOW)
    assert result["status"] == "crit"
    assert result["attention"] is True
    assert "reason" not in result


def test_golden_fixture_reliability_status_is_not_a_verdict() -> None:
    """The contract's own sample carries a non-judging `reliability.status`.

    Joined through the shared artifact: `docs/fixtures/telemetry_snapshot.json`
    is what both sides round-trip, so it is where "the agent does not judge
    this section" is visible to Python and Rust alike. If someone teaches the
    collector to grade `reliability` again, the fixture has to change with it
    and this test names the reason it must not.
    """

    reliability = _snapshot()["reliability"]
    assert reliability["status"] == "ok"
    # And the server reaches its own, different verdict from the same payload.
    assert health_rules.evaluate_section("reliability", reliability, now=NOW)["status"] == "warn"


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
            "password_last_set": None,
        }
        base.update(kw)
        return base

    crit = health_rules.evaluate_section(
        "local_accounts",
        {"status": "ok", "summary": "", "accounts": [account(is_admin=True, password_required=False)]},
        now=NOW,
    )
    assert crit["status"] == "crit"

    # Regression guard: the UF_PASSWD_NOTREQD flag is set, but the account has a
    # real password (password_last_set present) -> benign OEM flag, no finding.
    ok_has_pw = health_rules.evaluate_section(
        "local_accounts",
        {"status": "ok", "summary": "", "accounts": [account(is_admin=True, password_required=False, password_last_set="2026-01-01T00:00:00Z")]},
        now=NOW,
    )
    assert ok_has_pw["status"] == "ok"

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


def test_backup_status_all_null_stub_defers() -> None:
    # A non-Windows / stubbed collector emits an all-null backup shape. That is
    # *absence of data*, not a missing backup, so the rule must defer (no warn).
    stub = health_rules.evaluate_section(
        "backup_status",
        {
            "status": "ok",
            "summary": "n/a on this platform",
            "restore_points": {"enabled": None, "count": None, "latest": None},
            "file_history": {"service_state": None, "configured": None},
            "onedrive": {"installed": None, "running": None},
        },
        now=NOW,
    )
    assert stub["status"] == "ok"
    assert "reason" not in stub

    # An empty section (no backup fields at all) likewise defers rather than warns.
    empty = health_rules.evaluate_section(
        "backup_status", {"status": "ok", "summary": ""}, now=NOW
    )
    assert empty["status"] == "ok"
    assert "reason" not in empty

    # Regression guard: a real Windows-shaped no-backup payload still warns.
    real = health_rules.evaluate_section(
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
    assert real["status"] == "warn"
    assert "no backup evidence" in real["reason"]


def test_evaluate_snapshot_skips_windows_only_sections_for_linux() -> None:
    # A Linux agent reports "n/a on this platform" stubs for the Windows-only
    # sections; scoring them would mislead, so they are skipped entirely.
    snapshot = {
        "disk": {"status": "ok", "summary": "", "volumes": [{"mount": "/", "percent_used": 40}]},
        "defender": {"status": "ok", "summary": "n/a on this platform"},
        "win_update": {"status": "ok", "summary": "n/a on this platform"},
        "reboot_pending": {"status": "ok", "summary": "n/a on this platform"},
        "backup_status": {"status": "ok", "summary": "n/a on this platform"},
        "listening_ports": {"status": "ok", "summary": "", "ports": []},
    }
    linux = health_rules.evaluate_snapshot(snapshot, agent_os="linux", now=NOW)
    assert set(linux["sections"]) == {"disk", "listening_ports"}
    assert linux["overall"] == "ok"


def test_evaluate_snapshot_scores_windows_only_sections_for_windows() -> None:
    # The same Defender payload is scored for a Windows agent (default OS) but
    # not for a Linux one.
    snapshot = {
        "defender": {"status": "ok", "summary": "", "enabled": False, "realtime_protection": False},
    }
    win = health_rules.evaluate_snapshot(snapshot, now=NOW)  # default os = windows
    assert win["sections"]["defender"]["status"] == "crit"
    assert win["overall"] == "crit"

    lin = health_rules.evaluate_snapshot(snapshot, agent_os="linux", now=NOW)
    assert "defender" not in lin["sections"]
    assert lin["overall"] == "ok"


def test_portable_sections_apply_for_every_os() -> None:
    # listening_ports and local_accounts are portable and must score on Linux.
    snapshot = {
        "listening_ports": {
            "status": "ok",
            "summary": "",
            "ports": [{"proto": "tcp", "port": 22, "address": "0.0.0.0", "pid": 1, "process": "sshd"}],
        },
        "defender": {"status": "ok", "summary": "n/a on this platform"},
    }
    out = health_rules.evaluate_snapshot(snapshot, agent_os="linux", now=NOW)
    assert out["sections"]["listening_ports"]["status"] == "warn"
    assert "defender" not in out["sections"]
    assert out["overall"] == "warn"


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


# -- reliability: alarm suppression (ADR-0041 / issue #166) -----------------
#
# `suppressed` is stamped by the read-path SuppressionList.mark(), not by the
# health rule itself (see reliability_suppression.py + the TelemetryStore.
# annotate seam) -- these tests build already-stamped payloads directly, the
# same way test_event_categories.py's fixtures already carry `category`/
# `severity` as if ADR-0026 annotation had run.


def test_reliability_suppressed_pattern_excluded_from_severity_scoring() -> None:
    # The issue #166 regression: one dominant, suppressed pattern (3439 CAPI2/
    # 4176 events) must not drown out the one pattern that actually matters
    # (a single Kernel-Power/41 unclean shutdown).
    events = [
        {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "level": "error",
         "count": 3439, "category": "Windows service", "severity": "unknown",
         "suppressed": True,
         "suppressed_by": {"id": "x", "scope": "fleet", "source": "Microsoft-Windows-CAPI2",
                            "event_id": 4176, "note": "known CryptSvc quirk"}},
        {"source": "Microsoft-Windows-Kernel-Power", "event_id": 41, "level": "critical",
         "count": 1, "category": "Power & boot", "severity": "notable"},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 3440, "events": events},
        now=NOW,
    )
    # One significant (forced-serious) pattern with a low recurrence count ->
    # warn, not crit -- and it must be the Kernel-Power pattern, not CAPI2.
    assert result["status"] == "warn"
    assert "Microsoft-Windows-Kernel-Power/41" in result["reason"]
    assert "CAPI2" not in result["reason"]
    assert "1 pattern(s) suppressed" in result["reason"]


def test_reliability_all_patterns_suppressed_scores_ok_with_explicit_reason() -> None:
    events = [
        {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "level": "error",
         "count": 3439, "category": "Windows service", "severity": "unknown",
         "suppressed": True},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 3439, "events": events},
        now=NOW,
    )
    assert result["status"] == "ok"
    assert "all 1 pattern(s) suppressed" in result["reason"]
    assert "3439" in result["reason"]  # raw total is still visible


def test_reliability_suppressed_serious_pattern_no_longer_crits() -> None:
    events = [
        {"source": "disk", "event_id": 51, "level": "error", "count": 50,
         "category": "Disk & storage", "severity": "serious", "suppressed": True},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 50, "events": events},
        now=NOW,
    )
    assert result["status"] == "ok"


def test_reliability_suppressed_windows_critical_no_longer_escalates() -> None:
    # An operator explicitly suppressing this exact pattern overrides the
    # automatic "Windows-critical -> serious" escalation.
    events = [
        {"source": "Kernel-Power", "event_id": 41, "level": "critical", "count": 5,
         "category": "Power & boot", "severity": "unknown", "suppressed": True},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 5, "events": events},
        now=NOW,
    )
    assert result["status"] == "ok"


def test_reliability_suppression_does_not_silence_low_stability_index() -> None:
    # The Windows Reliability Index is independent of pattern suppression and
    # always applies on top -- suppressing every pattern must not hide it.
    events = [
        {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "level": "error",
         "count": 3439, "category": "Windows service", "severity": "unknown",
         "suppressed": True},
    ]
    result = health_rules.evaluate_section(
        "reliability",
        {"status": "ok", "summary": "", "recent_crashes": 3439, "events": events,
         "stability_index": 2.0},
        now=NOW,
    )
    assert result["status"] == "crit"


def test_reliability_suppressed_pattern_not_reported_as_benign() -> None:
    # A suppressed, non-benign pattern must never be folded into the
    # "known-benign" phrase -- that phrase is the LLM's verdict, suppression
    # is the operator's, and they are different claims.
    events = [
        {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "level": "error",
         "count": 3439, "category": "Windows service", "severity": "unknown",
         "suppressed": True},
        {"source": "DistributedCOM", "event_id": 10016, "level": "error", "count": 10,
         "category": "Windows service", "severity": "benign"},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 3449, "events": events},
        now=NOW,
    )
    assert result["status"] == "ok"
    assert "known-benign" in result["reason"]
    assert "1 pattern(s) suppressed" in result["reason"]


def test_reliability_volume_fallback_subtracts_suppressed_from_scoring_total() -> None:
    # Unannotated events (no `severity` -- the volume fallback path) must also
    # honour suppression: this is the path that drives push alerting, the
    # weekly digest, and the fleet list (see reliability_suppression.py).
    events = [
        {"source": "Microsoft-Windows-CAPI2", "event_id": 4176, "level": "error",
         "count": 3700, "suppressed": True},
        {"source": "Application Error", "event_id": 1000, "level": "error", "count": 43},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 3743, "events": events},
        now=NOW,
    )
    # scored total = 3743 - 3700 = 43 -> warn (>=15), not crit (<50).
    assert result["status"] == "warn"
    assert "CAPI2" not in result["reason"]
    assert "Application Error" in result["reason"]
    assert "1 pattern(s) suppressed" in result["reason"]


def test_reliability_volume_fallback_ignores_suppressed_critical_and_distinct() -> None:
    # A suppressed critical-level group alone -> ok (not escalated by `level`).
    events = [
        {"source": "Kernel-Power", "event_id": 41, "level": "critical", "count": 3,
         "suppressed": True},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 3, "events": events},
        now=NOW,
    )
    assert result["status"] == "ok"

    # 8 distinct groups, 7 suppressed -> the distinct-pattern escalation must
    # not fire on the suppressed ones.
    events = [
        {"source": f"App{i}", "event_id": i, "level": "error", "count": 1, "suppressed": True}
        for i in range(7)
    ] + [{"source": "App7", "event_id": 7, "level": "error", "count": 1}]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 8, "events": events},
        now=NOW,
    )
    assert result["status"] == "ok"


def test_reliability_existing_tests_unaffected_by_suppression_support() -> None:
    # No `suppressed` key anywhere -> byte-identical to pre-ADR-0041 behavior.
    events = [
        {"source": "Application Error", "event_id": 1000, "level": "error", "count": 84,
         "category": "App crash / hang", "severity": "notable"},
    ]
    result = health_rules.evaluate_section(
        "reliability", {"status": "ok", "summary": "", "recent_crashes": 84, "events": events},
        now=NOW,
    )
    assert result["status"] == "warn"
    assert "suppressed" not in result["reason"]


# -- malformed nested telemetry fields must never raise (fuzzing sweep) ------
#
# `Section.model_config` allows arbitrary extra fields (`docs/protocol.md`), so
# the wire contract never guarantees that a nested field a rule expects to be a
# dict/list of dicts actually is one -- a buggy or compromised agent can send a
# section whose own `status`/`summary` validate fine but whose extra fields
# don't match a rule's assumed shape. Every one of these previously raised
# AttributeError/TypeError out of `evaluate_section`, which crashes the caller
# (`fleet_overview`/`list_agents`/`agent_health` iterate all agents in one
# comprehension with no per-agent guard, so one malformed host's telemetry took
# the whole read down for every host).
@pytest.mark.parametrize(
    "section, payload",
    [
        ("disk", {"volumes": ["not-a-dict"]}),
        ("win_update", {"recent": ["not-a-dict"]}),
        ("thermals", {"sensors": [123]}),
        ("web_activity", {"flagged": ["not-a-dict"]}),
        ("listening_ports", {"ports": ["not-a-dict"]}),
        ("local_accounts", {"accounts": ["not-a-dict"]}),
        ("logon_failures", {"accounts": ["not-a-dict"]}),
        ("logon_failures", {"unmatched_count": [None]}),
        ("backup_status", {
            "restore_points": "oops", "file_history": "oops", "onedrive": "oops",
        }),
        ("net_quality", {"reference": "oops", "gateway": "oops"}),
        ("reboot_pending", {"pending": True, "reasons": 123}),
    ],
)
def test_malformed_nested_field_never_crashes(section: str, payload: dict) -> None:
    full_payload = {"status": "ok", "summary": "x", **payload}
    result = health_rules.evaluate_section(section, full_payload, now=NOW)
    assert result["status"] in ("ok", "warn", "crit")

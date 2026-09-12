"""Health-rule assertions against the golden telemetry snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
    # reliability: two unclassified patterns, each active on 3 of 7 days and
    # last seen on the fixture's own day -> warn (never crit without a
    # `serious` verdict), finding-shaped reason.
    assert sections["reliability"]["status"] == "warn"
    assert "×" in sections["reliability"]["reason"]


def test_snapshot_overall_is_crit() -> None:
    result = health_rules.evaluate_snapshot(_snapshot(), now=NOW)
    assert result["overall"] == "crit"


def test_non_dict_section_value_is_treated_as_unusable() -> None:
    """A pushed `telemetry` frame's `Section` is pydantic-validated (always a
    dict), but a `telemetry_collect` request/response round trip stores its
    `Response.result` (`dict[str, Any]`, unvalidated) the same way -- so a
    compromised/buggy agent can make a top-level section value anything JSON
    allows. That must defer to the agent-reported status (like any other
    unusable field on this module), not raise."""

    for bad_value in (None, "not a dict", 123, True, ["a", "b"], 1e400):
        result = health_rules.evaluate_snapshot({"disk": bad_value}, now=NOW)
        assert result["sections"]["disk"]["status"] == "ok"


def test_attention_flag_matches_status() -> None:
    """`attention` and `tier` are computed alongside `status` in
    evaluate_section itself (kenny-server/CLAUDE.md: thresholds live only
    here) -- every section in the golden snapshot must carry
    `attention == (status in {warn, crit})` and a matching tier. A posture
    section (the fixture's RDP listener) is not attention."""

    result = health_rules.evaluate_snapshot(_snapshot(), now=NOW)
    for name, section in result["sections"].items():
        assert section["attention"] == (section["status"] in ("warn", "crit")), name
        assert section["tier"] == health_rules.tier_of(section["status"]), name
    assert result["sections"]["listening_ports"]["status"] == "posture"
    assert result["sections"]["listening_ports"]["attention"] is False


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


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), int("9" * 320)])
def test_disk_non_finite_or_oversized_percent_does_not_crash(bad: float) -> None:
    """``percent_used`` is an unvalidated agent-reported field. A JSON int too large
    to represent as a float used to crash `_rule_disk` at `float(pct)`/the `:.0f`
    format (`OverflowError`), and Infinity/NaN (which Python's `json` module
    accepts on decode) compared fine but were never guarded either.
    """

    result = health_rules.evaluate_section(
        "disk", {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": bad}]},
        now=NOW,
    )
    assert result["status"] in ("ok", "warn", "crit")


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), int("9" * 320)])
def test_battery_and_memory_non_finite_or_oversized_percent_does_not_crash(bad: float) -> None:
    battery = health_rules.evaluate_section(
        "battery", {"status": "ok", "summary": "", "health_percent": bad}, now=NOW
    )
    assert battery["status"] in ("ok", "warn", "crit")
    memory = health_rules.evaluate_section(
        "memory", {"status": "ok", "summary": "", "percent_used": bad}, now=NOW
    )
    assert memory["status"] in ("ok", "warn", "crit")


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


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), int("9" * 320)])
def test_thermals_non_finite_or_oversized_temperature_does_not_crash(bad: float) -> None:
    result = health_rules.evaluate_section(
        "thermals",
        {"status": "ok", "summary": "", "sensors": [{"label": "zone0", "temperature_c": bad}]},
        now=NOW,
    )
    assert result["status"] in ("ok", "warn", "crit")


def test_thermals_no_sensors_defers_to_agent() -> None:
    # With no sensors the rule defers, so the agent-reported status passes through.
    result = health_rules.evaluate_section(
        "thermals", {"status": "ok", "summary": "no temperature sensors", "sensors": []}, now=NOW
    )
    assert result["status"] == "ok"
    assert "reason" not in result


# -- reliability: activity- and persistence-based scoring (ADR-0058) ---------
#
# The rule reads three per-pattern booleans derived from the `by_day` /
# `last_seen` evidence the agent sends (`active`, `recurring`, `burst`) plus
# the ADR-0026 severity. There is no count threshold: these tests build
# payloads that would have tripped the old volume / distinct-pattern rules
# and assert the verdict now follows what is *still happening*.


def _day(offset: int) -> str:
    """Calendar day ``offset`` days before NOW, as the agent's ``by_day`` key."""

    return (NOW - timedelta(days=offset)).date().isoformat()


def _pattern(
    source: str,
    event_id: int,
    *,
    days: dict[int, int],
    severity: str | None = None,
    level: str = "error",
    last_seen_hours_ago: float | None = None,
    **extra: object,
) -> dict:
    """One reliability event group. ``days`` maps day-offset -> count;
    ``last_seen`` defaults to the end of the most recent day."""

    by_day = {_day(off): n for off, n in days.items()}
    e: dict = {
        "source": source,
        "event_id": event_id,
        "level": level,
        "count": sum(days.values()),
        "by_day": by_day,
    }
    if last_seen_hours_ago is not None:
        e["last_seen"] = (NOW - timedelta(hours=last_seen_hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if severity is not None:
        e["severity"] = severity
    e.update(extra)
    return e


def _eval_reliability(events: list[dict], **fields: object) -> dict:
    payload = {
        "status": "ok",
        "summary": "",
        "recent_crashes": sum(int(e.get("count", 0)) for e in events),
        "window_days": 7,
        "events": events,
        **fields,
    }
    return health_rules.evaluate_section("reliability", payload, now=NOW)


def test_reliability_burst_days_ago_scores_ok() -> None:
    # The thomas-pc reboot storm: 80 DCOM errors on one day a week ago, quiet
    # since. Under the old rule 80 >= 50 was crit on its own.
    events = [
        _pattern("Microsoft-Windows-DistributedCOM", 10010, days={7: 80}, severity="notable",
                 last_seen_hours_ago=7 * 24),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert f"quiet since {_day(7)}" in result["reason"]
    pattern = result["details"]["patterns"][0]
    assert pattern["burst"] is True
    assert pattern["active"] is False
    assert pattern["recurring"] is False


def test_reliability_one_off_notable_patterns_score_ok() -> None:
    # Five distinct one-off errors from the same afternoon five days ago --
    # every Windows PC produces this in a week. The old "≥5 distinct
    # non-benign patterns -> crit" rule is the thing this test kills.
    events = [
        _pattern(f"App{i}", i, days={5: 1}, severity="notable", last_seen_hours_ago=5 * 24)
        for i in range(5)
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "5 historical pattern(s)" in result["reason"]


def test_reliability_active_recurring_unknown_scores_warn() -> None:
    # An unclassifiable pattern (no key, or the model unsure) that keeps
    # coming back is worth a look -- never silently benign (ADR-0026) ...
    recurring = [
        _pattern("Mystery", 1, days={1: 1, 0: 2}, severity="unknown", last_seen_hours_ago=1),
    ]
    assert _eval_reliability(recurring)["status"] == "warn"
    # ... but the same pattern seen on a single day is a one-off, even if it
    # was seen an hour ago.
    one_off = [_pattern("Mystery", 1, days={0: 3}, severity="unknown", last_seen_hours_ago=1)]
    assert _eval_reliability(one_off)["status"] == "ok"


def test_reliability_active_serious_scores_crit() -> None:
    events = [
        _pattern("disk", 51, days={2: 6, 1: 7, 0: 5}, severity="serious",
                 last_seen_hours_ago=9, suspected_cause="failing sectors on the boot drive"),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "crit"
    assert result["reason"].startswith("disk/51 ×18, 3 of 7 days, last seen 9h ago")
    assert "failing sectors" in result["reason"]


def test_reliability_inactive_serious_scores_warn() -> None:
    # A Kernel-Power/41 three days ago is still a finding (warn), just not an
    # active one (crit). It self-clears when it leaves the 7-day window.
    events = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={3: 1}, severity="serious",
                 last_seen_hours_ago=3 * 24),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "warn"
    assert "Microsoft-Windows-Kernel-Power/41" in result["reason"]
    assert "last seen 3d ago" in result["reason"]


def test_reliability_windows_critical_level_is_serious_unless_suppressed() -> None:
    # A Windows-critical entry counts as serious whatever the LLM said ...
    active_critical = [
        _pattern("Kernel-Power", 41, days={1: 1, 0: 1}, severity="unknown", level="critical",
                 last_seen_hours_ago=2),
    ]
    assert _eval_reliability(active_critical)["status"] == "crit"
    # ... unless the operator suppressed that exact pattern (ADR-0041):
    # explicit intent overrides the automatic escalation.
    suppressed = [dict(active_critical[0], suppressed=True)]
    assert _eval_reliability(suppressed)["status"] == "ok"


def test_reliability_unannotated_payload_never_crits_on_count_alone() -> None:
    # No `severity` anywhere (LLM never ran, no key): every pattern is
    # `unknown`, which can reach warn when active and recurring but never
    # crit -- a count alone is not a critical finding. 12 patterns × 200
    # events would have been crit twice over under the old volume rule.
    events = [
        _pattern(f"Src{i}", 100 + i, days={2: 50, 1: 50, 0: 100}, last_seen_hours_ago=1)
        for i in range(12)
    ]
    result = _eval_reliability(events)
    assert result["status"] == "warn"
    assert result["reason"].startswith("Src0/100 ×200, 3 of 7 days, last seen 1h ago")
    assert "+9 more active pattern(s)" in result["reason"]


def test_reliability_reason_falls_back_to_source_without_category() -> None:
    # Before annotation runs (or with no API key), the reason names the raw
    # source/event id and says the cause is unclear rather than inventing one.
    events = [_pattern("Ntfs", 55, days={2: 5, 1: 5, 0: 10}, last_seen_hours_ago=1)]
    result = _eval_reliability(events)
    assert result["status"] == "warn"
    assert "Ntfs/55 ×20" in result["reason"]
    assert "cause unclear" in result["reason"]


def test_reliability_reason_names_active_patterns_with_cadence_and_age() -> None:
    # The live thomas-pc shape: one suppressed firehose, one reboot burst, one
    # genuinely active pattern, a handful of one-offs. The reason must lead
    # with the finding, not with "3528 error/critical events".
    events = [
        _pattern("Microsoft-Windows-CAPI2", 4176, days={i: 480 for i in range(7)},
                 severity="unknown", last_seen_hours_ago=1, suppressed=True),
        _pattern("Microsoft-Windows-DistributedCOM", 10010, days={7: 80}, severity="benign",
                 last_seen_hours_ago=7 * 24),
        _pattern("Microsoft-Windows-DeviceAssociationService", 3503,
                 days={7: 19, 6: 1, 2: 2, 1: 29, 0: 1}, severity="notable", last_seen_hours_ago=0.5,
                 suspected_cause="Device pairing service cannot discover or enumerate endpoints"),
        _pattern("Universal Print", 1, days={7: 2}, severity="notable", last_seen_hours_ago=7 * 24),
        _pattern("Volsnap", 25, days={7: 1}, severity="notable", last_seen_hours_ago=7 * 24),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "warn"
    reason = result["reason"]
    assert reason.startswith(
        "Microsoft-Windows-DeviceAssociationService/3503 ×52, 5 of 7 days, last seen <1h ago"
        " — Device pairing service cannot discover or enumerate endpoints"
    )
    assert f"2 historical pattern(s) quiet since {_day(7)}" in reason
    assert reason.endswith("(1 pattern(s) suppressed)")
    assert "CAPI2" not in reason
    assert not reason[0].isdigit()


def test_reliability_details_carry_per_pattern_activity() -> None:
    # Consumers (dashboard chip, `agent_health`) read activity off `details`
    # instead of re-deriving thresholds -- and the shared `events` list the
    # heatmap draws from is never mutated.
    events = [
        _pattern("disk", 51, days={2: 6, 1: 7, 0: 5}, severity="serious", last_seen_hours_ago=9),
        _pattern("App", 1000, days={6: 4}, severity="notable", last_seen_hours_ago=6 * 24),
    ]
    result = _eval_reliability(events)
    details = result["details"]
    assert details["window_days"] == 7
    by_source = {p["source"]: p for p in details["patterns"]}
    assert by_source["disk"] == {
        "source": "disk", "event_id": 51, "level": "error", "count": 18, "severity": "serious",
        "category": None, "cause": None, "suppressed": False, "active_days": 3,
        "first_day": _day(2), "last_day": _day(0), "last_seen_age_hours": 9.0,
        "active": True, "recurring": True, "burst": False,
    }
    assert by_source["App"]["active"] is False
    assert by_source["App"]["burst"] is True
    assert "active" not in events[0] and "last_seen_age_hours" not in events[0]


def test_reliability_activity_is_relative_to_now() -> None:
    # The same payload judged ten days later -- past its own 7-day window --
    # is history: this is why history reads must evaluate "as of" the
    # snapshot's own `collected_at`. (Three active days in the window would
    # otherwise keep it "active" forever.)
    events = [
        _pattern("disk", 51, days={2: 6, 1: 7, 0: 5}, severity="notable", last_seen_hours_ago=9),
    ]
    payload = {"status": "ok", "summary": "", "recent_crashes": 18, "window_days": 7, "events": events}
    assert health_rules.evaluate_section("reliability", payload, now=NOW)["status"] == "warn"
    later = NOW + timedelta(days=10)
    assert health_rules.evaluate_section("reliability", payload, now=later)["status"] == "ok"


def test_reliability_by_day_stands_in_for_a_missing_last_seen() -> None:
    # A group without `last_seen` (older agents, hand-built payloads) still
    # gets an age from the end of its most recent `by_day` day: NOW is 18:30,
    # so "last seen yesterday" is 18.5h ago.
    events = [_pattern("disk", 51, days={2: 1, 1: 1}, severity="serious")]
    result = _eval_reliability(events)
    assert result["status"] == "crit"
    assert result["details"]["patterns"][0]["last_seen_age_hours"] == 18.5


def test_reliability_benign_repetition_scores_ok() -> None:
    # 300 repeats of ONE known-benign pattern, active every day, must not
    # warn on volume or persistence: benign is benign (ADR-0026).
    events = [
        _pattern("DistributedCOM", 10016, days={i: 43 for i in range(7)}, severity="benign",
                 last_seen_hours_ago=1, category="Windows service",
                 suspected_cause="two apps colliding over a stale COM permission"),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "known-benign" in result["reason"]


def test_reliability_quiet_host_reasons() -> None:
    assert _eval_reliability([])["reason"] == "no error patterns in 7d"
    assert _eval_reliability([])["status"] == "ok"
    # Historical and benign patterns are both named in the calm reason.
    events = [
        _pattern("App", 1000, days={6: 4}, severity="notable", last_seen_hours_ago=6 * 24),
        _pattern("DistributedCOM", 10016, days={0: 3}, severity="benign", last_seen_hours_ago=1),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert result["reason"] == f"no active error patterns; 1 historical pattern(s) quiet since {_day(6)}, 1 known-benign"


def test_reliability_annotated_stability_index_still_applies() -> None:
    # The Windows Reliability Index is an independent signal that still
    # applies on top of pattern scoring, on a host with no patterns at all.
    result = _eval_reliability([], stability_index=2.0)
    assert result["status"] == "crit"
    assert _eval_reliability([], stability_index=5.0)["status"] == "warn"


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


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), int("9" * 320)])
def test_number_rejects_non_finite_and_oversized_values(bad: float) -> None:
    """`_number` is the coercion every rule above (and `_reliability_reason`,
    `_cadence_label`, the accounts/backup rules) applies to unvalidated telemetry
    fields. A JSON int too large for `float()` (`OverflowError`) and the
    `Infinity`/`NaN` decode extension must both come back as None, not raise.
    """

    assert health_rules._number(bad) is None


def test_number_still_coerces_real_numbers() -> None:
    assert health_rules._number(42) == 42.0
    assert health_rules._number(3.5) == 3.5
    assert health_rules._number(True) is None
    assert health_rules._number("42") is None


def test_worst_of() -> None:
    assert health_rules.worst("ok", "warn", "crit") == "crit"
    assert health_rules.worst("ok", "warn") == "warn"
    assert health_rules.worst("ok", "ok") == "ok"


def test_listening_ports_remote_access_is_posture() -> None:
    # A remote-access listener is how the machine is set up, not something
    # that happened: listed and aged, never alarmed on (ADR-0058).
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
    assert exposed["status"] == "posture"
    assert exposed["attention"] is False
    assert exposed["tier"] == "posture"
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
    assert out["sections"]["listening_ports"]["status"] == "posture"
    assert "defender" not in out["sections"]
    # Posture never rolls up: a host whose only finding is posture is ok.
    assert out["overall"] == "ok"


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


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), int("9" * 320)])
def test_net_quality_non_finite_or_oversized_metrics_does_not_crash(bad: float) -> None:
    result = health_rules.evaluate_section(
        "net_quality",
        {
            "status": "ok",
            "summary": "",
            "gateway": {"host": "192.168.1.1", "latency_ms": bad, "loss_percent": bad},
            "reference": {"host": "1.1.1.1", "latency_ms": bad, "loss_percent": bad},
        },
        now=NOW,
    )
    assert result["status"] in ("ok", "warn", "crit")


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


def test_reliability_suppression_applies_without_annotation() -> None:
    # Unannotated events (no `severity` -- the LLM never ran) must also honour
    # suppression: with the classification persisted (ADR-0058) this is a
    # rare state, but a fresh install without a key lives in it permanently.
    events = [
        _pattern("Microsoft-Windows-CAPI2", 4176, days={i: 480 for i in range(7)},
                 last_seen_hours_ago=1, suppressed=True),
        _pattern("Application Error", 1000, days={2: 10, 1: 20, 0: 13}, last_seen_hours_ago=2),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "warn"
    assert "CAPI2" not in result["reason"]
    assert result["reason"].startswith("Application Error/1000 ×43")
    assert "1 pattern(s) suppressed" in result["reason"]

    # A suppressed critical-level group alone -> ok (not escalated by `level`).
    events = [
        _pattern("Kernel-Power", 41, days={1: 2, 0: 1}, level="critical", last_seen_hours_ago=1,
                 suppressed=True),
    ]
    assert _eval_reliability(events)["status"] == "ok"


def test_reliability_existing_tests_unaffected_by_suppression_support() -> None:
    # No `suppressed` key anywhere -> the reason carries no suppression clause.
    events = [
        _pattern("Application Error", 1000, days={2: 30, 1: 30, 0: 24}, severity="notable",
                 last_seen_hours_ago=1, category="App crash / hang"),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "warn"
    assert "suppressed" not in result["reason"]


# -- the posture tier and the sections it covers (ADR-0058) -----------------


def test_posture_never_rolls_up_to_overall() -> None:
    # `max` keeps the first of equally-ranked candidates, so a posture section
    # listed first would leak into `overall` unless worst() maps it back.
    assert health_rules.worst("posture") == "ok"
    assert health_rules.worst("posture", "ok") == "ok"
    assert health_rules.worst("ok", "posture") == "ok"
    assert health_rules.worst("posture", "warn") == "warn"
    assert health_rules.worst("crit", "posture") == "crit"
    snapshot = {
        "encryption": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "protection_status": 0}]},
        "disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 10}]},
    }
    out = health_rules.evaluate_snapshot(snapshot, now=NOW)
    assert out["sections"]["encryption"]["status"] == "posture"
    assert out["overall"] == "ok"


def test_tier_of_and_attention_for_every_status() -> None:
    assert health_rules.tier_of("crit") == "incident"
    assert health_rules.tier_of("warn") == "incident"
    assert health_rules.tier_of("posture") == "posture"
    assert health_rules.tier_of("ok") == "none"
    assert health_rules.tier_of("unknown") == "none"


def test_services_windows_auto_stopped_is_posture() -> None:
    # The live thomas-pc list: every "auto service stopped" is a trigger-start
    # or updater service idling by design. Posture, not a warning.
    stopped = ["amd3dvcacheSvc", "AsusUpdateCheck", "edgeupdate", "GoogleUpdaterInternalService152",
               "GoogleUpdaterService152", "gpsvc", "MapsBroker", "PrismaAccessBrowserUpdater",
               "PrismaAccessBrowserUpdaterInternal", "sppsvc"]
    services = [{"name": n, "display": n, "start": "Auto", "status": "Stopped"} for n in stopped]
    services += [{"name": "Dhcp", "display": "DHCP", "start": "Auto", "status": "Running"},
                 {"name": "BITS", "display": "BITS", "start": "Manual", "status": "Stopped"}]
    result = health_rules.evaluate_section(
        "services", {"status": "ok", "summary": "", "services": services}, now=NOW
    )
    assert result["status"] == "posture"
    assert result["reason"].startswith("10 auto-start service(s) not running (e.g. amd3dvcacheSvc, AsusUpdateCheck, edgeupdate, +7 more)")
    # "Auto (Delayed Start)" counts as auto-start too.
    delayed = [{"name": "X", "start": "Auto (Delayed Start)", "status": "Stopped"}]
    assert health_rules.evaluate_section("services", {"status": "ok", "summary": "", "services": delayed}, now=NOW)["status"] == "posture"
    running = [{"name": "Dhcp", "start": "Auto", "status": "Running"}]
    assert health_rules.evaluate_section("services", {"status": "ok", "summary": "", "services": running}, now=NOW)["status"] == "ok"
    # Nothing reported -> the agent's own status stands ("services unavailable").
    assert "reason" not in health_rules.evaluate_section("services", {"status": "ok", "summary": "", "services": []}, now=NOW)


def test_services_linux_failed_unit_is_warn() -> None:
    failed = [{"name": "nginx.service", "display": "nginx.service", "status": "failed", "start": ""}]
    result = health_rules.evaluate_section(
        "services", {"status": "ok", "summary": "", "services": failed}, now=NOW, agent_os="linux"
    )
    assert result["status"] == "warn"
    assert "nginx.service" in result["reason"]


def test_encryption_unprotected_system_drive_is_posture_and_skipped_on_linux() -> None:
    payload = {"status": "ok", "summary": "", "volumes": [
        {"mount": "D:", "protection_status": 1}, {"mount": "C:\\", "protection_status": 0}]}
    result = health_rules.evaluate_section("encryption", payload, now=NOW)
    assert result["status"] == "posture"
    assert result["reason"] == "C: not BitLocker-protected"
    encrypted = {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "protection_status": 1}]}
    assert health_rules.evaluate_section("encryption", encrypted, now=NOW)["status"] == "ok"
    # "BitLocker state unavailable" carries no volumes -> defer, never "encrypted".
    assert "reason" not in health_rules.evaluate_section("encryption", {"status": "ok", "summary": "", "volumes": []}, now=NOW)
    out = health_rules.evaluate_snapshot({"encryption": payload}, agent_os="linux", now=NOW)
    assert "encryption" not in out["sections"]


def test_printers_offline_is_ok_with_reason() -> None:
    payload = {"status": "ok", "summary": "", "printers": [
        {"name": "HP OfficeJet", "status": "Offline"}, {"name": "PDF", "status": "Normal"}]}
    result = health_rules.evaluate_section("printers", payload, now=NOW)
    assert result["status"] == "ok"
    assert result["reason"] == "1 of 2 printer(s) offline/error (HP OfficeJet)"


def test_time_sync_rules() -> None:
    def _eval(**fields: object) -> dict:
        return health_rules.evaluate_section(
            "time_sync", {"status": "ok", "summary": "", **fields}, now=NOW
        )

    assert _eval(synchronized=True, source="time.windows.com", offset_secs=0.01)["status"] == "ok"
    assert _eval(synchronized=False, source="Local CMOS Clock", offset_secs=None)["status"] == "warn"
    big = _eval(synchronized=True, source="time.windows.com", offset_secs=-42.5)
    assert big["status"] == "warn" and "42.50" in big["reason"]
    # No reading at all (service not responding / no time service): not a finding.
    assert "reason" not in _eval(synchronized=None, source=None, offset_secs=None)


def test_uptime_windows_30d_is_posture_linux_is_ok() -> None:
    month = {"status": "ok", "summary": "", "uptime_secs": 31 * 86_400, "boot_time_unix": 0}
    win = health_rules.evaluate_section("uptime", month, now=NOW)
    assert win["status"] == "posture"
    assert win["reason"].startswith("up 31d")
    assert health_rules.evaluate_section("uptime", month, now=NOW, agent_os="linux")["status"] == "ok"
    fresh = {"status": "ok", "summary": "", "uptime_secs": 5 * 86_400}
    assert health_rules.evaluate_section("uptime", fresh, now=NOW)["status"] == "ok"


def _wu(kb: str, at: str, result: str = "failed") -> dict:
    return {"kb": kb, "title": f"2026-08 Sicherheitsupdate ({kb})", "result": result, "installed_at": at}


def test_win_update_repeated_failure_over_days_is_crit() -> None:
    # The live linus-pc payload: two KBs retried every ~4h for three days.
    recent = [
        _wu("KB5121003", "2026-06-04T15:46:00Z"), _wu("KB5120708", "2026-06-04T15:46:00Z"),
        _wu("KB5121003", "2026-06-04T11:46:00Z"), _wu("KB5120708", "2026-06-04T11:46:00Z"),
        _wu("KB5121003", "2026-06-03T19:46:00Z"), _wu("KB5120708", "2026-06-03T19:46:00Z"),
        _wu("KB5121003", "2026-06-02T23:46:00Z"), _wu("KB890830", "2026-06-02T12:00:00Z"),
        _wu("KB5037853", "2026-05-15T04:00:00Z", "succeeded"),
    ]
    result = health_rules.evaluate_section(
        "win_update", {"status": "ok", "summary": "", "last_check": "2026-06-04T16:00:00Z", "recent": recent}, now=NOW
    )
    assert result["status"] == "crit"
    assert result["reason"] == (
        "KB5121003 failed 4× since 2026-06-02 (last 3h ago), "
        "KB5120708 failed 3× since 2026-06-03 (last 3h ago), +1 more"
    )
    failed = result["details"]["failed"]
    assert [f["kb"] for f in failed] == ["KB5121003", "KB5120708", "KB890830"]
    assert failed[0]["attempts"] == 4 and failed[0]["days"] == 3


def test_win_update_single_failure_is_warn_and_stale_check_warns() -> None:
    once = [_wu("KB5039211", "2026-06-02T04:00:00Z")]
    result = health_rules.evaluate_section(
        "win_update", {"status": "ok", "summary": "", "last_check": "2026-06-03T09:00:00Z", "recent": once}, now=NOW
    )
    assert result["status"] == "warn"
    assert result["reason"] == "KB5039211 failed 1× since 2026-06-02 (last 3d ago)"
    # The same KB failing three times on one day is still a warn: not yet recurrence.
    same_day = [_wu("KB1", f"2026-06-04T{h:02d}:00:00Z") for h in (1, 5, 9)]
    assert health_rules.evaluate_section("win_update", {"status": "ok", "summary": "", "recent": same_day}, now=NOW)["status"] == "warn"
    stale = health_rules.evaluate_section(
        "win_update", {"status": "ok", "summary": "", "last_check": "2026-05-20T09:00:00Z", "recent": []}, now=NOW
    )
    assert stale["status"] == "warn" and stale["reason"] == "no update check for 15d"
    healthy = health_rules.evaluate_section(
        "win_update", {"status": "ok", "summary": "", "last_check": "2026-06-03T09:00:00Z", "recent": []}, now=NOW
    )
    assert healthy["status"] == "ok" and healthy["details"] == {"failed": []}


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
        ("reliability", {"events": ["not-a-dict"], "recent_crashes": "many"}),
        ("reliability", {"events": [{"source": 1, "event_id": "x", "count": "3",
                                     "by_day": ["2026-06-04"], "last_seen": 5}]}),
        ("reliability", {"events": [{"by_day": {"not-a-date": 1, "2026-06-04": "2"}}]}),
        ("services", {"services": ["not-a-dict", {"start": 1, "status": None}]}),
        ("encryption", {"volumes": ["not-a-dict", {"mount": 3}]}),
        ("printers", {"printers": [{"status": 12}, "x"]}),
        ("time_sync", {"synchronized": "yes", "offset_secs": "far"}),
        ("uptime", {"uptime_secs": "long"}),
        ("win_update", {"recent": [{"kb": None, "result": "failed", "installed_at": 12}], "last_check": 5}),
    ],
)
def test_malformed_nested_field_never_crashes(section: str, payload: dict) -> None:
    full_payload = {"status": "ok", "summary": "x", **payload}
    result = health_rules.evaluate_section(section, full_payload, now=NOW)
    assert result["status"] in ("ok", "posture", "warn", "crit")

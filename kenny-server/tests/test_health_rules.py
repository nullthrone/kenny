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
    # reliability: the fixture carries no read-path annotation, so neither
    # of its two patterns has an established user-visible impact and neither
    # is a finding. That is the point of the impact model -- an unclassified
    # event log is not evidence of a problem -- and the reason says it was
    # looked at rather than going silent.
    assert sections["reliability"]["status"] == "ok"
    # ... and the reason does not claim health it has not established: the
    # fixture's patterns are unjudged, and it says so.
    assert sections["reliability"]["reason"] == (
        "no verdict yet on 2 pattern(s) in 7d [2 awaiting classification]"
    )


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


# -- reliability: scored on user-visible impact ------------------------------
#
# A finding is something the person at the machine would have noticed. The
# Windows Error/Critical log is mostly internal component chatter Windows
# itself tolerates, so these payloads deliberately include the shapes that
# used to produce red -- a suppressed firehose, a shutdown-time VSS cluster, a
# one-off "serious" event -- and assert they produce nothing at all.


def _day(offset: int) -> str:
    """Calendar day ``offset`` days before NOW, as the agent's ``by_day`` key."""

    return (NOW - timedelta(days=offset)).date().isoformat()


def _pattern(
    source: str,
    event_id: int,
    *,
    days: dict[int, int],
    severity: str | None = None,
    impact: str | None = None,
    symptom: str = "",
    state: str | None = "classified",
    level: str = "error",
    last_seen_hours_ago: float | None = None,
    **extra: object,
) -> dict:
    """One annotated reliability event group. ``days`` maps day-offset ->
    count; ``last_seen`` defaults to the end of the most recent day.

    ``impact``/``symptom``/``state`` stand in for the read-path annotation
    (event_categories.mark), the same way the payloads here have always
    carried ``severity`` as if the classifier had run.
    """

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
    if impact is not None:
        e["user_impact"] = impact
    if symptom:
        e["symptom"] = symptom
    if state is not None:
        e["classification_state"] = state
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


def test_reliability_internal_chatter_is_never_a_finding() -> None:
    # The live thomas-pc shape: a 3373-event certificate-service firehose, a
    # 260-event device-pairing complaint on 6 of 7 days, and a handful of
    # service errors. Every one of them is internal; none has a counterpart a
    # person would notice. Volume, recurrence and recency all say "active" --
    # and none of that matters without an impact.
    events = [
        _pattern("Microsoft-Windows-CAPI2", 4176, days={i: 480 for i in range(7)},
                 severity="notable", impact="none", last_seen_hours_ago=0.2),
        _pattern("Microsoft-Windows-DeviceAssociationService", 3503,
                 days={5: 120, 3: 78, 2: 15, 1: 39, 0: 7}, severity="unknown",
                 impact="none", last_seen_hours_ago=21),
        _pattern("Microsoft-Windows-DistributedCOM", 10010, days={0: 8},
                 severity="benign", impact="none", last_seen_hours_ago=2),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "CAPI2" not in result["reason"]
    assert "3373" not in result["reason"]


def test_reliability_shutdown_cluster_is_not_a_finding() -> None:
    """The case that made the module worth deleting.

    On the live fleet thomas-pc was `crit` because of three "serious" events
    from one evening: Volsnap/25, VSS/13 and VSS/8193, all carrying
    `0x8007045b` -- ERROR_SHUTDOWN_IN_PROGRESS. The machine had not crashed;
    the user had shut it down, and VSS complained on the way out. There is no
    Kernel-Power/41 anywhere in the window, which is Windows' own way of
    saying the shutdown was clean.
    """

    events = [
        _pattern("Volsnap", 25, days={1: 1}, severity="serious", impact="none",
                 last_seen_hours_ago=32.1),
        _pattern("VSS", 13, days={1: 1}, severity="serious", impact="none",
                 last_seen_hours_ago=32.1),
        _pattern("VSS", 8193, days={1: 1}, severity="serious", impact="none",
                 last_seen_hours_ago=32.1),
    ]
    result = _eval_reliability(events, boot_sessions=[
        (NOW - timedelta(hours=31.5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ])
    assert result["status"] == "ok"


def test_reliability_one_crash_warns_and_a_second_crits() -> None:
    once = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={1: 1}, level="critical",
                 impact="crashed", symptom="The PC restarted without shutting down properly",
                 last_seen_hours_ago=20),
    ]
    assert _eval_reliability(once)["status"] == "warn"

    # Twice is a pattern, and a machine that cannot stay up is the thing this
    # section exists to catch.
    twice = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={1: 1, 0: 1}, level="critical",
                 impact="crashed", symptom="The PC restarted without shutting down properly",
                 last_seen_hours_ago=4),
    ]
    assert _eval_reliability(twice)["status"] == "crit"


def test_reliability_data_at_risk_still_needs_to_recur_to_crit() -> None:
    """Critical means it is still happening, for every impact alike.

    Exempting `data_at_risk` from recurrence looked right -- the second
    occurrence of data loss is the loss -- until the live fleet was replayed
    through it: one shadow-copy cleanup, 33 hours old and already
    self-corrected, turned the host red again. A single data-risk event is
    worth one notification; `disk_smart` carries the hardware signal
    independently, and `disk` carries the cause.
    """

    once = [
        _pattern("disk", 51, days={0: 1}, severity="serious", impact="data_at_risk",
                 symptom="Files on the system drive may be unreadable",
                 last_seen_hours_ago=3),
    ]
    result = _eval_reliability(once)
    assert result["status"] == "warn"
    assert "Files on the system drive may be unreadable" in result["reason"]

    twice = [
        _pattern("disk", 51, days={1: 1, 0: 1}, severity="serious", impact="data_at_risk",
                 symptom="Files on the system drive may be unreadable",
                 last_seen_hours_ago=3),
    ]
    assert _eval_reliability(twice)["status"] == "crit"


def test_reliability_degraded_warns_however_long_it_persists() -> None:
    # marianne-pc: BitLocker asks for the recovery key on every restart, 24
    # times over 6 days. Genuinely worth fixing, genuinely not worth paging
    # about -- the machine works. A standing annoyance that goes red every day
    # is how this section became ignorable.
    events = [
        _pattern("Microsoft-Windows-BitLocker-Driver", 24641,
                 days={5: 4, 4: 4, 3: 4, 2: 4, 1: 4, 0: 4}, severity="serious",
                 impact="degraded",
                 symptom="The PC asks for the BitLocker recovery key on every restart",
                 last_seen_hours_ago=14.7),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "warn"
    assert "BitLocker recovery key" in result["reason"]
    assert "24641" not in result["reason"]


def test_reliability_crash_markers_score_without_any_classifier() -> None:
    # No API key: every pattern is unclassified. The section must not report
    # silence as health, so the closed crash-marker set still scores, and the
    # reason says why everything else is quiet.
    events = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={2: 1, 0: 1}, level="critical",
                 state="unavailable", last_seen_hours_ago=5),
        _pattern("Microsoft-Windows-CAPI2", 4176, days={i: 480 for i in range(7)},
                 state="unavailable", last_seen_hours_ago=1),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "crit"
    assert health_rules._RELIABILITY_CRASH_SYMPTOM in result["reason"]
    assert "classification unavailable (no API key)" in result["reason"]
    # The unclassified firehose still contributes nothing.
    assert "CAPI2" not in result["reason"]


def test_reliability_crash_marker_floor_respects_suppression() -> None:
    # ADR-0041: explicit operator intent overrides an automatic escalation,
    # or a suppressed marker could never actually be muted.
    events = [
        _pattern("BugCheck", 1001, days={1: 2, 0: 1}, level="critical",
                 state="unavailable", suppressed=True, last_seen_hours_ago=3),
    ]
    assert _eval_reliability(events)["status"] == "ok"


def test_reliability_crash_marker_floor_never_lowers_a_worse_verdict() -> None:
    # The floor raises an impact to `crashed`; it must not pull a classifier
    # verdict that was already worse back down to it.
    events = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={1: 1, 0: 1}, level="critical",
                 impact="data_at_risk", symptom="The disk lost data during a crash",
                 last_seen_hours_ago=1),
    ]
    result = _eval_reliability(events)
    assert result["details"]["patterns"][0]["user_impact"] == "data_at_risk"
    assert result["reason"].startswith("The disk lost data during a crash")
    assert result["status"] == "crit"


def test_reliability_unclassified_patterns_are_counted_not_scored() -> None:
    # A pattern awaiting classification is an absence of information, not
    # evidence -- but a reader must be able to see it is absent.
    events = [
        _pattern("Something", 999, days={2: 50, 1: 50, 0: 100}, state="pending",
                 last_seen_hours_ago=1),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "1 awaiting classification" in result["reason"]


def test_reliability_reason_never_names_the_log_vocabulary() -> None:
    # A reason the reader has to look up delegates the work back to them.
    events = [
        _pattern("Microsoft-Windows-WER-SystemErrorReporting", 1001, days={1: 1, 0: 2},
                 level="critical", impact="crashed",
                 symptom="The PC froze and restarted by itself", last_seen_hours_ago=2),
    ]
    reason = _eval_reliability(events)["reason"]
    assert reason.startswith("The PC froze and restarted by itself")
    assert "3×" in reason
    for forbidden in ("WER-SystemErrorReporting", "1001", "0x"):
        assert forbidden not in reason


def test_reliability_impact_that_stopped_is_history_not_a_finding() -> None:
    # maria-pc: the same VSS pair, quiet for ten days. It self-clears rather
    # than staying a dated warning until it leaves the window.
    events = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={6: 1}, level="critical",
                 impact="crashed", symptom="The PC restarted unexpectedly",
                 last_seen_hours_ago=150),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "resolved" in result["reason"]


def test_reliability_a_pattern_that_went_quiet_days_ago_is_not_active() -> None:
    # The old rule called any pattern with >=3 active days "active" for the
    # whole 7-day window, which was a second red anchor independent of the 48h
    # one. Three days of hits that stopped four days ago is history.
    events = [
        _pattern("Bonjour Service", 100, days={6: 7, 5: 7, 4: 7}, impact="degraded",
                 symptom="Network name collision", last_seen_hours_ago=100),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert result["details"]["patterns"][0]["active"] is False

    # Still active when the three days are recent.
    recent = [
        _pattern("Bonjour Service", 100, days={2: 7, 1: 7, 0: 7}, impact="degraded",
                 symptom="Network name collision", last_seen_hours_ago=60),
    ]
    assert _eval_reliability(recent)["status"] == "warn"


def test_reliability_details_carry_the_per_pattern_record() -> None:
    events = [
        _pattern("disk", 51, days={2: 6, 0: 12}, severity="serious", impact="data_at_risk",
                 symptom="Files may be unreadable", last_seen_hours_ago=2,
                 category="Disk & storage", suspected_cause="failing sectors"),
    ]
    shared = events[0]
    result = _eval_reliability(events, boot_sessions=["2026-06-01T06:00:00Z"])
    assert result["details"]["patterns"] == [
        {
            "source": "disk", "event_id": 51, "level": "error", "count": 18,
            "severity": "serious", "category": "Disk & storage",
            "cause": "failing sectors", "user_impact": "data_at_risk",
            "symptom": "Files may be unreadable", "classification_state": "classified",
            "suppressed": False, "active_days": 2,
            "first_day": _day(2), "last_day": _day(0),
            "last_seen_age_hours": 2.0, "active": True, "recurring": True,
            "burst": False, "scores": True,
        }
    ]
    assert result["details"]["window_days"] == 7
    assert result["details"]["boot_sessions"] == 1
    # Pure: the group the dashboard heatmap shares is not mutated.
    assert "user_impact" in shared and "scores" not in shared


def test_reliability_activity_is_relative_to_now() -> None:
    events = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={1: 1, 0: 1}, level="critical",
                 impact="crashed", symptom="The PC restarted unexpectedly",
                 last_seen_hours_ago=2),
    ]
    assert _eval_reliability(events)["status"] == "crit"
    later = health_rules.evaluate_section(
        "reliability",
        {"status": "ok", "summary": "", "recent_crashes": 2, "window_days": 7, "events": events},
        now=NOW + timedelta(days=10),
    )
    assert later["status"] == "ok"


def test_reliability_by_day_stands_in_for_a_missing_last_seen() -> None:
    events = [_pattern("x", 1, days={1: 3}, impact="none")]
    p = _eval_reliability(events)["details"]["patterns"][0]
    assert p["last_seen_age_hours"] == 18.5


def test_reliability_quiet_host_reasons() -> None:
    assert _eval_reliability([])["reason"] == "no errors logged in 7d"
    checked = _eval_reliability([
        _pattern("a", 1, days={0: 3}, impact="none"),
        _pattern("b", 2, days={0: 1}, impact="none"),
    ])
    assert checked["status"] == "ok"
    assert checked["reason"] == "nothing user-visible in 7d (2 pattern(s) checked)"


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
    # And the server reaches its own verdict from the same payload: annotate
    # the disk group the way the read path would and the section goes crit
    # while the payload's own `status` still says "ok".
    annotated = dict(reliability)
    annotated["events"] = [
        {**e, "user_impact": "data_at_risk", "symptom": "Files may be unreadable",
         "classification_state": "classified"}
        if e["source"] == "disk"
        else {**e, "user_impact": "none", "classification_state": "classified"}
        for e in reliability["events"]
    ]
    verdict = health_rules.evaluate_section("reliability", annotated, now=NOW)
    assert verdict["status"] == "crit"
    assert annotated["status"] == "ok"


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
# same way the payloads above carry the classifier's annotation.
#
# Under the impact model suppression matters less than it did: a pattern with
# no user-visible impact never scored in the first place, so there is nothing
# left to mute. What it still has to do is override the two things the rule
# decides on its own -- the crash-marker floor, and a classifier verdict the
# operator disagrees with -- without ever touching the raw counts or the
# independent stability index.


def test_reliability_suppressed_impact_no_longer_scores() -> None:
    events = [
        _pattern("disk", 51, days={1: 25, 0: 25}, severity="serious", impact="data_at_risk",
                 symptom="Files may be unreadable", last_seen_hours_ago=1, suppressed=True),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "1 suppressed" in result["reason"]
    assert "Files may be unreadable" not in result["reason"]


def test_reliability_suppression_does_not_silence_low_stability_index() -> None:
    # The Windows Reliability Index is independent of pattern suppression and
    # always applies on top -- suppressing every pattern must not hide it.
    events = [
        _pattern("Microsoft-Windows-CAPI2", 4176, days={i: 480 for i in range(7)},
                 impact="none", last_seen_hours_ago=1, suppressed=True),
    ]
    result = _eval_reliability(events, stability_index=2.0)
    assert result["status"] == "crit"
    # ... and it says so by name, so a red status is never unexplained.
    assert "stability index 2.0/10" in result["reason"]


def test_reliability_suppressed_firehose_stays_out_of_the_reason() -> None:
    # The issue #166 regression, restated for the impact model: the muted
    # pattern is counted so a reader can tell "quiet" from "quieted", but it
    # is never named and its 3439 events never lead.
    events = [
        _pattern("Microsoft-Windows-CAPI2", 4176, days={i: 491 for i in range(7)},
                 severity="notable", impact="none", last_seen_hours_ago=1, suppressed=True),
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={1: 1, 0: 1}, level="critical",
                 impact="crashed", symptom="The PC restarted without shutting down properly",
                 last_seen_hours_ago=2),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "crit"
    assert result["reason"].startswith("The PC restarted without shutting down properly")
    assert "CAPI2" not in result["reason"]
    assert "3437" not in result["reason"]
    assert "1 suppressed" in result["reason"]


def test_reliability_all_patterns_suppressed_scores_ok() -> None:
    events = [
        _pattern("Microsoft-Windows-CAPI2", 4176, days={0: 3439}, severity="notable",
                 impact="none", last_seen_hours_ago=1, suppressed=True),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "1 suppressed" in result["reason"]


def test_reliability_suppression_applies_without_annotation() -> None:
    # A fresh install without an API key lives permanently in this state: the
    # crash-marker floor is the only thing that scores, and suppressing that
    # exact pattern is the operator's call.
    events = [
        _pattern("Microsoft-Windows-Kernel-Power", 41, days={1: 2, 0: 1}, level="critical",
                 state="unavailable", last_seen_hours_ago=1, suppressed=True),
        _pattern("Application Error", 1000, days={2: 10, 1: 20, 0: 13},
                 state="unavailable", last_seen_hours_ago=2),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
    assert "1 suppressed" in result["reason"]
    assert "classification unavailable (no API key)" in result["reason"]


def test_reliability_unsuppressed_payload_carries_no_suppression_clause() -> None:
    events = [
        _pattern("Application Error", 1000, days={2: 30, 1: 30, 0: 24}, severity="notable",
                 impact="none", last_seen_hours_ago=1, category="App crash / hang"),
    ]
    result = _eval_reliability(events)
    assert result["status"] == "ok"
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

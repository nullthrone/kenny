"""The alert dedup-key format, its inverse, and the upgrade path."""

from __future__ import annotations

import pytest

from kenny_server import alert_subject, ticket_rules


def test_sections_key_sorted_so_visit_order_does_not_matter() -> None:
    assert alert_subject.dedup_key("pc1", "health", ["memory", "disk"]) == alert_subject.dedup_key(
        "pc1", "health", ["disk", "memory"]
    )
    assert alert_subject.dedup_key("pc1", "health", ["disk"]) == "alert|pc1|state|disk"


def test_a_producer_with_no_section_keys_on_its_event_type() -> None:
    assert alert_subject.dedup_key("pc1", "offline", []) == "alert|pc1|state|offline"


def test_offline_and_a_section_finding_on_one_host_are_different_subjects() -> None:
    # Without the event-type fallback both would collapse to "alert|pc1|state|":
    # "the PC is unreachable" and "the disk is full" are not one case.
    assert alert_subject.dedup_key("pc1", "offline", []) != alert_subject.dedup_key(
        "pc1", "health", ["disk"]
    )


def test_a_change_never_shares_a_subject_with_a_finding_on_the_same_section() -> None:
    assert alert_subject.dedup_key("pc1", "change", ["services"]) != alert_subject.dedup_key(
        "pc1", "health", ["services"]
    )


@pytest.mark.parametrize("event_type", ticket_rules.EVENT_TYPES)
@pytest.mark.parametrize("sections", [[], ["disk"], ["disk", "memory"]])
def test_parse_round_trips_every_key_the_producers_can_build(
    event_type: str, sections: list[str]
) -> None:
    key = alert_subject.dedup_key("pc1", event_type, sections)
    parsed = alert_subject.parse(key)
    assert parsed is not None
    agent_id, space, subjects = parsed
    assert agent_id == "pc1"
    assert space in alert_subject.SUBJECT_SPACES
    assert subjects == (sorted(sections) or [event_type])


def test_parse_declines_keys_that_are_not_ours() -> None:
    assert alert_subject.parse("") is None
    assert alert_subject.parse("something|else|entirely|here") is None
    # A host name containing the separator is left alone rather than mis-split
    # into a wrong host and a wrong subject.
    assert alert_subject.parse("alert|pc|1|state|disk") is None


@pytest.mark.parametrize(
    ("old", "expected"),
    [
        ("alert|pc1|health|disk", "alert|pc1|state|disk"),
        ("alert|pc1|health|disk+memory", "alert|pc1|state|disk+memory"),
        ("alert|pc1|offline|", "alert|pc1|state|offline"),
        # The forecast now names the section it is about, so a key written
        # before it did has to land on the same subject as an acute finding.
        ("alert|pc1|disk_forecast|", "alert|pc1|state|disk"),
        ("alert|pc1|change|services", "alert|pc1|change|services"),
        ("", ""),
    ],
)
def test_migrate_maps_every_old_shape(old: str, expected: str) -> None:
    assert alert_subject.migrate(old) == expected


@pytest.mark.parametrize(
    "old",
    [
        "alert|pc1|health|disk",
        "alert|pc1|offline|",
        "alert|pc1|disk_forecast|",
        "alert|pc1|change|services",
        "alert||health|disk",
        "",
        "not-an-alert-key",
    ],
)
def test_migrate_is_a_fixed_point_on_its_own_output(old: str) -> None:
    once = alert_subject.migrate(old)
    assert alert_subject.migrate(once) == once


def test_a_forecast_and_an_acute_finding_on_disk_share_one_subject() -> None:
    # The whole point of the space/subject split: the forecast declares the
    # `disk` section (see AlertEngine._forecast_alert), so both key the same.
    assert alert_subject.dedup_key("pc1", "disk_forecast", ["disk"]) == alert_subject.dedup_key(
        "pc1", "health", ["disk"]
    )

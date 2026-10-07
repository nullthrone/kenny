"""The removable-media definition is shared by the health rules and the history rollup.

The ``disk_smart`` rule sets USB and SD disks aside, the ``hardware_errors`` rule
discounts storage retries on them, and the rollup (``hardware_metrics.extract``)
must not track either -- these tests run the same rows through both halves and
fail when they disagree.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from support.hardware import disk, disk_smart, group, hardware_errors, snapshot

from kenny_server import hardware_catalog
from kenny_server import hardware_metrics as hm
from kenny_server import health_rules

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
DAY = "2026-07-01"

ROWS = {
    "internal-nvme": disk("S-NVME", bus_type="NVMe"),
    "internal-sata": disk("S-SATA", bus_type="SATA"),
    "usb": disk("S-USB", bus_type="USB"),
    "usb-lower": disk("S-USB2", bus_type="usb"),
    "sd": disk("S-SD", bus_type="SD"),
    "sd-padded": disk("S-SD2", bus_type=" sd "),
    "flagged-removable": disk("S-REM", bus_type="SATA", removable=True),
    "unknown-bus": disk("S-UNK", bus_type="Unknown"),
}
INTERNAL = {"internal-nvme", "internal-sata", "unknown-bus"}


def _rule_judges(row: dict) -> bool:
    """Whether the ``disk_smart`` rule counts the row as an internal disk."""

    sick = {**row, "health_status": "Unhealthy"}
    result = health_rules._rule_disk_smart(disk_smart(sick), NOW)
    assert result is not None
    return result[1] != "No internal disks to judge"


def _rollup_tracks(row: dict) -> bool:
    # `wear` is a value the rollup can extract from a row without an NVMe block.
    rows = hm.extract([snapshot(disk_smart=disk_smart({**row, "wear": 5}))], DAY)
    return any(key == f"disk:{row['serial']}" for key, _, _ in rows)


@pytest.mark.parametrize("name", sorted(ROWS))
def test_the_rule_and_the_rollup_agree_on_which_disks_count(name: str) -> None:
    row = ROWS[name]
    assert _rule_judges(row) == _rollup_tracks(row) == (name in INTERNAL)
    assert hardware_catalog.is_removable_disk(row) == (name not in INTERNAL)


def test_device_labels_follow_the_same_definition() -> None:
    labels = hm.device_labels(snapshot(disk_smart=disk_smart(*ROWS.values())))
    assert set(labels) == {f"disk:{ROWS[n]['serial']}" for n in INTERNAL}


def _storage_events(bus: dict[str, int] | None) -> float | None:
    if bus is None:
        grp = group("disk", 153, {DAY: 4})
    else:
        grp = group("disk", 153, {DAY: sum(bus.values())}, details={"disk_bus_type": bus})
    rows = hm.extract([snapshot(hardware_errors=hardware_errors(grp))], DAY)
    return {(k, m): v for k, m, v in rows}.get(("host:storage", "instability_events"))


def test_usb_or_sd_only_storage_retries_add_nothing_to_the_component_history() -> None:
    assert _storage_events({"USB": 4}) == 0.0
    assert _storage_events({"SD": 2, "usb": 2}) == 0.0


def test_mixed_storage_retries_are_weighted_by_the_internal_share() -> None:
    assert _storage_events({"USB": 2, "SATA": 2}) == pytest.approx(2.0)
    assert _storage_events({"NVMe": 4}) == pytest.approx(4.0)
    assert _storage_events(None) == pytest.approx(4.0)  # silence about the bus is internal


@pytest.mark.parametrize(
    "bus", [{"USB": 4}, {"SD": 4}, {"USB": 9, "SATA": 1}, {"USB": 2, "SATA": 2}, {"SATA": 4}]
)
def test_the_rollup_weight_is_the_share_the_rule_uses(bus: dict[str, int]) -> None:
    share = hardware_catalog.internal_share({"disk_bus_type": bus})
    assert _storage_events(bus) == pytest.approx(sum(bus.values()) * share)

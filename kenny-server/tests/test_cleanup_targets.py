"""The server's copy of the agent's cleanup-target catalog, joined to the contract vector."""

from __future__ import annotations

import json
from pathlib import Path

from kenny_server import cleanup_targets as ct

VECTOR = Path(__file__).resolve().parents[2] / "docs" / "fixtures" / "vectors" / "fs_cleanup_targets.json"


def test_the_literal_equals_the_contract_vector() -> None:
    vector = json.loads(VECTOR.read_text())
    assert {os_: tuple(e["target"] for e in entries) for os_, entries in vector.items()} == dict(ct.TARGETS)


def test_all_targets_is_the_union() -> None:
    assert ct.ALL_TARGETS == {"temp_system", "temp_users", "windows_update_cache", "recycle_bin",
                              "logs_old", "tmp", "package_cache", "journal"}


def test_the_suggested_targets_are_catalog_targets_without_the_irreversible_ones() -> None:
    assert set(ct.SUGGESTED_TARGETS) <= ct.ALL_TARGETS
    assert not {"recycle_bin", "logs_old"} & set(ct.SUGGESTED_TARGETS)
    assert set(ct.SUGGESTED_TARGETS) == {
        "temp_system", "temp_users", "windows_update_cache", "tmp", "package_cache", "journal"
    }


def test_unknown_targets_and_targets_for() -> None:
    assert ct.unknown_targets(["tmp", "tmpp", "Tmp", "journal"]) == ["Tmp", "tmpp"]
    assert ct.targets_for("Linux") == ct.TARGETS["linux"]
    assert ct.targets_for("macos") == () and ct.targets_for(None) == ()


def test_the_brief_lists_only_what_the_host_serves() -> None:
    allowed = ["journal", "temp_users", "tmp", "logs_old"]
    assert ct.brief_line(allowed, "linux") == (
        "Cleanup targets you may scan and clean on this machine: journal, logs_old, tmp."
    )
    assert "temp_users" in ct.brief_line(allowed, "windows")
    assert "journal" not in ct.brief_line(allowed, "windows")
    for line in (ct.brief_line([], "linux"), ct.brief_line(allowed, "macos"), ct.brief_line(allowed, None)):
        assert line == "No cleanup target is allowed on this machine; scan and clean nothing."

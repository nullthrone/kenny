"""Tests for :mod:`kenny_server.diffs` (pure snapshot diffing, ADR-0030)."""

from __future__ import annotations

from kenny_server.diffs import diff_section, diff_snapshots


def _autostart(entries: list[dict]) -> dict:
    return {"status": "ok", "summary": "", "entries": entries}


def test_added_removed_changed_matrix() -> None:
    old = _autostart(
        [
            {"name": "OneDrive", "location": "HKCU\\Run", "command": "onedrive.exe"},
            {"name": "Steam", "location": "HKCU\\Run", "command": "steam.exe -silent"},
        ]
    )
    new = _autostart(
        [
            {"name": "OneDrive", "location": "HKCU\\Run", "command": "onedrive.exe /background"},
            {"name": "Sketchy", "location": "HKLM\\Run", "command": "C:\\Temp\\x.exe"},
        ]
    )
    changes = diff_section("autostart", old, new)
    kinds = {(c["kind"], c["key"]) for c in changes}
    assert ("added", "Sketchy | HKLM\\Run") in kinds
    assert ("removed", "Steam | HKCU\\Run") in kinds
    assert ("changed", "OneDrive | HKCU\\Run") in kinds
    changed = next(c for c in changes if c["kind"] == "changed")
    assert "onedrive.exe -> onedrive.exe /background" in changed["detail"]


def test_service_start_mode_change() -> None:
    old = {"services": [{"name": "wuauserv", "display": "Windows Update", "start": "auto"}]}
    new = {"services": [{"name": "wuauserv", "display": "Windows Update", "start": "disabled"}]}
    changes = diff_section("services", old, new)
    assert len(changes) == 1
    assert changes[0]["kind"] == "changed"
    assert "auto -> disabled" in changes[0]["detail"]


def test_identical_sections_produce_no_changes() -> None:
    payload = {"devices": [{"name": "USB Mouse", "class": "HIDClass", "status": "OK"}]}
    assert diff_section("peripherals", payload, dict(payload)) == []


def test_unknown_section_and_missing_lists_are_skipped() -> None:
    assert diff_section("disk", {"volumes": []}, {"volumes": [{"mount": "C:"}]}) == []
    assert diff_section("autostart", {"status": "ok"}, {"entries": []}) == []


def test_diff_snapshots_skips_sections_missing_on_either_side() -> None:
    old = {"autostart": _autostart([])}
    new = {
        "autostart": _autostart([{"name": "New", "location": "HKCU\\Run", "command": "n.exe"}]),
        # peripherals only in the new snapshot: a collector rollout must not
        # flood the diff with "added" rows.
        "peripherals": {"devices": [{"name": "USB Mouse"}]},
    }
    changes = diff_snapshots(old, new)
    assert [c["section"] for c in changes] == ["autostart"]
    assert changes[0]["kind"] == "added"


def test_local_accounts_admin_flip() -> None:
    old = {"accounts": [{"name": "kid", "enabled": True, "is_admin": False}]}
    new = {"accounts": [{"name": "kid", "enabled": True, "is_admin": True}]}
    changes = diff_section("local_accounts", old, new)
    assert len(changes) == 1
    assert "is_admin: False -> True" in changes[0]["detail"]

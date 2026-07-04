"""Agent registry: the typed, OS-aware ``Agent.os`` view (ADR-0035)."""

from __future__ import annotations

from kenny_server.registry import Agent


def test_agent_os_defaults_to_windows_for_legacy_meta() -> None:
    # Legacy agents that never reported an OS default to windows.
    assert Agent("a").os == "windows"
    assert Agent("a", meta={"hostname": "PC"}).os == "windows"
    assert Agent("a", meta={"os": None}).os == "windows"


def test_agent_os_reads_and_lowercases_meta() -> None:
    assert Agent("a", meta={"os": "Linux"}).os == "linux"
    assert Agent("a", meta={"os": "macos"}).os == "macos"
    assert Agent("a", meta={"os": "WINDOWS"}).os == "windows"


def test_to_public_surfaces_os() -> None:
    pub = Agent("a", meta={"os": "linux"}).to_public()
    assert pub["os"] == "linux"
    assert pub["agent_id"] == "a"
    # Legacy agent without meta.os still exposes an explicit "os" key.
    assert Agent("b").to_public()["os"] == "windows"

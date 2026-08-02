"""Agent registry: the typed, OS-aware ``Agent.os`` view (ADR-0035) and the
telemetry-reported arch mirror (ADR-0040)."""

from __future__ import annotations

from kenny_server.registry import Agent, AgentRegistry


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


async def _noop(_frame: object) -> None:
    return None


def test_note_arch_merges_without_wiping_other_meta() -> None:
    reg = AgentRegistry()
    reg.register_signed_async("legacy-pc", {"os": "linux", "hostname": "PC"}, _noop)
    reg.note_arch("legacy-pc", "aarch64")
    agent = reg.get("legacy-pc")
    assert agent is not None
    assert agent.arch == "aarch64"
    # The rest of meta (set at register time) survives the merge.
    assert agent.meta["hostname"] == "PC"
    assert agent.meta["os"] == "linux"


def test_note_arch_is_a_noop_for_an_unknown_agent() -> None:
    # A telemetry push for an agent the registry doesn't (yet, or anymore) know
    # about must never raise — the caller doesn't gate on this.
    reg = AgentRegistry()
    reg.note_arch("ghost-pc", "aarch64")
    assert reg.get("ghost-pc") is None


# -- channel: the agent's actual/built release channel (ADR-0052) ------------


def test_agent_channel_defaults_to_stable_for_legacy_meta() -> None:
    assert Agent("a").channel == "stable"
    assert Agent("a", meta={"hostname": "PC"}).channel == "stable"
    assert Agent("a", meta={"channel": None}).channel == "stable"


def test_agent_channel_reads_reported_dev() -> None:
    assert Agent("a", meta={"channel": "dev"}).channel == "dev"
    assert Agent("a", meta={"channel": "stable"}).channel == "stable"


def test_agent_channel_guards_malformed_value() -> None:
    # An unrecognized value must never be trusted literally; falls back to
    # "stable" rather than propagating garbage into campaign eligibility.
    assert Agent("a", meta={"channel": "bogus"}).channel == "stable"
    assert Agent("a", meta={"channel": "DEV"}).channel == "dev"  # case-insensitive


def test_note_channel_merges_without_wiping_other_meta() -> None:
    reg = AgentRegistry()
    reg.register_signed_async("legacy-pc", {"os": "linux", "hostname": "PC"}, _noop)
    reg.note_channel("legacy-pc", "dev")
    agent = reg.get("legacy-pc")
    assert agent is not None
    assert agent.channel == "dev"
    # The rest of meta (set at register time) survives the merge.
    assert agent.meta["hostname"] == "PC"
    assert agent.meta["os"] == "linux"


def test_note_channel_guards_malformed_value() -> None:
    reg = AgentRegistry()
    reg.register_signed_async("pc-1", {"channel": "stable"}, _noop)
    reg.note_channel("pc-1", "not-a-channel")
    # A malformed reported value must never clobber the last-known-good one.
    assert reg.get("pc-1").channel == "stable"


def test_note_channel_is_a_noop_for_an_unknown_agent() -> None:
    reg = AgentRegistry()
    reg.note_channel("ghost-pc", "dev")
    assert reg.get("ghost-pc") is None

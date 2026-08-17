"""UpdateManager: pinned campaign approval, on-connect rollout, attempt budget (ADR-0040).

Uses a duck-typed fake tunnel (an object exposing async ``send_request``) rather
than a real ``AgentTunnel``/WebSocket, matching how the project already
recommends testing against "a mock agent" for higher-level flows while keeping
this unit test focused purely on ``UpdateManager``'s own decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kenny_server import agent_release, server_release, update_manager
from kenny_server.config import Settings
from kenny_server.distribution import ShareLinks
from kenny_server.registry import AgentRegistry
from kenny_server.store import ATTEMPT_BUDGET, UpdateStore
from kenny_server.tunnel import ToolError
from kenny_server.update_manager import UpdateManager


class _MemStore:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    async def all(self):
        return dict(self.data)

    async def set(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        return self.data.pop(key, None) is not None


def _settings(overrides=None) -> Settings:
    # Settings.load() (async) is what normally turns a store's contents into
    # live overrides; passing them as env instead reads synchronously on every
    # get() with no separate load() call needed, which is all these tests need.
    return Settings(_MemStore(), env=overrides or {}, apply_hooks={})


class _FakeTunnel:
    """Records every ``send_request`` call; replays queued outcomes per agent."""

    def __init__(self, outcomes: dict[str, list] | None = None):
        self.outcomes = outcomes or {}
        self.calls: list[tuple] = []

    async def send_request(self, agent_id, tool, args, timeout_s=120):
        self.calls.append((agent_id, tool, args))
        queue = self.outcomes.get(agent_id)
        if not queue:
            return {"ok": True, "staged_version": args.get("version")}
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _noop(_frame):
    return None


def _mgr(tmp_path, *, tunnel=None, settings=None, registry=None) -> UpdateManager:
    store = UpdateStore(str(tmp_path / "updates.sqlite"))
    return UpdateManager(
        db_path=str(tmp_path / "kenny.sqlite"),
        store=store,
        registry=registry or AgentRegistry(),
        tunnel=tunnel or _FakeTunnel(),
        share_links=ShareLinks(),
        settings=settings or _settings(),
        campaign_dir=str(tmp_path / "update_campaigns"),
    )


def _write_cached_binary(
    tmp_path, monkeypatch, os_name, arch, version, content=b"binary-bytes", channel="stable"
):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    path = agent_release.cache_path(os_name, arch, channel)
    with open(path, "wb") as fh:
        fh.write(content)
    with open(path + ".version", "w", encoding="utf-8") as fh:
        fh.write(version)
    return path


# -- approve_campaign: pinning ------------------------------------------------


async def test_approve_campaign_pins_only_matching_version_binaries(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0", b"win-bytes")
    # a differently-versioned cached linux binary must NOT be pinned into this campaign
    _write_cached_binary(tmp_path, monkeypatch, "linux", "x86_64", "0.9.0", b"linux-old-bytes")

    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    campaign = await mgr.approve_campaign(version="1.0.0")
    assert campaign["version"] == "1.0.0"
    assert campaign["status"] == "active"

    targets = await mgr.store.campaign_targets(campaign["id"])
    assert [(t["os"], t["arch"]) for t in targets] == [("windows", "x86_64")]
    with open(targets[0]["path"], "rb") as fh:
        assert fh.read() == b"win-bytes"
    await mgr.store.close()


async def test_approve_campaign_defaults_to_latest_known_availability(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "2.0.0")
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    await mgr.store.set_availability("agent", version="2.0.0", ok=True, message="ok")
    campaign = await mgr.approve_campaign()  # no version -> resolves from availability
    assert campaign["version"] == "2.0.0"
    await mgr.store.close()


async def test_approve_campaign_raises_without_a_cached_match(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    with pytest.raises(ValueError, match="no cached agent binary"):
        await mgr.approve_campaign(version="9.9.9")
    await mgr.store.close()


async def test_approve_campaign_supersedes_and_cleans_up_prior_dir(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0")
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    first = await mgr.approve_campaign(version="1.0.0")
    first_dir = tmp_path / "update_campaigns" / first["id"]
    assert first_dir.exists()

    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.1.0")
    second = await mgr.approve_campaign(version="1.1.0")
    assert not first_dir.exists()  # cleaned up once superseded
    active = await mgr.store.get_active_campaign()
    assert active["id"] == second["id"]
    await mgr.store.close()


# -- apply / attempt budget ---------------------------------------------------


async def _campaign_with_agent(tmp_path, monkeypatch, *, agent_version="0.9.0"):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0")
    registry = AgentRegistry()
    registry.register_signed_async(
        "pc-1", {"os": "windows", "arch": "x86_64", "version": agent_version}, _noop
    )
    tunnel = _FakeTunnel()
    mgr = _mgr(tmp_path, tunnel=tunnel, registry=registry)
    await mgr.store.connect()
    campaign = await mgr.approve_campaign(version="1.0.0")
    return mgr, tunnel, campaign


async def test_apply_now_success_marks_updated_and_completes_campaign(tmp_path, monkeypatch):
    mgr, tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch)
    result = await mgr.apply_now(campaign["id"])
    assert result["attempted"] == ["pc-1"]
    assert len(tunnel.calls) == 1
    agent_id, tool, args = tunnel.calls[0]
    assert tool == "agent_update"
    assert args["version"] == "1.0.0"

    state = await mgr.store.get_agent_state(campaign["id"], "pc-1")
    assert state["updated_version"] is True
    # the only eligible agent succeeded -> the campaign auto-completes
    done = await mgr.store.get_campaign(campaign["id"])
    assert done["status"] == "completed"
    campaign_dir = tmp_path / "update_campaigns" / campaign["id"]
    assert not campaign_dir.exists()
    await mgr.store.close()


async def test_apply_now_skips_agent_already_reporting_target_version(tmp_path, monkeypatch):
    mgr, tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch, agent_version="1.0.0")
    result = await mgr.apply_now(campaign["id"])
    assert result["attempted"] == ["pc-1"]  # attempted, but no tool call needed
    assert tunnel.calls == []  # already at target — never triggers a wire call
    state = await mgr.store.get_agent_state(campaign["id"], "pc-1")
    assert state["updated_version"] is True
    await mgr.store.close()


async def test_disabled_response_counts_against_budget_and_holds(tmp_path, monkeypatch):
    outcomes = {"pc-1": [ToolError("disabled", "remote control is off")] * ATTEMPT_BUDGET}
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0")
    registry = AgentRegistry()
    registry.register_signed_async("pc-1", {"os": "windows", "arch": "x86_64", "version": "0.9.0"}, _noop)
    tunnel = _FakeTunnel(outcomes)
    mgr = _mgr(tmp_path, tunnel=tunnel, registry=registry)
    await mgr.store.connect()
    campaign = await mgr.approve_campaign(version="1.0.0")

    for _ in range(ATTEMPT_BUDGET):
        await mgr.apply_now(campaign["id"])
    state = await mgr.store.get_agent_state(campaign["id"], "pc-1")
    assert state["attempts"] == ATTEMPT_BUDGET
    assert state["held"] is True

    # a held agent is never retried again under this campaign
    await mgr.apply_now(campaign["id"])
    assert len(tunnel.calls) == ATTEMPT_BUDGET
    await mgr.store.close()


async def test_paused_response_retries_without_spending_budget(tmp_path, monkeypatch):
    outcomes = {"pc-1": [ToolError("paused", "anti-cheat active")] * 5}
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0")
    registry = AgentRegistry()
    registry.register_signed_async("pc-1", {"os": "windows", "arch": "x86_64", "version": "0.9.0"}, _noop)
    tunnel = _FakeTunnel(outcomes)
    mgr = _mgr(tmp_path, tunnel=tunnel, registry=registry)
    await mgr.store.connect()
    campaign = await mgr.approve_campaign(version="1.0.0")

    for _ in range(5):
        await mgr.apply_now(campaign["id"])
    state = await mgr.store.get_agent_state(campaign["id"], "pc-1")
    assert state["attempts"] == 0
    assert state["held"] is False
    assert len(tunnel.calls) == 5  # each pass still retries — never spends the budget
    await mgr.store.close()


async def test_apply_now_skips_offline_agents(tmp_path, monkeypatch):
    mgr, tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch)
    mgr.registry.mark_offline("pc-1")
    result = await mgr.apply_now(campaign["id"])
    assert result["attempted"] == []
    assert tunnel.calls == []
    await mgr.store.close()


async def test_apply_now_raises_without_an_active_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    with pytest.raises(ValueError, match="no active campaign"):
        await mgr.apply_now()
    await mgr.store.close()


# -- suspend / resume ---------------------------------------------------------


async def test_suspend_stops_both_triggers(tmp_path, monkeypatch):
    mgr, tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch)
    assert await mgr.suspend_campaign(campaign["id"]) is True

    # get_active_campaign() no longer returns it...
    assert await mgr.store.get_active_campaign() is None
    # ...and apply_now() refuses rather than silently no-oping.
    with pytest.raises(ValueError, match="suspended"):
        await mgr.apply_now(campaign["id"])
    assert tunnel.calls == []
    await mgr.store.close()


async def test_suspend_keeps_the_pinned_artifact_directory(tmp_path, monkeypatch):
    mgr, _tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch)
    campaign_dir = tmp_path / "update_campaigns" / campaign["id"]
    assert campaign_dir.exists()

    await mgr.suspend_campaign(campaign["id"])
    assert campaign_dir.exists()  # unlike revoke, suspend never cleans this up
    await mgr.store.close()


async def test_resume_restores_the_campaign_and_artifacts_stay_on_disk(tmp_path, monkeypatch):
    mgr, tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch)
    campaign_dir = tmp_path / "update_campaigns" / campaign["id"]
    await mgr.suspend_campaign(campaign["id"])

    assert await mgr.resume_campaign(campaign["id"]) is True
    assert campaign_dir.exists()
    active = await mgr.store.get_active_campaign()
    assert active is not None and active["id"] == campaign["id"]

    # and it is applicable again, exactly as before suspension.
    result = await mgr.apply_now(campaign["id"])
    assert result["attempted"] == ["pc-1"]
    assert len(tunnel.calls) == 1
    await mgr.store.close()


async def test_held_agents_attempt_count_survives_suspend_and_resume(tmp_path, monkeypatch):
    """The entire point of suspend over revoke-then-recreate: a held agent's
    attempt budget must not reset just because the campaign was paused."""

    outcomes = {"pc-1": [ToolError("disabled", "remote control is off")] * ATTEMPT_BUDGET}
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0")
    registry = AgentRegistry()
    registry.register_signed_async("pc-1", {"os": "windows", "arch": "x86_64", "version": "0.9.0"}, _noop)
    tunnel = _FakeTunnel(outcomes)
    mgr = _mgr(tmp_path, tunnel=tunnel, registry=registry)
    await mgr.store.connect()
    campaign = await mgr.approve_campaign(version="1.0.0")

    for _ in range(ATTEMPT_BUDGET):
        await mgr.apply_now(campaign["id"])
    held_state = await mgr.store.get_agent_state(campaign["id"], "pc-1")
    assert held_state["attempts"] == ATTEMPT_BUDGET
    assert held_state["held"] is True

    await mgr.suspend_campaign(campaign["id"])
    await mgr.resume_campaign(campaign["id"])

    resumed_state = await mgr.store.get_agent_state(campaign["id"], "pc-1")
    assert resumed_state["attempts"] == ATTEMPT_BUDGET
    assert resumed_state["held"] is True

    # and a held agent is still never retried again under the resumed campaign.
    await mgr.apply_now(campaign["id"])
    assert len(tunnel.calls) == ATTEMPT_BUDGET  # no new call spent on the held agent
    await mgr.store.close()


async def test_suspend_refused_on_completed_campaign(tmp_path, monkeypatch):
    mgr, _tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch)
    await mgr.apply_now(campaign["id"])  # the only eligible agent succeeds -> auto-completes
    completed = await mgr.store.get_campaign(campaign["id"])
    assert completed["status"] == "completed"

    assert await mgr.suspend_campaign(campaign["id"]) is False
    still = await mgr.store.get_campaign(campaign["id"])
    assert still["status"] == "completed"
    await mgr.store.close()


# -- on-connect hook -----------------------------------------------------------


async def test_on_agent_connect_noop_when_setting_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(update_manager, "ON_CONNECT_DELAY_S", 0)
    mgr, tunnel, campaign = await _campaign_with_agent(tmp_path, monkeypatch)
    # KENNY_AGENT_ROLLOUT_ON_CONNECT defaults to "0" (off)
    await mgr.on_agent_connect("pc-1")
    assert tunnel.calls == []
    await mgr.store.close()


async def test_on_agent_connect_applies_pinned_campaign_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(update_manager, "ON_CONNECT_DELAY_S", 0)
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0")
    registry = AgentRegistry()
    registry.register_signed_async("pc-1", {"os": "windows", "arch": "x86_64", "version": "0.9.0"}, _noop)
    tunnel = _FakeTunnel()
    settings = _settings({"KENNY_AGENT_ROLLOUT_ON_CONNECT": "1"})
    mgr = _mgr(tmp_path, tunnel=tunnel, settings=settings, registry=registry)
    await mgr.store.connect()
    await mgr.approve_campaign(version="1.0.0", on_connect=True)

    await mgr.on_agent_connect("pc-1")
    assert len(tunnel.calls) == 1
    assert tunnel.calls[0][2]["version"] == "1.0.0"
    await mgr.store.close()


async def test_on_agent_connect_swallows_errors(tmp_path, monkeypatch):
    """A hook failure must never raise into the tunnel (ADR-0040)."""

    monkeypatch.setattr(update_manager, "ON_CONNECT_DELAY_S", 0)
    settings = _settings({"KENNY_AGENT_ROLLOUT_ON_CONNECT": "1"})
    mgr = _mgr(tmp_path, settings=settings)

    class _BoomStore(UpdateStore):
        async def get_active_campaign(self, component="agent"):
            raise RuntimeError("boom")

    mgr.store = _BoomStore(str(tmp_path / "boom.sqlite"))
    await mgr.store.connect()
    await mgr.on_agent_connect("pc-1")  # must not raise
    await mgr.store.close()


# -- detection: server-image availability --------------------------------------


async def test_check_now_records_newer_server_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    monkeypatch.setattr(update_manager, "__version__", "1.0.0")

    async def fake_fetch(image_ref, *, github_token=None, client_factory=None):
        return server_release.ServerReleaseInfo(
            ok=True, message="latest tag 1.4.0", tag="1.4.0", digest="sha256:" + "d" * 64
        )

    monkeypatch.setattr(server_release, "fetch_latest_server_tag", fake_fetch)
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    await mgr.check_now()
    row = await mgr.store.get_availability("server")
    assert row["version"] == "1.4.0"
    assert row["digest"] == "sha256:" + "d" * 64
    await mgr.store.close()


async def test_check_now_records_up_to_date_when_not_newer(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    monkeypatch.setattr(update_manager, "__version__", "1.4.0")

    async def fake_fetch(image_ref, *, github_token=None, client_factory=None):
        return server_release.ServerReleaseInfo(
            ok=True, message="latest tag 1.4.0", tag="1.4.0", digest="sha256:" + "d" * 64
        )

    monkeypatch.setattr(server_release, "fetch_latest_server_tag", fake_fetch)
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    await mgr.check_now()
    row = await mgr.store.get_availability("server")
    assert row["version"] == "1.4.0"
    assert row["message"] == "up to date"
    await mgr.store.close()


# -- expiry --------------------------------------------------------------------


async def test_expire_stale_campaign_transitions_and_cleans_up(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0")
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    campaign = await mgr.approve_campaign(version="1.0.0", max_age_secs=1_000_000)
    campaign_dir = tmp_path / "update_campaigns" / campaign["id"]
    assert campaign_dir.exists()

    # force it into the past directly (approve_campaign always computes a future one)
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    await mgr.store._conn.execute(
        "UPDATE update_campaigns SET expires_at = ? WHERE id = ?", (past, campaign["id"])
    )
    await mgr.store._conn.commit()

    await mgr._expire_stale_campaign()
    expired = await mgr.store.get_campaign(campaign["id"])
    assert expired["status"] == "expired"
    assert not campaign_dir.exists()
    assert await mgr.store.get_active_campaign() is None
    await mgr.store.close()


# -- channel (ADR-0048): desired-channel eligibility, stable/dev campaign
# independence, dev-channel detection ------------------------------------------


async def test_dev_campaign_does_not_capture_stable_desired_agent(tmp_path, monkeypatch):
    """The whole point of a per-agent desired channel: a dev campaign must
    never apply to an agent an operator hasn't opted into dev."""

    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.1.0-dev.5", channel="dev")
    registry = AgentRegistry()
    registry.register_signed_async(
        "pc-1", {"os": "windows", "arch": "x86_64", "version": "1.0.0"}, _noop
    )
    tunnel = _FakeTunnel()
    mgr = _mgr(tmp_path, tunnel=tunnel, registry=registry)
    await mgr.store.connect()
    # pc-1's desired channel defaults to stable — never explicitly set to dev.
    campaign = await mgr.approve_campaign(version="1.1.0-dev.5", channel="dev")

    result = await mgr.apply_now(campaign["id"])
    assert result["attempted"] == ["pc-1"]  # attempted, but ineligible -> no-op
    assert tunnel.calls == []
    state = await mgr.store.get_agent_state(campaign["id"], "pc-1")
    assert state is None  # never even recorded an attempt (not eligible)
    await mgr.store.close()


async def test_dev_campaign_applies_once_agent_opts_into_dev(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.1.0-dev.5", channel="dev")
    registry = AgentRegistry()
    registry.register_signed_async(
        "pc-1", {"os": "windows", "arch": "x86_64", "version": "1.0.0"}, _noop
    )
    tunnel = _FakeTunnel()
    mgr = _mgr(tmp_path, tunnel=tunnel, registry=registry)
    await mgr.store.connect()
    await mgr.store.set_desired_channel("pc-1", "dev")
    campaign = await mgr.approve_campaign(version="1.1.0-dev.5", channel="dev")

    result = await mgr.apply_now(campaign["id"])
    assert result["attempted"] == ["pc-1"]
    assert len(tunnel.calls) == 1
    assert tunnel.calls[0][2]["version"] == "1.1.0-dev.5"
    await mgr.store.close()


async def test_approving_dev_campaign_does_not_revoke_active_stable_one(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0", channel="stable")
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.1.0-dev.1", channel="dev")
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    stable = await mgr.approve_campaign(version="1.0.0", channel="stable")
    dev = await mgr.approve_campaign(version="1.1.0-dev.1", channel="dev")

    stable_reloaded = await mgr.store.get_campaign(stable["id"])
    dev_reloaded = await mgr.store.get_campaign(dev["id"])
    assert stable_reloaded["status"] == "active"
    assert dev_reloaded["status"] == "active"
    await mgr.store.close()


async def test_approving_stable_campaign_does_not_revoke_active_dev_one(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.1.0-dev.1", channel="dev")
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0", channel="stable")
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    dev = await mgr.approve_campaign(version="1.1.0-dev.1", channel="dev")
    stable = await mgr.approve_campaign(version="1.0.0", channel="stable")

    dev_reloaded = await mgr.store.get_campaign(dev["id"])
    stable_reloaded = await mgr.store.get_campaign(stable["id"])
    assert dev_reloaded["status"] == "active"
    assert stable_reloaded["status"] == "active"
    await mgr.store.close()


async def test_fleet_status_reports_actual_and_desired_channel(tmp_path, monkeypatch):
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.1.0-dev.1", channel="dev")
    registry = AgentRegistry()
    registry.register_signed_async(
        "pc-1", {"os": "windows", "arch": "x86_64", "version": "1.0.0", "channel": "stable"}, _noop
    )
    mgr = _mgr(tmp_path, registry=registry)
    await mgr.store.connect()
    await mgr.store.set_desired_channel("pc-1", "dev")
    await mgr.approve_campaign(version="1.1.0-dev.1", channel="dev")

    status = await mgr.fleet_status()
    assert status["active_campaign"] is None  # no stable campaign was approved
    assert status["active_campaign_dev"] is not None
    dev_row = next(a for a in status["agents_dev"] if a["agent_id"] == "pc-1")
    assert dev_row["channel"] == "stable"  # actual/reported (hasn't updated yet)
    assert dev_row["desired_channel"] == "dev"  # soll
    assert dev_row["eligible"] is True
    await mgr.store.close()


async def test_check_now_populates_dev_availability_without_disturbing_stable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    _write_cached_binary(tmp_path, monkeypatch, "windows", "x86_64", "1.0.0", channel="stable")

    async def fake_fetch_tag(image_ref, *, github_token=None, client_factory=None, channel="stable"):
        if channel == "dev":
            return server_release.ServerReleaseInfo(
                ok=True, message="latest tag 1.1.0-dev.9", tag="1.1.0-dev.9", digest="sha256:" + "f" * 64
            )
        return server_release.ServerReleaseInfo(ok=False, message="no semver-tagged release found")

    monkeypatch.setattr(server_release, "fetch_latest_server_tag", fake_fetch_tag)
    mgr = _mgr(tmp_path)
    await mgr.store.connect()
    result = await mgr.check_now()

    assert "agent_dev" in result
    assert "server_dev" in result
    stable_row = await mgr.store.get_availability("agent")
    dev_row = await mgr.store.get_availability("agent:dev")
    assert stable_row["version"] == "1.0.0"  # untouched by the dev pass
    assert dev_row is not None  # additive: no dev binary cached, but the row exists

    server_dev_row = await mgr.store.get_availability("server:dev")
    assert server_dev_row["version"] == "1.1.0-dev.9"
    assert server_dev_row["digest"] == "sha256:" + "f" * 64
    await mgr.store.close()

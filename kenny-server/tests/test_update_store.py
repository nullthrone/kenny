"""``UpdateStore`` round-trip tests: availability, campaigns, attempt budget (ADR-0044)."""

from __future__ import annotations

from kenny_server.store import ATTEMPT_BUDGET, UpdateStore


async def _store(tmp_path) -> UpdateStore:
    store = UpdateStore(str(tmp_path / "updates.sqlite"))
    await store.connect()
    return store


async def test_availability_round_trips_and_upserts(tmp_path) -> None:
    store = await _store(tmp_path)
    assert await store.get_availability("agent") is None
    await store.set_availability("agent", version="0.2.0", sha256="a" * 64, ok=True, message="ok")
    row = await store.get_availability("agent")
    assert row is not None
    assert row["version"] == "0.2.0"
    assert row["ok"] is True
    # a later call upserts rather than duplicating
    await store.set_availability("agent", version="0.3.0", ok=True, message="newer")
    row = await store.get_availability("agent")
    assert row["version"] == "0.3.0"
    both = await store.list_availability()
    assert set(both) == {"agent"}
    await store.close()


async def test_create_campaign_generates_id_and_supersedes_prior_active(tmp_path) -> None:
    store = await _store(tmp_path)
    targets = [{"os": "windows", "arch": "x86_64", "path": "/tmp/a", "sha256": "a" * 64}]
    first_id = await store.create_campaign(
        version="0.2.0", on_connect=False, expires_at=None, targets=targets
    )
    assert first_id
    assert (await store.get_active_campaign())["id"] == first_id

    second_id = await store.create_campaign(
        id="explicit-id", version="0.3.0", on_connect=True, expires_at=None, targets=targets
    )
    assert second_id == "explicit-id"
    # the prior campaign is superseded (revoked), the new one is active
    prior = await store.get_campaign(first_id)
    assert prior["status"] == "revoked"
    assert prior["revoked_at"] is not None
    active = await store.get_active_campaign()
    assert active["id"] == second_id
    assert active["on_connect"] is True
    await store.close()


async def test_campaign_targets_persisted(tmp_path) -> None:
    store = await _store(tmp_path)
    targets = [
        {"os": "windows", "arch": "x86_64", "path": "/tmp/w", "sha256": "a" * 64},
        {"os": "linux", "arch": "aarch64", "path": "/tmp/l", "sha256": "b" * 64},
    ]
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=targets)
    stored = await store.campaign_targets(cid)
    assert {(t["os"], t["arch"]) for t in stored} == {("windows", "x86_64"), ("linux", "aarch64")}
    await store.close()


async def test_set_campaign_status_only_transitions_active(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    assert await store.set_campaign_status(cid, "revoked") is True
    # already terminal: a second transition attempt is a no-op (guarded by status='active')
    assert await store.set_campaign_status(cid, "expired") is False
    assert (await store.get_campaign(cid))["status"] == "revoked"
    await store.close()


async def test_record_attempt_success_marks_updated_version(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    row = await store.record_attempt(cid, "pc-1", ok=True)
    assert row["updated_version"] is True
    assert row["held"] is False
    assert row["attempts"] == 0
    await store.close()


async def test_record_attempt_failure_holds_after_budget(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    row = None
    for i in range(ATTEMPT_BUDGET):
        row = await store.record_attempt(cid, "pc-1", ok=False, error="disabled: kill switch off")
        assert row["attempts"] == i + 1
        assert row["held"] == (i + 1 >= ATTEMPT_BUDGET)
    assert row["held"] is True
    # a held agent's state is queryable via list_agent_states too
    states = await store.list_agent_states(cid)
    assert states["pc-1"]["held"] is True
    await store.close()


async def test_record_attempt_paused_does_not_count_against_budget(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    for _ in range(ATTEMPT_BUDGET + 5):
        row = await store.record_attempt(
            cid, "pc-1", ok=False, error="paused: anti-cheat", count_against_budget=False
        )
    assert row["attempts"] == 0
    assert row["held"] is False
    await store.close()


async def test_list_campaigns_newest_first(tmp_path) -> None:
    store = await _store(tmp_path)
    for v in ("1.0.0", "1.1.0", "1.2.0"):
        await store.create_campaign(version=v, on_connect=False, expires_at=None, targets=[])
    campaigns = await store.list_campaigns()
    versions = [c["version"] for c in campaigns]
    # newest (most recently created -> active) first; each create supersedes the prior
    assert versions[0] == "1.2.0"
    assert len(campaigns) == 3
    await store.close()

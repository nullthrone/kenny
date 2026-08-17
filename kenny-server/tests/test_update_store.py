"""``UpdateStore`` round-trip tests: availability, campaigns, attempt budget (ADR-0040)."""

from __future__ import annotations

import aiosqlite

from kenny_server.store import ATTEMPT_BUDGET, UpdateStore, _availability_key


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


async def test_suspend_and_resume_round_trip(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    assert await store.set_campaign_status(cid, "suspended") is True
    assert (await store.get_campaign(cid))["status"] == "suspended"
    # a suspended campaign is no longer the active one...
    assert await store.get_active_campaign() is None
    # ...but is still fetchable by id, so it can be resumed.
    assert await store.get_campaign(cid) is not None

    assert await store.set_campaign_status(cid, "active", from_status="suspended") is True
    resumed = await store.get_campaign(cid)
    assert resumed["status"] == "active"
    assert (await store.get_active_campaign())["id"] == cid
    await store.close()


async def test_suspend_refused_on_a_terminal_campaign(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    assert await store.set_campaign_status(cid, "revoked") is True
    # a revoked campaign cannot be suspended -- refused, not silently ignored
    assert await store.set_campaign_status(cid, "suspended") is False
    assert (await store.get_campaign(cid))["status"] == "revoked"
    await store.close()


async def test_resume_refused_unless_currently_suspended(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    # still active -- "resume" (from_status="suspended") does not match
    assert await store.set_campaign_status(cid, "active", from_status="suspended") is False
    assert (await store.get_campaign(cid))["status"] == "active"
    await store.close()


async def test_suspended_status_survives_a_restart(tmp_path) -> None:
    """A "restart" here is literal (mirrors ``tests/test_approval_persistence.py``):
    every store is closed and a fresh one is opened over the same SQLite file,
    so the suspended status can only be surviving in the file itself."""

    db_path = str(tmp_path / "updates.sqlite")
    boot1 = UpdateStore(db_path)
    await boot1.connect()
    cid = await boot1.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    assert await boot1.set_campaign_status(cid, "suspended") is True
    await boot1.close()

    boot2 = UpdateStore(db_path)
    await boot2.connect()
    try:
        row = await boot2.get_campaign(cid)
        assert row is not None
        assert row["status"] == "suspended"
        assert await boot2.get_active_campaign() is None

        # and it resumes correctly on the fresh connection too
        assert await boot2.set_campaign_status(cid, "active", from_status="suspended") is True
        resumed = await boot2.get_campaign(cid)
        assert resumed["status"] == "active"
    finally:
        await boot2.close()


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


# -- channel (ADR-0048): availability key composition, campaign channel scoping,
# migration, and desired-channel round trip ----------------------------------


def test_availability_key_composition() -> None:
    assert _availability_key("agent") == "agent"
    assert _availability_key("agent", "stable") == "agent"
    assert _availability_key("agent", "dev") == "agent:dev"
    assert _availability_key("server", "dev") == "server:dev"


async def test_availability_stable_and_dev_are_independent_rows(tmp_path) -> None:
    store = await _store(tmp_path)
    await store.set_availability("agent", version="1.0.0", ok=True, message="ok")
    await store.set_availability(_availability_key("agent", "dev"), version="1.1.0-dev.3", ok=True, message="ok")
    stable = await store.get_availability("agent")
    dev = await store.get_availability("agent:dev")
    assert stable["version"] == "1.0.0"
    assert dev["version"] == "1.1.0-dev.3"
    both = await store.list_availability()
    assert set(both) == {"agent", "agent:dev"}
    await store.close()


async def test_campaign_channel_defaults_to_stable_and_is_selectable(tmp_path) -> None:
    store = await _store(tmp_path)
    cid = await store.create_campaign(version="1.0.0", on_connect=False, expires_at=None, targets=[])
    row = await store.get_campaign(cid)
    assert row["channel"] == "stable"
    await store.close()


async def test_stable_and_dev_campaigns_coexist_without_superseding(tmp_path) -> None:
    store = await _store(tmp_path)
    stable_id = await store.create_campaign(
        channel="stable", version="1.0.0", on_connect=False, expires_at=None, targets=[]
    )
    dev_id = await store.create_campaign(
        channel="dev", version="1.1.0-dev.5", on_connect=False, expires_at=None, targets=[]
    )
    # approving the dev campaign must not revoke the concurrently-active stable one
    stable_active = await store.get_active_campaign(channel="stable")
    dev_active = await store.get_active_campaign(channel="dev")
    assert stable_active["id"] == stable_id
    assert dev_active["id"] == dev_id
    assert stable_active["status"] == "active"
    assert dev_active["status"] == "active"
    await store.close()


async def test_list_campaigns_scoped_by_channel(tmp_path) -> None:
    store = await _store(tmp_path)
    await store.create_campaign(
        channel="stable", version="1.0.0", on_connect=False, expires_at=None, targets=[]
    )
    await store.create_campaign(
        channel="dev", version="1.1.0-dev.1", on_connect=False, expires_at=None, targets=[]
    )
    stable_list = await store.list_campaigns(channel="stable")
    dev_list = await store.list_campaigns(channel="dev")
    assert [c["version"] for c in stable_list] == ["1.0.0"]
    assert [c["version"] for c in dev_list] == ["1.1.0-dev.1"]
    await store.close()


async def test_channel_column_migrated_onto_pre_existing_db(tmp_path) -> None:
    """A DB file created before the ``channel`` column existed gets it added
    on the next connect (mirrors ``keystore.KeyStore``'s grace-column migration)."""

    db_path = str(tmp_path / "updates.sqlite")
    # Simulate a pre-migration DB: create the table without the `channel` column.
    conn = await aiosqlite.connect(db_path)
    await conn.executescript(
        """
        CREATE TABLE update_campaigns (
            id           TEXT PRIMARY KEY,
            component    TEXT NOT NULL DEFAULT 'agent',
            version      TEXT NOT NULL,
            on_connect   INTEGER NOT NULL DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TEXT NOT NULL,
            expires_at   TEXT,
            revoked_at   TEXT,
            completed_at TEXT
        );
        """
    )
    await conn.execute(
        "INSERT INTO update_campaigns (id, version, on_connect, status, created_at) "
        "VALUES ('legacy-id', '0.9.0', 0, 'active', '2025-01-01T00:00:00+00:00')"
    )
    await conn.commit()
    await conn.close()

    store = UpdateStore(db_path)
    await store.connect()
    row = await store.get_campaign("legacy-id")
    assert row is not None
    # the migrated column defaults to 'stable' for a pre-existing row
    assert row["channel"] == "stable"
    await store.close()


async def test_desired_channel_round_trip(tmp_path) -> None:
    store = await _store(tmp_path)
    # no row -> defaults to stable
    assert await store.get_desired_channel("pc-1") == "stable"
    await store.set_desired_channel("pc-1", "dev")
    assert await store.get_desired_channel("pc-1") == "dev"
    # upsert: setting again updates rather than duplicating
    await store.set_desired_channel("pc-1", "stable")
    assert await store.get_desired_channel("pc-1") == "stable"
    await store.close()


async def test_desired_channel_rejects_invalid_value(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        await store.set_desired_channel("pc-1", "beta")
        assert False, "expected ValueError"
    except ValueError:
        pass
    await store.close()


async def test_list_desired_channels(tmp_path) -> None:
    store = await _store(tmp_path)
    await store.set_desired_channel("pc-1", "dev")
    await store.set_desired_channel("pc-2", "stable")
    channels = await store.list_desired_channels()
    assert channels == {"pc-1": "dev", "pc-2": "stable"}
    await store.close()

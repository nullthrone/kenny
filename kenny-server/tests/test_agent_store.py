"""``AgentStore``: per-agent mode and run history (ADR-0071)."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.agents.spec import MODES
from kenny_server.agents.store import (
    INTERRUPTED_ERROR,
    AgentRun,
    AgentStore,
)


@pytest.fixture
async def store(tmp_path) -> AsyncIterator[AgentStore]:
    s = AgentStore(str(tmp_path / "kenny.sqlite"))
    await s.connect()
    yield s
    await s.close()


async def start(store: AgentStore, **kw: object) -> AgentRun:
    args: dict[str, object] = {
        "agent_id": "triage",
        "spec_hash": "h1",
        "trigger": "ticket_created",
        "mode": "shadow",
    }
    args.update(kw)
    return await store.start_run(**args)  # type: ignore[arg-type]


async def backdate(store: AgentStore, run_id: str, when: datetime) -> None:
    """Rewrite a run's start stamp; the store itself only ever stamps ``now``."""

    await store._conn.execute(
        "UPDATE agent_runs SET started_at = ? WHERE id = ?", (when.isoformat(), run_id)
    )
    await store._conn.commit()


# -- lifecycle -----------------------------------------------------------------


async def test_connect_is_idempotent_and_close_is_safe_twice(tmp_path) -> None:
    s = AgentStore(str(tmp_path / "kenny.sqlite"))
    await s.connect()
    await s.connect()
    await s.close()
    await s.close()


async def test_use_before_connect_says_so(tmp_path) -> None:
    s = AgentStore(str(tmp_path / "kenny.sqlite"))
    with pytest.raises(RuntimeError, match="not connected"):
        await s.get_mode("triage")


async def test_schema_has_the_documented_columns_and_indexes(store: AgentStore) -> None:
    async with store._conn.execute("PRAGMA table_info(agent_runs)") as cur:
        cols = [r["name"] for r in await cur.fetchall()]
    assert cols == [f.name for f in fields(AgentRun)]
    async with store._conn.execute("PRAGMA table_info(agent_settings)") as cur:
        assert [r["name"] for r in await cur.fetchall()] == [
            "agent_id",
            "mode",
            "updated_at",
            "updated_by",
        ]
    async with store._conn.execute("PRAGMA index_list(agent_runs)") as cur:
        indexes = {r["name"] for r in await cur.fetchall()}
    assert {"idx_agent_runs_agent", "idx_agent_runs_started"} <= indexes


async def test_data_survives_a_reconnect(tmp_path) -> None:
    path = str(tmp_path / "kenny.sqlite")
    a = AgentStore(path)
    await a.connect()
    await a.set_mode("triage", "act", actor="alice")
    run = await start(a)
    await a.close()
    b = AgentStore(path)
    await b.connect()
    try:
        assert await b.get_mode("triage") == "act"
        assert await b.get_run(run.id) == run
    finally:
        await b.close()


# -- mode ----------------------------------------------------------------------


async def test_get_mode_is_none_until_one_is_set(store: AgentStore) -> None:
    assert await store.get_mode("triage") is None


async def test_set_mode_round_trips_and_overwrites(store: AgentStore) -> None:
    await store.set_mode("triage", "act", actor="alice")
    assert await store.get_mode("triage") == "act"
    await store.set_mode("triage", "off", actor="bob")
    assert await store.get_mode("triage") == "off"
    async with store._conn.execute(
        "SELECT updated_by, updated_at FROM agent_settings WHERE agent_id = 'triage'"
    ) as cur:
        row = await cur.fetchone()
    assert row["updated_by"] == "bob"
    datetime.fromisoformat(row["updated_at"])


async def test_modes_are_per_agent(store: AgentStore) -> None:
    await store.set_mode("triage", "act", actor="alice")
    await store.set_mode("other", "off", actor="alice")
    assert await store.get_mode("triage") == "act"
    assert await store.get_mode("other") == "off"


@pytest.mark.parametrize("mode", MODES)
async def test_every_declared_mode_can_be_set(store: AgentStore, mode: str) -> None:
    await store.set_mode("triage", mode, actor="alice")
    assert await store.get_mode("triage") == mode


async def test_an_unknown_mode_is_refused_and_nothing_is_written(store: AgentStore) -> None:
    with pytest.raises(ValueError, match="mode"):
        await store.set_mode("triage", "yolo", actor="alice")
    assert await store.get_mode("triage") is None
    with pytest.raises(ValueError, match="mode"):
        await start(store, mode="yolo")
    assert await store.list_runs() == []


# -- start / finish ------------------------------------------------------------


async def test_start_run_opens_a_running_row(store: AgentStore) -> None:
    run = await start(store, subject="ticket 7", host_id="pc1", ticket_id="t7")
    assert run.status == "running"
    assert (run.agent_id, run.spec_hash, run.trigger, run.mode) == (
        "triage",
        "h1",
        "ticket_created",
        "shadow",
    )
    assert (run.subject, run.host_id, run.ticket_id) == ("ticket 7", "pc1", "t7")
    assert run.finished_at is None
    assert run.verdict is None and run.summary is None and run.error is None
    assert (run.input_tokens, run.output_tokens) == (0, 0)
    assert (run.cache_read_tokens, run.cache_creation_tokens) == (0, 0)
    stamp = datetime.fromisoformat(run.started_at)
    assert stamp.utcoffset() == timedelta(0)
    assert await store.get_run(run.id) == run


async def test_run_ids_are_unique(store: AgentStore) -> None:
    ids = {(await start(store)).id for _ in range(5)}
    assert len(ids) == 5


async def test_finish_run_records_outcome_and_usage(store: AgentStore) -> None:
    run = await start(store, ticket_id="t7")
    done = await store.finish_run(
        run.id,
        status="completed",
        verdict="phantom",
        summary="no such device",
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 5,
            "cache_creation_tokens": 7,
        },
    )
    assert done.status == "completed"
    assert done.verdict == "phantom"
    assert done.summary == "no such device"
    assert (done.input_tokens, done.output_tokens) == (100, 20)
    assert (done.cache_read_tokens, done.cache_creation_tokens) == (5, 7)
    assert done.error is None
    assert done.ticket_id == "t7"  # kept: finish passed none
    assert done.finished_at is not None
    assert done.started_at == run.started_at
    assert await store.get_run(run.id) == done


async def test_missing_usage_keys_count_as_zero(store: AgentStore) -> None:
    run = await start(store)
    done = await store.finish_run(run.id, status="completed", usage={"output_tokens": 9})
    assert (done.input_tokens, done.output_tokens) == (0, 9)
    assert (done.cache_read_tokens, done.cache_creation_tokens) == (0, 0)
    none_usage = await store.finish_run((await start(store)).id, status="skipped")
    assert none_usage.input_tokens == 0


async def test_finish_can_attach_a_ticket_and_an_error(store: AgentStore) -> None:
    run = await start(store)
    done = await store.finish_run(run.id, status="failed", error="boom", ticket_id="t9")
    assert done.status == "failed"
    assert done.error == "boom"
    assert done.ticket_id == "t9"


@pytest.mark.parametrize("status", ["completed", "failed", "skipped"])
async def test_each_final_status_is_accepted(store: AgentStore, status: str) -> None:
    run = await start(store)
    assert (await store.finish_run(run.id, status=status)).status == status


@pytest.mark.parametrize("status", ["running", "done", "", "COMPLETED"])
async def test_a_non_final_or_unknown_status_is_refused(store: AgentStore, status: str) -> None:
    run = await start(store)
    with pytest.raises(ValueError, match="status"):
        await store.finish_run(run.id, status=status)
    still = await store.get_run(run.id)
    assert still is not None and still.status == "running"


async def test_finishing_twice_is_refused_and_keeps_the_first_outcome(store: AgentStore) -> None:
    run = await start(store)
    first = await store.finish_run(run.id, status="completed", usage={"input_tokens": 3})
    with pytest.raises(ValueError, match="already completed"):
        await store.finish_run(run.id, status="failed", usage={"input_tokens": 999})
    assert await store.get_run(run.id) == first


async def test_finishing_an_unknown_run_is_refused(store: AgentStore) -> None:
    with pytest.raises(ValueError, match="no agent run"):
        await store.finish_run("nope", status="completed")


async def test_get_run_unknown_is_none(store: AgentStore) -> None:
    assert await store.get_run("nope") is None


async def test_actions_and_recommendations_default_to_empty_lists(store: AgentStore) -> None:
    run = await start(store)
    assert run.actions == [] and run.recommendations == []
    done = await store.finish_run(run.id, status="completed")
    assert done.actions == [] and done.recommendations == []
    assert done.to_public()["actions"] == []
    assert done.to_public()["recommendations"] == []


async def test_actions_and_recommendations_round_trip_as_given(store: AgentStore) -> None:
    action = {
        "tool": "diag_services",
        "args": {"name": "Spooler"},
        "agent_id": "pc1",
        "tool_class": "read_only",
        "ok": True,
    }
    rec = {
        "tool": "winget_update",
        "args": {"id": "Git.Git"},
        "agent_id": "pc1",
        "tool_class": "standard_change",
        "code": "shadow_mode",
    }
    run = await start(store)
    done = await store.finish_run(
        run.id, status="completed", actions=[action, action], recommendations=(rec,)
    )
    assert done.actions == [action, action]
    assert done.recommendations == [rec]
    assert await store.get_run(run.id) == done
    public = done.to_public()
    assert public["actions"] == [action, action]
    assert public["recommendations"] == [rec]
    # A copy: mutating the public dict does not reach the frozen record.
    public["actions"].append({"tool": "x"})
    assert len(done.actions) == 2
    assert [r.actions for r in await store.list_runs()] == [[action, action]]


async def test_unserialisable_argument_values_are_stringified_not_fatal(
    store: AgentStore,
) -> None:
    run = await start(store)
    done = await store.finish_run(
        run.id, status="completed", actions=[{"tool": "t", "args": {"when": datetime(2026, 1, 1)}}]
    )
    assert done.actions == [{"tool": "t", "args": {"when": "2026-01-01 00:00:00"}}]


# -- count_running -------------------------------------------------------------


async def test_count_running_counts_only_in_flight_runs(store: AgentStore) -> None:
    assert await store.count_running() == 0
    a = await start(store)
    await start(store)
    await start(store, agent_id="other")
    assert await store.count_running() == 3
    assert await store.count_running(agent_id="triage") == 2
    assert await store.count_running(agent_id="other") == 1
    assert await store.count_running(agent_id="missing") == 0
    await store.finish_run(a.id, status="completed")
    assert await store.count_running(agent_id="triage") == 1
    await store.fail_interrupted()
    assert await store.count_running() == 0


# -- list ----------------------------------------------------------------------


async def test_list_runs_is_newest_first_and_filterable(store: AgentStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = await start(store, agent_id="triage")
    b = await start(store, agent_id="other")
    c = await start(store, agent_id="triage")
    for offset, run in enumerate((a, b, c)):
        await backdate(store, run.id, base + timedelta(hours=offset))
    assert [r.id for r in await store.list_runs()] == [c.id, b.id, a.id]
    assert [r.id for r in await store.list_runs(agent_id="triage")] == [c.id, a.id]
    assert [r.id for r in await store.list_runs(limit=2)] == [c.id, b.id]
    assert await store.list_runs(agent_id="missing") == []


async def test_list_runs_orders_same_instant_runs_by_insertion(store: AgentStore) -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first, second = await start(store), await start(store)
    await backdate(store, first.id, when)
    await backdate(store, second.id, when)
    assert [r.id for r in await store.list_runs()] == [second.id, first.id]


# -- tokens_since --------------------------------------------------------------


async def test_tokens_since_sums_input_and_output_inside_the_window(store: AgentStore) -> None:
    now = datetime.now(timezone.utc)
    old = await start(store)
    recent = await start(store)
    other = await start(store, agent_id="other")
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1000}
    for run in (old, recent, other):
        await store.finish_run(run.id, status="completed", usage=usage)
    await backdate(store, old.id, now - timedelta(days=2))

    since = (now - timedelta(days=1)).isoformat()
    assert await store.tokens_since(since) == 30  # recent + other; cache not counted
    assert await store.tokens_since(since, agent_id="triage") == 15
    assert await store.tokens_since(since, agent_id="other") == 15
    assert await store.tokens_since((now - timedelta(days=3)).isoformat()) == 45
    assert await store.tokens_since((now + timedelta(days=1)).isoformat()) == 0


async def test_tokens_since_is_inclusive_of_the_boundary(store: AgentStore) -> None:
    when = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    run = await start(store)
    await store.finish_run(run.id, status="completed", usage={"input_tokens": 4})
    await backdate(store, run.id, when)
    assert await store.tokens_since(when.isoformat()) == 4
    assert await store.tokens_since((when + timedelta(microseconds=1)).isoformat()) == 0


async def test_tokens_since_compares_instants_not_text(store: AgentStore) -> None:
    """A cutoff in another offset, or with ``Z``, means the same instant."""

    when = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    run = await start(store)
    await store.finish_run(run.id, status="completed", usage={"input_tokens": 4})
    await backdate(store, run.id, when)
    assert await store.tokens_since("2026-03-01T12:00:00Z") == 4
    assert await store.tokens_since("2026-03-01T14:00:00+02:00") == 4
    assert await store.tokens_since("2026-03-01T14:00:01+02:00") == 0
    assert await store.tokens_since("2026-03-01T12:00:00") == 4  # naive means UTC


async def test_tokens_since_counts_a_run_still_running(store: AgentStore) -> None:
    run = await start(store)
    assert run.status == "running"
    assert await store.tokens_since("2000-01-01T00:00:00+00:00") == 0


async def test_tokens_since_rejects_garbage(store: AgentStore) -> None:
    with pytest.raises(ValueError):
        await store.tokens_since("yesterday")


# -- fail_interrupted ----------------------------------------------------------


async def test_fail_interrupted_fails_only_running_runs(store: AgentStore) -> None:
    running_a = await start(store)
    running_b = await start(store, agent_id="other")
    completed = await start(store)
    await store.finish_run(completed.id, status="completed", verdict="phantom")

    assert await store.fail_interrupted() == 2

    for run in (running_a, running_b):
        got = await store.get_run(run.id)
        assert got is not None
        assert got.status == "failed"
        assert got.error == INTERRUPTED_ERROR == "interrupted by a server restart"
        assert got.finished_at is not None
    untouched = await store.get_run(completed.id)
    assert untouched is not None
    assert untouched.status == "completed" and untouched.error is None
    assert await store.fail_interrupted() == 0


async def test_an_interrupted_run_cannot_be_finished_again(store: AgentStore) -> None:
    run = await start(store)
    await store.fail_interrupted()
    with pytest.raises(ValueError, match="already failed"):
        await store.finish_run(run.id, status="completed")


# -- prune ---------------------------------------------------------------------


async def test_prune_deletes_old_finished_runs_and_keeps_running_ones(store: AgentStore) -> None:
    now = datetime.now(timezone.utc)
    old_done = await start(store)
    old_failed = await start(store)
    old_skipped = await start(store)
    old_running = await start(store)
    new_done = await start(store)
    await store.finish_run(old_done.id, status="completed")
    await store.finish_run(old_failed.id, status="failed", error="x")
    await store.finish_run(old_skipped.id, status="skipped")
    await store.finish_run(new_done.id, status="completed")
    for run in (old_done, old_failed, old_skipped, old_running):
        await backdate(store, run.id, now - timedelta(days=60))

    assert await store.prune((now - timedelta(days=30)).isoformat()) == 3

    remaining = {r.id for r in await store.list_runs()}
    assert remaining == {old_running.id, new_done.id}
    assert await store.prune((now - timedelta(days=30)).isoformat()) == 0


async def test_prune_cutoff_is_exclusive(store: AgentStore) -> None:
    when = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    run = await start(store)
    await store.finish_run(run.id, status="completed")
    await backdate(store, run.id, when)
    assert await store.prune(when.isoformat()) == 0
    assert await store.prune((when + timedelta(seconds=1)).isoformat()) == 1


# -- AgentRun ------------------------------------------------------------------


async def test_agent_run_is_frozen_and_publishes_every_column(store: AgentStore) -> None:
    run = await start(store)
    with pytest.raises(FrozenInstanceError):
        run.status = "completed"  # type: ignore[misc]
    public = run.to_public()
    assert set(public) == {f.name for f in fields(AgentRun)}
    assert public["id"] == run.id
    assert public["status"] == "running"
    assert public["finished_at"] is None


async def test_the_table_matches_the_dataclass_on_a_fresh_db(tmp_path) -> None:
    path = str(tmp_path / "kenny.sqlite")
    s = AgentStore(path)
    await s.connect()
    await s.close()
    with sqlite3.connect(path) as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(agent_runs)")]
    assert cols == [f.name for f in fields(AgentRun)]

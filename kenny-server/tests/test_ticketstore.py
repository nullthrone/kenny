"""``TicketStore`` schema and retention tests."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.ticketstore import TicketStore, to_iso

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _store(tmp_path, name: str = "tickets.sqlite", **kwargs) -> TicketStore:
    store = TicketStore(str(tmp_path / name), **kwargs)
    await store.connect()
    return store


async def test_connect_is_idempotent(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        # Second connect() must not re-run the schema against a live connection
        # nor swap the connection out from under an in-flight caller.
        conn = store._conn
        await store.connect()
        assert store._conn is conn
    finally:
        await store.close()

    # A brand new store over the *same* file re-runs the schema harmlessly.
    again = await _store(tmp_path)
    try:
        assert await again.list() == []
    finally:
        await again.close()


async def test_ticket_round_trips(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(
            title="Printer offline",
            origin="discord",
            priority="high",
            category="hardware",
            requester_user_id=12,
            agent_id="pc-lena",
            role_snapshot="user",
            profile_snapshot="family",
            summary="cannot print",
            now=NOW,
        )
        assert ticket.number == 1
        assert ticket.state == "new"
        assert ticket.closed_at is None
        assert ticket.created_at == to_iso(NOW)

        fetched = await store.get(ticket.id)
        assert fetched == ticket
        assert await store.get_by_number(1) == ticket
        assert await store.get("nope") is None

        patched = await store.update(
            ticket.id,
            summary="printer spooler stuck",
            resolution="restarted spooler",
            priority="normal",
            category="software",
            now=NOW,
        )
        assert patched is not None
        assert patched.summary == "printer spooler stuck"
        assert patched.resolution == "restarted spooler"
        assert patched.priority == "normal"
        assert patched.category == "software"
        # The state column is not reachable through update().
        assert patched.state == "new"
    finally:
        await store.close()


async def test_list_filters_by_state_and_requester(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        a = await store.create(title="a", origin="discord", requester_user_id=1, now=NOW)
        b = await store.create(
            title="b",
            origin="dashboard",
            requester_user_id=2,
            state="in_progress",
            now=NOW + timedelta(seconds=1),
        )
        await store.create(title="c", origin="alert", now=NOW + timedelta(seconds=2))

        assert [t.id for t in await store.list(state="in_progress")] == [b.id]
        assert [t.id for t in await store.list(requester_user_id=1)] == [a.id]
        assert len(await store.list(states=["new"])) == 2
        assert len(await store.list(limit=2)) == 2
        # Newest-updated first.
        assert [t.title for t in await store.list()] == ["c", "b", "a"]
        cutoff = to_iso(NOW + timedelta(seconds=1))
        assert [t.title for t in await store.list(updated_before=cutoff)] == ["a"]
    finally:
        await store.close()


async def test_set_state_writes_event_and_stamps_closed_at(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="alert", now=NOW)
        moved = await store.set_state(
            ticket.id, "triage", actor="system", reason="picked up", now=NOW
        )
        assert moved is not None
        assert moved.state == "triage"
        assert moved.closed_at is None

        closed = await store.set_state(
            ticket.id, "cancelled", actor="operator:3", now=NOW + timedelta(minutes=1)
        )
        assert closed is not None
        assert closed.closed_at == to_iso(NOW + timedelta(minutes=1))

        events = await store.list_events(ticket.id, kind="state")
        assert [(e.from_state, e.to_state, e.actor) for e in events] == [
            ("new", "triage", "system"),
            ("triage", "cancelled", "operator:3"),
        ]
        assert events[0].summary == "picked up"
        assert await store.set_state("nope", "triage", actor="system") is None
    finally:
        await store.close()


async def test_set_agent_id_records_handoff(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="alert", agent_id="pc-a", now=NOW)
        moved = await store.set_agent_id(
            ticket.id, "pc-b", actor="operator:1", reason="wrong host", now=NOW
        )
        assert moved is not None and moved.agent_id == "pc-b"
        (event,) = await store.list_events(ticket.id, kind="handoff")
        assert event.fields == {"from_agent_id": "pc-a", "to_agent_id": "pc-b"}
    finally:
        await store.close()


async def test_event_round_trips(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="dashboard", now=NOW)
        await store.append_event(
            ticket_id=ticket.id,
            kind="tool_call",
            actor="system",
            tool="account_create",
            tool_class="admin",
            ok=True,
            summary="created account",
            fields={"args": {"username": "lena"}},
            now=NOW,
        )
        (event,) = await store.list_events(ticket.id, kind="tool_call")
        assert event.tool == "account_create"
        assert event.tool_class == "admin"
        assert event.ok is True
        assert event.fields == {"args": {"username": "lena"}}
        assert event.from_state is None
    finally:
        await store.close()


async def test_run_state_round_trips_and_merges(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="dashboard", now=NOW)
        empty = await store.load_run(ticket.id)
        assert empty.messages == [] and empty.queue == [] and empty.turns == 0

        await store.save_run(
            ticket.id,
            messages=[{"role": "user", "content": "hi"}],
            staged_results=[{"tool_use_id": "tu1"}],
            queue=["screen_capture"],
            turns=1,
            now=NOW,
        )
        # Omitted parts keep their stored value.
        merged = await store.save_run(ticket.id, turns=2, now=NOW)
        assert merged.turns == 2
        assert merged.messages == [{"role": "user", "content": "hi"}]

        loaded = await store.load_run(ticket.id)
        assert loaded.staged_results == [{"tool_use_id": "tu1"}]
        assert loaded.queue == ["screen_capture"]
        assert loaded.updated_at == to_iso(NOW)
    finally:
        await store.close()


async def test_approval_round_trips_and_decides(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="discord", now=NOW)
        approval = await store.create_approval(
            ticket_id=ticket.id,
            tool_use_id="tu1",
            tool="account_create",
            tool_class="admin",
            args={"username": "lena", "password": "hunter2"},
            kind="operator_approval",
            agent_id="pc-lena",
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )
        assert approval.status == "pending"
        assert approval.args == {"username": "lena", "password": "hunter2"}
        assert approval.expires_at == to_iso(NOW + timedelta(hours=1))

        assert await store.get_open_approval(ticket.id) == approval
        assert [a.id for a in await store.list_open_approvals()] == [approval.id]
        assert await store.list_open_approvals(due_at=NOW) == []
        assert [
            a.id for a in await store.list_open_approvals(due_at=NOW + timedelta(hours=2))
        ] == [approval.id]

        assert await store.set_approval_message(
            approval.id, channel_id="c1", message_id="m1"
        )
        decided = await store.decide_approval(
            approval.id,
            status="approved",
            decided_by=3,
            decided_via="dashboard",
            now=NOW + timedelta(minutes=5),
        )
        assert decided is not None
        assert decided.status == "approved"
        assert decided.decided_by == 3
        assert decided.decided_via == "dashboard"
        assert decided.discord_channel_id == "c1"
        assert decided.discord_message_id == "m1"
        # A second decision finds nothing pending.
        assert await store.decide_approval(approval.id, status="denied") is None
        assert await store.get_open_approval(ticket.id) is None
    finally:
        await store.close()


async def test_second_pending_approval_violates_partial_index(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="discord", now=NOW)
        first = await store.create_approval(
            ticket_id=ticket.id,
            tool_use_id="tu1",
            tool="service_restart",
            tool_class="admin",
            args={},
            kind="operator_approval",
            now=NOW,
        )
        # The partial UNIQUE index (ticket_id WHERE status = 'pending') is what
        # enforces "at most one open gate per ticket" -- not application logic.
        with pytest.raises(sqlite3.IntegrityError):
            await store.create_approval(
                ticket_id=ticket.id,
                tool_use_id="tu2",
                tool="reboot",
                tool_class="admin",
                args={},
                kind="operator_approval",
                now=NOW,
            )
        # Once the first is decided, a new gate may open.
        await store.expire_approval(first.id, now=NOW)
        second = await store.create_approval(
            ticket_id=ticket.id,
            tool_use_id="tu2",
            tool="reboot",
            tool_class="admin",
            args={},
            kind="operator_approval",
            now=NOW,
        )
        assert second.status == "pending"
        expired = await store.get_approval(first.id)
        assert expired is not None and expired.status == "expired"
    finally:
        await store.close()


async def test_channel_bind_get_and_lookup_by_thread(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="discord", now=NOW)
        bound = await store.bind_channel(
            ticket_id=ticket.id,
            guild_id="g1",
            channel_id="c1",
            thread_id="t1",
            private=True,
            now=NOW,
        )
        assert bound.private is True
        assert await store.get_channel(ticket.id) == bound
        found = await store.channel_by_thread("t1")
        assert found is not None and found.ticket_id == ticket.id
        assert await store.channel_by_thread("nope") is None

        rebound = await store.bind_channel(
            ticket_id=ticket.id,
            guild_id="g1",
            channel_id="c1",
            thread_id="t2",
            private=False,
            now=NOW,
        )
        assert rebound.thread_id == "t2"
        assert rebound.private is False
        assert await store.archive_channel(ticket.id, now=NOW)
        archived = await store.get_channel(ticket.id)
        assert archived is not None and archived.archived_at == to_iso(NOW)
    finally:
        await store.close()


async def test_prune_drops_old_runs_but_keeps_the_record(tmp_path) -> None:
    store = await _store(tmp_path, run_retention_days=30)
    try:
        old = await store.create(title="old", origin="discord", now=NOW - timedelta(days=90))
        recent = await store.create(title="recent", origin="discord", now=NOW)
        live = await store.create(title="live", origin="discord", now=NOW)
        for ticket in (old, recent, live):
            await store.save_run(ticket.id, messages=[{"role": "user"}], now=NOW)
        await store.set_state(
            old.id, "cancelled", actor="operator:1", now=NOW - timedelta(days=60)
        )
        await store.set_state(recent.id, "cancelled", actor="operator:1", now=NOW)

        assert await store.prune(now=NOW) == 1

        # Only the long-closed ticket's transcript is gone ...
        assert (await store.load_run(old.id)).messages == []
        assert (await store.load_run(recent.id)).messages == [{"role": "user"}]
        assert (await store.load_run(live.id)).messages == [{"role": "user"}]
        # ... the ticket and its trail survive: they are the curated record.
        assert await store.get(old.id) is not None
        assert len(await store.list_events(old.id)) == 1
    finally:
        await store.close()


async def test_number_is_monotonic_under_concurrent_creates(tmp_path) -> None:
    # Two stores = two connections on the same file, so the number really is
    # contended rather than serialized by one aiosqlite worker thread.
    db = str(tmp_path / "tickets.sqlite")
    a, b = TicketStore(db), TicketStore(db)
    await a.connect()
    await b.connect()
    try:
        first, second = await asyncio.gather(
            a.create(title="a", origin="discord", now=NOW),
            b.create(title="b", origin="dashboard", now=NOW),
        )
        assert {first.number, second.number} == {1, 2}

        more = await asyncio.gather(
            *[a.create(title=f"t{i}", origin="alert", now=NOW) for i in range(5)],
            *[b.create(title=f"u{i}", origin="alert", now=NOW) for i in range(5)],
        )
        numbers = sorted(t.number for t in more)
        assert numbers == list(range(3, 13))
    finally:
        await a.close()
        await b.close()


async def test_delete_removes_everything_hanging_off_the_ticket(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="discord", now=NOW)
        await store.save_run(ticket.id, messages=[{"role": "user"}], now=NOW)
        await store.append_event(
            ticket_id=ticket.id, kind="note", actor="operator:1", summary="hi", now=NOW
        )
        await store.create_approval(
            ticket_id=ticket.id,
            tool_use_id="tu1",
            tool="reboot",
            tool_class="admin",
            args={},
            kind="operator_approval",
            now=NOW,
        )
        await store.bind_channel(
            ticket_id=ticket.id, guild_id="g", channel_id="c", thread_id="t", now=NOW
        )

        assert await store.delete(ticket.id) is True
        assert await store.get(ticket.id) is None
        assert await store.list_events(ticket.id) == []
        assert await store.get_open_approval(ticket.id) is None
        assert await store.get_channel(ticket.id) is None
        assert (await store.load_run(ticket.id)).messages == []
        assert await store.delete(ticket.id) is False
    finally:
        await store.close()


async def test_use_before_connect_raises(tmp_path) -> None:
    store = TicketStore(str(tmp_path / "tickets.sqlite"))
    with pytest.raises(RuntimeError):
        await store.get("x")


# -- pending requests (the pre-ticket "which PC?" phase) ----------------------


async def _pending(store: TicketStore, **kwargs):
    return await store.open_pending_request(
        discord_user_id="900000000000000001",
        user_id=7,
        guild_id="g1",
        channel_id="c1",
        content="my pc is slow",
        candidates=["a-pc", "b-pc"],
        **kwargs,
    )


async def test_pending_request_round_trips(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        opened = await _pending(store, message_id="m1", now=NOW)
        assert opened.candidates == ["a-pc", "b-pc"]
        assert opened.consumed_at is None
        # The window is derived from the creation stamp, not the wall clock, so
        # an injected clock governs both ends of it.
        assert opened.expires_at == to_iso(NOW + timedelta(seconds=900))
        assert await store.get_pending_request(opened.id) == opened
        assert await store.get_pending_request("nope") is None
    finally:
        await store.close()


async def test_pending_request_is_consumed_exactly_once(tmp_path) -> None:
    """Two buttons clicked in quick succession must not open two tickets."""

    store = await _store(tmp_path)
    try:
        opened = await _pending(store, now=NOW)
        claimed = await store.consume_pending_request(opened.id, now=NOW)
        assert claimed is not None and claimed.content == "my pc is slow"
        assert await store.consume_pending_request(opened.id, now=NOW) is None
        # The losing click still finds the row — it just cannot claim it.
        stale = await store.get_pending_request(opened.id)
        assert stale is not None and stale.consumed_at is not None
    finally:
        await store.close()


async def test_an_expired_pending_request_cannot_be_consumed(tmp_path) -> None:
    """A card from last week must not open a ticket about last week's problem."""

    store = await _store(tmp_path)
    try:
        opened = await _pending(store, ttl_secs=900, now=NOW)
        late = NOW + timedelta(seconds=901)
        assert await store.consume_pending_request(opened.id, now=late) is None
        untouched = await store.get_pending_request(opened.id)
        assert untouched is not None and untouched.consumed_at is None
    finally:
        await store.close()


async def test_prune_clears_dead_pending_requests_only(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        expired = await _pending(store, ttl_secs=60, now=NOW - timedelta(hours=1))
        consumed = await _pending(store, now=NOW)
        await store.consume_pending_request(consumed.id, now=NOW)
        live = await _pending(store, now=NOW)

        assert await store.prune(now=NOW) == 2

        assert await store.get_pending_request(expired.id) is None
        assert await store.get_pending_request(consumed.id) is None
        assert await store.get_pending_request(live.id) is not None
    finally:
        await store.close()

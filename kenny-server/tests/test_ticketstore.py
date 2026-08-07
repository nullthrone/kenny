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
            ticket.id, "in_progress", actor="system", reason="picked up", now=NOW
        )
        assert moved is not None
        assert moved.state == "in_progress"
        assert moved.closed_at is None

        closed = await store.set_state(
            ticket.id, "cancelled", actor="operator:3", now=NOW + timedelta(minutes=1)
        )
        assert closed is not None
        assert closed.closed_at == to_iso(NOW + timedelta(minutes=1))

        events = await store.list_events(ticket.id, kind="state")
        assert [(e.from_state, e.to_state, e.actor) for e in events] == [
            ("new", "in_progress", "system"),
            ("in_progress", "cancelled", "operator:3"),
        ]
        assert events[0].summary == "picked up"
        assert await store.set_state("nope", "in_progress", actor="system") is None
    finally:
        await store.close()


async def test_set_state_clears_the_block_when_leaving_in_progress(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="discord", now=NOW)
        await store.set_state(ticket.id, "in_progress", actor="system", now=NOW)
        await store.set_blocked(ticket.id, "user", actor="system", ref="tu-1", now=NOW)

        resolved = await store.set_state(
            ticket.id, "resolved", actor="operator:3", now=NOW + timedelta(minutes=1)
        )
        assert resolved is not None
        assert resolved.blocked_on == ""
        assert resolved.blocked_since is None
        assert resolved.blocked_ref == ""
        assert resolved.blocked_nudged_at is None

        # Re-entering in_progress does not resurrect the old block.
        reopened = await store.set_state(
            ticket.id, "in_progress", actor="operator:3", now=NOW + timedelta(minutes=2)
        )
        assert reopened is not None
        assert reopened.blocked_on == ""
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


async def test_set_blocked_records_block_event_and_resets_nudge(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="discord", now=NOW)
        blocked = await store.set_blocked(
            ticket.id, "user", actor="system", ref="tu-1", reason="waiting", now=NOW
        )
        assert blocked is not None
        assert blocked.blocked_on == "user"
        assert blocked.blocked_since == to_iso(NOW)
        assert blocked.blocked_ref == "tu-1"

        await store.mark_nudged(ticket.id, now=NOW + timedelta(hours=1))
        nudged = await store.get(ticket.id)
        assert nudged is not None and nudged.blocked_nudged_at == to_iso(NOW + timedelta(hours=1))

        # Re-blocking (escalation) resets the nudge stamp and blocked_since.
        escalated = await store.set_blocked(
            ticket.id,
            "operator",
            actor="system",
            ref="tu-1",
            now=NOW + timedelta(hours=2),
        )
        assert escalated is not None
        assert escalated.blocked_on == "operator"
        assert escalated.blocked_since == to_iso(NOW + timedelta(hours=2))
        assert escalated.blocked_nudged_at is None

        (b1, b2) = await store.list_events(ticket.id, kind="block")
        assert b1.fields == {"from_blocked_on": "", "to_blocked_on": "user", "ref": "tu-1"}
        assert b2.fields == {
            "from_blocked_on": "user",
            "to_blocked_on": "operator",
            "ref": "tu-1",
        }

        unblocked = await store.set_blocked(
            ticket.id, "", actor="operator:3", now=NOW + timedelta(hours=3)
        )
        assert unblocked is not None
        assert unblocked.blocked_on == ""
        assert unblocked.blocked_since is None

        assert await store.set_blocked("nope", "user", actor="system") is None
    finally:
        await store.close()


async def test_set_assignee_records_assign_event(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="dashboard", now=NOW)
        claimed = await store.set_assignee(ticket.id, 3, actor="operator:3", now=NOW)
        assert claimed is not None and claimed.assignee_user_id == 3
        unclaimed = await store.set_assignee(
            ticket.id, None, actor="operator:3", now=NOW + timedelta(minutes=1)
        )
        assert unclaimed is not None and unclaimed.assignee_user_id is None
        (e1, e2) = await store.list_events(ticket.id, kind="assign")
        assert e1.fields == {"from_assignee_user_id": None, "to_assignee_user_id": 3}
        assert e2.fields == {"from_assignee_user_id": 3, "to_assignee_user_id": None}
        assert await store.set_assignee("nope", 1, actor="operator:3") is None
    finally:
        await store.close()


async def test_list_filters_by_blocked_on_and_nudged(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        a = await store.create(title="a", origin="discord", now=NOW)
        b = await store.create(title="b", origin="discord", now=NOW)
        c = await store.create(title="c", origin="discord", now=NOW)
        for t in (a, b, c):
            await store.set_state(t.id, "in_progress", actor="system", now=NOW)
        await store.set_blocked(a.id, "user", actor="system", now=NOW)
        await store.set_blocked(b.id, "operator", actor="system", now=NOW)
        # c stays unblocked.

        assert [t.id for t in await store.list(blocked_on="user")] == [a.id]
        assert {t.id for t in await store.list(blocked_on_in=("user", "operator"))} == {
            a.id,
            b.id,
        }
        assert [t.id for t in await store.list(nudged=False, blocked_on="user")] == [a.id]
        await store.mark_nudged(a.id, now=NOW + timedelta(minutes=1))
        assert await store.list(nudged=False, blocked_on="user") == []
        assert [t.id for t in await store.list(nudged=True, blocked_on="user")] == [a.id]

        cutoff = to_iso(NOW + timedelta(minutes=1))
        assert {t.id for t in await store.list(blocked_on_in=("user", "operator"), blocked_before=cutoff)} == {
            a.id,
            b.id,
        }
        assert await store.list(blocked_before=to_iso(NOW - timedelta(minutes=1))) == []
    finally:
        await store.close()


async def test_counts_buckets_by_state_and_blocked_on(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        new_owned = await store.create(
            title="new", origin="discord", requester_user_id=1, now=NOW
        )
        new_alert = await store.create(title="alert", origin="alert", now=NOW)
        working = await store.create(
            title="working", origin="discord", requester_user_id=1, now=NOW
        )
        await store.set_state(working.id, "in_progress", actor="system", now=NOW)
        waiting = await store.create(
            title="waiting", origin="discord", requester_user_id=1, now=NOW
        )
        await store.set_state(waiting.id, "in_progress", actor="system", now=NOW)
        await store.set_blocked(waiting.id, "user", actor="system", now=NOW)
        gated = await store.create(title="gated", origin="discord", now=NOW)
        await store.set_state(gated.id, "in_progress", actor="system", now=NOW)
        await store.set_blocked(gated.id, "approval", actor="system", now=NOW)
        done = await store.create(title="done", origin="discord", now=NOW)
        await store.set_state(done.id, "in_progress", actor="system", now=NOW)
        await store.set_state(done.id, "resolved", actor="system", now=NOW)

        counts = await store.counts()
        assert counts == {"needs_you": 2, "waiting": 1, "working": 1, "new": 1, "done": 1}
        assert new_owned.id and new_alert.id  # both counted, one per bucket above

        scoped = await store.counts(requester_user_id=1)
        assert scoped == {"needs_you": 0, "waiting": 1, "working": 1, "new": 1, "done": 0}
    finally:
        await store.close()


async def test_migration_folds_legacy_states_into_the_two_axis_model(tmp_path) -> None:
    """A DB file written by the nine-state model must come up clean.

    Simulates a pre-migration row by inserting directly against the bare
    ``CREATE TABLE`` schema (no ``blocked_on`` columns yet, the legacy state
    strings) and then connecting the current :class:`TicketStore` over it --
    exactly what happens to a real operator's ``kenny.db`` on upgrade.
    """

    import aiosqlite

    from kenny_server.ticketstore import _SCHEMA
    from kenny_server.store import _configure_connection

    db_path = str(tmp_path / "legacy.sqlite")
    legacy_ids = {}
    raw = await aiosqlite.connect(db_path)
    try:
        await _configure_connection(raw)
        await raw.executescript(_SCHEMA)
        for i, state in enumerate(
            ["new", "triage", "in_progress", "awaiting_user", "awaiting_approval", "awaiting_agent"]
        ):
            ticket_id = f"legacy-{i}"
            legacy_ids[state] = ticket_id
            await raw.execute(
                "INSERT INTO tickets (id, number, title, state, origin, priority, "
                "requester_user_id, summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'discord', 'normal', 1, '', ?, ?)",
                (ticket_id, i + 1, state, state, to_iso(NOW), to_iso(NOW)),
            )
            await raw.execute(
                "INSERT INTO ticket_events (ticket_id, at, kind, actor, from_state, "
                "to_state, summary) VALUES (?, ?, 'state', 'system', NULL, ?, 'created')",
                (ticket_id, to_iso(NOW), state),
            )
        await raw.commit()
    finally:
        await raw.close()

    store = TicketStore(db_path)
    await store.connect()
    try:
        expect = {
            "new": ("new", ""),
            "triage": ("in_progress", ""),
            "in_progress": ("in_progress", ""),
            "awaiting_user": ("in_progress", "user"),
            "awaiting_approval": ("in_progress", "approval"),
            "awaiting_agent": ("in_progress", "operator"),
        }
        for legacy_state, (state, blocked_on) in expect.items():
            ticket = await store.get(legacy_ids[legacy_state])
            assert ticket is not None
            assert ticket.state == state
            assert ticket.blocked_on == blocked_on
            if blocked_on:
                assert ticket.blocked_since == to_iso(NOW)

        # The pre-migration trail is untouched -- ADR-0046 makes it the
        # authority, and back-dating it would be exactly what that forbids.
        events = await store.list_events(legacy_ids["awaiting_approval"], kind="state")
        assert events[-1].to_state == "awaiting_approval"
    finally:
        await store.close()

    # A second connect() over the same file changes nothing further.
    again = TicketStore(db_path)
    await again.connect()
    try:
        for legacy_state, (state, blocked_on) in expect.items():
            ticket = await again.get(legacy_ids[legacy_state])
            assert ticket is not None
            assert ticket.state == state
            assert ticket.blocked_on == blocked_on
    finally:
        await again.close()


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


# -- write_lock (ADR-0051): the _insert_event nesting must not deadlock -----
#
# set_state/set_agent_id/set_blocked/set_assignee/mark_nudged/append_event all
# hold write_lock() around an UPDATE + _insert_event() + commit, and
# _insert_event() itself takes write_lock() again on the same task. A
# non-reentrant lock would deadlock here; wait_for gives a hard ceiling so a
# regression fails fast instead of hanging the test run.


async def test_nested_event_write_does_not_deadlock(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        ticket = await store.create(title="a", origin="alert", now=NOW)
        moved = await asyncio.wait_for(
            store.set_state(ticket.id, "in_progress", actor="system", now=NOW),
            timeout=5,
        )
        assert moved is not None and moved.state == "in_progress"

        blocked = await asyncio.wait_for(
            store.set_blocked(ticket.id, "user", actor="system", now=NOW),
            timeout=5,
        )
        assert blocked is not None and blocked.blocked_on == "user"

        await asyncio.wait_for(
            store.append_event(ticket_id=ticket.id, kind="note", actor="system", now=NOW),
            timeout=5,
        )

        events = await store.list_events(ticket.id)
        assert [e.kind for e in events] == ["state", "block", "note"]
    finally:
        await store.close()


async def test_concurrent_set_state_calls_stay_exclusive(tmp_path) -> None:
    """The lock must still serialize across *different* tasks, not just make
    the same-task nesting a no-op — two concurrent set_state calls on two
    different tickets must both land cleanly rather than racing.
    """

    store = await _store(tmp_path)
    try:
        a = await store.create(title="a", origin="alert", now=NOW)
        b = await store.create(title="b", origin="alert", now=NOW)
        results = await asyncio.wait_for(
            asyncio.gather(
                store.set_state(a.id, "in_progress", actor="system", now=NOW),
                store.set_state(b.id, "in_progress", actor="system", now=NOW),
            ),
            timeout=5,
        )
        assert all(r is not None and r.state == "in_progress" for r in results)
        assert len(await store.list_events(a.id, kind="state")) == 1
        assert len(await store.list_events(b.id, kind="state")) == 1
    finally:
        await store.close()

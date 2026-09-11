"""`GET /api/inbox` -- the ticket queue: grouping, row shape, and scoping.

Every row is a ticket (ADR-0059). The tests that used to pin the flagged-section
and standalone-approval rows are gone with those rows; the guarantees they
carried did not go with them:

* `section_target()` is still what Today links a standing finding by, and is
  pinned there (`tests/test_today_api.py`).
* a scoped user's ownership boundary is pinned below on the alert-origin
  ticket, which is the row that now carries a host name into this aggregate.

`webui/inbox.py` is a standalone route builder (not folded into
`webui/tickets.py` -- another surface owns that file). Tests go through the
composed app (`build_app`) the same way `tests/test_today_api.py` does.
"""

from __future__ import annotations

import re
from functools import partial

from starlette.testclient import TestClient

from kenny_server.main import build_app

# Every key `kenny-web/src/api/types.ts`'s `InboxItem` declares.
INBOX_ITEM_KEYS = {
    "id",
    "waits_on",
    "priority",
    "title",
    "meta",
    "host",
    "age_seconds",
    "target",
}


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _hdr(token: str):
    return {"Authorization": f"Bearer {token}"}


_CRIT_SNAPSHOT = {"disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 97}]}}

_GROUPS = ("needs_you", "waiting", "working", "new", "done")


async def _seed_one_of_each(app):
    """One ticket per group, plus a crit host and a held gate.

    The crit host is seeded deliberately: it is the case that used to add a
    row and a count of its own, and the queue must now be indifferent to it.
    """

    tickets = app.state.tickets
    users = app.state.user_store

    working = await tickets.create(title="reinstall driver", origin="dashboard", actor="system")
    await tickets.transition(working.id, "in_progress", actor="system")

    waiting = await tickets.create(title="need a screenshot", origin="dashboard", actor="system")
    await tickets.transition(waiting.id, "in_progress", actor="system")
    await tickets.block(waiting.id, "user", actor="system")

    needs_you = await tickets.create(title="confirm risky change", origin="dashboard", actor="system")
    await tickets.transition(needs_you.id, "in_progress", actor="system")
    await tickets.block(needs_you.id, "operator", actor="system")

    # `new` with a requester. A `new` ticket with NO requester is alert-origin
    # and counts as needs_you instead -- see TicketStore.counts()'s docstring.
    requester = await users.create_user("req", "pw-123456", "user")
    await tickets.create(
        title="slow laptop", origin="dashboard", actor="system",
        requester_user_id=requester["id"],
    )

    done = await tickets.create(title="fixed already", origin="dashboard", actor="system")
    await tickets.transition(done.id, "in_progress", actor="system")
    await tickets.transition(done.id, "resolved", actor="system")

    gate_ticket = await tickets.create(
        title="apply update", origin="dashboard", agent_id="gate-pc", actor="system"
    )
    await tickets.transition(gate_ticket.id, "in_progress", actor="system")
    approval = await tickets.open_approval(
        gate_ticket.id,
        tool_use_id="tu-1",
        tool="winget_update",
        tool_class="standard_change",
        args={"id": "Some.App"},
        agent_id="gate-pc",
        actor="system",
    )
    # Mirrors TicketAssistant.on_hold: opening a gate and blocking the ticket
    # on it are two calls in production too.
    await tickets.block(gate_ticket.id, "approval", actor="system", ref=approval.id)
    return {"gate_ticket_id": gate_ticket.id, "approval_id": approval.id}


def test_the_inbox_lists_only_tickets(tmp_path) -> None:
    """Every id in every group resolves as a ticket.

    Driven against a fleet that has both of the things the queue used to also
    carry -- a crit section and a held approval -- so the assertion is "they
    are not rows", not "the fixture happened not to produce any".
    """

    app = build_app(db_path=str(tmp_path / "inbox-only-tickets.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.portal.call(partial(app.state.store.insert, "crit-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))
        seeded = c.portal.call(partial(_seed_one_of_each, app))

        ids: list[str] = []
        for group in _GROUPS:
            for item in c.get(f"/api/inbox?group={group}", headers=h).json()["items"]:
                assert set(item) == INBOX_ITEM_KEYS, (group, sorted(item))
                ids.append(item["id"])

        for tid in ids:
            assert c.get(f"/api/tickets/{tid}", headers=h).status_code == 200, tid

        # The approval's own id is not among them: it is a state of its ticket,
        # not a row of its own.
        assert seeded["approval_id"] not in ids
        assert seeded["gate_ticket_id"] in ids
        # And the crit host contributed nothing at all.
        assert "crit-pc" not in {
            i["host"] for i in c.get("/api/inbox?group=needs_you", headers=h).json()["items"]
        }


def test_the_rows_in_a_group_are_exactly_its_count(tmp_path) -> None:
    """The chip numbers and the list are one fact.

    Only assertable now that the counts are exactly `TicketStore.counts()`:
    while the route added flagged sections and approvals to `needs_you`, the
    count and the rows came from two different rules.
    """

    app = build_app(db_path=str(tmp_path / "inbox-counts.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.portal.call(partial(app.state.store.insert, "crit-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))
        c.portal.call(partial(_seed_one_of_each, app))

        for group in _GROUPS:
            body = c.get(f"/api/inbox?group={group}", headers=h).json()
            assert len(body["items"]) == body["counts"][group], group


def test_the_header_badge_and_the_needs_you_chip_are_the_same_number(tmp_path) -> None:
    """`Shell.tsx` reads `/api/tickets/summary` for the nav badge while the
    Inbox reads `/api/inbox`. Two sources for one number, which the queue's
    own docs already claimed were the same."""

    app = build_app(db_path=str(tmp_path / "inbox-badge.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.portal.call(partial(app.state.store.insert, "crit-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))
        c.portal.call(partial(_seed_one_of_each, app))

        summary = c.get("/api/tickets/summary", headers=h).json()
        counts = c.get("/api/inbox?group=needs_you", headers=h).json()["counts"]
        assert counts == {k: summary[k] for k in counts}


def test_a_ticket_blocked_on_an_approval_is_an_ordinary_row(tmp_path) -> None:
    """It waits for an approval, and it says so -- but the queue offers no
    decision: approving happens on the ticket, where the frozen call is."""

    app = build_app(db_path=str(tmp_path / "inbox-gate.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        seeded = c.portal.call(partial(_seed_one_of_each, app))

        items = c.get("/api/inbox?group=needs_you", headers=h).json()["items"]
        row = next(i for i in items if i["id"] == seeded["gate_ticket_id"])
        assert row["waits_on"] == "approval"
        assert "waiting for approval" in row["meta"]
        assert "gate" not in row


def test_every_row_carries_a_priority_the_vocabulary_declares(tmp_path) -> None:
    """The row's badge is the priority, so it is never absent and never a
    value the console has no colour for."""

    from kenny_server import tickets as tickets_module

    app = build_app(db_path=str(tmp_path / "inbox-priority.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.portal.call(partial(_seed_one_of_each, app))

        vocabulary = c.get("/api/tickets/vocabulary", headers=h).json()["priorities"]
        assert set(vocabulary) == set(tickets_module.PRIORITIES)
        for group in _GROUPS:
            for item in c.get(f"/api/inbox?group={group}", headers=h).json()["items"]:
                assert item["priority"] in vocabulary, item


_TARGET_TICKET_ID_RE = re.compile(r"^#/inbox/ticket/[0-9a-f]{32}$")


def test_inbox_ticket_target_is_fetchable_by_the_route_it_names(tmp_path) -> None:
    """An inbox row's `target` must be the ticket's uuid `id`, not its display
    `number` -- `/api/tickets/{tid}` only ever resolves `id` (see
    `TicketStore.get`; `get_by_number` has no HTTP route). This is a joined
    seam test: it fails if `_ticket_item`'s `target` ever regresses to
    `ticket.number`, even though nothing about the item's own shape would
    look wrong in isolation. Reproduces the "Could not load this ticket:
    not_found" bug for a ticket clicked from the Working lane.
    """

    app = build_app(db_path=str(tmp_path / "inbox-target.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        tickets = app.state.tickets

        async def seed():
            t = await tickets.create(title="DCOM errors", origin="dashboard", actor="system")
            await tickets.transition(t.id, "in_progress", actor="system")
            return t

        ticket = c.portal.call(seed)

        working = c.get("/api/inbox?group=working", headers=h).json()
        assert [i["title"] for i in working["items"]] == ["DCOM errors"]
        target = working["items"][0]["target"]

        # Shape check: catches a revert to `.number` even in a fixture where
        # `id` and `number` could both plausibly stringify to something short.
        assert _TARGET_TICKET_ID_RE.match(target), target

        # Round-trip: what the console actually does after following the
        # link -- fetch /api/tickets/{tid} with the tail of `target`.
        tid = target.removeprefix("#/inbox/ticket/")
        detail = c.get(f"/api/tickets/{tid}", headers=h)
        assert detail.status_code == 200
        assert detail.json()["id"] == ticket.id


def test_every_group_puts_its_tickets_where_they_belong(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "inbox-groups.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        c.portal.call(partial(_seed_one_of_each, app))

        def titles(group: str) -> set[str]:
            return {i["title"] for i in c.get(f"/api/inbox?group={group}", headers=h).json()["items"]}

        assert titles("needs_you") == {"confirm risky change", "apply update"}
        assert titles("waiting") == {"need a screenshot"}
        assert titles("working") == {"reinstall driver"}
        assert titles("new") == {"slow laptop"}
        assert titles("done") == {"fixed already"}


def test_inbox_rejects_unknown_group(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "inbox-bad.sqlite"))
    with TestClient(app) as c:
        r = c.get("/api/inbox?group=bogus", headers=_bearer(app))
        assert r.status_code == 400


def test_a_scoped_user_never_sees_an_alert_origin_ticket(tmp_path) -> None:
    """An alert-origin ticket has no requester -- it belongs to the fleet, not
    to a person -- and its title and host name another user's machine. It is
    the row that now carries a host into this aggregate, so it is where the
    ownership boundary is pinned."""

    app = build_app(db_path=str(tmp_path / "inbox-scope.sqlite"))
    with TestClient(app) as c:
        users = app.state.user_store
        tickets = app.state.tickets

        async def seed():
            alice = await users.create_user("alice", "pw-123456", "user")
            await users.set_user_hosts(alice["id"], ["alice-pc"])
            await tickets.create(
                title="bob-pc: disk is full", origin="alert", agent_id="bob-pc",
                requester_user_id=None, actor="system",
            )
            own = await tickets.create(
                title="my laptop is slow", origin="dashboard", agent_id="alice-pc",
                requester_user_id=alice["id"], actor="system",
            )
            await tickets.transition(own.id, "in_progress", actor="system")
            await tickets.block(own.id, "operator", actor="system")
            return await users.create_pat(alice["id"], "t")

        alice_pat = c.portal.call(seed)

        body = c.get("/api/inbox?group=needs_you", headers=_hdr(alice_pat)).json()
        assert {i["title"] for i in body["items"]} == {"my laptop is slow"}
        assert "bob-pc" not in {i["host"] for i in body["items"]}
        assert body["counts"]["needs_you"] == 1


def test_the_done_list_says_which_tickets_kenny_resolved_itself(tmp_path) -> None:
    """The DONE group is where the hit rate gets read.

    That only works if kenny's decisions are distinguishable from a person's
    without opening each ticket — otherwise judging whether to switch on
    `KENNY_TRIAGE_RESOLVE` means reading every timeline, which is the work this
    whole feature exists to remove.
    """

    app = build_app(db_path=str(tmp_path / "done.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        tickets = app.state.tickets

        async def seed():
            by_kenny = await tickets.create(
                title="phantom disk", origin="alert", agent_id="pc1", actor="system"
            )
            await tickets.transition(
                by_kenny.id, "resolved", actor="system", reason="triage: phantom",
                resolved_by="triage",
            )
            by_person = await tickets.create(
                title="printer jam", origin="alert", agent_id="pc1", actor="system"
            )
            await tickets.transition(by_person.id, "resolved", actor="operator", reason="fixed it")

        c.portal.call(seed)

        rows = c.get("/api/inbox?group=done", headers=h).json()["items"]
        by_title = {r["title"]: r["meta"] for r in rows}
        assert "resolved by kenny" in by_title["phantom disk"]
        assert "resolved by kenny" not in by_title["printer jam"]


def test_a_row_says_where_its_ticket_came_from(tmp_path) -> None:
    """The badge is the priority now, so the row still has to let a reader tell
    a case kenny opened from one a person did."""

    app = build_app(db_path=str(tmp_path / "inbox-origin.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        tickets = app.state.tickets

        async def seed():
            await tickets.create(
                title="pc1: disk is full", origin="alert", agent_id="pc1", actor="system"
            )

        c.portal.call(seed)

        row = c.get("/api/inbox?group=needs_you", headers=h).json()["items"][0]
        assert "alert" in row["meta"]

"""`GET /api/inbox` -- grouping, the ticket/section/approval merge, and scoping.

`webui/inbox.py` is a new, standalone route builder (not folded into
`webui/tickets.py` -- another surface owns that file). Tests go through the
composed app (`build_app`) the same way `tests/test_today_api.py` does.
"""

from __future__ import annotations

from functools import partial

from starlette.testclient import TestClient

from kenny_server.main import build_app


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _hdr(token: str):
    return {"Authorization": f"Bearer {token}"}


_CRIT_SNAPSHOT = {"disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 97}]}}


def test_inbox_groups_tickets_and_merges_sections_and_approvals(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "inbox.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        tickets = app.state.tickets
        store = app.state.store
        users = app.state.user_store

        c.portal.call(partial(store.insert, "crit-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))

        async def seed():
            # working: in_progress, unblocked.
            working = await tickets.create(title="reinstall driver", origin="dashboard", actor="system")
            await tickets.transition(working.id, "in_progress", actor="system")

            # waiting: blocked on the requester.
            waiting = await tickets.create(title="need a screenshot", origin="dashboard", actor="system")
            await tickets.transition(waiting.id, "in_progress", actor="system")
            await tickets.block(waiting.id, "user", actor="system")

            # needs_you (operator-blocked ticket).
            needs_you = await tickets.create(title="confirm risky change", origin="dashboard", actor="system")
            await tickets.transition(needs_you.id, "in_progress", actor="system")
            await tickets.block(needs_you.id, "operator", actor="system")

            # new: not started, has a requester (a `new` ticket with NO
            # requester is alert-origin and would count as needs_you instead --
            # see TicketStore.counts()'s docstring).
            requester = await users.create_user("req", "pw-123456", "user")
            await tickets.create(
                title="slow laptop", origin="dashboard", actor="system",
                requester_user_id=requester["id"],
            )

            # done: resolved then closed.
            done = await tickets.create(title="fixed already", origin="dashboard", actor="system")
            await tickets.transition(done.id, "in_progress", actor="system")
            await tickets.transition(done.id, "resolved", actor="system")

            # A held approval -- a `needs_you` row of its own, distinct from any
            # ticket, carrying enough of the gate for the console to decide it.
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
            # Mirrors TicketAssistant.on_hold: opening a gate and blocking the
            # ticket on it are two calls in production too.
            await tickets.block(gate_ticket.id, "approval", actor="system", ref=approval.id)

        c.portal.call(seed)

        counts = c.get("/api/inbox?group=needs_you", headers=h).json()["counts"]
        # 2 tickets (blocked on operator, and blocked on approval -- both count
        # per TicketStore.counts()'s rule) + 1 flagged section + 1 standalone
        # held-approval row.
        assert counts["needs_you"] == 4
        assert counts["waiting"] == 1
        assert counts["working"] == 1
        assert counts["new"] == 1
        assert counts["done"] == 1

        needs_you = c.get("/api/inbox?group=needs_you", headers=h).json()
        assert needs_you["group"] == "needs_you"
        kinds = {i["kind"] for i in needs_you["items"]}
        assert kinds == {"section", "approval", "ticket"}
        section_item = next(i for i in needs_you["items"] if i["kind"] == "section")
        assert section_item["severity"] == "crit"
        assert section_item["waits_on"] == "attention"
        approval_item = next(i for i in needs_you["items"] if i["kind"] == "approval")
        assert approval_item["waits_on"] == "approval"
        assert approval_item["gate"]["tool"] == "winget_update"
        assert approval_item["gate"]["args"] == {"id": "Some.App"}
        ticket_titles = {
            i["title"] for i in needs_you["items"] if i["kind"] == "ticket"
        }
        assert ticket_titles == {"confirm risky change", "apply update"}
        blocked_ticket = next(
            i for i in needs_you["items"]
            if i["kind"] == "ticket" and i["title"] == "confirm risky change"
        )
        assert blocked_ticket["waits_on"] == "operator"

        waiting = c.get("/api/inbox?group=waiting", headers=h).json()
        assert [i["title"] for i in waiting["items"]] == ["need a screenshot"]
        assert waiting["items"][0]["waits_on"] == "user"

        working = c.get("/api/inbox?group=working", headers=h).json()
        assert [i["title"] for i in working["items"]] == ["reinstall driver"]

        new = c.get("/api/inbox?group=new", headers=h).json()
        assert [i["title"] for i in new["items"]] == ["slow laptop"]

        done = c.get("/api/inbox?group=done", headers=h).json()
        assert [i["title"] for i in done["items"]] == ["fixed already"]


def test_inbox_rejects_unknown_group(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "inbox-bad.sqlite"))
    with TestClient(app) as c:
        r = c.get("/api/inbox?group=bogus", headers=_bearer(app))
        assert r.status_code == 400


def test_inbox_approvals_stay_operator_only_for_a_scoped_user(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "inbox-approvals-scope.sqlite"))
    with TestClient(app) as c:
        users = app.state.user_store
        tickets = app.state.tickets

        async def seed():
            kid = await users.create_user("kid", "pw-123456", "user")
            await users.set_user_hosts(kid["id"], ["kid-pc"])
            gate_ticket = await tickets.create(
                title="apply update", origin="dashboard", agent_id="kid-pc",
                requester_user_id=kid["id"], actor="system",
            )
            await tickets.transition(gate_ticket.id, "in_progress", actor="system")
            approval = await tickets.open_approval(
                gate_ticket.id,
                tool_use_id="tu-2",
                tool="winget_update",
                tool_class="standard_change",
                args={},
                agent_id="kid-pc",
                actor="system",
            )
            await tickets.block(gate_ticket.id, "approval", actor="system", ref=approval.id)
            return await users.create_pat(kid["id"], "t")

        kid_pat = c.portal.call(seed)

        body = c.get("/api/inbox?group=needs_you", headers=_hdr(kid_pat)).json()
        # The ticket itself (blocked on approval) is visible -- it's the kid's
        # own ticket -- but the approval row (the gate's args) is not: that
        # stays at least as strict as GET /api/approvals (operator-only).
        assert all(i["kind"] != "approval" for i in body["items"])
        assert any(i["kind"] == "ticket" for i in body["items"])


def test_inbox_scopes_flagged_sections_to_a_users_own_hosts(tmp_path) -> None:
    """A host-scoped `user` must not see another user's host through this
    aggregate: not as a flagged-section row, and not in the needs_you count."""

    app = build_app(db_path=str(tmp_path / "inbox-section-scope.sqlite"))
    with TestClient(app) as c:
        users = app.state.user_store
        store = app.state.store

        c.portal.call(partial(store.insert, "alice-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))
        c.portal.call(partial(store.insert, "bob-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))

        async def seed():
            alice = await users.create_user("alice", "pw-123456", "user")
            await users.set_user_hosts(alice["id"], ["alice-pc"])
            return await users.create_pat(alice["id"], "t")

        alice_pat = c.portal.call(seed)

        body = c.get("/api/inbox?group=needs_you", headers=_hdr(alice_pat)).json()
        hosts = {i["host"] for i in body["items"]}
        assert "bob-pc" not in hosts
        assert "alice-pc" in hosts
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

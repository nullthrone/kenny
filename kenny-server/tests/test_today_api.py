"""`GET /api/today` -- ranking/cap and host scoping.

Built against the real composed app (`build_app`) the way
`tests/test_dashboard_api.py` is: a temp SQLite file, `TestClient`, the
app's own operator token for the unscoped cases, and a PAT + `set_user_hosts`
for the scoped ones (see `notes/backend-map.md`'s "test idiom" section).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import partial

from starlette.testclient import TestClient

from kenny_server.main import build_app


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _hdr(token: str):
    return {"Authorization": f"Bearer {token}"}


_CRIT_SNAPSHOT = {"disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 97}]}}
_WARN_SNAPSHOT = {"disk": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "percent_used": 85}]}}


def test_today_ranks_and_caps_at_three(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "today.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        store = app.state.store
        tickets = app.state.tickets
        ticket_store = app.state.ticket_store

        c.portal.call(partial(store.insert, "crit-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))
        c.portal.call(partial(store.insert, "warn-pc", "2026-08-01T00:00:00+00:00", _WARN_SNAPSHOT))

        async def seed():
            # A held approval -- the third-ranked source.
            ticket = await tickets.create(
                title="restart print spooler",
                origin="dashboard",
                agent_id="approval-pc",
                actor="system",
            )
            await tickets.transition(ticket.id, "in_progress", actor="system")
            approval = await tickets.open_approval(
                ticket.id,
                tool_use_id="tu-1",
                tool="service_restart",
                tool_class="standard_change",
                args={"name": "spooler"},
                agent_id="approval-pc",
                actor="system",
            )
            await tickets.block(ticket.id, "approval", actor="system", ref=approval.id)
            # A stale ticket -- the fourth-ranked source, and the one the cap
            # must exclude given the three items above already fill the list.
            stale = await ticket_store.create(
                title="printer wont print",
                origin="dashboard",
                agent_id="stale-pc",
                requester_user_id=None,
            )
            await ticket_store.set_state(stale.id, "in_progress", actor="system")
            old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            await ticket_store.set_blocked(stale.id, "user", actor="system", now=old)

        c.portal.call(seed)

        r = c.get("/api/today", headers=h)
        assert r.status_code == 200
        body = r.json()

        assert len(body["items"]) == 3
        severities = [i["severity"] for i in body["items"]]
        assert severities == ["crit", "warn", "held"]
        # crit before warn before the held approval; the stale ticket (also
        # "held", but ranked after approvals) is capped out entirely.
        assert body["items"][0]["host"] == "crit-pc"
        assert body["items"][1]["host"] == "warn-pc"
        assert body["items"][2]["host"] == "approval-pc"
        assert all(i["host"] != "stale-pc" for i in body["items"])
        assert "printer wont print" not in [i["title"] for i in body["items"]]

        # The envelope's other pieces are present and shaped as documented.
        assert isinstance(body["verdict_sentence"], str) and body["verdict_sentence"]
        assert "segments" in body["donut"]
        assert "days" in body["trend_30d"]
        assert isinstance(body["kpis"], list) and body["kpis"]


_TARGET_TICKET_ID_RE = re.compile(r"^#/inbox/ticket/[0-9a-f]{32}$")


def test_today_items_target_is_fetchable_by_the_route_it_names(tmp_path) -> None:
    """Both `_today_approval_item` and `_today_ticket_item` must link
    `target` to the ticket's uuid `id`, not its display `number` -- same seam
    as `/api/inbox` (see `test_inbox_api.py`'s matching test): a `#/inbox/
    ticket/{number}` target 404s against `/api/tickets/{tid}`, which only
    resolves `id`.
    """

    app = build_app(db_path=str(tmp_path / "today-target.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        tickets = app.state.tickets
        ticket_store = app.state.ticket_store

        async def seed():
            approval_ticket = await tickets.create(
                title="restart print spooler",
                origin="dashboard",
                agent_id="approval-pc",
                actor="system",
            )
            await tickets.transition(approval_ticket.id, "in_progress", actor="system")
            approval = await tickets.open_approval(
                approval_ticket.id,
                tool_use_id="tu-target",
                tool="service_restart",
                tool_class="standard_change",
                args={"name": "spooler"},
                agent_id="approval-pc",
                actor="system",
            )
            await tickets.block(approval_ticket.id, "approval", actor="system", ref=approval.id)

            stale_ticket = await ticket_store.create(
                title="printer wont print",
                origin="dashboard",
                agent_id="stale-pc",
                requester_user_id=None,
            )
            await ticket_store.set_state(stale_ticket.id, "in_progress", actor="system")
            old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            await ticket_store.set_blocked(stale_ticket.id, "user", actor="system", now=old)

            return approval_ticket, stale_ticket

        approval_ticket, stale_ticket = c.portal.call(seed)

        body = c.get("/api/today", headers=h).json()
        by_action = {i["action"]: i for i in body["items"]}

        for action, expected_id in (
            ("REVIEW APPROVAL", approval_ticket.id),
            ("OPEN TICKET", stale_ticket.id),
        ):
            item = by_action[action]
            target = item["target"]
            assert _TARGET_TICKET_ID_RE.match(target), target
            tid = target.removeprefix("#/inbox/ticket/")
            detail = c.get(f"/api/tickets/{tid}", headers=h)
            assert detail.status_code == 200
            assert detail.json()["id"] == expected_id


def test_today_empty_is_first_class(tmp_path) -> None:
    """No agents, no tickets: `/api/today` still renders -- an empty `items`
    array, not an error, and a verdict sentence that says so."""

    app = build_app(db_path=str(tmp_path / "today-empty.sqlite"))
    with TestClient(app) as c:
        r = c.get("/api/today", headers=_bearer(app))
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert isinstance(body["verdict_sentence"], str) and body["verdict_sentence"]


def test_today_scopes_a_user_to_their_own_hosts(tmp_path) -> None:
    """A host-scoped `user` must not see another user's host through this
    aggregate -- not in `items`, not in the donut/kpi/trend member drill-downs."""

    app = build_app(db_path=str(tmp_path / "today-scope.sqlite"))
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

        r = c.get("/api/today", headers=_hdr(alice_pat))
        assert r.status_code == 200
        body = r.json()

        assert all(i["host"] != "bob-pc" for i in body["items"])
        assert any(i["host"] == "alice-pc" for i in body["items"])

        donut_hosts = {
            m["agent_id"] for seg in body["donut"]["segments"] for m in seg["members"]
        }
        assert "bob-pc" not in donut_hosts

        kpi_hosts = {m["agent_id"] for k in body["kpis"] for m in k["members"]}
        assert "bob-pc" not in kpi_hosts

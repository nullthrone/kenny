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


_POSTURE_SNAPSHOT = {
    "encryption": {"status": "ok", "summary": "", "volumes": [{"mount": "C:", "protection_status": 0}]},
}


def test_today_ranks_newest_incident_first_and_counts_posture(tmp_path) -> None:
    """Joined across the store, alert_state and the ranking: two crit hosts
    order newest-first by the age the alert loop recorded, and a posture-only
    host contributes to `posture_count`, never to `items`."""

    app = build_app(db_path=str(tmp_path / "today_age.sqlite"))
    now = datetime.now(timezone.utc)
    with TestClient(app) as c:
        h = _bearer(app)
        store, state = app.state.store, app.state.alert_state
        at = (now - timedelta(minutes=5)).isoformat()
        c.portal.call(partial(store.insert, "old-crit", at, _CRIT_SNAPSHOT))
        c.portal.call(partial(store.insert, "new-crit", at, _CRIT_SNAPSHOT))
        c.portal.call(partial(store.insert, "posture-pc", at, _POSTURE_SNAPSHOT))
        c.portal.call(partial(state.upsert, "old-crit", "section:disk", status="crit",
                              since=(now - timedelta(days=3)).isoformat(), last_notified_at=None))
        c.portal.call(partial(state.upsert, "new-crit", "section:disk", status="crit",
                              since=(now - timedelta(hours=1)).isoformat(), last_notified_at=None))

        body = c.get("/api/today", headers=h).json()
        assert [i["host"] for i in body["items"]] == ["new-crit", "old-crit"]
        assert body["items"][0]["age_seconds"] < body["items"][1]["age_seconds"]
        assert body["items"][0]["since"] is not None
        assert body["posture_count"] == 1
        assert body["posture_line"] == "1 posture finding unchanged"
        # A posture-only host is a quiet host in the verdict and the donut.
        assert "One machine needs attention" not in body["verdict_sentence"]
        assert {s["key"]: s["value"] for s in body["donut"]["segments"]} == {"crit": 2, "ok": 1}


_TARGET_SECTION_RE = re.compile(r"^#/fleet/(?P<host>[^?]+)\?section=(?P<section>[^&]+)$")


def _health_section_names(detail: dict) -> dict[str, dict]:
    """`/api/agent/{id}`'s health sections, keyed by name.

    Accepts both shapes the console's `normalizeSections` accepts (a dict keyed
    by section name, or an array of sections carrying their own `name`), so
    this test pins the *link*, not which of the two the handler happens to
    return today.
    """

    sections = detail["health"]["sections"]
    if isinstance(sections, list):
        return {s["name"]: s for s in sections}
    return dict(sections)


def test_a_flagged_section_opens_the_section_not_the_machine(tmp_path) -> None:
    """Today is where a standing finding is read, so its row links to the
    finding, not to the host.

    Joined seam between `section_target()` and the console's `FleetHost.tsx`:
    the target is followed the way the console follows it -- the path names a
    host `/api/agent/{id}` resolves, and `?section=` carries a raw section
    name that host really reports as needing attention, which is the key
    `FleetHost.tsx` matches against `HostSection.name` to open the detail. It
    fails on a regression to a bare `#/fleet/{host}` (the reader lands on the
    machine and has to find the section again) and on a param carrying a
    humanised label ("Disk") instead of the stored name ("disk").
    """

    from urllib.parse import unquote

    app = build_app(db_path=str(tmp_path / "today-section-target.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        store = app.state.store
        c.portal.call(partial(store.insert, "crit-pc", "2026-08-01T00:00:00+00:00", _CRIT_SNAPSHOT))

        row = next(i for i in c.get("/api/today", headers=h).json()["items"] if i["severity"] == "crit")

        match = _TARGET_SECTION_RE.match(row["target"])
        assert match, row["target"]
        host = unquote(match["host"])
        section = unquote(match["section"])
        assert host == row["host"] == "crit-pc"

        detail = c.get(f"/api/agent/{host}", headers=h)
        assert detail.status_code == 200
        sections = _health_section_names(detail.json())
        assert section in sections, sorted(sections)
        assert sections[section]["status"] in ("warn", "crit")

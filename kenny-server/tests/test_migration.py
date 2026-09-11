"""Seamless upgrade from a pre-ADR-0033 single-token install.

Simulates an existing database (a host with telemetry, no user accounts) and
asserts that booting the new server: creates the new tables idempotently, keeps
the existing host in inventory, keeps the shared ``KENNY_OPERATOR_TOKEN`` working
as a superuser machine credential, and offers first-run setup because no account
exists yet.
"""

from __future__ import annotations

import asyncio
import sqlite3
from functools import partial
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from kenny_server.main import build_app
from kenny_server.store import TelemetryStore


def _seed_existing_db(db_path: str) -> None:
    """Seed a pre-upgrade database with one host that has telemetry.

    The timestamp is relative to now, not absolute: ``build_app``'s lifespan
    prunes snapshots older than ``RETENTION_DAYS`` (30), so a hardcoded date
    silently turns this test into a time bomb that starts failing 30 days after
    it was written — which is exactly what happened to the original
    ``2026-07-01`` value. "Recent enough to survive retention" is the property
    the test actually needs.
    """

    collected_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    async def seed() -> None:
        ts = TelemetryStore(db_path)
        await ts.connect()
        await ts.insert("OLD-PC", collected_at, {"system": {"host": "OLD-PC"}})
        await ts.close()

    asyncio.run(seed())


def test_upgrade_preserves_hosts_and_shared_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_OPERATOR_TOKEN", "legacy-secret")
    db_path = str(tmp_path / "old.sqlite")
    _seed_existing_db(db_path)

    app = build_app(db_path=db_path)
    with TestClient(app) as c:
        h = {"Authorization": "Bearer legacy-secret"}
        # The shared token still authorizes, as a back-compat superuser.
        me = c.get("/api/me", headers=h).json()
        assert me["role"] == "superuser"
        assert me["is_shared_token"] is True
        # The pre-existing host is still in inventory.
        agents = c.get("/api/fleet", headers=h).json()["agents"]
        assert any(a["agent_id"] == "OLD-PC" for a in agents)
        # No account yet → the browser is guided to first-run setup.
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/setup"

    # The new user tables were created idempotently in the existing DB.
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"users", "user_tokens", "sessions", "user_hosts"} <= tables
    # The reliability alarm suppression table (ADR-0041 / issue #166) too.
    assert "reliability_suppressions" in tables
    # And the persisted event classifications (ADR-0058).
    assert "event_classifications" in tables


def test_suppression_table_created_idempotently_and_survives_a_second_boot(tmp_path) -> None:
    db_path = str(tmp_path / "twice.sqlite")
    app1 = build_app(db_path=db_path)
    with TestClient(app1) as c:
        h = {"Authorization": f"Bearer {app1.state.operator_token}"}
        resp = c.post(
            "/api/reliability/suppressions", headers=h,
            json={"event_id": 4176, "source": "Microsoft-Windows-CAPI2"},
        )
        assert resp.status_code == 200

    # Booting a second app instance against the same DB file must not error,
    # and the rule inserted in boot #1 must survive and be in the mirror.
    app2 = build_app(db_path=db_path)
    with TestClient(app2) as c:
        h = {"Authorization": f"Bearer {app2.state.operator_token}"}
        rules = c.get("/api/reliability/suppressions", headers=h).json()["rules"]
        assert len(rules) == 1
        assert rules[0]["event_id"] == 4176
        assert app2.state.suppression.match("ANY-PC", "Microsoft-Windows-CAPI2", 4176) is not None


def test_event_classifications_table_created_and_survives_a_second_boot(tmp_path) -> None:
    """The persisted LLM verdicts (ADR-0058) outlive the process: a row
    written through in boot #1 is in the classifier's cache after boot #2,
    with no client involved."""

    from kenny_server import event_categories

    db_path = str(tmp_path / "classified.sqlite")
    app1 = build_app(db_path=db_path)
    with TestClient(app1) as c:
        c.portal.call(partial(app1.state.classification_store.upsert_many, [{
            "source": "disk", "event_id": 51, "category": "Disk & storage",
            "severity": "serious", "cause": "bad sectors", "model": event_categories.CATEGORIZE_MODEL,
        }]))
    event_categories.reset_state()

    app2 = build_app(db_path=db_path)
    with TestClient(app2) as c:
        assert event_categories._cache[("disk", 51)] == {
            "category": "Disk & storage", "severity": "serious", "cause": "bad sectors",
        }
        rows = c.portal.call(app2.state.classification_store.list)
        assert [(r["source"], r["event_id"], r["model"]) for r in rows] == [
            ("disk", 51, event_categories.CATEGORIZE_MODEL)
        ]
    event_categories.reset_state()


def test_setup_closes_after_first_account(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "fresh.sqlite"))
    with TestClient(app) as c:
        assert c.post("/setup", data={"username": "admin", "password": "pw-123456"},
                      follow_redirects=False).status_code == 303
        # Once an account exists, setup is closed.
        assert c.post("/setup", data={"username": "x", "password": "y"},
                      follow_redirects=False).status_code == 409


def _tickets_schema_without(column: str) -> str:
    """``ticketstore._SCHEMA`` as it looked before ``column`` was added.

    Derived from the live schema rather than copied, so it stays honest as the
    table grows — and it repairs the column list properly instead of patching
    one comma by hand. The hand-patched version worked only while the column
    under test happened to be the last one; the next column added made it emit
    invalid SQL, which is exactly the brittleness this avoids.
    """

    from kenny_server import ticketstore

    out: list[str] = []
    for line in ticketstore._SCHEMA.splitlines():
        if column in line:
            continue
        out.append(line)
    # Whichever column definition is now last must not end with a comma. Walk
    # back from the closing paren of the tickets table, skipping comment lines.
    end = next(i for i, line in enumerate(out) if line.strip() == ");")
    for i in range(end - 1, -1, -1):
        stripped = out[i].strip()
        if not stripped or stripped.startswith("--"):
            continue
        out[i] = out[i].rstrip().rstrip(",")
        break
    return "\n".join(out)


def test_tickets_created_before_resolved_by_gain_the_column_and_keep_their_rows(
    tmp_path,
) -> None:
    """Same migration path as ``dedup_key`` below, for the same reason.

    A column added after the table's original release reaches a *live*
    deployment only through ``PRAGMA table_info`` + ``ALTER TABLE ADD COLUMN``;
    the ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` is a no-op there. Every
    fresh-database test passes either way, so this is the one that would catch a
    missing ``_TICKET_MIGRATED_COLUMNS`` entry.
    """

    from kenny_server import ticketstore

    db_path = str(tmp_path / "old-resolved-by.sqlite")
    con = sqlite3.connect(db_path)
    con.executescript(_tickets_schema_without("resolved_by"))
    con.execute(
        "INSERT INTO tickets (id, number, title, state, origin, priority, summary, "
        "created_at, updated_at) VALUES ('t1', 1, 'resolved before the upgrade', "
        "'resolved', 'alert', 'normal', '', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
    )
    con.commit()
    assert "resolved_by" not in {r[1] for r in con.execute("PRAGMA table_info(tickets)")}
    con.close()

    async def reopen() -> None:
        store = ticketstore.TicketStore(db_path)
        await store.connect()  # runs _migrate
        tickets = await store.list(limit=10)
        assert len(tickets) == 1
        assert tickets[0].title == "resolved before the upgrade"
        # Backfilled empty: a ticket resolved before triage existed was not
        # resolved by it, and must not start claiming so.
        assert tickets[0].resolved_by == ""
        await store.close()

    asyncio.run(reopen())


def test_tickets_created_before_dedup_key_gain_the_column_and_keep_their_rows(tmp_path) -> None:
    """An existing ``tickets`` table gains ``dedup_key`` without losing a row.

    ``TicketStore`` has no migration framework beyond ``PRAGMA table_info`` +
    ``ALTER TABLE ADD COLUMN`` (see its docstring), so a column added after the
    table's original release only reaches a *live* deployment through that path
    — the ``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA`` is a no-op there. This
    test is the one that would catch forgetting the ``_TICKET_MIGRATED_COLUMNS``
    entry, because every fresh-database test passes either way.

    The backfilled value must be the empty "not deduplicated" sentinel: a
    pre-existing ticket was never keyed, and must not start suppressing new ones.
    """

    from kenny_server import ticketstore

    db_path = str(tmp_path / "old-tickets.sqlite")

    con = sqlite3.connect(db_path)
    con.executescript(_tickets_schema_without("dedup_key"))
    con.execute(
        "INSERT INTO tickets (id, number, title, state, origin, priority, summary, "
        "created_at, updated_at) VALUES ('t1', 1, 'from before the upgrade', 'new', "
        "'discord', 'normal', '', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
    )
    con.commit()
    cols = {r[1] for r in con.execute("PRAGMA table_info(tickets)")}
    assert "dedup_key" not in cols  # the pre-upgrade shape
    con.close()

    async def reopen() -> None:
        store = ticketstore.TicketStore(db_path)
        await store.connect()  # runs _migrate
        tickets = await store.list(limit=10)
        assert len(tickets) == 1
        assert tickets[0].title == "from before the upgrade"
        assert tickets[0].dedup_key == ""
        # And the empty sentinel never matches, so nothing is suppressed by it.
        assert await store.find_open_by_dedup_key("") is None
        await store.close()

    asyncio.run(reopen())


def _events_schema_without(column: str) -> str:
    """``store._EVENTS_SCHEMA`` as it looked before ``column`` was added.

    Derived from the live schema rather than copied, for the same reason
    :func:`_tickets_schema_without` is: it stays honest as the table grows.
    """

    from kenny_server import store as store_mod

    out: list[str] = []
    for line in store_mod._EVENTS_SCHEMA.splitlines():
        if column in line:
            continue
        out.append(line)
    end = next(i for i, line in enumerate(out) if line.strip() == ");")
    for i in range(end - 1, -1, -1):
        stripped = out[i].strip()
        if not stripped or stripped.startswith("--"):
            continue
        out[i] = out[i].rstrip().rstrip(",")
        break
    return "\n".join(out)


def test_events_created_before_ticket_id_gain_the_column_and_keep_their_rows(tmp_path) -> None:
    """An existing ``events`` table gains ``ticket_id`` without losing a row.

    ``EventStore``'s schema script is ``CREATE TABLE IF NOT EXISTS``, a no-op on
    a live deployment, so the column only reaches one through ``_migrate``. This
    is the test that catches forgetting that method entirely: every
    fresh-database test passes either way.

    The backfilled value is NULL — an alert recorded before the relation existed
    belongs to no ticket, and must not start claiming one.
    """

    from kenny_server.store import EventStore

    db_path = str(tmp_path / "old-events.sqlite")

    con = sqlite3.connect(db_path)
    con.executescript(_events_schema_without("ticket_id"))
    con.execute(
        "INSERT INTO events (at, agent_id, source, level, kind, target, message, fields) "
        "VALUES ('2026-08-01T00:00:00Z', 'OLD-PC', 'server', 'warn', 'alert', "
        "'kenny.alert', 'OLD-PC: disk is filling up\nC: 91% used', '{\"priority\": \"high\"}')"
    )
    con.commit()
    assert "ticket_id" not in {r[1] for r in con.execute("PRAGMA table_info(events)")}
    con.close()

    async def reopen() -> None:
        store = EventStore(db_path)
        await store.connect()  # runs _migrate
        rows = await store.query(kind="alert")
        assert len(rows) == 1
        assert rows[0]["agent_id"] == "OLD-PC"
        # A row without fields["title"] still splits into title and body, so an
        # alert older than that field renders in the panel like any other.
        alerts = await store.alerts_for_ticket("t1")
        assert alerts == []  # nothing is linked yet
        await store.link_alert_to_ticket(1, "t1")
        alerts = await store.alerts_for_ticket("t1")
        assert [a["title"] for a in alerts] == ["OLD-PC: disk is filling up"]
        assert alerts[0]["body"] == "C: 91% used"
        await store.close()

    asyncio.run(reopen())


def test_an_open_alert_ticket_keeps_deduplicating_across_the_key_change(tmp_path) -> None:
    """A ticket opened before the key format changed still absorbs its next alert.

    Joined on purpose: the assertion is "no second ticket appeared", driven
    through the real alert -> ticket hook, not a string comparison of the
    migrated key against an expected one. A migration that produced a
    plausible-looking key the live producer never mints would pass the latter
    and silently double every alert ticket on the first upgraded server.

    The forecast is the hard case — its key had no subject at all before the
    producer named the ``disk`` section.
    """

    from kenny_server.notify import Notification

    db_path = str(tmp_path / "old-keys.sqlite")

    async def seed() -> None:
        from kenny_server import ticketstore

        store = ticketstore.TicketStore(db_path)
        await store.connect()
        await store.create(
            title="pc1: disk filling up",
            origin="alert",
            priority="normal",
            category="alert",
            summary="",
            agent_id="pc1",
            dedup_key="alert|pc1|disk_forecast|",
        )
        await store.create(
            title="pc1: reliability",
            origin="alert",
            priority="high",
            category="alert",
            summary="",
            agent_id="pc1",
            dedup_key="alert|pc1|health|reliability",
        )
        await store.close()

    asyncio.run(seed())

    app = build_app(db_path=db_path)  # boots, so _migrate rewrites the keys
    with TestClient(app):
        open_ticket = app.state.alert_engine._open_ticket

        forecast = Notification(
            title="pc1: disk filling up",
            body="C: ~11d until full",
            agent_id="pc1",
            kind="alert",
            event_type="disk_forecast",
            sections={"disk": "warn"},
        )
        health = Notification(
            title="pc1: reliability is crit",
            body="12 crashes",
            agent_id="pc1",
            kind="alert",
            event_type="health",
            sections={"reliability": "crit"},
        )
        asyncio.run(open_ticket(forecast))
        asyncio.run(open_ticket(health))

        tickets = asyncio.run(app.state.ticket_store.list(limit=50))
        assert len(tickets) == 2  # both recurrences attached; nothing was minted

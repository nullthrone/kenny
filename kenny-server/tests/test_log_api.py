"""`GET /api/log` -- the merged tools/alerts/events stream: kind mapping,
free-text search, cursor pagination, and host scoping.

The underlying merged table is `EventStore` (ADR-0017); these tests seed it
directly (`c.portal.call`, per the backend map's test idiom) rather than
through the tool-call/alert paths that normally write it, since only the read
side (`/api/log`) is new here.
"""

from __future__ import annotations

from functools import partial

from starlette.testclient import TestClient

from kenny_server.main import build_app


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _hdr(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_log_kind_mapping_and_shape(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "log-kind.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        es = app.state.event_store

        c.portal.call(partial(
            es.insert_audit, agent_id="pc1", tool="winget_update", ok=True,
            at="2026-08-01T00:00:01+00:00",
        ))
        c.portal.call(partial(
            es.insert_alert, agent_id="pc1", message="disk critical", level="warning",
            at="2026-08-01T00:00:02+00:00",
        ))
        c.portal.call(partial(
            es.insert_log, source="server", at="2026-08-01T00:00:03+00:00", level="info",
            message="hello world", agent_id="pc1",
        ))

        tools = c.get("/api/log?kind=tools", headers=h).json()["rows"]
        assert len(tools) == 1
        assert tools[0]["kind"] == "tools"
        assert tools[0]["tag"] == "TOOL"
        assert tools[0]["what"] == "winget_update"
        assert tools[0]["host"] == "pc1"
        assert set(tools[0]) == {"ts", "kind", "tag", "host", "actor", "what", "message", "meta"}

        alerts = c.get("/api/log?kind=alerts", headers=h).json()["rows"]
        assert len(alerts) == 1
        assert alerts[0]["kind"] == "alerts"
        assert alerts[0]["tag"] == "ALERT"
        assert alerts[0]["message"] == "disk critical"

        # The server logs during boot too (a missing KENNY_GITHUB_TOKEN, for
        # one), and those records land here by design (ADR-0017). Assert on the
        # seeded rows rather than on the absence of the server's own.
        events = c.get("/api/log?kind=events", headers=h).json()["rows"]
        seeded = [r for r in events if r["message"] == "hello world"]
        assert len(seeded) == 1
        assert seeded[0]["kind"] == "events"

        # Merged (no kind filter): all three seeded rows, newest first.
        merged = c.get("/api/log", headers=h).json()["rows"]
        seeded_ts = {"2026-08-01T00:00:01+00:00", "2026-08-01T00:00:02+00:00",
                     "2026-08-01T00:00:03+00:00"}
        assert [r["message"] for r in merged if r["ts"] in seeded_ts] == [
            "hello world", "disk critical", ""
        ]

        assert c.get("/api/log?kind=bogus", headers=h).status_code == 400


def test_log_free_text_search(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "log-search.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        es = app.state.event_store

        c.portal.call(partial(
            es.insert_audit, agent_id="pc1", tool="winget_update", ok=True,
            at="2026-08-01T00:00:01+00:00",
        ))
        c.portal.call(partial(
            es.insert_audit, agent_id="pc1", tool="telemetry_collect", ok=True,
            at="2026-08-01T00:00:02+00:00",
        ))

        rows = c.get("/api/log?q=winget", headers=h).json()["rows"]
        assert [r["what"] for r in rows] == ["winget_update"]

        rows = c.get("/api/log?q=nomatch", headers=h).json()["rows"]
        assert rows == []


def test_log_cursor_pagination_covers_every_row_once(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "log-cursor.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        es = app.state.event_store

        for i in range(5):
            c.portal.call(partial(
                es.insert_alert, agent_id="pc1", message=f"alert-{i}", level="info",
                at=f"2026-08-01T00:00:0{i}+00:00",
            ))

        seen: list[str] = []
        cursor = None
        for _ in range(10):  # bounded so a pagination bug can't hang the test
            params = "kind=alerts&limit=2"
            if cursor:
                params += f"&cursor={cursor}"
            body = c.get(f"/api/log?{params}", headers=h).json()
            seen.extend(r["message"] for r in body["rows"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        # Newest-first (alert-4 down to alert-0), no gaps, no repeats.
        assert seen == ["alert-4", "alert-3", "alert-2", "alert-1", "alert-0"]


def test_log_invalid_cursor_is_rejected(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "log-bad-cursor.sqlite"))
    with TestClient(app) as c:
        r = c.get("/api/log?cursor=not-a-real-cursor", headers=_bearer(app))
        assert r.status_code == 400


def test_log_scopes_a_user_to_their_own_hosts(tmp_path) -> None:
    """A host-scoped `user` must not see another user's host's log lines
    through this aggregate, with or without a search term."""

    app = build_app(db_path=str(tmp_path / "log-scope.sqlite"))
    with TestClient(app) as c:
        es = app.state.event_store
        users = app.state.user_store

        c.portal.call(partial(
            es.insert_audit, agent_id="alice-pc", tool="telemetry_collect", ok=True,
            at="2026-08-01T00:00:01+00:00",
        ))
        c.portal.call(partial(
            es.insert_audit, agent_id="bob-pc", tool="telemetry_collect", ok=True,
            at="2026-08-01T00:00:02+00:00",
        ))

        async def seed():
            alice = await users.create_user("alice", "pw-123456", "user")
            await users.set_user_hosts(alice["id"], ["alice-pc"])
            return await users.create_pat(alice["id"], "t")

        alice_pat = c.portal.call(seed)

        rows = c.get("/api/log", headers=_hdr(alice_pat)).json()["rows"]
        assert [r["host"] for r in rows] == ["alice-pc"]

        rows = c.get("/api/log?q=telemetry_collect", headers=_hdr(alice_pat)).json()["rows"]
        assert [r["host"] for r in rows] == ["alice-pc"]

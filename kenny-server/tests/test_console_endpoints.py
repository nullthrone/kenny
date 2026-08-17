"""The console's additive server-side changes: chat scope, config `editable`,
`start_immediately`, and the persisted theme.

Four small changes with one thing in common: each adds a field or a flag the new
frontend needs, and each had to be added *without* moving a boundary the rest of
the system already relies on. That is what these tests pin — not the field's
presence, which a type would catch, but the invariant that made the shape legal:

* ``auto_run`` distinguishes a call that ran unattended from one that was held
  and approved — and it reads the tool's tier, never the session's scope
  (ADR-0045).
* ``editable`` on a settings row is the same predicate the write path enforces
  with a 403, so a rendered control can always be submitted.
* ``start_immediately`` goes through the lifecycle service, so the deliberate
  ``new -> in_progress`` actor rule still decides.
* ``theme`` is skipped, not failed, for the identity that has no account.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server import notify, tool_classes
from kenny_server.chat import FleetSession, _context_note
from kenny_server.main import build_app


def _app(tmp_path):
    return build_app(db_path=str(tmp_path / "console.sqlite"))


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


# -- item 6: chat scope ------------------------------------------------------


def test_scope_is_derived_from_agent_id_not_stored() -> None:
    """One field, two readings. ``agent_id`` stays the only stored fact.

    A second stored field could disagree with the one every routing path
    (``toolloop._resolve_chat_target``) actually reads; a property cannot.
    """

    session = FleetSession(id="s1")
    assert session.scope == "fleet"

    session.agent_id = "linus-pc"
    assert session.scope == "host"

    # Clearing the selection returns to fleet — the dashboard sends `agent_id`
    # on every request precisely so this can happen.
    session.agent_id = None
    assert session.scope == "fleet"
    session.agent_id = ""
    assert session.scope == "fleet"

    # And it is derived, so it cannot be set out of step with the selection.
    with pytest.raises(AttributeError):
        session.scope = "host"  # type: ignore[misc]


def test_both_scopes_state_themselves_to_the_model_uncached() -> None:
    """Either scope is a sentence in the system context, and neither is cached.

    Per-session state must stay out of ``_cached_system()`` or it busts the
    Anthropic prompt-cache prefix for every other session.
    """

    fleet = _context_note(FleetSession(id="s1"))
    host = _context_note(FleetSession(id="s2", agent_id="linus-pc"))
    assert len(fleet) == 1 and len(host) == 1
    assert "fleet-wide" in fleet[0]["text"]
    assert "linus-pc" in host[0]["text"]
    assert "cache_control" not in fleet[0]
    assert "cache_control" not in host[0]


async def test_scope_never_changes_a_tool_s_tier(tmp_path) -> None:
    """ADR-0045's invariant, asserted against the gate rather than assumed.

    The tier is a property of the tool; the gate is a property of the surface. A
    scope is neither. So for every tool in the catalog, the dashboard's gate must
    return the identical decision whether the session is host-scoped or
    fleet-wide — and the classification must be identical too.
    """

    from kenny_server.chat import FleetPolicy
    from kenny_server.toolloop import Allow, Hold

    policy = FleetPolicy()
    fleet_session = FleetSession(id="fleet")
    host_session = FleetSession(id="host", agent_id="linus-pc")

    for tool in sorted(tool_classes.TOOL_CLASSES):
        fleet_decision = await policy.gate(fleet_session, tool, {}, None)
        host_decision = await policy.gate(host_session, tool, {}, "linus-pc")
        assert type(fleet_decision) is type(host_decision), tool
        expected = Allow if tool_classes.classify(tool) == tool_classes.READ_ONLY else Hold
        assert isinstance(fleet_decision, expected), tool


async def test_auto_run_separates_an_unattended_call_from_an_approved_one(
    tmp_path,
) -> None:
    """The gate distinction, as an additive flag on the event that already exists.

    The event *types* are deliberately unchanged: ``webui/tickets.py``'s ticket
    chat documents reusing this vocabulary verbatim and the frontend keys off the
    literal strings, so a read-only call still arrives as ``tool_result`` and a
    held one still arrives as ``pending``. ``auto_run`` is what makes the
    difference legible without renaming either.
    """

    from kenny_server.toolloop import PendingCall, apply_confirmation

    class _Executor:
        async def run_server_tool(self, tool: str, args: dict, *, session: Any) -> dict:
            return {"ok": True}

        async def run_capability(self, tool: str, args: dict, *, agent_id: str) -> dict:
            return {"ok": True}

    session = FleetSession(id="s1", agent_id="linus-pc")
    session.pending = PendingCall(
        id="p1",
        tool_use_id="tu1",
        tool="net_dns_flush",
        args={},
        agent_id="linus-pc",
        tool_class=tool_classes.classify("net_dns_flush"),
    )
    resumed = await apply_confirmation(session, approve=True, executor=_Executor())
    assert resumed["type"] == "tool_result"
    # It ran, but only because someone said so.
    assert resumed["auto_run"] is False


async def test_the_live_loop_flags_the_two_cases_differently(tmp_path) -> None:
    """End to end through ``drive_events`` with the dashboard's own policy.

    A read-only call reaches the client only as ``tool_result`` — there is no
    "about to run" event for it — carrying ``auto_run: True``. A state-changing
    one reaches it as ``pending`` and runs nothing until it is decided. Same two
    event types as before this change; the flag is the only addition.
    """

    from test_chat import FakeAnthropic, _Response, tool_use_block
    from test_toolloop import FakeSession, _executor

    from kenny_server.chat import _FLEET_POLICY
    from kenny_server.store import TelemetryStore
    from kenny_server.toolloop import drive_events

    store = TelemetryStore(db_path=str(tmp_path / "loop.sqlite"))
    await store.connect()
    try:
        executor, _registry, _tunnel = _executor(store)
        session = FakeSession(id="s1", agent_id="linus-pc")
        session.messages.append({"role": "user", "content": "check it, then flush dns"})
        client = FakeAnthropic(
            [
                _Response(
                    [
                        tool_use_block("tu1", "agent_health", {"id": "linus-pc"}),
                        tool_use_block("tu2", "net_dns_flush", {}),
                    ],
                    "tool_use",
                ),
            ]
        )
        events = [
            ev
            async for ev in drive_events(
                session, executor, client=client, model="m", policy=_FLEET_POLICY
            )
        ]
    finally:
        await store.close()

    results = [e for e in events if e["type"] == "tool_result"]
    pendings = [e for e in events if e["type"] == "pending"]

    assert [e["tool"] for e in results] == ["agent_health"]
    assert results[0]["auto_run"] is True

    # The state-changing call never became a tool_result: it is a gate.
    assert [e["tool"] for e in pendings] == ["net_dns_flush"]
    assert "auto_run" not in pendings[0]


def test_replayed_transcript_carries_auto_run(tmp_path) -> None:
    """History replay reports the tier, since gate state is never persisted."""

    from kenny_server.chat import public_transcript

    messages = [
        {"role": "user", "content": "how is it doing?"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu1", "name": "agent_health", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "{}"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu2", "name": "powershell_exec", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu2", "content": "{}"},
            ],
        },
    ]
    events = {e["tool"]: e for e in public_transcript(messages) if e["type"] == "tool_result"}
    assert events["agent_health"]["auto_run"] is True
    assert events["powershell_exec"]["auto_run"] is False


# -- item 7: the config catalog ----------------------------------------------


def test_every_settings_row_says_whether_it_is_editable(tmp_path) -> None:
    """``editable`` must agree with what the write path actually enforces.

    The console renders an input only for an editable row. If that flag and the
    403 the server returns could disagree, the console would offer a control
    guaranteed to fail — so they are read from the same ``SettingSpec.writable``
    property, and this walks every row to prove it.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        groups = c.get("/api/settings", headers=h).json()["groups"]
        rows = [row for g in groups for row in g["settings"]]
        assert rows

        env_only = [r for r in rows if r["lifecycle"] == "env_only"]
        assert env_only, "the catalog must still contain env-derived keys"

        for row in rows:
            assert row["editable"] is (row["lifecycle"] != "env_only"), row["key"]
            # The contract's other required fields are all present already.
            assert set(row) >= {"key", "label", "help", "value", "source", "editable"}
            assert row["source"] in ("db", "env", "default")

        # A non-editable row really is refused, with the status the console
        # would otherwise have walked into.
        key = env_only[0]["key"]
        assert c.put(f"/api/settings/{key}", headers=h, json={"value": "x"}).status_code == 403
        assert c.delete(f"/api/settings/{key}", headers=h).status_code == 403


def test_single_key_write_response_carries_editable_too(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.put(
            "/api/settings/KENNY_ALERT_COOLDOWN_SECS", headers=_bearer(app), json={"value": 120}
        )
        assert r.status_code == 200
        assert r.json()["editable"] is True


def test_alert_channels_are_editable_rows_and_stay_redacted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four push channels render as real controls, and never leak a value.

    Admin renders the catalog generically, so "editable" here *is* the whole
    frontend story (ADR-0054). The second half is the price of that: the value
    goes in and is never readable back out — a webhook URL is bearer-equivalent.
    """

    # A host that happens to export one of these would otherwise make the
    # "not set" starting point untrue.
    for key in notify.CHANNEL_KEYS:
        monkeypatch.delenv(key, raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        secret_key = "KENNY_WEBHOOK_URL"
        secret = "https://hook.example/2f9c-never-echo-me"

        rows = {
            row["key"]: row
            for g in c.get("/api/settings", headers=h).json()["groups"]
            for row in g["settings"]
        }
        for key in notify.CHANNEL_KEYS:
            assert rows[key]["editable"] is True, key
            assert rows[key]["sensitive"] is True, key
            assert rows[key]["value"] is None and rows[key]["is_set"] is False, key

        written = c.put(f"/api/settings/{secret_key}", headers=h, json={"value": secret})
        assert written.status_code == 200
        assert written.json()["value"] is None  # the write does not echo it back
        assert secret not in written.text

        listed = c.get("/api/settings", headers=h)
        assert secret not in listed.text
        after = {
            row["key"]: row
            for g in listed.json()["groups"]
            for row in g["settings"]
        }[secret_key]
        assert after["is_set"] is True and after["source"] == "db"

        # And the live engine picks it up with no restart in between.
        assert [n.name for n in app.state.notifier_provider.current()] == ["webhook"]

        assert c.delete(f"/api/settings/{secret_key}", headers=h).status_code == 200
        assert app.state.notifier_provider.current() == []


# -- item 8: start_immediately -----------------------------------------------


def _setup_admin(c) -> None:
    r = c.post(
        "/setup",
        data={"username": "admin", "password": "pw-123456"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _pat_for(c, username: str) -> str:
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    return c.post(f"/api/users/{users[username]['id']}/pats", json={"label": "t"}).json()["token"]


def test_start_immediately_starts_the_ticket_for_an_operator(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/tickets",
            headers=_bearer(app),
            json={
                "title": "disk filling on study-pc",
                "agent_id": "study-pc",
                "summary": "12 GB left",
                "start_immediately": True,
            },
        )
        assert r.status_code == 201
        body = r.json()
        assert body["state"] == "in_progress"
        assert body["started"] is True
        assert body["start_error"] is None

        # Without the flag the ticket stays where `TicketService.create` puts it.
        plain = c.post("/api/tickets", headers=_bearer(app), json={"title": "later"}).json()
        assert plain["state"] == "new"
        assert plain["started"] is False


def test_start_immediately_does_not_bypass_the_new_to_in_progress_gate(tmp_path) -> None:
    """A requester may open a ticket; they still may not start work on it.

    ``tickets.py``'s ``_ACTORS`` makes ``new -> in_progress`` system/operator
    only, on the stated ground that "opening a ticket does not entitle its author
    to drive its lifecycle". Honouring ``start_immediately`` by re-issuing the
    transition as ``actor="system"`` would have turned a request body flag into a
    way around that rule, so the route asks as the caller and reports the
    refusal instead. The ticket is still created — that write already happened,
    and failing the whole call would lose it.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        c.post(
            "/api/users",
            json={"username": "kid", "password": "pw-123456", "role": "user"},
        )
        kid_pat = _pat_for(c, "kid")

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {kid_pat}"}
        r = c.post(
            "/api/tickets",
            headers=h,
            json={"title": "my laptop is slow", "start_immediately": True},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["state"] == "new"  # not forced
        assert body["started"] is False
        assert body["start_error"]
        assert "in_progress" in body["start_error"]

        # And the ticket is genuinely there, at `new`, not half-written.
        again = c.get(f"/api/tickets/{body['id']}", headers=h).json()
        assert again["state"] == "new"


# -- item 10: the persisted theme --------------------------------------------


def test_theme_round_trips_for_an_account(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        assert c.get("/api/me").json()["theme"] is None

        r = c.put("/api/me/theme", json={"theme": "dark"})
        assert r.status_code == 200
        assert r.json() == {"theme": "dark", "stored": True}
        assert c.get("/api/me").json()["theme"] == "dark"

        assert c.put("/api/me/theme", json={"theme": "light"}).status_code == 200
        assert c.get("/api/me").json()["theme"] == "light"

    # It followed the account into a new process, which is the whole point:
    # the choice travels between browsers, not just between reloads.
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "pw-123456"},
               follow_redirects=False)
        assert c.get("/api/me").json()["theme"] == "light"


def test_theme_rejects_anything_outside_the_closed_vocabulary(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        for bad in ("solarized", "", None, 1):
            assert c.put("/api/me/theme", json={"theme": bad}).status_code == 400
        assert c.put("/api/me/theme", json={}).status_code == 400
        assert c.get("/api/me").json()["theme"] is None


def test_theme_is_skipped_cleanly_for_a_shared_token_identity(tmp_path) -> None:
    """The legacy shared token has no account, so there is nowhere to store it.

    That is not an error the console should have to handle: the call succeeds
    and says ``stored: false``, and ``GET /api/me`` still carries the key so the
    response shape does not change with the identity. localStorage remains that
    session's only store.
    """

    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)  # the env/shared operator token, user_id is None
        me = c.get("/api/me", headers=h).json()
        assert me["is_shared_token"] is True
        assert me["theme"] is None

        r = c.put("/api/me/theme", headers=h, json={"theme": "dark"})
        assert r.status_code == 200
        assert r.json() == {"theme": "dark", "stored": False}

        # Nothing was invented to persist it against.
        assert c.get("/api/me", headers=h).json()["theme"] is None

        # A bad value is still a bad value, whoever asks.
        assert c.put("/api/me/theme", headers=h, json={"theme": "neon"}).status_code == 400

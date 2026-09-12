"""The copilot's two ticket tools, and the evidence a drafted ticket carries.

Three seams meet here and each is tested joined, not half:

1. **Who may reach the tools.** The dashboard copilot must offer them and every
   ticket-bound turn must not. Asserting only the exclusion would pass with the
   tools never wired up at all, so both directions are one test.
2. **What a draft does.** Nothing, to the ticket store. The whole design rests on
   that, so it is asserted against a real store rather than a mock.
3. **What reaches the operator's browser.** ``chat._surface_events`` turns the
   loop's tool result into the event the drawer renders, on the turn stream and
   the resumed-after-a-gate stream alike.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kenny_server import chat
from kenny_server.copilot_tickets import (
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    CopilotTickets,
    evidence_from_session,
)
from kenny_server.registry import AgentRegistry
from kenny_server.store import TelemetryStore
from kenny_server.ticket_assistant import allowed_tools_for
from kenny_server.ticketstore import TicketStore
from kenny_server.tickets import TicketService
from kenny_server.toolloop import (
    TICKET_DRAFT_TOOL,
    TICKET_FIND_TOOL,
    build_tool_schemas,
)
from kenny_server.tunnel import ToolError

NOW = datetime(2026, 9, 12, 9, 0, 0, tzinfo=timezone.utc)


# -- seam 1: offered to the copilot, withheld from every ticket --------------


def test_the_copilot_gets_both_tools_and_no_ticket_turn_does() -> None:
    """Both halves, in one test.

    ``build_tool_schemas()`` with no allowlist is exactly what ``chat.py``
    passes, so this is the copilot's real schema set; ``allowed_tools_for`` is
    what every ticket-bound and triage turn is narrowed to. A test that only
    checked the second half would still pass if the tools were never offered
    anywhere.
    """

    offered = {t["name"] for t in build_tool_schemas()}
    assert {TICKET_DRAFT_TOOL, TICKET_FIND_TOOL} <= offered

    for kwargs in (
        {"profile": None, "scoped": False},  # an operator working any ticket
        {"profile": None, "scoped": True},  # a host-scoped requester
        {"profile": "power-user", "scoped": True},
        {"profile": None, "scoped": True, "triage": True},  # nobody watching
    ):
        allowed = allowed_tools_for(**kwargs)  # type: ignore[arg-type]
        assert TICKET_DRAFT_TOOL not in allowed, kwargs
        assert TICKET_FIND_TOOL not in allowed, kwargs


def test_neither_tool_reaches_the_mcp_surface() -> None:
    """These are dashboard affordances; an MCP client has no drawer to fill."""

    from kenny_server import tools

    assert TICKET_DRAFT_TOOL not in tools.CAPABILITY_TOOLS
    assert TICKET_FIND_TOOL not in tools.CAPABILITY_TOOLS


# -- seam 2: a draft proposes and creates nothing ----------------------------


@asynccontextmanager
async def _copilot(tmp_path: Any) -> AsyncIterator[tuple[CopilotTickets, TicketStore, TicketService]]:
    """A real copilot over real stores, with ``pc-kid`` the one known host."""

    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    db = str(Path(tmp_path) / "copilot.sqlite")
    ticket_store = TicketStore(db)
    telemetry = TelemetryStore(db)
    await ticket_store.connect()
    await telemetry.connect()
    registry = AgentRegistry(tokens={"pc-kid": "t"})
    registry.register("pc-kid", "t", {"os": "windows"}, lambda *a, **kw: None)
    try:
        yield (
            CopilotTickets(tickets=ticket_store, registry=registry, store=telemetry),
            ticket_store,
            TicketService(ticket_store, now=lambda: NOW),
        )
    finally:
        await telemetry.close()
        await ticket_store.close()


@pytest.mark.asyncio
async def test_a_draft_writes_no_ticket(tmp_path) -> None:
    async with _copilot(tmp_path) as (copilot, store, _svc):
        result = await copilot.draft(
            {"title": "Updates fail", "summary": "0x80070422 since Tuesday.",
             "agent_id": "pc-kid"},
        )
        assert result["drafted"] is True
        assert result["created"] is False
        assert result["agent_id"] == "pc-kid"
        assert await store.list(limit=50) == []


@pytest.mark.asyncio
async def test_a_draft_falls_back_to_the_conversations_own_host(tmp_path) -> None:
    """A model leaving `agent_id` out is not a claim the ticket has no host."""

    class _Session:
        agent_id = "pc-kid"

    async with _copilot(tmp_path) as (copilot, _store, _svc):
        result = await copilot.draft(
            {"title": "Updates fail", "summary": "0x80070422."}, session=_Session()
        )
        assert result["agent_id"] == "pc-kid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,code",
    [
        ({"title": "", "summary": "x"}, "bad_args"),
        ({"title": "t", "summary": "  "}, "bad_args"),
        ({"title": "t" * (MAX_TITLE_CHARS + 1), "summary": "x"}, "bad_args"),
        ({"title": "t", "summary": "s" * (MAX_SUMMARY_CHARS + 1)}, "bad_args"),
        ({"title": "t", "summary": "x", "agent_id": "pc-nope"}, "unknown_agent"),
    ],
)
async def test_a_draft_refuses_what_the_form_could_not_take(tmp_path, args, code) -> None:
    async with _copilot(tmp_path) as (copilot, store, _svc):
        with pytest.raises(ToolError) as exc:
            await copilot.draft(args)
        assert exc.value.code == code
        assert await store.list(limit=50) == []


@pytest.mark.asyncio
async def test_find_reports_open_tickets_for_a_host(tmp_path) -> None:
    async with _copilot(tmp_path) as (copilot, _store, svc):
        open_here = await svc.create(title="still open", origin="dashboard", agent_id="pc-kid")
        done = await svc.create(title="finished", origin="dashboard", agent_id="pc-kid")
        await svc.transition(done.id, "in_progress", actor="operator")
        await svc.transition(done.id, "resolved", actor="operator")
        await svc.create(title="other host", origin="dashboard", agent_id="pc-other")

        found = await copilot.find({"agent_id": "pc-kid"})
        assert [t["id"] for t in found["tickets"]] == [open_here.id]

        everywhere = await copilot.find({})
        assert len(everywhere["tickets"]) == 2


# -- the evidence a drafted ticket carries ----------------------------------


def _turn(tool: str, tool_use_id: str, args: dict[str, Any], *, ok: bool = True) -> list[dict]:
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_use_id, "name": tool, "input": args}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "{}",
             **({} if ok else {"is_error": True})}
        ]},
    ]


def test_evidence_names_successful_read_only_calls_only() -> None:
    """What was *checked* — not what failed, and not what was changed.

    A change-tier call in a chat session belongs to the audit log; offering it
    here would make the ticket claim the conversation did something to the
    machine on this ticket's behalf, which it did not.
    """

    session = chat.FleetSession(id="s1")
    session.messages = [
        {"role": "user", "content": "why are updates failing?"},
        *_turn("diag_services", "a", {"agent_id": "pc-kid"}),
        *_turn("fs_disk_usage", "b", {"agent_id": "pc-kid"}),
        *_turn("diag_eventlog", "c", {"agent_id": "pc-kid"}, ok=False),
        *_turn("powershell_exec", "d", {"agent_id": "pc-kid", "script": "x"}),
        *_turn("diag_services", "e", {"agent_id": "pc-kid"}),  # same call again
    ]

    assert evidence_from_session(session) == [
        {"tool": "diag_services", "agent_id": "pc-kid"},
        {"tool": "fs_disk_usage", "agent_id": "pc-kid"},
    ]


def test_evidence_leaves_out_what_looked_at_no_machine() -> None:
    """A draft citing itself as evidence is what this prevents.

    `ticket_draft` is read-only because it creates nothing, not because it
    checked anything — so is `ticket_find`, and so is `select_agent`, which only
    moves a pointer. None of them belongs in a sentence beginning "already
    checked".
    """

    session = chat.FleetSession(id="s3")
    session.messages = [
        *_turn("select_agent", "a", {"id": "pc-kid"}),
        *_turn(TICKET_FIND_TOOL, "b", {"agent_id": "pc-kid"}),
        *_turn("diag_services", "c", {"agent_id": "pc-kid"}),
        *_turn(TICKET_DRAFT_TOOL, "d", {"title": "x", "summary": "y"}),
    ]

    assert evidence_from_session(session) == [
        {"tool": "diag_services", "agent_id": "pc-kid"},
    ]


def test_evidence_of_an_empty_conversation_is_empty() -> None:
    assert evidence_from_session(chat.FleetSession(id="s2")) == []


# -- seam 3: what reaches the drawer ----------------------------------------


async def _drain(events: Any) -> list[dict[str, Any]]:
    return [ev async for ev in events]


async def _events(*evs: dict[str, Any]) -> Any:
    for ev in evs:
        yield ev


@pytest.mark.asyncio
async def test_a_draft_result_becomes_a_draft_event() -> None:
    out = await _drain(
        chat._surface_events(
            _events(
                {"type": "tool_result", "tool": "diag_services", "args": {}, "ok": True},
                {
                    "type": "tool_result",
                    "tool": TICKET_DRAFT_TOOL,
                    "args": {"title": "Updates fail", "summary": "0x80070422.",
                             "agent_id": "pc-kid"},
                    "ok": True,
                },
            )
        )
    )
    assert [e["type"] for e in out] == ["tool_result", "tool_result", "ticket_draft"]
    assert out[-1] == {
        "type": "ticket_draft",
        "title": "Updates fail",
        "summary": "0x80070422.",
        "agent_id": "pc-kid",
    }


@pytest.mark.asyncio
async def test_a_failed_draft_produces_no_card() -> None:
    """A refused draft is an error to report, not a form to fill in."""

    out = await _drain(
        chat._surface_events(
            _events({"type": "tool_result", "tool": TICKET_DRAFT_TOOL,
                     "args": {"title": "x"}, "ok": False})
        )
    )
    assert [e["type"] for e in out] == ["tool_result"]


# -- the whole path, on one app ---------------------------------------------


def test_a_conversation_becomes_a_ticket_that_records_what_it_checked(tmp_path) -> None:
    """Drawer to inbox, through the real routes.

    A turn runs a read-only check and drafts a ticket; the stream carries the
    draft; the create route opens it and the trail says both where it came from
    and what had already been looked at. Every unit test above asserts one link
    of this; none of them asserts that the links are joined.
    """

    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.testclient import TestClient

    from kenny_server.auth import OperatorAuthMiddleware
    from kenny_server.chat import ChatSessions
    from kenny_server.main import _prior_checks_reader
    from kenny_server.store import ChatHistoryStore, EventStore
    from kenny_server.tools import CallLog, ScreenshotStore
    from kenny_server.tunnel import AgentTunnel
    from kenny_server.userstore import UserStore
    from kenny_server.webui import build_chat_routes
    from kenny_server.webui.tickets import build_ticket_routes

    from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    db = str(Path(tmp_path) / "e2e.sqlite")
    telemetry = TelemetryStore(db_path=db)
    registry = AgentRegistry(tokens={"pc-kid": "t"})
    registry.register("pc-kid", "t", {"os": "windows"}, lambda *a, **kw: None)
    tunnel = AgentTunnel(registry, telemetry, EventStore(db_path=db))
    history = ChatHistoryStore(db_path=db)
    sessions = ChatSessions(store=history)
    ticket_store = TicketStore(db)
    service = TicketService(ticket_store, now=lambda: NOW)
    users = UserStore(db)

    turn = [
        _Response([tool_use_block("t1", "agent_health", {"id": "pc-kid"})], "tool_use"),
        _Response(
            [tool_use_block("t2", TICKET_DRAFT_TOOL, {
                "title": "Windows updates fail",
                "summary": "Error 0x80070422; the update service is disabled.",
                "agent_id": "pc-kid",
            })],
            "tool_use",
        ),
        _Response([text_block("Drafted one for you.")], "end_turn"),
    ]

    routes = build_chat_routes(
        registry=registry,
        store=telemetry,
        tunnel=tunnel,
        call_log=CallLog(),
        sessions=sessions,
        screenshots=ScreenshotStore(),
        history_store=history,
        client_factory=lambda: FakeAnthropic(turn),
        copilot_tickets=CopilotTickets(
            tickets=ticket_store, registry=registry, store=telemetry
        ),
    ) + build_ticket_routes(
        tickets=service,
        store=ticket_store,
        user_store=users,
        evidence_reader=_prior_checks_reader(sessions),
    )

    @asynccontextmanager
    async def lifespan(_app):
        for s in (telemetry, history, ticket_store, users):
            await s.connect()
        op = await users.create_user("op", "pw-123456", "operator")
        _app.state.pat = await users.create_pat(op["id"], "t")
        yield
        for s in (telemetry, history, ticket_store, users):
            await s.close()

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(OperatorAuthMiddleware, token="unused", user_store=users)
        ],
        lifespan=lifespan,
    )

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {app.state.pat}"}
        stream = c.post(
            "/api/chat/stream",
            json={"message": "make a ticket out of this", "agent_id": "pc-kid",
                  "scope": "host"},
            headers=h,
        )
        frames = [
            __import__("json").loads(line[5:].strip())
            for block in stream.text.split("\n\n")
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        draft = next(f for f in frames if f["type"] == "ticket_draft")
        assert draft["title"] == "Windows updates fail"
        assert draft["agent_id"] == "pc-kid"
        # Drafting filed nothing.
        assert c.get("/api/tickets", headers=h).json()["tickets"] == []

        session_id = frames[-1]["session_id"]
        created = c.post(
            "/api/tickets",
            json={
                # The operator's wording, not the model's -- the form was edited.
                "title": "Update service disabled on pc-kid",
                "summary": draft["summary"],
                "agent_id": draft["agent_id"],
                "origin": "copilot",
                "chat_session_id": session_id,
            },
            headers=h,
        )
        assert created.status_code == 201
        assert created.json()["title"] == "Update service disabled on pc-kid"

        events = c.get(f"/api/tickets/{created.json()['id']}/events", headers=h).json()
        genesis = next(e for e in events["events"] if e["kind"] == "state")
        assert genesis["summary"] == "opened from an Ask kenny conversation"
        note = next(e for e in events["events"] if e["kind"] == "note")
        # The check the turn actually ran, named by the server -- and the draft
        # call itself is not in it: it looked at no machine.
        assert note["summary"] == (
            "already checked in the conversation this came from: `agent_health` on pc-kid"
        )

"""RBAC + ownership tests for the ticket/approval/discord/tool-classes API.

This module builds a minimal standalone Starlette app from
``build_ticket_routes`` plus the *real* ``OperatorAuthMiddleware`` and
``UserStore`` — the same authentication stack ``tests/test_rbac.py`` exercises
against the full app, just without the rest of the dashboard's routes mounted
(``tests/test_main_wiring.py`` covers the composed app). Accounts and PATs are
minted directly through ``UserStore`` (there is no ``/api/users`` route in this
standalone app), and tickets/approvals are seeded directly through
``TicketService``/``TicketStore`` inside the app's own lifespan, so everything
runs on the one event loop ``TestClient`` drives.

The Discord routes are exercised against the *real* collaborators: a real
``DiscordIdentityStore`` and a real ``DiscordService`` over the in-memory
``FakeDiscordGateway``. ``with_discord=False`` builds the same routes with
neither, which is what a server without Discord configuration serves.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from kenny_server.auth import OperatorAuthMiddleware
from kenny_server.discord_adapter import GuildMember
from kenny_server.discord_identity import DiscordIdentityStore
from kenny_server.discord_service import DiscordService
from kenny_server.ticket_assistant import TicketAssistant
from kenny_server.ticketstore import TicketStore
from kenny_server.tickets import TicketService
from kenny_server.userstore import UserStore
from kenny_server.webui.tickets import build_ticket_routes

from support.fake_discord import FakeDiscordGateway

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
GUILD = "guild-1"

Seed = Callable[[UserStore, TicketStore, TicketService], Awaitable[dict[str, Any]]]


# -- a scripted fake Anthropic client, for the chat/stream route's own tests ---
# (shape copied from tests/test_discord_service.py: a text-only end_turn is all
# these routes need to script, since none of them exercises a capability tool.)


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


def text_turn(text: str) -> _Response:
    return _Response([_Block(type="text", text=text)], "end_turn")


class _StreamCtx:
    def __init__(self, response: _Response) -> None:
        self._response = response

    def __iter__(self) -> Any:
        # The ``content_block_delta`` shape the loop reads: one delta per block,
        # text and thinking on their own channels.
        for block in self._response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                yield _Block(
                    type="content_block_delta",
                    delta=_Block(type="text_delta", text=block.text),
                )
            elif kind == "thinking":
                yield _Block(
                    type="content_block_delta",
                    delta=_Block(type="thinking_delta", thinking=getattr(block, "thinking", "")),
                )

    def __enter__(self) -> _StreamCtx:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def get_final_message(self) -> _Response:
        return self._response


class _FakeMessages:
    def __init__(self, scripted: list[_Response]) -> None:
        self._scripted = scripted
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _StreamCtx:
        self.calls.append(kwargs)
        return _StreamCtx(self._scripted.pop(0))


class _FakeAnthropic:
    def __init__(self, scripted: list[_Response]) -> None:
        self.messages = _FakeMessages(scripted)


class _FakeExecutor:
    """Runs a capability call without an agent, recording what it was asked to run.

    Only the approval routes need one: approving an ``operator_approval``
    *executes* the frozen call, so a test that stops at the decision would be
    asserting half of what the route does."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, dict[str, Any], str | None]] = []
        self.server_tool_handlers: dict[str, Any] = {}

    async def run_capability(
        self, tool: str, args: dict[str, Any], *, agent_id: str | None = None
    ) -> dict[str, Any]:
        self.ran.append((tool, dict(args), agent_id))
        return {"ok": True, "stdout": "hi"}

    async def run_server_tool(
        self, tool: str, args: dict[str, Any], session: Any = None
    ) -> dict[str, Any]:
        self.ran.append((tool, dict(args), None))
        return {"ok": True}


def _build_app(
    tmp_path,
    seed: Seed,
    *,
    with_discord: bool = False,
    with_assistant: bool = False,
    scripted: list[_Response] | None = None,
    with_executor: bool = False,
    evidence_reader: Any = None,
) -> Starlette:
    """Build the standalone ticket API app.

    ``with_assistant`` (or ``with_discord``, which always implies it) builds a
    real :class:`TicketAssistant` — with a scripted fake Anthropic client when
    ``scripted`` is given, or a client-less one (never driven, just present for
    the routes that only check ``assistant is not None``) otherwise.
    """

    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    db_path = str(Path(tmp_path) / "tickets_api.sqlite")
    user_store = UserStore(db_path)
    ticket_store = TicketStore(db_path)
    service = TicketService(ticket_store, now=lambda: NOW)
    identities: DiscordIdentityStore | None = None
    discord: DiscordService | None = None
    assistant: TicketAssistant | None = None
    client = _FakeAnthropic(list(scripted)) if scripted is not None else None
    executor = _FakeExecutor() if with_executor else None
    if with_assistant or with_discord:
        assistant = TicketAssistant(
            tickets=service,
            users=user_store,
            executor=executor,  # type: ignore[arg-type] - None unless a test runs a call
            client=client,
            model="scripted",
        )
    if with_discord:
        identities = DiscordIdentityStore(db_path)
        gateway = FakeDiscordGateway(
            members={GUILD: [GuildMember(user_id="900", display_hint="Kid")]}
        )
        discord = DiscordService(
            gateway=gateway,
            identities=identities,
            tickets=service,
            users=user_store,
            executor=None,  # type: ignore[arg-type]
            assistant=assistant,
            guild_ids={GUILD},
        )
    routes = build_ticket_routes(
        tickets=service,
        store=ticket_store,
        identities=identities,
        user_store=user_store,
        discord=discord,
        assistant=assistant,
        evidence_reader=evidence_reader,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await user_store.connect()
        await ticket_store.connect()
        if identities is not None:
            await identities.connect()
        app.state.seed = await seed(user_store, ticket_store, service)
        try:
            yield
        finally:
            await user_store.close()
            await ticket_store.close()
            if identities is not None:
                await identities.close()

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                OperatorAuthMiddleware, token="unused-shared-token", user_store=user_store
            )
        ],
        lifespan=lifespan,
    )
    app.state.identities = identities
    app.state.discord = discord
    app.state.assistant = assistant
    app.state.gateway = discord.gateway if discord is not None else None
    app.state.tickets = service
    app.state.ticket_store = ticket_store
    app.state.executor = executor
    return app


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sse_events(text: str) -> list[dict[str, Any]]:
    """Parse an SSE response body into its ``data:`` event payloads, in order."""

    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


# -- ownership: a `user` reads their own ticket, not another's -----------------


def test_user_reads_own_ticket_not_others(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        sib_pat = await users.create_pat(sib["id"], "t")
        kid_ticket = await svc.create(
            title="printer jam", origin="dashboard", requester_user_id=kid["id"]
        )
        return {
            "kid_pat": kid_pat,
            "sib_pat": sib_pat,
            "kid_ticket_id": kid_ticket.id,
        }

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        # Owner reads their own ticket.
        r = c.get(f"/api/tickets/{s['kid_ticket_id']}", headers=_hdr(s["kid_pat"]))
        assert r.status_code == 200
        assert r.json()["id"] == s["kid_ticket_id"]

        # Sibling is refused, consistently 403 (known ticket, not theirs).
        r = c.get(f"/api/tickets/{s['kid_ticket_id']}", headers=_hdr(s["sib_pat"]))
        assert r.status_code == 403

        # An unknown ticket id is 404 for anyone, including the owner.
        r = c.get("/api/tickets/does-not-exist", headers=_hdr(s["kid_pat"]))
        assert r.status_code == 404

        # Events follow the same ownership rule.
        assert (
            c.get(f"/api/tickets/{s['kid_ticket_id']}/events", headers=_hdr(s["sib_pat"])
                  ).status_code == 403
        )
        assert (
            c.get(f"/api/tickets/{s['kid_ticket_id']}/events", headers=_hdr(s["kid_pat"])
                  ).status_code == 200
        )

        # The presented timeline is the same rows seen differently, so it is
        # the same ownership rule: filtering there is presentation, never
        # access control.
        assert (
            c.get(f"/api/tickets/{s['kid_ticket_id']}/timeline", headers=_hdr(s["sib_pat"])
                  ).status_code == 403
        )
        assert (
            c.get(f"/api/tickets/{s['kid_ticket_id']}/timeline", headers=_hdr(s["kid_pat"])
                  ).status_code == 200
        )


def test_list_returns_only_own_rows_for_user(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        kid_pat = await users.create_pat(kid["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")
        await svc.create(title="kid's ticket", origin="dashboard", requester_user_id=kid["id"])
        await svc.create(title="sib's ticket", origin="dashboard", requester_user_id=sib["id"])
        # An alert-origin ticket has no requester at all.
        await svc.create(title="disk full alert", origin="alert", requester_user_id=None)
        return {"kid_pat": kid_pat, "op_pat": op_pat}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        kid_tickets = c.get("/api/tickets", headers=_hdr(s["kid_pat"])).json()["tickets"]
        assert len(kid_tickets) == 1
        assert kid_tickets[0]["title"] == "kid's ticket"

        # Even asking for someone else's requester_user_id by query param does
        # not widen a scoped user's view (the param is only honoured for
        # operator+).
        r = c.get(
            "/api/tickets?requester_user_id=999", headers=_hdr(s["kid_pat"])
        ).json()["tickets"]
        assert len(r) == 1
        assert r[0]["title"] == "kid's ticket"

        # Operator sees everything, including the alert-origin ticket.
        op_tickets = c.get("/api/tickets", headers=_hdr(s["op_pat"])).json()["tickets"]
        assert len(op_tickets) == 3


def test_alert_origin_ticket_invisible_to_user_and_operator_can_see_it(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        kid_pat = await users.create_pat(kid["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")
        alert = await svc.create(title="disk full", origin="alert", requester_user_id=None)
        return {"kid_pat": kid_pat, "op_pat": op_pat, "alert_id": alert.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        assert (
            c.get(f"/api/tickets/{s['alert_id']}", headers=_hdr(s["kid_pat"])).status_code
            == 403
        )
        assert (
            c.get(f"/api/tickets/{s['alert_id']}", headers=_hdr(s["op_pat"])).status_code
            == 200
        )


# -- action gating: user cannot approve/reassign/note; operator can ------------


def test_user_cannot_note_or_approve(tmp_path) -> None:
    async def seed(users: UserStore, store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        kid_pat = await users.create_pat(kid["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(
            title="need help", origin="dashboard", requester_user_id=kid["id"]
        )
        approval = await svc.open_approval(
            ticket.id,
            tool_use_id="tu-1",
            tool="shell_exec",
            tool_class="normal_change",
            args={"cmd": "echo hi"},
        )
        return {
            "kid_pat": kid_pat,
            "op_pat": op_pat,
            "ticket_id": ticket.id,
            "approval_id": approval.id,
        }

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        kid_h = _hdr(s["kid_pat"])
        op_h = _hdr(s["op_pat"])

        assert (
            c.post(
                f"/api/tickets/{s['ticket_id']}/note", json={"summary": "hi"}, headers=kid_h
            ).status_code == 403
        )
        assert (
            c.post(
                f"/api/approvals/{s['approval_id']}", json={"approve": True}, headers=kid_h
            ).status_code == 403
        )
        # ... and an operator can do both.
        assert (
            c.post(
                f"/api/tickets/{s['ticket_id']}/note", json={"summary": "hi"}, headers=op_h
            ).status_code == 201
        )
        r = c.post(
            f"/api/approvals/{s['approval_id']}", json={"approve": True}, headers=op_h
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"


def test_requester_sees_only_approvals_on_their_own_tickets(tmp_path) -> None:
    """Bug: a `user_consent` gate can only be decided by the ticket's own
    requester (`TicketService.decide_approval`: "Approving is enforced here"),
    but `/api/approvals` used to floor at `operator`, so that requester had no
    route to even see the one gate they are the only person allowed to act on.

    Fails against the pre-fix code because ``kid``'s and ``sib``'s GET both
    return 403 (route floored at `operator`) instead of each seeing only their
    own ticket's approval. Widening what a `user` may *see* here must not
    widen what they may *decide* -- the second half of this test pins that
    down: `sib`/`op` still cannot grant `kid`'s consent gate.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        await users.set_user_hosts(kid["id"], ["pc-kid"])
        await users.set_user_hosts(sib["id"], ["pc-sib"])
        kid_pat = await users.create_pat(kid["id"], "t")
        sib_pat = await users.create_pat(sib["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")

        kid_ticket = await svc.create(
            title="kid's request", origin="dashboard", requester_user_id=kid["id"],
            agent_id="pc-kid",
        )
        kid_approval = await svc.open_approval(
            kid_ticket.id, tool_use_id="tu-1", tool="screen_view",
            tool_class="normal_change", kind="user_consent", args={},
            agent_id="pc-kid",
        )
        sib_ticket = await svc.create(
            title="sib's request", origin="dashboard", requester_user_id=sib["id"],
            agent_id="pc-sib",
        )
        sib_approval = await svc.open_approval(
            sib_ticket.id, tool_use_id="tu-2", tool="screen_view",
            tool_class="normal_change", kind="user_consent", args={},
            agent_id="pc-sib",
        )
        return {
            "kid_pat": kid_pat,
            "sib_pat": sib_pat,
            "op_pat": op_pat,
            "kid_approval_id": kid_approval.id,
            "sib_approval_id": sib_approval.id,
            "sib_ticket_id": sib_ticket.id,
        }

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed

        # Each requester sees exactly their own ticket's gate, nobody else's.
        kid_rows = c.get("/api/approvals", headers=_hdr(s["kid_pat"])).json()["approvals"]
        assert [r["id"] for r in kid_rows] == [s["kid_approval_id"]]

        sib_rows = c.get("/api/approvals", headers=_hdr(s["sib_pat"])).json()["approvals"]
        assert [r["id"] for r in sib_rows] == [s["sib_approval_id"]]

        # The operator-wide listing is unchanged: both gates, untouched.
        op_rows = c.get("/api/approvals", headers=_hdr(s["op_pat"])).json()["approvals"]
        assert {r["id"] for r in op_rows} == {s["kid_approval_id"], s["sib_approval_id"]}

        # Naming someone else's ticket by id is refused the same way every
        # other per-ticket route refuses it.
        assert (
            c.get(
                f"/api/approvals?ticket_id={s['sib_ticket_id']}",
                headers=_hdr(s["kid_pat"]),
            ).status_code
            == 403
        )

        # Seeing kid's gate does not let sib decide it -- sib is not its
        # requester, so the route's own ownership pre-check refuses them
        # before the service is even consulted.
        assert (
            c.post(
                f"/api/approvals/{s['kid_approval_id']}", json={"approve": True},
                headers=_hdr(s["sib_pat"]),
            ).status_code
            == 403
        )
        # ... nor does it let an operator grant it: `decide_approval` itself
        # refuses anyone but the requester for a `user_consent` approval.
        assert (
            c.post(
                f"/api/approvals/{s['kid_approval_id']}", json={"approve": True},
                headers=_hdr(s["op_pat"]),
            ).status_code
            == 403
        )
        # ... only kid, the person the consent concerns, may grant their own.
        # (See ``test_requester_can_decide_their_own_consent_gate_over_the_dashboard``
        # and its siblings below for the dedicated, from-scratch coverage of
        # this route.)
        r = c.post(
            f"/api/approvals/{s['kid_approval_id']}", json={"approve": True},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"


# -- POST /api/approvals/{aid}: a requester may decide their own consent gate -


def _seed_two_consent_tickets(
    users: UserStore, svc: TicketService
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Two users, two hosts, one `user_consent` gate each -- the fixture every
    test below shares, so the negative cases are real (a second real user and
    a second real gate to wrongly reach), not an empty-store accident.
    """

    async def seed() -> dict[str, Any]:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        await users.set_user_hosts(kid["id"], ["pc-kid"])
        await users.set_user_hosts(sib["id"], ["pc-sib"])
        kid_pat = await users.create_pat(kid["id"], "t")
        sib_pat = await users.create_pat(sib["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")

        kid_ticket = await svc.create(
            title="kid's consent", origin="dashboard", requester_user_id=kid["id"],
            agent_id="pc-kid",
        )
        kid_consent = await svc.open_approval(
            kid_ticket.id, tool_use_id="tu-1", tool="screen_view",
            tool_class="normal_change", kind="user_consent", args={}, agent_id="pc-kid",
        )
        sib_ticket = await svc.create(
            title="sib's consent", origin="dashboard", requester_user_id=sib["id"],
            agent_id="pc-sib",
        )
        sib_consent = await svc.open_approval(
            sib_ticket.id, tool_use_id="tu-2", tool="screen_view",
            tool_class="normal_change", kind="user_consent", args={}, agent_id="pc-sib",
        )
        kid_change_ticket = await svc.create(
            title="kid's normal change", origin="dashboard", requester_user_id=kid["id"],
            agent_id="pc-kid",
        )
        kid_change = await svc.open_approval(
            kid_change_ticket.id, tool_use_id="tu-3", tool="winget_install",
            tool_class="normal_change", kind="operator_approval", args={}, agent_id="pc-kid",
        )
        return {
            "kid_pat": kid_pat,
            "sib_pat": sib_pat,
            "op_pat": op_pat,
            "kid_consent_id": kid_consent.id,
            "sib_consent_id": sib_consent.id,
            "kid_change_id": kid_change.id,
        }

    return seed


def test_requester_can_decide_their_own_consent_gate_over_the_dashboard(tmp_path) -> None:
    """The gap the coordinator flagged: `decide_approval` already lets a
    `user_consent` gate's own requester decide it, but the route floored at
    `operator`, so a requester could now *see* their gate (once listing was
    fixed) and still never act on it -- visible but unusable. Lowering the
    floor makes both directions (approve and deny) reachable for the gate's
    own requester.

    Fails against the pre-fix floor because both calls below 403 before ever
    reaching the service.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        return await _seed_two_consent_tickets(users, svc)()

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        kid_h = _hdr(s["kid_pat"])

        r = c.post(
            f"/api/approvals/{s['kid_consent_id']}", json={"approve": True}, headers=kid_h
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    app2 = _build_app(tmp_path / "deny", seed)
    with TestClient(app2) as c:
        s = app2.state.seed
        r = c.post(
            f"/api/approvals/{s['kid_consent_id']}", json={"approve": False},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "denied"


def test_requester_cannot_decide_an_operator_approval_on_their_own_ticket(tmp_path) -> None:
    """Lowering the floor must not reach ``operator_approval`` gates at all,
    even on a ticket the caller themselves requested -- that kind is an
    operator-only decision by design (``TicketService.decide_approval``), and
    the route's own kind check refuses it before the service is consulted.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        return await _seed_two_consent_tickets(users, svc)()

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/approvals/{s['kid_change_id']}", json={"approve": True},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 403

        r = c.post(
            f"/api/approvals/{s['kid_change_id']}", json={"approve": False},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 403


def test_a_user_cannot_decide_anothers_consent_gate_either_direction(tmp_path) -> None:
    """The exact widening a naive floor-lower would have opened: denial is
    left open to *any* actor at the service layer (`decide_approval`'s own
    docstring: "Denying stays open to every actor" -- the sweeper's expiry
    relies on it). Without the route's own ownership pre-check, sib could
    have denied kid's consent gate outright. Checked in both directions since
    only ``approve=True`` is gated by the service itself.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        return await _seed_two_consent_tickets(users, svc)()

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        sib_h = _hdr(s["sib_pat"])

        assert (
            c.post(
                f"/api/approvals/{s['kid_consent_id']}", json={"approve": True}, headers=sib_h
            ).status_code
            == 403
        )
        assert (
            c.post(
                f"/api/approvals/{s['kid_consent_id']}", json={"approve": False}, headers=sib_h
            ).status_code
            == 403
        )
        # kid's gate is still exactly as sib left it: pending.
        assert (
            c.get("/api/approvals", headers=_hdr(s["op_pat"])).json()["approvals"][0]["status"]
            == "pending"
        )


def test_operator_can_still_decide_both_kinds(tmp_path) -> None:
    """The floor change is additive for a scoped `user` only -- an operator's
    existing reach is untouched: they can still grant/deny an
    ``operator_approval`` (unchanged), and still deny (never grant -- that was
    always refused, by ``decide_approval`` itself, not the route) a
    ``user_consent`` gate that is not theirs.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        return await _seed_two_consent_tickets(users, svc)()

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        op_h = _hdr(s["op_pat"])

        r = c.post(
            f"/api/approvals/{s['kid_change_id']}", json={"approve": True}, headers=op_h
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

        r = c.post(
            f"/api/approvals/{s['sib_consent_id']}", json={"approve": False}, headers=op_h
        )
        assert r.status_code == 200
        assert r.json()["status"] == "denied"


def test_approving_twice_is_conflict(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(title="risky op", origin="dashboard")
        approval = await svc.open_approval(
            ticket.id,
            tool_use_id="tu-1",
            tool="account_delete",
            tool_class="normal_change",
            args={},
        )
        return {"op_pat": op_pat, "approval_id": approval.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        h = _hdr(s["op_pat"])
        r1 = c.post(f"/api/approvals/{s['approval_id']}", json={"approve": True}, headers=h)
        assert r1.status_code == 200
        r2 = c.post(f"/api/approvals/{s['approval_id']}", json={"approve": True}, headers=h)
        assert r2.status_code == 409


def test_unknown_ticket_and_approval_yield_404(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        return {"op_pat": await users.create_pat(op["id"], "t")}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        h = _hdr(app.state.seed["op_pat"])
        assert c.get("/api/tickets/nope", headers=h).status_code == 404
        assert (
            c.post("/api/approvals/nope", json={"approve": True}, headers=h).status_code
            == 404
        )


# -- user's own close/patch on their own ticket ---------------------------------


def test_user_can_close_own_resolved_ticket(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        ticket = await svc.create(
            title="fixed now", origin="dashboard", requester_user_id=kid["id"]
        )
        await svc.transition(ticket.id, "in_progress", actor="system")
        await svc.transition(ticket.id, "resolved", actor="system")
        return {"kid_pat": kid_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(f"/api/tickets/{s['ticket_id']}/close", headers=_hdr(s["kid_pat"]))
        assert r.status_code == 200
        assert r.json()["state"] == "closed"


# -- operator lifecycle: resolve/reopen/cancel via the generic transition route --


def test_operator_can_resolve_a_ticket_a_requester_cannot(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        kid_pat = await users.create_pat(kid["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(
            title="printer jam", origin="dashboard", requester_user_id=kid["id"]
        )
        await svc.transition(ticket.id, "in_progress", actor="system")
        return {"kid_pat": kid_pat, "op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        # A requester (role `user`) may not resolve — only close an already
        # resolved ticket (the pre-existing, separate `/close` route).
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "resolved"},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 403

        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "resolved", "reason": "reseated the paper"},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 200
        assert r.json()["state"] == "resolved"


def test_requester_can_cancel_their_own_ticket_via_transition(tmp_path) -> None:
    """The route this whole change exists to open up.

    ``_ACTORS`` has always let a requester cancel their own ticket, but the
    route used to floor at ``operator`` and had no ownership check, so that
    right had no HTTP path. It does now — the same generic ``/transition``
    route a `user` may call, narrowed by the pre-existing ``_owned_or_operator``
    check every other per-ticket handler already carries.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        sib_pat = await users.create_pat(sib["id"], "t")
        ticket = await svc.create(
            title="never mind", origin="dashboard", requester_user_id=kid["id"]
        )
        return {"kid_pat": kid_pat, "sib_pat": sib_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        # A sibling cannot even reach the transition check -- ownership is
        # refused before the lifecycle rule is consulted.
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "cancelled"},
            headers=_hdr(s["sib_pat"]),
        )
        assert r.status_code == 403

        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "cancelled", "reason": "sorted it myself"},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 200
        assert r.json()["state"] == "cancelled"


def test_transition_to_an_illegal_state_is_conflict(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        # Freshly created tickets start in "new" -- "closed" is not a legal
        # direct successor from there (it has to pass through "resolved").
        ticket = await svc.create(title="brand new", origin="dashboard")
        return {"op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "closed"},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 409


def test_operator_can_resolve_a_brand_new_ticket(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(title="turned out fine on its own", origin="dashboard")
        return {"op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        h = _hdr(s["op_pat"])
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "resolved", "and_close": True, "reason": "fixed itself"},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["state"] == "closed"

        events = c.get(f"/api/tickets/{s['ticket_id']}/events", headers=h).json()["events"]
        state_events = [e for e in events if e["kind"] == "state"]
        assert [e["to_state"] for e in state_events[-2:]] == ["resolved", "closed"]


def test_resolve_with_and_close_chains_straight_to_closed(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(title="quick fix", origin="dashboard")
        await svc.transition(ticket.id, "in_progress", actor="system")
        return {"op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        h = _hdr(s["op_pat"])
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "resolved", "and_close": True},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["state"] == "closed"

        events = c.get(f"/api/tickets/{s['ticket_id']}/events", headers=h).json()["events"]
        state_events = [e for e in events if e["kind"] == "state"]
        assert [e["to_state"] for e in state_events[-2:]] == ["resolved", "closed"]


def test_operator_can_reopen_a_resolved_ticket(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(title="maybe fixed", origin="dashboard")
        await svc.transition(ticket.id, "in_progress", actor="system")
        await svc.transition(ticket.id, "resolved", actor="system")
        return {"op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/transition",
            json={"to": "in_progress", "reason": "still broken"},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 200
        assert r.json()["state"] == "in_progress"


# -- vocabulary, summary, block/unblock/assign ----------------------------------


def test_vocabulary_matches_the_live_tickets_module(tmp_path) -> None:
    """Seam test: the endpoint exists precisely so the dashboard never hardcodes
    a second copy of these constants that can silently drift from the service.
    """

    from kenny_server.tickets import BLOCKED_REASONS, KNOWN_CATEGORIES, PRIORITIES, STATES

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        return {"kid_pat": await users.create_pat(kid["id"], "t")}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        body = c.get("/api/tickets/vocabulary", headers=_hdr(app.state.seed["kid_pat"])).json()
        assert set(body["states"]) == STATES
        assert set(body["blocked_reasons"]) == BLOCKED_REASONS
        assert tuple(body["priorities"]) == PRIORITIES
        assert set(body["categories"]) == KNOWN_CATEGORIES


def test_summary_counts_are_narrowed_for_a_scoped_user(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        kid_pat = await users.create_pat(kid["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")
        await svc.create(title="kid's", origin="dashboard", requester_user_id=kid["id"])
        await svc.create(title="alert", origin="alert")
        return {"kid_pat": kid_pat, "op_pat": op_pat}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        kid_summary = c.get("/api/tickets/summary", headers=_hdr(s["kid_pat"])).json()
        assert kid_summary["new"] == 1  # kid's own ticket only
        op_summary = c.get("/api/tickets/summary", headers=_hdr(s["op_pat"])).json()
        assert op_summary["new"] == 1
        assert op_summary["needs_you"] == 1  # the alert-origin ticket


def test_block_is_reachable_by_nobody_over_http(tmp_path) -> None:
    """Not even an operator may declare a ticket to be waiting on something.

    A block says something is being waited *for*, and is written where that
    wait begins, carrying the ``ref`` that identifies it -- the assistant
    opening a gate, asking the requester a question, or the stall sweep
    escalating. Set by hand over HTTP it produced a wait with no referent: an
    ``approval`` block with no approval row, which the header chip, the inbox
    row and the timeline all described as a pending decision, whose banner
    rendered nothing, and which neither ``expire_due`` (no row to find) nor
    ``nudge_stalled`` (approval is excluded) could ever clear.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        kid_pat = await users.create_pat(kid["id"], "t")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(
            title="need info", origin="dashboard", requester_user_id=kid["id"]
        )
        await svc.transition(ticket.id, "in_progress", actor="system")
        return {"kid_pat": kid_pat, "op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        for pat in (s["kid_pat"], s["op_pat"]):
            for reason in ("user", "operator", "approval"):
                r = c.post(
                    f"/api/tickets/{s['ticket_id']}/block",
                    json={"blocked_on": reason},
                    headers=_hdr(pat),
                )
                assert r.status_code == 403, (pat, reason, r.status_code)
        # And the payload says so, so the console never offers what would fail.
        body = c.get(f"/api/tickets/{s['ticket_id']}", headers=_hdr(s["op_pat"])).json()
        assert body["allowed_blocks"] == []


def test_unblock_lets_the_requester_answer_their_own_user_block(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        sib_pat = await users.create_pat(sib["id"], "t")
        ticket = await svc.create(
            title="need info", origin="dashboard", requester_user_id=kid["id"]
        )
        await svc.transition(ticket.id, "in_progress", actor="system")
        await svc.block(ticket.id, "user", actor="system")
        return {"kid_pat": kid_pat, "sib_pat": sib_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        assert (
            c.post(
                f"/api/tickets/{s['ticket_id']}/unblock", json={}, headers=_hdr(s["sib_pat"])
            ).status_code
            == 403
        )
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/unblock", json={}, headers=_hdr(s["kid_pat"])
        )
        assert r.status_code == 200
        assert r.json()["blocked_on"] == ""


def test_create_rejects_unknown_priority(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        return {"kid_pat": await users.create_pat(kid["id"], "t")}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        h = _hdr(app.state.seed["kid_pat"])
        r = c.post(
            "/api/tickets",
            json={"title": "x", "priority": "urgent!!!1"},
            headers=h,
        )
        assert r.status_code == 400
        r = c.post("/api/tickets", json={"title": "x", "priority": "urgent"}, headers=h)
        assert r.status_code == 201


def test_create_rejects_a_host_outside_the_requesters_scope(tmp_path) -> None:
    """Bug: a ticket's `agent_id` is the frozen routing target every later
    tool call is checked against, but `api_tickets_create` never ran a scoped
    `user`'s chosen `agent_id` through `principal.may_see` -- so a host-scoped
    user could open a ticket aimed at a machine they cannot otherwise even
    see. Fails against the pre-fix code because the out-of-scope create
    below returns 201 instead of 403; the in-scope create must keep working.
    """

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        await users.set_user_hosts(kid["id"], ["pc-kid"])
        # A second, real host exists (and is owned by nobody in particular)
        # so the negative case is refusing an actual machine, not merely an
        # empty fixture accepting anything.
        return {"kid_pat": await users.create_pat(kid["id"], "t")}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        h = _hdr(app.state.seed["kid_pat"])

        # Out of scope: refused, in the same shape as every other host-scope
        # denial (`webui/authz.Forbidden` -> {"error": "forbidden", ...}).
        r = c.post(
            "/api/tickets",
            json={"title": "reach the sibling's PC", "agent_id": "pc-sib"},
            headers=h,
        )
        assert r.status_code == 403
        assert r.json()["error"] == "forbidden"
        assert c.get("/api/tickets", headers=h).json()["tickets"] == []

        # In scope: still succeeds.
        r = c.post(
            "/api/tickets",
            json={"title": "fix my own PC", "agent_id": "pc-kid"},
            headers=h,
        )
        assert r.status_code == 201
        assert r.json()["agent_id"] == "pc-kid"

        # No agent_id at all is not a host-scope question -- still allowed.
        r = c.post("/api/tickets", json={"title": "no target yet"}, headers=h)
        assert r.status_code == 201
        assert r.json()["agent_id"] is None


def test_claiming_and_retargeting_have_no_http_surface(tmp_path) -> None:
    """``/assign`` and ``/reassign`` are gone, for an operator too.

    404 rather than 403 is the assertion that matters: the routes are not
    registered at all, so there is no handler left whose authorization could
    later be loosened by accident.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(title="retarget me", origin="dashboard", agent_id="pc-a")
        return {"op_pat": op_pat, "op_id": op["id"], "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        h = _hdr(s["op_pat"])
        tid = s["ticket_id"]
        assert c.post(f"/api/tickets/{tid}/reassign", json={"agent_id": "pc-b"}, headers=h).status_code == 404
        assert c.post(f"/api/tickets/{tid}/assign", json={"assignee_user_id": s["op_id"]}, headers=h).status_code == 404
        # The host the ticket was opened against is untouched by either attempt.
        assert c.get(f"/api/tickets/{tid}", headers=h).json()["agent_id"] == "pc-a"


def test_ticket_payload_carries_only_legal_affordances(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        ticket = await svc.create(
            title="x", origin="dashboard", requester_user_id=kid["id"]
        )
        return {"kid_pat": kid_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        body = c.get(f"/api/tickets/{s['ticket_id']}", headers=_hdr(s["kid_pat"])).json()
        # A "new" ticket's requester may cancel it, never resolve it.
        assert set(body["allowed_transitions"]) == {"cancelled"}
        assert body["allowed_blocks"] == []
        assert body["can_unblock"] is False


# -- superuser-only surfaces: identities, members, claims, profiles ------------


def test_only_superuser_reaches_discord_and_profile_routes(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        su = await users.create_user("su", "pw-123456", "superuser")
        return {
            "kid_pat": await users.create_pat(kid["id"], "t"),
            "op_pat": await users.create_pat(op["id"], "t"),
            "su_pat": await users.create_pat(su["id"], "t"),
            "kid_id": kid["id"],
        }

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        kid_h, op_h, su_h = _hdr(s["kid_pat"]), _hdr(s["op_pat"]), _hdr(s["su_pat"])

        for path, method in (
            ("/api/discord/identities", "GET"),
            ("/api/discord/identities", "POST"),
            ("/api/discord/identities/123", "DELETE"),
            ("/api/discord/members", "GET"),
            ("/api/discord/claims", "GET"),
            ("/api/discord/claims/abc", "POST"),
            (f"/api/users/{s['kid_id']}/profile", "PUT"),
        ):
            assert c.request(method, path, headers=kid_h).status_code == 403
            assert c.request(method, path, headers=op_h).status_code == 403
            # Superuser clears the role gate. This app has no Discord wired, so
            # the Discord routes answer 503 rather than 403 — the RBAC gate is
            # what this test asserts, not the data path (see the tests below for
            # that). /api/users/{uid}/profile is backed by the UserStore and
            # succeeds outright.
            r = c.request(method, path, headers=su_h)
            assert r.status_code != 403

        # Discord status/tool-classes are operator+ (not superuser-only).
        assert c.get("/api/discord/status", headers=op_h).status_code == 200
        assert c.get("/api/discord/status", headers=kid_h).status_code == 403
        assert c.get("/api/tool-classes", headers=op_h).status_code == 200
        assert c.get("/api/tool-classes", headers=kid_h).status_code == 403


def test_discord_routes_are_503_without_a_discord_configuration(tmp_path) -> None:
    """A server without Discord serves the ticket API and refuses only Discord."""

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        su = await users.create_user("su", "pw-123456", "superuser")
        await svc.create(title="printer jam", origin="dashboard")
        return {"su_pat": await users.create_pat(su["id"], "t")}

    app = _build_app(tmp_path, seed, with_discord=False)
    with TestClient(app) as c:
        h = _hdr(app.state.seed["su_pat"])
        for path, method in (
            ("/api/discord/identities", "GET"),
            ("/api/discord/identities", "POST"),
            ("/api/discord/identities/123", "DELETE"),
            ("/api/discord/members", "GET"),
            ("/api/discord/claims", "GET"),
            ("/api/discord/claims/abc", "POST"),
        ):
            assert c.request(method, path, headers=h, json={}).status_code == 503

        # Status is the one Discord route that answers instead of erroring —
        # "is it configured?" is the question it exists to answer.
        assert c.get("/api/discord/status", headers=h).json() == {
            "connected": False,
            "configured": False,
        }
        # ... and the tickets themselves are entirely unaffected.
        assert len(c.get("/api/tickets", headers=h).json()["tickets"]) == 1


def test_status_reports_gateway_diagnostics_when_wired(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        return {"op_pat": await users.create_pat(op["id"], "t")}

    app = _build_app(tmp_path, seed, with_discord=True)
    with TestClient(app) as c:
        body = c.get("/api/discord/status", headers=_hdr(app.state.seed["op_pat"])).json()
        assert body["configured"] is True
        assert body["connected"] is False  # the fake gateway was never started
        assert body["guilds"] == [GUILD]


def test_member_picker_links_and_unlinks_an_identity(tmp_path) -> None:
    """Enrollment path B, end to end over the real store and gateway."""

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        su = await users.create_user("su", "pw-123456", "superuser")
        return {
            "su_pat": await users.create_pat(su["id"], "t"),
            "kid_id": kid["id"],
            "su_id": su["id"],
        }

    app = _build_app(tmp_path, seed, with_discord=True)
    with TestClient(app) as c:
        s = app.state.seed
        h = _hdr(s["su_pat"])

        members = c.get("/api/discord/members", headers=h).json()
        assert members["guild_id"] == GUILD
        assert members["members"] == [{"user_id": "900", "display_hint": "Kid"}]

        # The single configured guild is used when the caller names none.
        r = c.post(
            "/api/discord/identities",
            json={"discord_user_id": "900", "user_id": s["kid_id"]},
            headers=h,
        )
        assert r.status_code == 201
        assert r.json()["guild_id"] == GUILD
        assert r.json()["linked_via"] == "member_list"
        assert r.json()["linked_by"] == s["su_id"]

        listed = c.get("/api/discord/identities", headers=h).json()["identities"]
        assert [i["discord_user_id"] for i in listed] == ["900"]

        # One account, one snowflake per guild.
        conflict = c.post(
            "/api/discord/identities",
            json={"discord_user_id": "901", "user_id": s["kid_id"]},
            headers=h,
        )
        assert conflict.status_code == 409

        # A guild outside the allowlist is refused, not silently accepted.
        assert (
            c.post(
                "/api/discord/identities",
                json={"discord_user_id": "902", "user_id": s["kid_id"], "guild_id": "other"},
                headers=h,
            ).status_code
            == 403
        )
        # Unknown account / missing fields.
        assert (
            c.post(
                "/api/discord/identities",
                json={"discord_user_id": "903", "user_id": 999999},
                headers=h,
            ).status_code
            == 404
        )
        assert (
            c.post("/api/discord/identities", json={"user_id": 1}, headers=h).status_code == 400
        )

        assert c.delete("/api/discord/identities/900", headers=h).status_code == 200
        assert c.delete("/api/discord/identities/900", headers=h).status_code == 404


# -- self-service: see and remove your own Discord binding (ADR-0044) ----------


def test_me_discord_shows_only_the_callers_own_binding(tmp_path) -> None:
    """Two real users, two real bindings — GET /api/me/discord is per-caller."""

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        loner = await users.create_user("loner", "pw-123456", "user")
        su = await users.create_user("su", "pw-123456", "superuser")
        return {
            "kid_pat": await users.create_pat(kid["id"], "t"),
            "sib_pat": await users.create_pat(sib["id"], "t"),
            "loner_pat": await users.create_pat(loner["id"], "t"),
            "su_pat": await users.create_pat(su["id"], "t"),
            "kid_id": kid["id"],
            "sib_id": sib["id"],
        }

    app = _build_app(tmp_path, seed, with_discord=True)
    with TestClient(app) as c:
        s = app.state.seed
        su_h = _hdr(s["su_pat"])
        kid_h, sib_h, loner_h = _hdr(s["kid_pat"]), _hdr(s["sib_pat"]), _hdr(s["loner_pat"])

        # Two real bindings, seeded through the real operator route — not an
        # empty fixture that happens to make the negative case pass.
        assert (
            c.post(
                "/api/discord/identities",
                json={"discord_user_id": "900", "user_id": s["kid_id"]},
                headers=su_h,
            ).status_code
            == 201
        )
        assert (
            c.post(
                "/api/discord/identities",
                json={"discord_user_id": "901", "user_id": s["sib_id"]},
                headers=su_h,
            ).status_code
            == 201
        )

        kid_body = c.get("/api/me/discord", headers=kid_h).json()
        assert kid_body["linked"] is True
        assert len(kid_body["bindings"]) == 1
        binding = kid_body["bindings"][0]
        assert binding["discord_user_id"] == "900"
        assert binding["guild_id"] == GUILD
        assert binding["linked_via"] == "member_list"
        assert isinstance(binding["linked_at"], str) and binding["linked_at"]
        # Only what the store actually holds — no invented display name, and
        # no internal `linked_by` either.
        assert set(binding) == {"discord_user_id", "guild_id", "linked_at", "linked_via"}

        sib_body = c.get("/api/me/discord", headers=sib_h).json()
        assert [b["discord_user_id"] for b in sib_body["bindings"]] == ["901"]

        # A user with no binding at all gets a clean "not linked" answer, not
        # a 404 or an error.
        r = c.get("/api/me/discord", headers=loner_h)
        assert r.status_code == 200
        assert r.json()["linked"] is False
        assert r.json()["bindings"] == []

        # Unauthenticated is still 401, same as every other `user`-floor route.
        assert c.get("/api/me/discord").status_code == 401


def test_me_discord_not_linked_is_clean_without_discord_configured(tmp_path) -> None:
    """No Discord collaborator at all still answers cleanly, not 503/error."""

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        return {"kid_pat": await users.create_pat(kid["id"], "t")}

    app = _build_app(tmp_path, seed, with_discord=False)
    with TestClient(app) as c:
        h = _hdr(app.state.seed["kid_pat"])
        r = c.get("/api/me/discord", headers=h)
        assert r.status_code == 200
        assert r.json()["linked"] is False
        assert r.json()["bindings"] == []

        r = c.delete("/api/me/discord", headers=h)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "removed": 0}


def test_user_cannot_remove_another_users_binding_by_any_route(tmp_path) -> None:
    """The test that matters: kid can never take sib's binding down.

    kid tries the obvious ways to aim ``DELETE /api/me/discord`` at sib's
    binding — sib's snowflake, sib's guild, even sib's own user id — as query
    params and as a JSON body, on top of the operator-only path-parameter
    route. None of it works: the route resolves its target from
    ``principal.user_id`` alone, so kid only ever removes kid's own binding.
    """

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        su = await users.create_user("su", "pw-123456", "superuser")
        return {
            "kid_pat": await users.create_pat(kid["id"], "t"),
            "sib_pat": await users.create_pat(sib["id"], "t"),
            "su_pat": await users.create_pat(su["id"], "t"),
            "kid_id": kid["id"],
            "sib_id": sib["id"],
        }

    app = _build_app(tmp_path, seed, with_discord=True)
    with TestClient(app) as c:
        s = app.state.seed
        su_h = _hdr(s["su_pat"])
        kid_h, sib_h = _hdr(s["kid_pat"]), _hdr(s["sib_pat"])

        assert (
            c.post(
                "/api/discord/identities",
                json={"discord_user_id": "900", "user_id": s["kid_id"]},
                headers=su_h,
            ).status_code
            == 201
        )
        assert (
            c.post(
                "/api/discord/identities",
                json={"discord_user_id": "901", "user_id": s["sib_id"]},
                headers=su_h,
            ).status_code
            == 201
        )

        # kid supplies sib's snowflake, sib's guild and sib's own user id —
        # as both query params and a JSON body — trying to reach sib's row
        # through the self-service route.
        r = c.request(
            "DELETE",
            "/api/me/discord",
            params={"discord_user_id": "901", "guild_id": GUILD, "user_id": str(s["sib_id"])},
            json={"discord_user_id": "901", "guild_id": GUILD, "user_id": s["sib_id"]},
            headers=kid_h,
        )
        assert r.status_code == 200
        # Exactly one row removed — kid's own, never sib's.
        assert r.json() == {"ok": True, "removed": 1}

        # kid is unlinked now...
        kid_after = c.get("/api/me/discord", headers=kid_h).json()
        assert kid_after == {
            "linked": False,
            "bindings": [],
            "note": kid_after["note"],
        }
        # ...but sib's binding is completely untouched, confirmed both from
        # sib's own view and from the operator's admin listing.
        sib_after = c.get("/api/me/discord", headers=sib_h).json()
        assert sib_after["linked"] is True
        assert [b["discord_user_id"] for b in sib_after["bindings"]] == ["901"]
        listed = c.get("/api/discord/identities", headers=su_h).json()["identities"]
        assert [i["discord_user_id"] for i in listed] == ["901"]

        # A second self-service delete is a clean, idempotent no-op.
        r = c.delete("/api/me/discord", headers=kid_h)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "removed": 0}


def test_operator_identity_delete_route_is_unchanged(tmp_path) -> None:
    """Adding self-service unbind must not touch the operator confirmation path."""

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        su = await users.create_user("su", "pw-123456", "superuser")
        return {
            "su_pat": await users.create_pat(su["id"], "t"),
            "kid_pat": await users.create_pat(kid["id"], "t"),
            "kid_id": kid["id"],
        }

    app = _build_app(tmp_path, seed, with_discord=True)
    with TestClient(app) as c:
        s = app.state.seed
        su_h, kid_h = _hdr(s["su_pat"]), _hdr(s["kid_pat"])

        assert (
            c.post(
                "/api/discord/identities",
                json={"discord_user_id": "900", "user_id": s["kid_id"]},
                headers=su_h,
            ).status_code
            == 201
        )
        # A plain `user` still can't reach the operator route at all.
        assert c.delete("/api/discord/identities/900", headers=kid_h).status_code == 403

        assert c.delete("/api/discord/identities/900", headers=su_h).status_code == 200
        assert c.delete("/api/discord/identities/900", headers=su_h).status_code == 404
        # And the self-service view now agrees it's gone.
        assert c.get("/api/me/discord", headers=kid_h).json()["linked"] is False


def test_pending_claim_is_listed_and_confirmed_once(tmp_path) -> None:
    """Enrollment path A: `/link` opens a claim, an operator confirms it."""

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        su = await users.create_user("su", "pw-123456", "superuser")
        return {
            "su_pat": await users.create_pat(su["id"], "t"),
            "kid_id": kid["id"],
            "su_id": su["id"],
        }

    app = _build_app(tmp_path, seed, with_discord=True)
    with TestClient(app) as c:
        s = app.state.seed
        h = _hdr(s["su_pat"])
        claim = c.portal.call(
            partial(
                app.state.identities.open_claim,
                discord_user_id="900",
                display_hint="Kid",
                guild_id=GUILD,
            )
        )

        pending = c.get("/api/discord/claims", headers=h).json()["claims"]
        assert [p["code"] for p in pending] == [claim.code]
        assert pending[0]["display_hint"] == "Kid"

        assert c.post(f"/api/discord/claims/{claim.code}", json={}, headers=h).status_code == 400

        r = c.post(
            f"/api/discord/claims/{claim.code}", json={"user_id": s["kid_id"]}, headers=h
        )
        assert r.status_code == 200
        assert r.json() == {
            "discord_user_id": "900",
            "user_id": s["kid_id"],
            "guild_id": GUILD,
            "linked_at": r.json()["linked_at"],
            "linked_by": s["su_id"],
            "linked_via": "claim",
            "disabled": False,
        }

        # Single-use, and gone from the pending list.
        assert c.get("/api/discord/claims", headers=h).json()["claims"] == []
        assert (
            c.post(
                f"/api/discord/claims/{claim.code}", json={"user_id": s["kid_id"]}, headers=h
            ).status_code
            == 404
        )
        assert (
            c.post("/api/discord/claims/nope", json={"user_id": s["kid_id"]}, headers=h
                   ).status_code == 404
        )


def test_superuser_can_set_capability_profile(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        su = await users.create_user("su", "pw-123456", "superuser")
        return {
            "su_pat": await users.create_pat(su["id"], "t"),
            "kid_id": kid["id"],
        }

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        h = _hdr(s["su_pat"])
        r = c.put(
            f"/api/users/{s['kid_id']}/profile",
            json={"capability_profile": "self-service-basic"},
            headers=h,
        )
        assert r.status_code == 200
        assert r.json()["capability_profile"] == "self-service-basic"
        assert c.put("/api/users/999999/profile", json={}, headers=h).status_code == 404


# -- POST /api/tickets/{tid}/chat/stream ----------------------------------------


def test_chat_stream_needs_assistant_agent_and_no_open_gate(tmp_path) -> None:
    async def seed(users: UserStore, store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        sib = await users.create_user("sib", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        sib_pat = await users.create_pat(sib["id"], "t")
        owned = await svc.create(
            title="slow pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        no_agent = await svc.create(
            title="no target", origin="dashboard", requester_user_id=kid["id"]
        )
        alert = await svc.create(title="disk full", origin="alert", requester_user_id=None)
        gated = await svc.create(
            title="gate open", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        await svc.open_approval(
            gated.id, tool_use_id="tu-1", tool="winget_install", tool_class="normal_change",
            args={},
        )
        closed = await svc.create(
            title="already closed", origin="dashboard", requester_user_id=kid["id"],
            agent_id="pc-1",
        )
        await store.set_state(closed.id, "closed", actor="system")
        return {
            "kid_pat": kid_pat,
            "sib_pat": sib_pat,
            "owned_id": owned.id,
            "no_agent_id": no_agent.id,
            "alert_id": alert.id,
            "gated_id": gated.id,
            "closed_id": closed.id,
        }

    # No assistant configured at all -> 503, before any other check.
    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['owned_id']}/chat/stream",
            json={"message": "hi"},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 503

    app = _build_app(tmp_path / "2", seed, with_assistant=True)
    with TestClient(app) as c:
        s = app.state.seed
        kid_h = _hdr(s["kid_pat"])

        # Someone else's ticket: 403, before anything assistant-related runs.
        assert (
            c.post(
                f"/api/tickets/{s['owned_id']}/chat/stream", json={"message": "hi"},
                headers=_hdr(s["sib_pat"]),
            ).status_code == 403
        )
        # An alert-origin ticket has no owner: 403 for a scoped user.
        assert (
            c.post(
                f"/api/tickets/{s['alert_id']}/chat/stream", json={"message": "hi"}, headers=kid_h
            ).status_code == 403
        )
        # Empty message: 400, pre-stream.
        assert (
            c.post(
                f"/api/tickets/{s['owned_id']}/chat/stream", json={"message": "  "}, headers=kid_h
            ).status_code == 400
        )
        # No target machine: 400.
        assert (
            c.post(
                f"/api/tickets/{s['no_agent_id']}/chat/stream", json={"message": "hi"},
                headers=kid_h,
            ).status_code == 400
        )
        # A gate is already open on this ticket: 409.
        assert (
            c.post(
                f"/api/tickets/{s['gated_id']}/chat/stream", json={"message": "hi"},
                headers=kid_h,
            ).status_code == 409
        )
        # Closed ticket: 409.
        assert (
            c.post(
                f"/api/tickets/{s['closed_id']}/chat/stream", json={"message": "hi"},
                headers=kid_h,
            ).status_code == 409
        )
        # Asking to mirror without a Discord thread bound (and no Discord at
        # all here): 400.
        assert (
            c.post(
                f"/api/tickets/{s['owned_id']}/chat/stream",
                json={"message": "hi", "mirror_to_discord": True},
                headers=kid_h,
            ).status_code == 400
        )


def test_chat_stream_drives_a_turn_and_records_verbatim_text(tmp_path) -> None:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        ticket = await svc.create(
            title="slow pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        return {"kid_pat": kid_pat, "ticket_id": ticket.id}

    app = _build_app(
        tmp_path, seed, with_assistant=True, scripted=[text_turn("I'll take a look.")]
    )
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/chat/stream",
            json={"message": "my pc is being slow today"},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(r.text)
        assert events, "expected at least one SSE event"
        assert events[-1]["type"] == "done"
        assert events[-1]["assistant_text"] == "I'll take a look."

        trail = c.portal.call(app.state.tickets.events, s["ticket_id"])
        messages = [e for e in trail if e.kind == "message"]
        assert len(messages) == 2
        human, kenny = messages
        assert human.fields["text"] == "my pc is being slow today"
        assert human.fields["surface"] == "dashboard"
        assert human.fields["actionable"] is True
        assert kenny.actor == "assistant"
        assert kenny.fields["text"] == "I'll take a look."
        assert kenny.fields["surface"] == "dashboard"


def test_chat_stream_envelopes_the_typed_message(tmp_path) -> None:
    """The dashboard path must not be the one surface that skips the envelope.

    ``_SYSTEM_PROMPT`` promises the model that every message arrives wrapped
    in a ``<message>`` envelope carrying the author's identity, role and
    ``actionable`` flag. This is the direct regression proof that a message
    typed into the ticket composer gets one too, exactly like a Discord
    message always has.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        ticket = await svc.create(
            title="slow pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        return {"kid_id": kid["id"], "kid_pat": kid_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed, with_assistant=True, scripted=[text_turn("ok")])
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/chat/stream",
            json={"message": "my pc is being slow today"},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 200

        calls = app.state.assistant.client.messages.calls
        sent = calls[0]["messages"][0]["content"]
        assert sent == (
            f'<message discord_id="user:{s["kid_id"]}" kenny_user="kid" role="user" '
            'actionable="true">my pc is being slow today</message>'
        )


def test_chat_stream_envelopes_an_operator_on_someone_elses_ticket(tmp_path) -> None:
    """ADR-0050: an operator's message is actionable too, and must say so.

    Distinct from the requester case above: the envelope's ``role`` must read
    ``operator``, not ``user``, and ``actionable`` must still be ``true`` — an
    operator working someone else's ticket is not a bystander.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("root", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(
            title="slow pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        return {"op_id": op["id"], "op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed, with_assistant=True, scripted=[text_turn("ok")])
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/chat/stream",
            json={"message": "checking in on this"},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 200

        calls = app.state.assistant.client.messages.calls
        sent = calls[0]["messages"][0]["content"]
        assert f'discord_id="operator:{s["op_id"]}"' in sent
        assert 'role="operator"' in sent
        assert 'actionable="true"' in sent


def test_chat_stream_gives_the_model_the_ticket_record(tmp_path) -> None:
    """End-to-end proof of the reported defect: kenny gets the ticket, not just the text.

    An alert-origin ticket has no requester and an empty transcript — before
    the briefing, the first turn's ``messages`` was just the typed word "hi"
    and kenny had nothing to work from but introduce itself.
    """

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("root", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(
            title="disk usage critical",
            origin="alert",
            agent_id="pc-1",
            priority="high",
            category="alert",
        )
        return {"op_pat": op_pat, "ticket_id": ticket.id}

    app = _build_app(tmp_path, seed, with_assistant=True, scripted=[text_turn("ok")])
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/chat/stream",
            json={"message": "hi"},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 200

        calls = app.state.assistant.client.messages.calls
        briefing = calls[0]["system"][-1]["text"]
        assert "Title: disk usage critical" in briefing
        assert "state: new" in briefing
        assert "Priority: high" in briefing


def test_chat_stream_mirrors_to_discord_only_when_asked(tmp_path) -> None:
    async def seed(users: UserStore, store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        ticket = await svc.create(
            title="slow pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        await store.bind_channel(
            ticket_id=ticket.id,
            guild_id=GUILD,
            channel_id="chan-1",
            thread_id="thread-1",
            private=True,
        )
        return {"kid_pat": kid_pat, "ticket_id": ticket.id}

    # mirror_to_discord=False (the default): nothing reaches the gateway.
    app = _build_app(
        tmp_path, seed, with_discord=True, scripted=[text_turn("no mirror here")]
    )
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/chat/stream",
            json={"message": "just dashboard, please"},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 200
        assert app.state.gateway.posted == []

    # mirror_to_discord=True: exactly one post lands in the bound thread.
    app = _build_app(
        tmp_path / "2", seed, with_discord=True, scripted=[text_turn("mirrored reply")]
    )
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/chat/stream",
            json={"message": "also tell discord", "mirror_to_discord": True},
            headers=_hdr(s["kid_pat"]),
        )
        assert r.status_code == 200
        assert len(app.state.gateway.posted) == 1
        channel_id, content = app.state.gateway.posted[0]
        assert channel_id == "thread-1"
        assert content == "mirrored reply"


def test_approval_decide_resolves_the_discord_card(tmp_path) -> None:
    async def seed(users: UserStore, store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(title="risky change", origin="discord", agent_id="pc-1")
        approval = await svc.open_approval(
            ticket.id, tool_use_id="tu-1", tool="winget_install", tool_class="normal_change",
            args={},
        )
        await store.set_approval_message(
            approval.id, channel_id="chan-1", message_id="card-1"
        )
        return {"op_pat": op_pat, "approval_id": approval.id}

    app = _build_app(tmp_path, seed, with_discord=True)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/approvals/{s['approval_id']}", json={"approve": True},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 200
        assert len(app.state.gateway.resolved) == 1
        resolved = app.state.gateway.resolved[0]
        assert resolved["channel_id"] == "chan-1"
        assert resolved["message_id"] == "card-1"
        assert resolved["outcome"] == "approved"


# =============================================================================
# §7: the stalled turn -- _ensure_in_progress, honest resume_status, the gate
# =============================================================================


def test_chat_stream_on_a_new_ticket_flips_it_to_in_progress(tmp_path) -> None:
    """F2, over the real HTTP route: a dashboard turn on a fresh ``new``
    ticket must leave it ``in_progress`` by the time the stream ends -- the
    half of the bug that isn't about the approval gate at all."""

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        ticket = await svc.create(
            title="slow pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        return {"kid_pat": kid_pat, "ticket_id": ticket.id}

    app = _build_app(
        tmp_path, seed, with_assistant=True, scripted=[text_turn("Looking into it.")]
    )
    with TestClient(app) as c:
        s = app.state.seed
        kid_h = _hdr(s["kid_pat"])
        before = c.get(f"/api/tickets/{s['ticket_id']}", headers=kid_h).json()
        assert before["state"] == "new"

        r = c.post(
            f"/api/tickets/{s['ticket_id']}/chat/stream",
            json={"message": "please help"},
            headers=kid_h,
        )
        assert r.status_code == 200
        events = _sse_events(r.text)
        assert events[-1]["type"] == "done"

        after = c.get(f"/api/tickets/{s['ticket_id']}", headers=kid_h).json()
        assert after["state"] == "in_progress"


def test_approval_decide_reports_an_honest_resume_status_when_it_degrades(tmp_path) -> None:
    """F4/F5: the response body must never hardcode ``resumed: true`` --
    an alert-origin ticket (no requester) whose resume cannot complete (no
    executor wired here) must report ``resumed: false`` and a real
    ``resume_status``, not the historical unconditional success."""

    async def seed(users: UserStore, store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        op_pat = await users.create_pat(op["id"], "t")
        ticket = await svc.create(title="disk alert", origin="alert", agent_id="pc-1")
        assert ticket.requester_user_id is None
        approval = await svc.open_approval(
            ticket.id, tool_use_id="tu-1", tool="winget_install", tool_class="normal_change",
            args={"id": "Git.Git"},
        )
        return {"op_pat": op_pat, "approval_id": approval.id, "ticket_id": ticket.id}

    # with_assistant=True and no scripted client builds a real TicketAssistant
    # with executor=None (see _build_app's docstring) -- exactly a resume that
    # cannot complete, the same shape the reported bug hit.
    app = _build_app(tmp_path, seed, with_assistant=True)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/approvals/{s['approval_id']}", json={"approve": True},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["resumed"] is False
        assert isinstance(body["resume_status"], str) and body["resume_status"] != "resumed"


def test_ticket_with_an_open_gate_reports_blocked_on_approval_and_an_approval_event(
    tmp_path,
) -> None:
    """The exact condition the frontend's ``ticketOpenGate()`` derives from:
    ``blocked_on == "approval"`` on the ticket, and an ``approval``-kind
    event with ``fields.approval_id`` set and ``ok`` still null."""

    async def seed(users: UserStore, store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        kid_pat = await users.create_pat(kid["id"], "t")
        ticket = await svc.create(
            title="risky change", origin="dashboard", requester_user_id=kid["id"],
            agent_id="pc-1",
        )
        await store.set_state(ticket.id, "in_progress", actor="system")
        approval = await svc.open_approval(
            ticket.id, tool_use_id="tu-1", tool="winget_install", tool_class="normal_change",
            args={"id": "Git.Git"},
        )
        await svc.block(ticket.id, "approval", actor="system", ref=approval.id)
        return {"kid_pat": kid_pat, "ticket_id": ticket.id, "approval_id": approval.id}

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        kid_h = _hdr(s["kid_pat"])

        detail = c.get(f"/api/tickets/{s['ticket_id']}", headers=kid_h).json()
        assert detail["blocked_on"] == "approval"

        events = c.get(f"/api/tickets/{s['ticket_id']}/events", headers=kid_h).json()["events"]
        approval_rows = [e for e in events if e["kind"] == "approval"]
        assert approval_rows, "no approval-kind event on the trail"
        row = approval_rows[-1]
        assert row["fields"]["approval_id"] == s["approval_id"]
        assert row["ok"] is None


def test_the_alerts_route_carries_the_same_ownership_rule_as_events(tmp_path) -> None:
    """An alert-origin ticket has no requester, so it is operator-only — and
    its alert history, which names hosts, must not be the one route that leaks
    past that."""

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        alert = await svc.create(title="disk full", origin="alert", requester_user_id=None)
        return {
            "kid_pat": await users.create_pat(kid["id"], "t"),
            "op_pat": await users.create_pat(op["id"], "t"),
            "alert_id": alert.id,
        }

    app = _build_app(tmp_path, seed)
    with TestClient(app) as c:
        s = app.state.seed
        for route in ("events", "alerts"):
            assert (
                c.get(
                    f"/api/tickets/{s['alert_id']}/{route}", headers=_hdr(s["kid_pat"])
                ).status_code
                == 403
            ), route


def test_the_alerts_route_says_so_when_no_telemetry_surface_is_wired(tmp_path) -> None:
    """``alert_reader`` is optional exactly like ``discord`` and ``assistant``:
    the one route that needs it answers 503, every other ticket route works."""

    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        alert = await svc.create(title="disk full", origin="alert", requester_user_id=None)
        return {"op_pat": await users.create_pat(op["id"], "t"), "alert_id": alert.id}

    app = _build_app(tmp_path, seed)  # built without an alert_reader
    with TestClient(app) as c:
        s = app.state.seed
        r = c.get(f"/api/tickets/{s['alert_id']}/alerts", headers=_hdr(s["op_pat"]))
        assert r.status_code == 503
        assert r.json()["error"] == "unavailable"
        # ... and the rest of the ticket API is unaffected.
        assert (
            c.get(f"/api/tickets/{s['alert_id']}", headers=_hdr(s["op_pat"])).status_code == 200
        )


# -- POST /api/tickets/{tid}/approvals/{aid}/decide/stream -----------------------
#
# The decision, answered where it was asked. Same durable gate and same
# authorization as ``POST /api/approvals/{aid}``; what it adds is that the
# operator who clicked watches the turn the decision releases instead of
# reloading the page to find out what happened.


def _gated_ticket_seed(*, tool: str = "shell_exec", tool_class: str = "normal_change") -> Seed:
    async def seed(users: UserStore, _store: TicketStore, svc: TicketService) -> dict:
        kid = await users.create_user("kid", "pw-123456", "user")
        op = await users.create_user("op", "pw-123456", "operator")
        ticket = await svc.create(
            title="slow pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-1"
        )
        approval = await svc.open_approval(
            ticket.id,
            tool_use_id="tu-1",
            tool=tool,
            tool_class=tool_class,
            args={"cmd": "echo hi"},
        )
        other = await svc.create(
            title="other pc", origin="dashboard", requester_user_id=kid["id"], agent_id="pc-2"
        )
        other_approval = await svc.open_approval(
            other.id,
            tool_use_id="tu-2",
            tool="winget_install",
            tool_class="normal_change",
            args={},
        )
        return {
            "kid_pat": await users.create_pat(kid["id"], "t"),
            "op_pat": await users.create_pat(op["id"], "t"),
            "ticket_id": ticket.id,
            "approval_id": approval.id,
            "other_ticket_id": other.id,
            "other_approval_id": other_approval.id,
        }

    return seed


def test_decide_stream_runs_the_decision_and_streams_what_follows(tmp_path) -> None:
    app = _build_app(
        tmp_path,
        _gated_ticket_seed(),
        with_assistant=True,
        with_executor=True,
        scripted=[text_turn("Done — the command ran.")],
    )
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/approvals/{s['approval_id']}/decide/stream",
            json={"approve": True},
            headers=_hdr(s["op_pat"]),
        )

        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(r.text)
        # Exactly one terminal event, and it says how the resume went — the
        # caller never has to tell "finished" from "never started".
        assert [e["type"] for e in events].count("done") == 1
        assert events[-1]["type"] == "done"
        assert events[-1]["resume_status"] == "resumed"

        # What the operator was actually deciding about ran, with the arguments
        # frozen when the gate opened — and its result is in the same stream.
        assert app.state.executor.ran == [("shell_exec", {"cmd": "echo hi"}, "pc-1")]
        assert any(e["type"] == "tool_result" and e["tool"] == "shell_exec" for e in events)
        assert events[-1]["assistant_text"] == "Done — the command ran."

        # The decision is durable, whatever the stream did.
        approval = c.portal.call(app.state.ticket_store.get_approval, s["approval_id"])
        assert approval.status == "approved"


def test_decide_stream_will_not_decide_another_ticket_s_gate(tmp_path) -> None:
    """The route is authorized through the ticket in its path, so an approval
    belonging to a different ticket must not be reachable through it — even for
    an operator, who could decide that same gate on its own ticket."""

    app = _build_app(tmp_path, _gated_ticket_seed(), with_assistant=True)
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/approvals/{s['other_approval_id']}/decide/stream",
            json={"approve": True},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 404

        untouched = c.portal.call(app.state.ticket_store.get_approval, s["other_approval_id"])
        assert untouched.status == "pending"


def test_decide_stream_keeps_every_control_the_json_route_has(tmp_path) -> None:
    app = _build_app(tmp_path, _gated_ticket_seed(), with_assistant=True)
    with TestClient(app) as c:
        s = app.state.seed
        url = f"/api/tickets/{s['ticket_id']}/approvals/{s['approval_id']}/decide/stream"

        # A scoped user may not decide an operator_approval on their own ticket:
        # owning the ticket is not deciding rights, and the gate is still open
        # afterwards.
        assert c.post(url, json={"approve": True}, headers=_hdr(s["kid_pat"])).status_code == 403
        assert c.post(url, json={"approve": False}, headers=_hdr(s["kid_pat"])).status_code == 403
        assert c.portal.call(app.state.ticket_store.get_approval, s["approval_id"]).status == "pending"

        # A missing/garbled decision is a pre-stream 400, never a stream that
        # says "error" with a 200 on it.
        assert c.post(url, json={}, headers=_hdr(s["op_pat"])).status_code == 400
        assert c.post(url, json={"approve": "yes"}, headers=_hdr(s["op_pat"])).status_code == 400


def test_decide_stream_is_unavailable_without_an_assistant(tmp_path) -> None:
    """503 before the gate is touched: without an assistant there is no turn to
    stream, and a decision that silently did not resume is the bug this whole
    route exists to remove."""

    app = _build_app(tmp_path, _gated_ticket_seed())
    with TestClient(app) as c:
        s = app.state.seed
        r = c.post(
            f"/api/tickets/{s['ticket_id']}/approvals/{s['approval_id']}/decide/stream",
            json={"approve": True},
            headers=_hdr(s["op_pat"]),
        )
        assert r.status_code == 503
        assert c.portal.call(app.state.ticket_store.get_approval, s["approval_id"]).status == "pending"


# -- a ticket drafted out of an Ask kenny conversation -----------------------


def test_a_drafted_ticket_records_what_the_conversation_already_checked(tmp_path) -> None:
    """The evidence row is the server's sentence, not the caller's.

    ``chat_session_id`` names a conversation; what the note says is derived from
    that conversation on the server, so the trail keeps reporting what happened.
    The genesis row names the origin the same way an alert's does.
    """

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        return {"op_pat": await users.create_pat(op["id"], "t")}

    seen: list[str] = []

    async def reader(session_id: str) -> list[dict[str, str]]:
        seen.append(session_id)
        if session_id != "sess-1":
            return []
        return [
            {"tool": "diag_services", "agent_id": "pc-kid"},
            {"tool": "fs_disk_usage", "agent_id": ""},
        ]

    app = _build_app(tmp_path, seed, evidence_reader=reader)
    with TestClient(app) as c:
        h = _hdr(app.state.seed["op_pat"])
        r = c.post(
            "/api/tickets",
            json={
                "title": "Updates fail",
                "summary": "0x80070422 since Tuesday.",
                "origin": "copilot",
                "chat_session_id": "sess-1",
            },
            headers=h,
        )
        assert r.status_code == 201
        tid = r.json()["id"]

        events = c.get(f"/api/tickets/{tid}/events", headers=h).json()["events"]
        genesis = next(e for e in events if e["kind"] == "state")
        assert genesis["summary"] == "opened from an Ask kenny conversation"

        notes = [e for e in events if e["kind"] == "note"]
        assert len(notes) == 1
        assert notes[0]["summary"] == (
            "already checked in the conversation this came from: "
            "`diag_services` on pc-kid, `fs_disk_usage`"
        )
        assert seen == ["sess-1"]


def test_a_ticket_with_no_conversation_gets_no_evidence_row(tmp_path) -> None:
    """A dashboard ticket, an unknown session and a reader that blows up all
    land in the same place: the ticket exists and carries no note it cannot
    stand behind."""

    async def seed(users: UserStore, _store: TicketStore, _svc: TicketService) -> dict:
        op = await users.create_user("op", "pw-123456", "operator")
        return {"op_pat": await users.create_pat(op["id"], "t")}

    async def reader(session_id: str) -> list[dict[str, str]]:
        if session_id == "boom":
            raise RuntimeError("session registry is having a day")
        return []

    app = _build_app(tmp_path, seed, evidence_reader=reader)
    with TestClient(app) as c:
        h = _hdr(app.state.seed["op_pat"])
        for body in (
            {"title": "typed by hand"},
            {"title": "stale drawer", "chat_session_id": "gone"},
            {"title": "reader fails", "chat_session_id": "boom"},
        ):
            r = c.post("/api/tickets", json=body, headers=h)
            assert r.status_code == 201
            events = c.get(f"/api/tickets/{r.json()['id']}/events", headers=h).json()["events"]
            assert [e for e in events if e["kind"] == "note"] == []

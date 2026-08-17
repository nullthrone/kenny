"""Do held calls really survive a restart, and does deciding them execute?

The claim under test: approvals are *persistent and asynchronous* — they reach
the operator in the operator channel **or in the dashboard**, they survive a
server restart, and expiry counts as a denial. That is what makes it acceptable
for a `user` role to queue a `normal_change` at all: the call is frozen, it is
decided later by someone else, and the exact frozen call is what runs.

A "restart" here is literal: every store is closed and a fresh set is opened over
the same SQLite file, so nothing can be smuggled across in memory. The dashboard
side is exercised through the real HTTP routes behind the real
``OperatorAuthMiddleware`` with a real PAT — approving is an authorization
decision, and a test that bypassed the route would not be testing it.

The bench, the scripted model and the Discord event constructors come from
``tests/test_discord_security.py`` so both files describe the same world.

Two tests here are **known failures** and say so in their docstrings.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware

from kenny_server.auth import OperatorAuthMiddleware
from kenny_server.webui.tickets import build_ticket_routes

from test_discord_security import (
    GUILD,
    MIA_PC,
    ROOT,
    Bench,
    button,
    calls,
    in_thread,
    mention,
    says,
    use,
)


@pytest.fixture
async def benches(tmp_path):
    """Factory for benches over distinct DB files, all closed at teardown.

    A "restart" opens a *second* ``Bench`` on an already-used path by hand (see
    the tests), so this only owns the ones it made.
    """

    made: list[Bench] = []

    async def make(name: str = "kenny", **kw: Any) -> Bench:
        bench = Bench(str(tmp_path / f"{name}.sqlite"))
        await bench.open(**kw)
        made.append(bench)
        return bench

    yield make
    for bench in reversed(made):
        await bench.close()


# -- the dashboard, over the real routes and the real auth middleware ---------


class Dashboard:
    """An HTTP client for one boot's ticket/approval API."""

    def __init__(self, bench: Bench, service: Any) -> None:
        routes = build_ticket_routes(
            tickets=bench.tickets,
            store=bench.ticket_store,
            identities=bench.identities,
            user_store=bench.users,
            discord=service,
            assistant=getattr(service, "assistant", None),
        )
        self.app = Starlette(
            routes=routes,
            middleware=[
                Middleware(
                    OperatorAuthMiddleware,
                    token="unused-shared-token",
                    user_store=bench.users,
                )
            ],
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://dash"
        )

    async def get(self, path: str, token: str) -> httpx.Response:
        return await self.client.get(path, headers={"Authorization": f"Bearer {token}"})

    async def post(self, path: str, token: str, body: dict) -> httpx.Response:
        return await self.client.post(
            path, json=body, headers={"Authorization": f"Bearer {token}"}
        )

    async def aclose(self) -> None:
        await self.client.aclose()


def transcript_pairs(messages: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """``(tool_use ids, answered tool_use ids)`` in a stored transcript.

    An unanswered ``tool_use`` is the failure mode a dropped queue entry leaves
    behind: the next turn would send the Messages API a transcript it rejects.
    """

    issued: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                issued.add(block["id"])
            elif block.get("type") == "tool_result":
                answered.add(block["tool_use_id"])
    return issued, answered


def run_pairs(run: Any) -> tuple[set[str], set[str]]:
    """:func:`transcript_pairs` plus ``run.staged_results`` on the answered side.

    §7's degraded resume (:meth:`TicketAssistant._resume_degraded`) stages the
    held call's answer directly into ``ticket_runs.staged_results`` without
    driving a turn, so it is not yet folded into ``messages`` — the fold is
    ``drive_events``' job, on the ticket's *next* turn. An id answered there is
    just as un-orphaned as one already folded: the next Messages API call is
    still valid either way, since ``drive_events`` folds staged results before
    ever calling the model again.
    """

    issued, answered = transcript_pairs(run.messages)
    for block in run.staged_results:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if tool_use_id:
                answered.add(tool_use_id)
    return issued, answered


async def drive_to_an_open_approval(bench: Bench, *blocks) -> tuple[str, str]:
    """Open a ticket whose turn ends in a held ``normal_change``.

    Returns ``(ticket_id, thread_id)``. Uses no capability profile so the role
    alone decides — the point of these tests is the gate, not the profile.
    """

    await bench.users.set_capability_profile(bench.mia["id"], None)
    service = bench.service(calls(*blocks))
    await service.handle_event(mention("please install the things"))
    ticket = await bench.the_ticket()
    assert ticket.state == "in_progress"
    assert ticket.blocked_on == "approval"
    return ticket.id, bench.gateway.threads[0].thread_id


async def drive_alert_origin_ticket_to_an_open_approval(bench: Bench, *blocks) -> str:
    """Same shape as :func:`drive_to_an_open_approval`, for a ticket with no
    requester at all (``requester_user_id IS NULL``) — the alert-origin case
    §7 of the plan is about. No Discord thread/message is involved in opening
    it (an alert has no author); an operator drives the one turn that gates it,
    the same way the dashboard would for a ticket nobody "owns".
    """

    ticket = await bench.tickets.create(
        title="disk usage critical", origin="alert", agent_id=MIA_PC,
        actor="system", reason="opened from an alert",
    )
    assert ticket.requester_user_id is None
    service = bench.service(calls(*blocks))
    operator = await service._principal_for(ROOT, GUILD)
    assert operator is not None
    session = await bench.assistant.session_for(ticket, actor=operator)
    assert session is not None
    session.messages.append({"role": "user", "content": "please fix the disk"})
    async for _event in bench.assistant.run_turn(session, ticket):
        pass
    refreshed = await bench.ticket_store.get(ticket.id)
    assert refreshed is not None
    assert refreshed.state == "in_progress"
    assert refreshed.blocked_on == "approval"
    return ticket.id


# =============================================================================
# The frozen call across a restart
# =============================================================================


async def test_an_open_approval_survives_a_restart_with_its_frozen_call(
    benches,
) -> None:
    boot1 = await benches("fleet")
    ticket_id, _thread = await drive_to_an_open_approval(
        boot1, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await boot1.ticket_store.get_open_approval(ticket_id)
    assert approval is not None
    approval_id = approval.id
    operator_pat = await boot1.users.create_pat(boot1.root["id"], "dash")
    await boot1.close()

    # -- restart: a brand-new process over the same file ------------------
    boot2 = Bench(boot1.db_path)
    await boot2.open(seed=False)
    try:
        dash = Dashboard(boot2, boot2.service())
        listing = await dash.get("/api/approvals", operator_pat)
        assert listing.status_code == 200
        rows = listing.json()["approvals"]
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == approval_id
        assert row["ticket_id"] == ticket_id
        assert row["tool"] == "winget_install"
        assert row["tool_class"] == "normal_change"
        assert row["kind"] == "operator_approval"
        assert row["args"] == {"id": "Git.Git"}
        assert row["agent_id"] == MIA_PC
        assert row["status"] == "pending"
        await dash.aclose()
    finally:
        await boot2.close()


async def test_a_user_cannot_decide_an_operator_approval_but_sees_it_on_their_own_ticket(
    benches,
) -> None:
    """A requester may always *see* the gate on their own ticket (whatever its
    ``kind``) but may only ever *decide* the ``user_consent`` half of it --
    this one is ``operator_approval`` (a ``normal_change`` tool with no
    consent involved), so deciding stays refused even though listing now
    succeeds.
    """

    bench = await benches("rbac")
    ticket_id, _thread = await drive_to_an_open_approval(
        bench, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await bench.ticket_store.get_open_approval(ticket_id)
    assert approval is not None
    assert approval.kind == "operator_approval"
    requester_pat = await bench.users.create_pat(bench.mia["id"], "own")

    dash = Dashboard(bench, bench.service())
    try:
        listed = await dash.get("/api/approvals", requester_pat)
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()["approvals"]] == [approval.id]

        decided = await dash.post(
            f"/api/approvals/{approval.id}", requester_pat, {"approve": True}
        )
        assert decided.status_code == 403
        still = await bench.ticket_store.get_open_approval(ticket_id)
        assert still is not None and still.status == "pending"
        assert bench.forwarded == []
    finally:
        await dash.aclose()


async def test_a_sibling_cannot_see_or_decide_anothers_approval_but_operator_sees_both(
    benches,
) -> None:
    """The listing-scope half of the fix, proven with two real requesters and
    two real hosts so the negative case (noah seeing nothing of mia's) is not
    just an empty fixture: mia's gate is visible only to mia and to an
    operator; noah -- scoped to a different host, with no ticket of his own
    here -- sees none of it.
    """

    bench = await benches("rbac-siblings")
    ticket_id, _thread = await drive_to_an_open_approval(
        bench, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await bench.ticket_store.get_open_approval(ticket_id)
    assert approval is not None
    mia_pat = await bench.users.create_pat(bench.mia["id"], "own")
    noah_pat = await bench.users.create_pat(bench.noah["id"], "sib")
    operator_pat = await bench.users.create_pat(bench.root["id"], "dash")

    dash = Dashboard(bench, bench.service())
    try:
        mia_rows = (await dash.get("/api/approvals", mia_pat)).json()["approvals"]
        assert [r["id"] for r in mia_rows] == [approval.id]

        noah_rows = (await dash.get("/api/approvals", noah_pat)).json()["approvals"]
        assert noah_rows == []

        op_rows = (await dash.get("/api/approvals", operator_pat)).json()["approvals"]
        assert [r["id"] for r in op_rows] == [approval.id]

        # Naming mia's ticket by id is refused for noah the same way every
        # other per-ticket route refuses it.
        assert (
            await dash.get(f"/api/approvals?ticket_id={ticket_id}", noah_pat)
        ).status_code == 403
    finally:
        await dash.aclose()


async def test_approving_from_the_dashboard_after_a_restart_executes_the_call(
    benches,
) -> None:
    """REGRESSION GUARD — a dashboard approval used to decide the row and nothing else.

    ``webui/tickets.api_approval_decide`` calls
    ``TicketService.decide_approval`` and returns. It never calls
    ``DiscordService.resume``, even though ``build_ticket_routes`` is handed the
    service precisely so it could. Only the Discord button path
    (``handle_component``) resumes. So the operator sees "approved", the ticket
    stays blocked on ``approval`` forever, the frozen call never runs, and the
    requester is never told — which is exactly the scenario the plan lists as
    verification step 6 ("restart during an open approval, then decide in the
    dashboard — the execution must happen").

    It fails closed, so it is a correctness/availability defect rather than an
    escalation; but the claim "approvals are persistent, asynchronous and
    reachable from the dashboard" does not hold.
    """

    boot1 = await benches("dashboard")
    ticket_id, _thread = await drive_to_an_open_approval(
        boot1, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await boot1.ticket_store.get_open_approval(ticket_id)
    assert approval is not None
    approval_id = approval.id
    operator_pat = await boot1.users.create_pat(boot1.root["id"], "dash")
    await boot1.close()

    boot2 = Bench(boot1.db_path)
    await boot2.open(seed=False)
    try:
        service = boot2.service(says("Git is installed now."))
        dash = Dashboard(boot2, service)
        decided = await dash.post(
            f"/api/approvals/{approval_id}", operator_pat, {"approve": True}
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "approved"
        await dash.aclose()

        # The frozen call — tool, args and target — is what must have run.
        assert boot2.forwarded == [
            {"agent_id": MIA_PC, "tool": "winget_install", "args": {"id": "Git.Git"}}
        ], "an approved call was never executed"
        # ...and the loop must have resumed and told the requester.
        assert "Git is installed now." in boot2.posted
        ticket = await boot2.ticket_store.get(ticket_id)
        assert ticket is not None and ticket.blocked_on != "approval"
    finally:
        await boot2.close()


async def test_approving_over_discord_after_a_restart_executes_the_frozen_call(
    benches,
) -> None:
    """The path that does work, kept as the contrast to the test above."""

    boot1 = await benches("discord")
    ticket_id, _thread = await drive_to_an_open_approval(
        boot1, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await boot1.ticket_store.get_open_approval(ticket_id)
    assert approval is not None
    approval_id = approval.id
    await boot1.close()

    boot2 = Bench(boot1.db_path)
    await boot2.open(seed=False)
    try:
        service = boot2.service(says("Git is installed now."))
        await service.handle_event(button(approval_id, by=ROOT, approve=True))

        assert boot2.forwarded == [
            {"agent_id": MIA_PC, "tool": "winget_install", "args": {"id": "Git.Git"}}
        ]
        assert "Git is installed now." in boot2.posted
        ticket = await boot2.ticket_store.get(ticket_id)
        assert ticket is not None
        assert ticket.state == "in_progress"
        assert ticket.blocked_on == "user"
        run = await boot2.ticket_store.load_run(ticket_id)
        issued, answered = transcript_pairs(run.messages)
        assert issued == answered == {"t1"}
    finally:
        await boot2.close()


# =============================================================================
# Two gated calls in one turn
# =============================================================================


async def test_two_queued_gated_calls_survive_two_restarts(benches) -> None:
    """The most likely correctness bug on this surface: one turn emits two
    ``normal_change`` calls, the second parks in ``_queue`` while the first
    holds. Restart between *each* decision, so the queue can only come from
    SQLite."""

    boot1 = await benches("queued")
    ticket_id, _thread = await drive_to_an_open_approval(
        boot1,
        use("t1", "winget_install", {"id": "Git.Git"}),
        use("t2", "winget_install", {"id": "Vim.Vim"}),
    )
    first = await boot1.ticket_store.get_open_approval(ticket_id)
    assert first is not None and first.args == {"id": "Git.Git"}
    # The second call is parked in the persisted queue, not lost with the turn.
    parked = await boot1.ticket_store.load_run(ticket_id)
    assert [b["input"] for b in parked.queue] == [{"id": "Vim.Vim"}]
    operator_pat = await boot1.users.create_pat(boot1.root["id"], "dash")
    first_id = first.id
    await boot1.close()

    # -- restart 1: approve the first over Discord ------------------------
    boot2 = Bench(boot1.db_path)
    await boot2.open(seed=False)
    second_id: str
    try:
        service = boot2.service()  # no scripted turn: the queue must gate first
        await service.handle_event(button(first_id, by=ROOT, approve=True))
        assert [c["args"]["id"] for c in boot2.forwarded] == ["Git.Git"]
        assert boot2.model_calls == 0, "the parked call must gate before a new turn"
        second = await boot2.ticket_store.get_open_approval(ticket_id)
        assert second is not None
        assert second.id != first_id
        assert second.args == {"id": "Vim.Vim"}
        assert second.agent_id == MIA_PC
        second_id = second.id
    finally:
        await boot2.close()

    # -- restart 2: it is still listed, still frozen, and still runs -------
    boot3 = Bench(boot1.db_path)
    await boot3.open(seed=False)
    try:
        dash = Dashboard(boot3, boot3.service())
        rows = (await dash.get("/api/approvals", operator_pat)).json()["approvals"]
        assert [r["args"] for r in rows] == [{"id": "Vim.Vim"}]
        await dash.aclose()

        service = boot3.service(says("Both are installed."))
        await service.handle_event(button(second_id, by=ROOT, approve=True))
        assert [c["args"]["id"] for c in boot3.forwarded] == ["Vim.Vim"]
        assert "Both are installed." in boot3.posted

        run = await boot3.ticket_store.load_run(ticket_id)
        assert run.queue == []
        issued, answered = transcript_pairs(run.messages)
        assert issued == {"t1", "t2"}
        assert answered == issued, "a queued tool_use was left unanswered"
        assert await boot3.ticket_store.get_open_approval(ticket_id) is None
    finally:
        await boot3.close()


async def test_a_denied_first_call_still_gates_the_queued_second(benches) -> None:
    """A denial must not flush the queue: the second call gets its own gate."""

    bench = await benches("denied")
    ticket_id, _thread = await drive_to_an_open_approval(
        bench,
        use("t1", "winget_install", {"id": "Git.Git"}),
        use("t2", "winget_uninstall", {"id": "Vim.Vim"}),
    )
    first = await bench.ticket_store.get_open_approval(ticket_id)
    assert first is not None

    service = bench.service()
    await service.handle_event(button(first.id, by=ROOT, approve=False))

    assert bench.forwarded == []
    second = await bench.ticket_store.get_open_approval(ticket_id)
    assert second is not None
    assert (second.tool, second.args) == ("winget_uninstall", {"id": "Vim.Vim"})


# =============================================================================
# Expiry
# =============================================================================


async def test_an_expired_approval_denies_and_does_not_park_the_ticket(
    benches,
) -> None:
    """REGRESSION GUARD — expiry used to close the row and abandon the ticket.

    ``TicketService.expire_due`` marks the gate ``expired`` and writes a trail
    row, and its own docstring says "resuming is the caller's job". Nobody is
    that caller: ``ticket_sweep_loop`` is wired in ``main.py`` with the
    ``TicketService`` alone, and ``DiscordService.resume`` is only ever called
    from ``handle_component``. So an approval nobody answers leaves the ticket
    blocked on ``approval`` for good — the requester is never told, and the
    requester cannot clear that block themselves (``_UNBLOCK_CLEARERS``
    allows only ``system``/``operator`` to clear an ``approval`` block, by
    design).

    Worse than parked: the stored transcript still ends with an unanswered
    ``tool_use`` block. The next message the requester sends appends a plain user
    message after it, which the real Messages API rejects — so the ticket is not
    merely stuck, it can no longer take a turn at all. This test asserts both
    halves.
    """

    bench = await benches("expired")
    ticket_id, thread = await drive_to_an_open_approval(
        bench, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await bench.ticket_store.get_open_approval(ticket_id)
    assert approval is not None

    # The sweeper runs, long after the TTL.
    await bench.tickets.sweep(bench.tickets.now() + timedelta(days=30))
    expired = await bench.ticket_store.get_approval(approval.id)
    assert expired is not None and expired.status == "expired"
    assert bench.forwarded == []

    ticket = await bench.ticket_store.get(ticket_id)
    assert ticket is not None
    assert ticket.blocked_on != "approval", "an expired gate parked the ticket"

    # And the transcript is answerable: no tool_use left dangling.
    service = bench.service(says("Nobody approved that, sorry."))
    await service.handle_event(in_thread("hello? still there?", thread_id=thread))
    run = await bench.ticket_store.load_run(ticket_id)
    issued, answered = transcript_pairs(run.messages)
    assert issued == answered, (
        "the expired call left an unanswered tool_use in the transcript: "
        f"{sorted(issued - answered)}"
    )
    assert "Nobody approved that" in bench.posted


async def test_an_expired_approval_is_gone_from_the_dashboard_queue(benches) -> None:
    """Whatever else expiry does, it must not leave a decidable row behind."""

    bench = await benches("expired-api")
    ticket_id, _thread = await drive_to_an_open_approval(
        bench, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await bench.ticket_store.get_open_approval(ticket_id)
    assert approval is not None
    operator_pat = await bench.users.create_pat(bench.root["id"], "dash")

    await bench.tickets.sweep(bench.tickets.now() + timedelta(days=30))

    dash = Dashboard(bench, bench.service())
    try:
        rows = (await dash.get("/api/approvals", operator_pat)).json()["approvals"]
        assert rows == []
        # Deciding an expired gate is a conflict, not a late execution.
        late = await dash.post(
            f"/api/approvals/{approval.id}", operator_pat, {"approve": True}
        )
        assert late.status_code == 409
        assert bench.forwarded == []
    finally:
        await dash.aclose()


# =============================================================================
# What is stored is what runs
# =============================================================================


async def test_the_executed_call_comes_from_the_frozen_row_not_the_transcript(
    benches,
) -> None:
    """A rewritten transcript must not change what an approved gate executes.

    The transcript is the only part of the resume state a compromised or
    confused turn could influence; the approval row is the authority.
    """

    boot1 = await benches("frozen")
    ticket_id, _thread = await drive_to_an_open_approval(
        boot1, use("t1", "winget_install", {"id": "Git.Git"})
    )
    approval = await boot1.ticket_store.get_open_approval(ticket_id)
    assert approval is not None
    approval_id = approval.id

    # Tamper with the persisted transcript the way an injected turn would like
    # to: the assistant "asked" for a different package on a different host.
    run = await boot1.ticket_store.load_run(ticket_id)
    tampered = json.loads(
        json.dumps(run.messages)
        .replace('"Git.Git"', '"Evil.Backdoor"')
        .replace(MIA_PC, "noah-pc")
    )
    await boot1.ticket_store.save_run(ticket_id, messages=tampered)
    await boot1.close()

    boot2 = Bench(boot1.db_path)
    await boot2.open(seed=False)
    try:
        service = boot2.service(says("done"))
        await service.handle_event(button(approval_id, by=ROOT, approve=True))
        assert boot2.forwarded == [
            {"agent_id": MIA_PC, "tool": "winget_install", "args": {"id": "Git.Git"}}
        ]
    finally:
        await boot2.close()


# =============================================================================
# §7: every decision route must leave a fully-answered transcript
# =============================================================================
#
# The reported wedge: a requester-less (alert-origin) ticket's gate, decided by
# whichever route, must never leave an unanswered ``tool_use`` behind — several
# of these six cases failed before the fix (the dashboard and Discord routes
# silently did nothing for an alert-origin ticket; the sweeper parked it on
# ``blocked_on="approval"`` forever).


@pytest.mark.parametrize("origin", ["requester", "alert"])
@pytest.mark.parametrize("route", ["dashboard", "discord", "sweeper"])
async def test_every_decision_route_leaves_issued_equal_to_answered(
    benches, route: str, origin: str
) -> None:
    bench = await benches(f"{route}-{origin}")
    if origin == "requester":
        ticket_id, _thread = await drive_to_an_open_approval(
            bench, use("t1", "winget_install", {"id": "Git.Git"})
        )
    else:
        ticket_id = await drive_alert_origin_ticket_to_an_open_approval(
            bench, use("t1", "winget_install", {"id": "Git.Git"})
        )
    approval = await bench.ticket_store.get_open_approval(ticket_id)
    assert approval is not None

    if route == "dashboard":
        operator_pat = await bench.users.create_pat(bench.root["id"], "dash")
        service = bench.service(says("done via the dashboard"))
        dash = Dashboard(bench, service)
        try:
            r = await dash.post(
                f"/api/approvals/{approval.id}", operator_pat, {"approve": True}
            )
            assert r.status_code == 200
        finally:
            await dash.aclose()
    elif route == "discord":
        service = bench.service(says("done via discord"))
        await service.handle_event(button(approval.id, by=ROOT, approve=True))
    else:
        bench.service(says("(the sweeper resumed this)"))
        await bench.tickets.sweep(bench.tickets.now() + timedelta(days=30))

    run = await bench.ticket_store.load_run(ticket_id)
    issued, answered = run_pairs(run)
    assert issued == answered, f"unanswered tool_use ids: {sorted(issued - answered)}"


async def test_an_alert_origin_gate_expiring_with_no_decider_degrades_cleanly(
    benches,
) -> None:
    """The case the architect asked for: an alert-origin ticket's gate expires
    with no decider anywhere reachable (pure sweeper path — ``resume_expired``
    passes no ``decided_by``, and there is no requester either). No unanswered
    ``tool_use``, the ticket is usable again (``in_progress``/``blocked_on ==
    "operator"``, not stuck on ``"approval"``), an explanatory trail row
    exists, and — the part that makes this a regression guard rather than a
    state assertion — a following operator turn on the same ticket succeeds.
    """

    bench = await benches("alert-no-decider")
    ticket_id = await drive_alert_origin_ticket_to_an_open_approval(
        bench, use("t1", "winget_install", {"id": "Git.Git"})
    )

    await bench.tickets.sweep(bench.tickets.now() + timedelta(days=30))

    run = await bench.ticket_store.load_run(ticket_id)
    issued, answered = run_pairs(run)
    assert issued == answered, f"unanswered tool_use ids: {sorted(issued - answered)}"

    ticket = await bench.ticket_store.get(ticket_id)
    assert ticket is not None
    assert ticket.state == "in_progress"
    assert ticket.blocked_on == "operator"

    error_rows = await bench.trail(ticket_id, "error")
    assert any(
        e.fields and (e.fields.get("error") or {}).get("code") == "no_principal"
        for e in error_rows
    )

    # And the ticket is not merely "unstuck" in name -- an operator can
    # actually keep working it afterward.
    service = bench.service(says("Back on it, an operator here."))
    operator = await service._principal_for(ROOT, GUILD)
    assert operator is not None
    session = await bench.assistant.session_for(ticket, actor=operator)
    assert session is not None
    bench.assistant.append_user_message(session, "any update?")
    events = [e async for e in bench.assistant.run_turn(session, ticket)]
    assert events[-1]["type"] == "done"

    run = await bench.ticket_store.load_run(ticket_id)
    issued, answered = transcript_pairs(run.messages)
    assert issued == answered, f"unanswered tool_use ids after the follow-up: {sorted(issued - answered)}"

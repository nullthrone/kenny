"""Seed a *running* app's in-memory + SQLite stores with the demo fleet.

Seeding must happen in-process against the same ``app.state`` the web server
serves from, because some state is in-memory only (``ScreenshotStore`` and the
``AgentRegistry`` online flags are not SQLite-backed). :func:`seed_app` is an
async coroutine run inside the server's own event loop after ``build_app``.

It populates, per host:

* a ~30-point daily telemetry series + a latest snapshot (``TelemetryStore``),
* the registry online state + metadata (so hosts render online),
* parental-controls config / custom list / observed events (``WebFilterStore``),
* a seeded desktop screenshot (``ScreenshotStore``),

plus a few fleet-wide Activity rows (an audit call, an alert, a log line) and a
couple of persisted copilot conversations, and it pre-seeds the reliability
categorization cache so the heatmaps and health scoring show friendly
categories/severities (ADR-0026) without an API key, plus one reliability
alarm suppression rule (ADR-0041 / issue #166) so the Reliability card's
suppressed badge and rule panel are populated in the captured screenshots.

It also seeds the **browser identity** the screenshot session signs in as: a real
superuser account ("thomas", matching the design's own persona) with 2FA and a
personal access token already configured, rather than the legacy shared
``KENNY_OPERATOR_TOKEN`` cookie — that back-compat identity is a synthetic
principal with no user row (``is_shared_token: true``), which would make
``profile.png`` show the empty "no editable account" state instead of the real
password/2FA/PAT rows the Profile page actually has. ``capture.py`` uses the
returned session id as the ``kenny_op`` cookie for the whole run.

It also seeds a handful of demo **tickets** (``TicketService``, no Discord
gateway involved) so the Tickets tab has a real list and one ticket shows a
full lifecycle — a message, an autonomous ``standard_change`` call, a held
``normal_change`` that was approved, and a resolution — through
``TicketService`` itself rather than by writing rows directly, so the seeded
state is exactly what driving the real gate would have produced. And a couple
of **Discord identities** plus one pending link claim (``DiscordIdentityStore``,
still no gateway) so the Settings Discord panel has real rows instead of the
"nothing linked yet" empty state.

A handful of ``UserStore`` accounts back the requester/assignee/decided-by ids
the tickets and identities above reference (``_seed_users``) — without them,
every id shown in a screenshot would fall back to its bare ``#id`` form
(``GET /api/users/directory`` returning nothing to resolve against), which
would not demonstrate the dashboard's own username-resolution at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from kenny_server import event_categories, security
from kenny_server.ticketstore import ASSISTANT_ACTOR

from . import demo_fleet
from .desktop_image import demo_desktop_png_b64


async def _noop_send(_frame: dict[str, Any]) -> None:
    """A send function for registry online state (frames are never delivered)."""


@dataclass
class SeedResult:
    """What :func:`seed_app` produced — the agent ids seeded and the browser
    session id ``capture.py`` should sign in with."""

    agent_ids: list[str]
    session_id: str


async def seed_app(app: Any, base: datetime | None = None) -> SeedResult:
    """Seed ``app.state`` with the demo fleet. Returns the seeded agent ids
    and the "thomas" superuser session id the screenshot browser signs in as."""

    base = base or datetime.now(timezone.utc)
    state = app.state
    hosts = demo_fleet.build_fleet(base)

    user_ids, session_id = await _seed_users(state.user_store)

    _seed_reliability_categories()
    if getattr(state, "suppression", None) is not None:
        await _seed_suppressions(state.suppression)

    screenshot_b64 = demo_desktop_png_b64()

    for host in hosts:
        # Telemetry: daily history (oldest-first) then a final latest snapshot.
        for collected_at, snapshot in demo_fleet.history_snapshots(host, base):
            await state.store.insert(host.agent_id, collected_at, snapshot)

        # Registry: mark online with metadata so the detail header + KPIs populate.
        if host.online:
            state.registry.register_signed_async(host.agent_id, host.meta, _noop_send)

        # Parental controls (kid-pc): config, custom list, observed events.
        if host.webfilter is not None:
            await _seed_webfilter(state.webfilter_store, host.agent_id, host.webfilter)

        # A seeded desktop capture for the screenshot card + modal.
        state.screenshots.put(host.agent_id, screenshot_b64, "png")

    await _seed_activity(state.event_store, base)
    await _seed_chat_history(state.chat_history_store, base)
    if getattr(state, "tickets", None) is not None:
        await _seed_tickets(state.tickets, base, user_ids)
    if getattr(state, "discord_identities", None) is not None:
        await _seed_discord_identities(state.discord_identities, base, user_ids)

    return SeedResult(agent_ids=[h.agent_id for h in hosts], session_id=session_id)


async def _seed_users(user_store: Any) -> tuple[dict[str, int], str]:
    """A few real accounts backing the ids ``_seed_tickets``/``_seed_discord_identities``
    reference, keyed by role in this demo story rather than by number, plus the
    "thomas" superuser the screenshot browser itself signs in as.

    Real rows (not just numbers) matter here: ``GET /api/users/directory`` — the
    dashboard's id -> username resolver — has nothing to resolve against
    otherwise, and every requester/assignee/actor in the captured screenshots
    would show its bare ``#id`` fallback instead of demonstrating the feature.
    IDs are whatever ``UserStore`` assigns (autoincrement from an empty demo
    DB); callers thread the returned dict through rather than assuming 1/2/3.

    "thomas" gets a TOTP secret and one personal access token so ``profile.png``
    shows those rows as genuinely configured, not their empty defaults — and a
    session created directly via ``UserStore.create_session`` (bypassing the
    ``/login`` form entirely, TOTP included) since nothing here needs to prove
    the login flow itself, only sign in as a real account before driving the app.
    """

    operator = await user_store.create_user("mama", "demo-password-1", "operator")
    grandpa = await user_store.create_user("grandpa", "demo-password-2", "user")
    papa = await user_store.create_user("papa", "demo-password-3", "user")
    thomas = await user_store.create_user(
        "thomas",
        "demo-password-0",
        "superuser",
        email="thomas@kenny.example",
        totp_secret=security.generate_totp_secret(),
    )
    await user_store.create_pat(thomas["id"], label="claude-desktop")
    session_id = await user_store.create_session(
        thomas["id"], ip="127.0.0.1", user_agent="kenny-screenshots"
    )
    user_ids = {
        "operator": operator["id"],
        "grandpa": grandpa["id"],
        "papa": papa["id"],
        "thomas": thomas["id"],
    }
    return user_ids, session_id


def _seed_reliability_categories() -> None:
    """Pre-fill the categorization cache so heatmaps + health scoring show
    friendly categories/severities (ADR-0026) without an API key.

    With no ANTHROPIC_API_KEY the categorizer coerces every group to the safe
    default (``category="Other"``, ``severity="unknown"``); priming the module
    cache by ``(source, event_id)`` makes ``categorize_events`` return our
    intended classifications from cache (it checks the cache before the
    client, so ``client is None`` still yields the primed values).
    """

    for key, classification in demo_fleet.RELIABILITY_CLASSIFICATIONS.items():
        event_categories._cache_put(key, classification)  # noqa: SLF001


async def _seed_suppressions(suppression: Any) -> None:
    """Apply the demo fleet's reliability alarm suppression rules (ADR-0041 /
    issue #166) via the real ``SuppressionService``, so the seeded state is
    identical to what an operator clicking "suppress" would produce."""

    for rule in demo_fleet.RELIABILITY_SUPPRESSIONS:
        await suppression.add(**rule)


async def _seed_webfilter(store: Any, agent_id: str, wf: dict[str, Any]) -> None:
    await store.set_config(agent_id, **wf["config"])
    for row in wf["domains"]:
        await store.add_domain(agent_id, row["domain"], row["action"], row.get("note"))
    await store.upsert_events(agent_id, wf["events"])
    # A prior "apply" so the drift/applied state shows real values in the editor.
    await store.set_applied_state(
        agent_id, "seedhash00000000", (datetime.now(timezone.utc)).isoformat(), True
    )


async def _seed_activity(event_store: Any, base: datetime) -> None:
    """A few fleet-wide Activity rows: an audit call, an alert, and log lines."""

    await event_store.insert_audit(
        agent_id="study-pc",
        tool="powershell_exec",
        ok=True,
        at=_iso(base - timedelta(minutes=4)),
    )
    await event_store.insert_audit(
        agent_id="kid-pc",
        tool="webfilter_apply",
        ok=True,
        at=_iso(base - timedelta(minutes=11)),
    )
    await event_store.insert_audit(
        agent_id="living-room-pc",
        tool="winget_update",
        ok=False,
        error="package Microsoft.PowerToys not found",
        at=_iso(base - timedelta(minutes=18)),
    )
    await event_store.insert_audit(
        agent_id="grandpa-pc",
        tool="diag_eventlog",
        ok=True,
        at=_iso(base - timedelta(minutes=26)),
    )

    await event_store.insert_alert(
        agent_id="grandpa-pc",
        message="Defender real-time protection is OFF on grandpa-pc",
        level="crit",
        fields={"section": "defender"},
        at=_iso(base - timedelta(minutes=8)),
    )
    await event_store.insert_alert(
        agent_id="study-pc",
        message="Disk C: on study-pc is forecast to fill within ~9 days",
        level="warn",
        fields={"section": "disk", "days_until_full": 9},
        at=_iso(base - timedelta(minutes=33)),
    )

    for i, (source, level, agent, message) in enumerate(
        [
            ("server", "info", None, "fleet telemetry refresh complete (6 hosts)"),
            ("agent", "warn", "living-room-pc", "reboot pending since last update"),
            ("agent", "error", "grandpa-pc", "Windows Defender real-time protection disabled"),
            ("server", "info", "kid-pc", "parental-controls block list applied (drift resolved)"),
            ("agent", "debug", "papa-pc", "telemetry push accepted"),
        ]
    ):
        await event_store.insert_log(
            source=source,
            at=_iso(base - timedelta(minutes=2 + i * 3)),
            level=level,
            agent_id=agent,
            message=message,
            target=f"kenny.{source}",
        )


async def _seed_chat_history(store: Any, base: datetime) -> None:
    """A couple of persisted conversations for the chat-history panel."""

    await store.save(
        id="conv-study-disk",
        title="Why is study-pc almost full?",
        agent_id="study-pc",
        messages=[
            {"role": "user", "content": "Why is study-pc almost full?"},
            {
                "role": "assistant",
                "content": "Drive C: is 96% full and rising ~0.5%/day — about 9 days to full.",
            },
        ],
    )
    await store.save(
        id="conv-kid-web",
        title="Review flagged sites on kid-pc",
        agent_id="kid-pc",
        messages=[
            {"role": "user", "content": "Review flagged sites on kid-pc"},
            {
                "role": "assistant",
                "content": "Three domains were flagged in the last 24h; the block list is applied.",
            },
        ],
    )
    await store.save(
        id="conv-fleet-health",
        title="Give me a fleet health summary",
        agent_id=None,
        messages=[
            {"role": "user", "content": "Give me a fleet health summary"},
            {
                "role": "assistant",
                "content": "3 of 6 PCs need attention: grandpa-pc, kid-pc and study-pc.",
            },
        ],
    )


async def _seed_tickets(tickets: Any, base: datetime, user_ids: dict[str, int]) -> None:
    """Four demo tickets across states/origins for the Tickets tab.

    Driven entirely through ``TicketService`` — create/transition/append_event/
    open_approval/decide_approval — so the seeded rows are exactly what the
    real lifecycle would have produced, not a shortcut through the store. The
    service's clock is temporarily backdated per step (restored in ``finally``)
    so the list's age column and the detail timeline read like a fleet that has
    been running for a while, not "just now" for everything.

    ``user_ids`` (see ``_seed_users``) is who plays each role in this story:
    "grandpa" opens the Wi-Fi ticket, "papa" opens the other two, "operator"
    (mama) is the one who decides the held approval.
    """

    grandpa_id = user_ids["grandpa"]
    papa_id = user_ids["papa"]
    operator_id = user_ids["operator"]

    original_now = tickets._now  # noqa: SLF001 - screenshot-only backdating

    def _at(**delta: float) -> None:
        tickets._now = lambda: base - timedelta(**delta)  # noqa: SLF001

    try:
        # -- the rich one: a full Discord lifecycle through both gates --------
        _at(hours=2, minutes=10)
        flush = await tickets.create(
            id="demo-tkt-flush",
            title="Wi-Fi keeps dropping and games lag on grandpa-pc",
            origin="discord",
            requester_user_id=grandpa_id,
            agent_id="grandpa-pc",
            role_snapshot="user",
            profile_snapshot="self-service-basic",
            actor=f"user:{grandpa_id}",
            reason="opened from Discord",
        )
        await tickets.transition(
            flush.id, "in_progress", actor="system", reason="opened from Discord"
        )
        await tickets.append_event(
            flush.id, kind="message", actor=f"user:{grandpa_id}", summary="opening message",
            fields={"actionable": True, "discord_id": "441029938271"},
        )
        _at(hours=2, minutes=6)
        await tickets.append_event(
            flush.id, kind="tool_call", actor=ASSISTANT_ACTOR, tool="diag_processes",
            tool_class="read_only", ok=True, summary="diag_processes succeeded",
            fields={"agent_id": "grandpa-pc"},
        )
        # A dashboard-chat exchange (ADR-0049): unlike a Discord-origin message, a
        # dashboard message and every kenny reply carry their verbatim wording in
        # the trail, which is what the ticket-detail screenshot needs to show off.
        _at(hours=2, minutes=5)
        await tickets.append_event(
            flush.id, kind="message", actor=f"user:{grandpa_id}", summary="message",
            fields={
                "text": "While you're at it, is grandpa-pc running low on disk space?",
                "actionable": True,
                "surface": "dashboard",
            },
        )
        await tickets.append_event(
            flush.id, kind="message", actor=ASSISTANT_ACTOR, summary="message",
            fields={
                "text": (
                    "No, plenty of room — 128 GB free out of 512 GB on the main drive. "
                    "That's not what's causing the Wi-Fi drops."
                ),
                "surface": "dashboard",
            },
        )
        _at(hours=2, minutes=4)
        await tickets.append_event(
            flush.id, kind="tool_call", actor=ASSISTANT_ACTOR, tool="net_dns_flush",
            tool_class="standard_change", args={},
            summary="net_dns_flush authorized autonomously as a standard change",
        )
        await tickets.append_event(
            flush.id, kind="tool_call", actor=ASSISTANT_ACTOR, tool="net_dns_flush",
            tool_class="standard_change", ok=True, summary="net_dns_flush succeeded",
            fields={"agent_id": "grandpa-pc"},
        )
        _at(hours=1, minutes=55)
        approval = await tickets.open_approval(
            flush.id, tool_use_id="toolu_demo1", tool="winget_install",
            tool_class="normal_change", args={"id": "Realtek.WiFiDriver"},
            kind="operator_approval", agent_id="grandpa-pc", actor=ASSISTANT_ACTOR,
        )
        await tickets.block(
            flush.id, "approval", actor="system",
            reason="winget_install held for operator_approval", ref=approval.id,
        )
        _at(hours=1, minutes=40)
        await tickets.decide_approval(
            approval.id, approve=True, decided_by=operator_id, decided_via="dashboard", actor=f"operator:{operator_id}",
        )
        await tickets.unblock(flush.id, actor="system", reason="gate decided")
        _at(hours=1, minutes=38)
        await tickets.append_event(
            flush.id, kind="tool_call", actor=ASSISTANT_ACTOR, tool="winget_install",
            tool_class="normal_change", ok=True, summary="winget_install succeeded",
            fields={"agent_id": "grandpa-pc"},
        )
        _at(hours=1, minutes=30)
        await tickets.transition(flush.id, "resolved", actor="system", reason="issue fixed")
        await tickets.update(
            flush.id,
            resolution="Updated the Wi-Fi driver; grandpa-pc now holds a stable connection.",
        )

        # -- an open approval, so the header's approvals badge has something --
        _at(minutes=42)
        printer = await tickets.create(
            id="demo-tkt-printer",
            title="Install printer driver on living-room-pc",
            origin="discord",
            requester_user_id=papa_id,
            agent_id="living-room-pc",
            role_snapshot="user",
            profile_snapshot="power-user",
            actor=f"user:{papa_id}",
            reason="opened from Discord",
        )
        await tickets.transition(
            printer.id, "in_progress", actor="system", reason="opened from Discord"
        )
        await tickets.append_event(
            printer.id, kind="message", actor=f"user:{papa_id}", summary="opening message",
            fields={"actionable": True, "discord_id": "552017744102"},
        )
        _at(minutes=39)
        printer_approval = await tickets.open_approval(
            printer.id, tool_use_id="toolu_demo2", tool="winget_install",
            tool_class="normal_change", args={"id": "Brother.iPrintScan"},
            kind="operator_approval", agent_id="living-room-pc", actor=ASSISTANT_ACTOR,
        )
        await tickets.block(
            printer.id, "approval", actor="system",
            reason="winget_install held for operator_approval", ref=printer_approval.id,
        )

        # -- a fresh, untouched ticket opened straight from the dashboard -----
        _at(minutes=6)
        await tickets.create(
            id="demo-tkt-webcam",
            title="Webcam not detected in Teams",
            origin="dashboard",
            requester_user_id=papa_id,
            agent_id="papa-pc",
            actor=f"user:{papa_id}",
            reason="opened from the dashboard",
        )

        # -- an alert that opened its own ticket, still waiting on an operator -
        _at(minutes=8)
        defender = await tickets.create(
            id="demo-tkt-defender",
            title="Defender real-time protection is OFF on grandpa-pc",
            origin="alert",
            requester_user_id=None,
            agent_id="grandpa-pc",
            priority="high",
            category="alert",
            summary="Defender real-time protection is OFF on grandpa-pc",
            actor="system",
            reason="opened from an alert",
        )
        await tickets.transition(defender.id, "in_progress", actor="system")
        await tickets.block(
            defender.id, "operator", actor="system",
            reason="waiting for an operator to pick this up",
        )

        # -- an alert kenny investigated by itself and closed out --------------
        #
        # The real case from the operator's own fleet: a disk error naming a
        # device that is not on that PC. It is the shot worth having because it
        # is the one a statistical filter gets wrong -- new, high-volume,
        # disk-related, and meaningless. See ADR-0056.
        _at(minutes=12)
        phantom = await tickets.create(
            id="demo-tkt-phantom",
            title="grandpa-pc health: crit",
            origin="alert",
            requester_user_id=None,
            agent_id="grandpa-pc",
            category="alert",
            summary=(
                "grandpa-pc health: crit\n"
                "reliability: ok -> crit (disk/7 ×650 (~93/day) — bad block reported "
                "on a storage device)"
            ),
            actor="system",
            reason="opened from an alert",
        )
        await tickets.append_event(
            phantom.id, kind="note", actor="triage",
            summary="looking into this before anyone is asked to",
        )
        _at(minutes=11)
        await tickets.append_event(
            phantom.id, kind="tool_call", actor="triage", tool="diag_services",
            tool_class="read_only", ok=True, summary="diag_services succeeded",
            fields={"agent_id": "grandpa-pc"},
        )
        await tickets.append_event(
            phantom.id, kind="note", actor="triage",
            summary=(
                "triage verdict: phantom — the drive this error names is not attached "
                "to this PC"
            ),
            fields={
                "verdict": "phantom",
                "finding": (
                    "The error names \\Device\\Harddisk1, but this PC only has "
                    "Harddisk0. It is the empty card reader reporting on itself — "
                    "nothing is failing."
                ),
                "evidence": (
                    "diag_services on grandpa-pc lists one physical disk (Harddisk0); "
                    "no device answers as Harddisk1"
                ),
                "resolvable": True,
                "suppression_suggestion": {"source": "disk", "event_id": 7},
            },
        )
        _at(minutes=10)
        await tickets.transition(
            phantom.id, "resolved", actor="system",
            reason="triage: phantom — the drive this error names is not attached to this PC",
            resolved_by="triage",
        )
    finally:
        tickets._now = original_now  # noqa: SLF001


_DEMO_GUILD_ID = "123456789012345678"


async def _seed_discord_identities(
    identities: Any, base: datetime, user_ids: dict[str, int]
) -> None:
    """Two linked accounts (one per enrollment path) and one pending claim.

    The same two Discord snowflakes used in :func:`_seed_tickets`, so the
    Settings panel and the ticket timeline agree with each other — and the
    same two accounts from :func:`_seed_users`, so the Discord panel's own
    id -> username resolution has something real to show too.
    """

    await identities.link(
        discord_user_id="441029938271",
        user_id=user_ids["grandpa"],
        guild_id=_DEMO_GUILD_ID,
        linked_via="claim",
        linked_by=user_ids["operator"],
        now=base - timedelta(days=12),
    )
    await identities.link(
        discord_user_id="552017744102",
        user_id=user_ids["papa"],
        guild_id=_DEMO_GUILD_ID,
        linked_via="member_list",
        linked_by=user_ids["operator"],
        now=base - timedelta(days=5),
    )
    await identities.open_claim(
        discord_user_id="998877665544332211",
        display_hint="papas_kid",
        guild_id=_DEMO_GUILD_ID,
        ttl_secs=3600,
        now=base - timedelta(minutes=10),
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def _selftest() -> None:  # pragma: no cover - manual smoke test
    """Build an app, seed it, and print the fleet overview shape."""

    import os
    import tempfile
    from pathlib import Path

    os.environ.setdefault("KENNY_ALERT_INTERVAL_SECS", "0")
    os.environ.setdefault("KENNY_WEBFILTER_REFRESH_SECS", "0")
    from kenny_server.main import build_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "demo.sqlite")
        app = build_app(db_path=db_path)
        async with app.router.lifespan_context(app):
            result = await seed_app(app)
            fleet = await app.state.store.known_agents()
            print("seeded:", result.agent_ids)
            print("session:", result.session_id)
            print("known_agents:", fleet)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_selftest())

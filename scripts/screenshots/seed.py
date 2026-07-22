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
categories/severities (ADR-0028) without an API key.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from kenny_server import event_categories

from . import demo_fleet
from .desktop_image import demo_desktop_png_b64


async def _noop_send(_frame: dict[str, Any]) -> None:
    """A send function for registry online state (frames are never delivered)."""


async def seed_app(app: Any, base: datetime | None = None) -> list[str]:
    """Seed ``app.state`` with the demo fleet. Returns the seeded agent ids."""

    base = base or datetime.now(timezone.utc)
    state = app.state
    hosts = demo_fleet.build_fleet(base)

    _seed_reliability_categories()

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

    return [h.agent_id for h in hosts]


def _seed_reliability_categories() -> None:
    """Pre-fill the categorization cache so heatmaps + health scoring show
    friendly categories/severities (ADR-0028) without an API key.

    With no ANTHROPIC_API_KEY the categorizer coerces every group to the safe
    default (``category="Other"``, ``severity="unknown"``); priming the module
    cache by ``(source, event_id)`` makes ``categorize_events`` return our
    intended classifications from cache (it checks the cache before the
    client, so ``client is None`` still yields the primed values).
    """

    for key, classification in demo_fleet.RELIABILITY_CLASSIFICATIONS.items():
        event_categories._cache_put(key, classification)  # noqa: SLF001


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
            ids = await seed_app(app)
            fleet = await app.state.store.known_agents()
            print("seeded:", ids)
            print("known_agents:", fleet)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_selftest())

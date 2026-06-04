"""Operator dashboard: static page + JSON API routes.

The dashboard is a single vanilla-JS page (``index.html``) that calls the
``/api/*`` routes built in :func:`build_api_routes`. Keep it dependency-light.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from ..registry import AgentRegistry
from ..store import TelemetryStore
from ..tools import CallLog, build_health
from ..tunnel import AgentTunnel, ToolError

_INDEX = Path(__file__).parent / "index.html"


def build_api_routes(
    *,
    registry: AgentRegistry,
    store: TelemetryStore,
    tunnel: AgentTunnel,
    call_log: CallLog,
) -> list[Route]:
    """Build the dashboard's static + JSON routes."""

    async def index(_request: Request) -> FileResponse:
        return FileResponse(_INDEX)

    async def api_fleet(_request: Request) -> JSONResponse:
        ids = await _known_ids(registry, store)
        agents = [await _overview(i, registry, store) for i in ids]
        from .. import health_rules

        overall = health_rules.worst(
            *(a["overall"] for a in agents if a["overall"] != "unknown")
        )
        return JSONResponse({"overall": overall or "unknown", "agents": agents})

    async def api_agent(request: Request) -> JSONResponse:
        agent_id = request.path_params["id"]
        agent = registry.get(agent_id)
        latest = await store.latest(agent_id)
        snapshot = latest["snapshot"] if latest else None
        history = await store.history(agent_id, limit=50)
        hist_points = [
            {"collected_at": h["collected_at"], "overall": build_health(h["snapshot"])["overall"]}
            for h in history
        ]
        return JSONResponse(
            {
                "agent_id": agent_id,
                "online": bool(agent and agent.online),
                "meta": agent.meta if agent else {},
                "collected_at": latest["collected_at"] if latest else None,
                "snapshot": snapshot,
                "health": build_health(snapshot),
                "history": hist_points,
                "call_log": [c for c in call_log.list() if c["agent_id"] == agent_id],
            }
        )

    async def api_refresh(request: Request) -> JSONResponse:
        agent_id = request.path_params["id"]
        try:
            result = await tunnel.send_request(agent_id, "telemetry.collect", {}, 60)
            call_log.record(agent_id, "telemetry.collect", {}, ok=True)
        except (ToolError, Exception) as exc:  # noqa: BLE001 - surface to UI
            message = exc.message if isinstance(exc, ToolError) else str(exc)
            call_log.record(agent_id, "telemetry.collect", {}, ok=False, error=message)
            return JSONResponse({"ok": False, "error": message}, status_code=502)
        # Store the freshly collected snapshot so the drill-down updates.
        if result:
            from datetime import datetime, timezone

            await store.insert(agent_id, datetime.now(timezone.utc).isoformat(), result)
        return JSONResponse({"ok": True})

    return [
        Route("/", index),
        Route("/api/fleet", api_fleet),
        Route("/api/agent/{id}", api_agent),
        Route("/api/agent/{id}/refresh", api_refresh, methods=["POST"]),
    ]


async def _known_ids(registry: AgentRegistry, store: TelemetryStore) -> list[str]:
    ids = {a.agent_id for a in registry.list()}
    ids.update(await store.known_agents())
    return sorted(ids)


async def _overview(
    agent_id: str, registry: AgentRegistry, store: TelemetryStore
) -> dict[str, Any]:
    agent = registry.get(agent_id)
    latest = await store.latest(agent_id)
    snapshot = latest["snapshot"] if latest else None
    health = build_health(snapshot)
    flagged = [n for n, s in health["sections"].items() if s["status"] in ("warn", "crit")]
    return {
        "agent_id": agent_id,
        "online": bool(agent and agent.online),
        "meta": agent.meta if agent else {},
        "overall": health["overall"],
        "flagged_sections": flagged,
        "collected_at": latest["collected_at"] if latest else None,
    }

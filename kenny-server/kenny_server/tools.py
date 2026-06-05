"""MCP tool registration.

Two kinds of tools (names match ``docs/protocol.md`` § Tool catalog exactly):

* **Forwarding capability tools** — require an active agent (via
  ``select_agent``) and forward a ``request`` frame to it through the tunnel.
* **Server-only tools** — ``list_agents``, ``select_agent``, ``fleet_overview``,
  ``agent_health``, ``agent_snapshot`` — read from the registry, store, and
  health rules; they are not forwarded to a single agent.

Every forwarded call is appended to an in-memory ``call_log`` for the dashboard
tool-call log.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP

from . import health_rules
from .registry import AgentRegistry
from .store import TelemetryStore
from .tunnel import AgentTunnel, ToolError

# Forwarding capability tools: name -> ordered arg keys (optional keys end "?").
CAPABILITY_TOOLS: dict[str, list[str]] = {
    "powershell_exec": ["script", "timeout_s"],
    "fs_list": ["path"],
    "fs_search": ["root", "pattern"],
    "fs_read": ["path"],
    "fs_disk_usage": [],
    "winget_list": [],
    "winget_install": ["id"],
    "winget_uninstall": ["id"],
    "winget_update": ["id?"],
    "diag_processes": [],
    "diag_services": ["filter?"],
    "diag_eventlog": ["log", "count"],
    "diag_autostart": [],
    "net_config": [],
    "net_dns_flush": [],
    "net_adapter_reset": ["name"],
    "screen_capture": [],
    "telemetry_collect": ["sections?"],
    "agent_update": ["version", "url", "sha256"],
}


class CallLog:
    """Bounded in-memory log of forwarded tool calls (for the dashboard)."""

    def __init__(self, maxlen: int = 200) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def record(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        ok: bool,
        error: str | None = None,
    ) -> None:
        self._entries.appendleft(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "agent_id": agent_id,
                "tool": tool,
                "args": args,
                "ok": ok,
                "error": error,
            }
        )

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._entries)[:limit]


def build_health(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Run health rules over a stored snapshot (or empty when none)."""

    if not snapshot:
        return {"overall": "unknown", "sections": {}}
    return health_rules.evaluate_snapshot(snapshot)


async def _agent_overview(
    agent_id: str, registry: AgentRegistry, store: TelemetryStore
) -> dict[str, Any]:
    agent = registry.get(agent_id)
    latest = await store.latest(agent_id)
    snapshot = latest["snapshot"] if latest else None
    health = build_health(snapshot)
    flagged = [
        name
        for name, s in health["sections"].items()
        if s["status"] in ("warn", "crit")
    ]
    return {
        "agent_id": agent_id,
        "online": bool(agent and agent.online),
        "meta": agent.meta if agent else {},
        "overall": health["overall"],
        "flagged_sections": flagged,
        "collected_at": latest["collected_at"] if latest else None,
    }


async def _known_agent_ids(registry: AgentRegistry, store: TelemetryStore) -> list[str]:
    ids = {a.agent_id for a in registry.list()}
    ids.update(await store.known_agents())
    return sorted(ids)


def register_tools(
    mcp: FastMCP,
    *,
    registry: AgentRegistry,
    store: TelemetryStore,
    tunnel: AgentTunnel,
    call_log: CallLog,
) -> None:
    """Register all MCP tools on ``mcp``."""

    # -- forwarding capability tools --------------------------------------

    def make_forwarder(tool_name: str):
        async def forward(args: dict[str, Any] | None = None) -> dict[str, Any]:
            """Forward this capability call to the active agent and return its result."""
            args = args or {}
            agent_id = registry.require_active()
            timeout_s = float(args.get("timeout_s", 30))
            try:
                result = await tunnel.send_request(agent_id, tool_name, args, timeout_s)
                call_log.record(agent_id, tool_name, args, ok=True)
                return result
            except ToolError as exc:
                call_log.record(agent_id, tool_name, args, ok=False, error=exc.message)
                raise

        return forward

    for tool_name in CAPABILITY_TOOLS:
        forwarder = make_forwarder(tool_name)
        keys = CAPABILITY_TOOLS[tool_name]
        desc = (
            f"Forward `{tool_name}` to the active agent "
            f"(args: {', '.join(keys) if keys else 'none'}). Requires select_agent."
        )
        mcp.tool(name=tool_name, description=desc)(forwarder)

    # -- server-only tools -------------------------------------------------

    @mcp.tool(name="list_agents", description="List known agents with online state and health.")
    async def list_agents() -> dict[str, Any]:
        ids = await _known_agent_ids(registry, store)
        agents = [await _agent_overview(i, registry, store) for i in ids]
        return {"active_agent": registry.active_agent, "agents": agents}

    @mcp.tool(name="select_agent", description="Set the active agent for forwarded tools.")
    async def select_agent(id: str) -> dict[str, Any]:
        try:
            agent = registry.select(id)
        except KeyError:
            # Allow selecting an agent known only from stored telemetry.
            if id in await store.known_agents():
                registry._active_agent = id  # noqa: SLF001 (intentional dev path)
                return {"active_agent": id, "online": False}
            raise
        return {"active_agent": agent.agent_id, "online": agent.online}

    @mcp.tool(name="fleet_overview", description="Per-agent rolled-up health for the dashboard.")
    async def fleet_overview() -> dict[str, Any]:
        ids = await _known_agent_ids(registry, store)
        agents = [await _agent_overview(i, registry, store) for i in ids]
        overall = health_rules.worst(*(a["overall"] for a in agents if a["overall"] != "unknown"))
        return {"overall": overall or "unknown", "agents": agents}

    @mcp.tool(name="agent_health", description="Per-section status/summary for one agent.")
    async def agent_health(id: str) -> dict[str, Any]:
        latest = await store.latest(id)
        snapshot = latest["snapshot"] if latest else None
        health = build_health(snapshot)
        return {
            "agent_id": id,
            "collected_at": latest["collected_at"] if latest else None,
            **health,
        }

    @mcp.tool(name="agent_snapshot", description="Latest stored snapshot (or one section).")
    async def agent_snapshot(id: str, section: str | None = None) -> dict[str, Any]:
        latest = await store.latest(id)
        if latest is None:
            return {"agent_id": id, "snapshot": None}
        snapshot = latest["snapshot"]
        if section is not None:
            return {
                "agent_id": id,
                "collected_at": latest["collected_at"],
                "section": section,
                "payload": snapshot.get(section),
            }
        return {
            "agent_id": id,
            "collected_at": latest["collected_at"],
            "snapshot": snapshot,
        }

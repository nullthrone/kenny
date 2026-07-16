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

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP

from . import health_rules
from .registry import AgentRegistry
from .store import EventStore, TelemetryStore
from .tunnel import AgentTunnel, ToolError
from .webfilter import WebFilterService, load_seed

logger = logging.getLogger("kenny.tools")

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
    "remotehelp_status": [],
    "remotehelp_start": [],
    "remotehelp_stop": [],
    "telemetry_collect": ["sections?"],
    "agent_update": ["version", "url", "sha256"],
    # Parental-controls enforcement (ADR-0026). apply/clear are mutating; status
    # is read-only. The server pre-merges the effective block set for apply.
    "webfilter_status": [],
    "webfilter_apply": ["domains", "doh_policy", "list_hash"],
    "webfilter_clear": [],
}


class CallLog:
    """Persistent log of forwarded tool calls (for the dashboard).

    Backed by :class:`~.store.EventStore` (kind='audit') when one is supplied;
    falls back to a bounded in-memory deque when ``event_store`` is ``None`` (so
    tests can use the log without a database).
    """

    def __init__(self, maxlen: int = 200, *, event_store: EventStore | None = None) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.event_store = event_store

    async def record(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        ok: bool,
        error: str | None = None,
    ) -> None:
        if self.event_store is not None:
            # Audit logging is a side effect of the call, not the call itself: a
            # transient write failure here (e.g. sqlite "database is locked" under
            # concurrent agent pushes) must not fail the tool call that already
            # succeeded against the agent. Log and swallow instead of propagating.
            try:
                await self.event_store.insert_audit(
                    agent_id=agent_id, tool=tool, ok=ok, error=error
                )
            except Exception:
                logger.warning(
                    "failed to persist audit log entry for %s -> %s",
                    tool,
                    agent_id,
                    exc_info=True,
                )
            return
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

    async def list(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.event_store is not None:
            rows = await self.event_store.query(kind="audit", limit=limit)
            return [
                {
                    "at": r["at"],
                    "agent_id": r["agent_id"],
                    "tool": r["tool"],
                    "ok": r["ok"],
                    "error": r["error"],
                }
                for r in rows
            ]
        return list(self._entries)[:limit]


class ScreenshotStore:
    """In-memory store of the latest screenshot per agent (for the dashboard)."""

    def __init__(self) -> None:
        self._latest: dict[str, dict[str, Any]] = {}

    def put(self, agent_id: str, image_b64: str, fmt: str = "png") -> None:
        self._latest[agent_id] = {
            "image_b64": image_b64,
            "format": fmt,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    def get(self, agent_id: str) -> dict[str, Any] | None:
        return self._latest.get(agent_id)

    def forget(self, agent_id: str) -> None:
        """Drop the cached screenshot for a removed host (ADR-0037)."""

        self._latest.pop(agent_id, None)


def build_health(
    snapshot: dict[str, Any] | None, *, agent_os: str = "windows"
) -> dict[str, Any]:
    """Run health rules over a stored snapshot (or empty when none).

    ``agent_os`` is the agent's OS family; it is forwarded to
    :func:`health_rules.evaluate_snapshot` so a non-Windows agent's Windows-only
    sections are not scored (ADR-0035). Defaults to ``windows`` for callers that
    have no agent context, preserving prior behavior.
    """

    if not snapshot:
        return {"overall": "unknown", "sections": {}}
    return health_rules.evaluate_snapshot(snapshot, agent_os=agent_os)


async def _agent_overview(
    agent_id: str, registry: AgentRegistry, store: TelemetryStore
) -> dict[str, Any]:
    agent = registry.get(agent_id)
    latest = await store.latest(agent_id)
    snapshot = latest["snapshot"] if latest else None
    agent_os = agent.os if agent else "windows"
    health = build_health(snapshot, agent_os=agent_os)
    flagged = [name for name, s in health["sections"].items() if s["status"] in ("warn", "crit")]
    return {
        "agent_id": agent_id,
        "online": bool(agent and agent.online),
        "os": agent_os,
        "meta": agent.meta if agent else {},
        "overall": health["overall"],
        "flagged_sections": flagged,
        "collected_at": latest["collected_at"] if latest else None,
    }


async def _known_agent_ids(registry: AgentRegistry, store: TelemetryStore) -> list[str]:
    ids = {a.agent_id for a in registry.list()}
    ids.update(await store.known_agents())
    return sorted(ids)


def _mcp_principal():
    """The authenticated principal for the current MCP HTTP request, if any.

    Returns ``None`` outside an HTTP request context (e.g. unit tests that call
    tools directly), in which case enforcement is skipped — the auth middleware
    still gates the real endpoint, and the shared-token principal is a superuser.
    """

    from fastmcp.server.dependencies import get_http_request

    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - no HTTP context (direct/in-proc call)
        return None
    return request.scope.get("kenny_principal") if request is not None else None


def _require_scope(principal, agent_id: str) -> None:
    """Raise if ``principal`` (a scoped ``user``) may not target ``agent_id``."""

    if principal is not None and not principal.may_see(agent_id):
        raise ToolError("forbidden", f"host {agent_id!r} is not in your scope")


def _require_role(principal, min_role: str) -> None:
    """Raise if ``principal`` lacks ``min_role`` (superuser > operator > user)."""

    if principal is not None and not principal.at_least(min_role):
        raise ToolError("forbidden", f"requires {min_role} role")


def _active_key(principal) -> str | None:
    """Per-caller active-agent key so concurrent MCP sessions don't collide."""

    return principal.active_key if principal is not None else None


def register_tools(
    mcp: FastMCP,
    *,
    registry: AgentRegistry,
    store: TelemetryStore,
    tunnel: AgentTunnel,
    call_log: CallLog,
    webfilter: WebFilterService | None = None,
) -> None:
    """Register all MCP tools on ``mcp``."""

    # -- forwarding capability tools --------------------------------------

    forward_logger = logging.getLogger("kenny.tools")

    def make_forwarder(tool_name: str):
        async def forward(args: dict[str, Any] | None = None) -> dict[str, Any]:
            """Forward this capability call to the active agent and return its result."""
            args = args or {}
            principal = _mcp_principal()
            agent_id = registry.require_active(_active_key(principal))
            # Defense in depth: the target is bound to this caller's selection, but
            # re-check scope so a user can never operate outside their hosts.
            _require_scope(principal, agent_id)
            timeout_s = float(args.get("timeout_s", 30))
            forward_logger.info("forward %s -> %s", tool_name, agent_id)
            try:
                result = await tunnel.send_request(agent_id, tool_name, args, timeout_s)
                await call_log.record(agent_id, tool_name, args, ok=True)
                return result
            except ToolError as exc:
                forward_logger.warning("forward %s -> %s failed: %s", tool_name, agent_id, exc.message)
                await call_log.record(agent_id, tool_name, args, ok=False, error=exc.message)
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
        principal = _mcp_principal()
        ids = await _known_agent_ids(registry, store)
        if principal is not None and principal.scoped:
            ids = [i for i in ids if i in principal.hosts]
        agents = [await _agent_overview(i, registry, store) for i in ids]
        return {"active_agent": registry.active_for(_active_key(principal)), "agents": agents}

    @mcp.tool(name="select_agent", description="Set the active agent for forwarded tools.")
    async def select_agent(id: str) -> dict[str, Any]:
        principal = _mcp_principal()
        _require_scope(principal, id)
        key = _active_key(principal)
        try:
            agent = registry.select(id, key=key)
        except KeyError:
            # Allow selecting an agent known only from stored telemetry.
            if id in await store.known_agents():
                if key is None:
                    registry._active_agent = id  # noqa: SLF001 (intentional dev path)
                else:
                    registry._active_by_key[key] = id  # noqa: SLF001
                return {"active_agent": id, "online": False}
            raise
        return {"active_agent": agent.agent_id, "online": agent.online}

    @mcp.tool(name="fleet_overview", description="Per-agent rolled-up health for the dashboard.")
    async def fleet_overview() -> dict[str, Any]:
        principal = _mcp_principal()
        ids = await _known_agent_ids(registry, store)
        if principal is not None and principal.scoped:
            ids = [i for i in ids if i in principal.hosts]
        agents = [await _agent_overview(i, registry, store) for i in ids]
        overall = health_rules.worst(*(a["overall"] for a in agents if a["overall"] != "unknown"))
        return {"overall": overall or "unknown", "agents": agents}

    @mcp.tool(name="agent_health", description="Per-section status/summary for one agent.")
    async def agent_health(id: str) -> dict[str, Any]:
        _require_scope(_mcp_principal(), id)
        latest = await store.latest(id)
        snapshot = latest["snapshot"] if latest else None
        agent = registry.get(id)
        health = build_health(snapshot, agent_os=agent.os if agent else "windows")
        return {
            "agent_id": id,
            "collected_at": latest["collected_at"] if latest else None,
            **health,
        }

    @mcp.tool(name="agent_snapshot", description="Latest stored snapshot (or one section).")
    async def agent_snapshot(id: str, section: str | None = None) -> dict[str, Any]:
        _require_scope(_mcp_principal(), id)
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

    # -- parental-controls (webfilter) server-only tools ------------------

    if webfilter is None:
        return

    async def _webfilter_overview(agent_id: str) -> dict[str, Any]:
        config = await webfilter.get_config(agent_id)
        custom = await webfilter.list_domains(agent_id)
        current_hash = await webfilter.current_list_hash(agent_id)
        applied_hash = config.get("applied_hash")
        return {
            "agent_id": agent_id,
            "config": config,
            "custom": custom,
            "seed_count": len(load_seed()),
            "external": webfilter.cache.stats(),
            "current_hash": current_hash,
            "drift": bool(applied_hash) and applied_hash != current_hash,
        }

    @mcp.tool(
        name="webfilter_get",
        description="Get the parental-controls config, custom list, and drift for an agent.",
    )
    async def webfilter_get(id: str) -> dict[str, Any]:
        _require_scope(_mcp_principal(), id)
        return await _webfilter_overview(id)

    @mcp.tool(
        name="webfilter_set",
        description=(
            "Update parental-controls config and/or the custom domain list for an agent "
            "(state-changing). Toggles: enabled, block_mode, use_external_adult, "
            "use_bypass_protection, doh_policy. Optional add_domain/remove_domain (+action)."
        ),
    )
    async def webfilter_set(
        id: str,
        enabled: bool | None = None,
        block_mode: bool | None = None,
        use_external_adult: bool | None = None,
        use_bypass_protection: bool | None = None,
        doh_policy: str | None = None,
        add_domain: str | None = None,
        remove_domain: str | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        principal = _mcp_principal()
        _require_role(principal, "operator")
        _require_scope(principal, id)
        await webfilter.set_config(
            id,
            enabled=enabled,
            block_mode=block_mode,
            use_external_adult=use_external_adult,
            use_bypass_protection=use_bypass_protection,
            doh_policy=doh_policy,
        )
        if add_domain:
            try:
                await webfilter.add_domain(id, add_domain, action or "block")
            except ValueError as exc:
                raise ToolError("bad_args", str(exc)) from exc
        if remove_domain:
            await webfilter.remove_domain(id, remove_domain)
        return await _webfilter_overview(id)

    @mcp.tool(
        name="webfilter_push",
        description=(
            "Push the effective parental-controls block list to an agent (state-changing): "
            "forwards webfilter_apply when block mode is on, else webfilter_clear."
        ),
    )
    async def webfilter_push(id: str) -> dict[str, Any]:
        principal = _mcp_principal()
        _require_role(principal, "operator")
        _require_scope(principal, id)
        config = await webfilter.get_config(id)
        args = await webfilter.build_apply(id)
        block_mode = bool(config["block_mode"])
        tool = "webfilter_apply" if block_mode else "webfilter_clear"
        call_args = args if block_mode else {}
        try:
            result = await tunnel.send_request(id, tool, call_args, 30)
            await call_log.record(id, tool, call_args, ok=True)
        except ToolError as exc:
            await call_log.record(id, tool, call_args, ok=False, error=exc.message)
            raise
        applied_at = str(result.get("applied_at") or datetime.now(timezone.utc).isoformat())
        await webfilter.set_applied_state(
            id,
            args["list_hash"] if block_mode else None,
            applied_at,
            bool(result.get("ok", True)),
        )
        return {"agent_id": id, "tool": tool, "result": result, "applied": call_args}

    @mcp.tool(
        name="web_activity_query",
        description="Query observed web domains for an agent (optionally flagged-only).",
    )
    async def web_activity_query(
        id: str, hours: int = 24, flagged_only: bool = False
    ) -> dict[str, Any]:
        _require_scope(_mcp_principal(), id)
        events = await webfilter.activity(id, hours=hours, flagged_only=flagged_only)
        return {"agent_id": id, "hours": hours, "events": events}

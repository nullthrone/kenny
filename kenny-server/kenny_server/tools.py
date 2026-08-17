"""MCP tool registration.

Two kinds of tools (names match ``docs/protocol.md`` § Tool catalog exactly):

* **Forwarding capability tools** — require an explicit ``agent_id`` argument
  naming the target host, and forward a ``request`` frame to it through the
  tunnel (see ADR-0038). ``agent_id`` is routing metadata: it is popped off the
  call's ``args`` before the wire frame is built, so it never reaches the agent
  and the wire contract is untouched.
* **Server-only tools** — ``list_agents``, ``select_agent``, ``fleet_overview``,
  ``agent_health``, ``agent_snapshot`` — read from the registry, store, and
  health rules; they are not forwarded to a single agent.

Every forwarded call is appended to an in-memory ``call_log`` for the dashboard
tool-call log.

``select_agent``/the registry's active-agent slot (ADR-0033) remain as an
advisory discovery/back-compat helper only — they no longer decide where a
forwarded MCP call lands (ADR-0038). Remote MCP clients (Claude Desktop,
claude.ai) send no reliable per-conversation identifier, so two concurrent
sessions authenticated with the same credential (PAT/OAuth token) would
otherwise share one sticky slot and silently clobber each other's selection.
Requiring ``agent_id`` on every forwarded call makes that race structurally
impossible: a call either names its host or fails closed.
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
from .webfilter import (
    ListTooLargeError,
    WebFilterService,
    describe_categories,
    load_seed,
    validate_categories,
)

logger = logging.getLogger("kenny.tools")

# OS-scoped capability tools: name -> the agent OS values that can serve it.
# Enforced in ``make_forwarder`` so a wrong-OS call is refused with an actionable
# message before ever reaching the tunnel (see docs/protocol.md § "OS-scoped
# tools").
#
# Account governance was pinned to ``windows`` in v0.15 and is deliberately no
# longer OS-scoped at all (ADR-0043): it is served on Windows and Linux alike, and
# what a *particular account on a particular host* can do is published per account
# in the ``local_accounts`` inventory's ``unsupported`` map. A whole-tool OS scope
# would be both coarser and less true. macOS keeps no implementation, so the seven
# tools are still listed here — with the two OSes that do serve them, which is what
# gives a macOS agent the same fast, actionable refusal it had before.
_OS_SCOPED_TOOLS: dict[str, frozenset[str]] = {
    "powershell_exec": frozenset({"windows"}),
    "shell_exec": frozenset({"linux", "macos"}),
    "account_set_enabled": frozenset({"windows", "linux"}),
    "account_set_admin": frozenset({"windows", "linux"}),
    "account_set_logon_rights": frozenset({"windows", "linux"}),
    "account_create": frozenset({"windows", "linux"}),
    "account_delete": frozenset({"windows", "linux"}),
    "account_session_action": frozenset({"windows", "linux"}),
    "password_policy_set": frozenset({"windows", "linux"}),
}

# The OS-scoped mirror of a key in ``_OS_SCOPED_TOOLS``, for error messages.
# Absent when the tool has no counterpart on the other family.
_OS_SCOPED_MIRROR: dict[str, str] = {
    "powershell_exec": "shell_exec",
    "shell_exec": "powershell_exec",
}


def supports_tool(tool_name: str, agent_os: str) -> bool:
    """Can an agent on ``agent_os`` serve ``tool_name``?

    The single answer for both the MCP forwarder and the dashboard's write route,
    so the two surfaces cannot refuse the same call differently (they did until
    ADR-0043: MCP refused pre-flight, the dashboard forwarded and surfaced the
    agent's refusal as a 502).
    """

    allowed = _OS_SCOPED_TOOLS.get(tool_name)
    return allowed is None or agent_os in allowed


# Minimum operator role for a forwarded capability tool. Absent means the
# default: seeing the host is enough.
#
# Account governance is the first forwarded family to need this (ADR-0042).
# Everything else in the catalog affects software, files or the network, all of
# which a scoped ``user`` may already reach; deciding who can sign in to a
# family PC — and being able to lock the household out by getting it wrong — is
# a different kind of authority.
_TOOL_MIN_ROLE: dict[str, str] = {
    "account_set_enabled": "operator",
    "account_set_admin": "operator",
    "account_set_logon_rights": "operator",
    "account_create": "operator",
    "account_delete": "operator",
    "account_session_action": "operator",
    "password_policy_set": "operator",
}

# Per-tool floor on the forwarding timeout, for tools whose normal path is
# slower than the 30 s default. ``account_session_action`` may show the
# signed-in user a warning and wait it out before acting (the agent caps that
# wait at 60 s), so the default would time out a perfectly healthy call.
_TOOL_MIN_TIMEOUT_S: dict[str, float] = {
    "account_session_action": 120.0,
}


# Forwarding capability tools: name -> ordered arg keys (optional keys end "?").
CAPABILITY_TOOLS: dict[str, list[str]] = {
    "powershell_exec": ["script", "timeout_s"],
    "shell_exec": ["command", "timeout_s"],
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
    # Parental-controls enforcement (ADR-0024). apply/clear are mutating; status
    # is read-only. The server pre-merges the effective block set for apply.
    "webfilter_status": [],
    "webfilter_apply": ["domains", "doh_policy", "list_hash"],
    "webfilter_clear": [],
    # Account governance (ADR-0042). `principal` is the SAM account name, which
    # local and Microsoft accounts share — there is deliberately no per-kind
    # variant of any of these. The inventory lives in the `local_accounts`
    # telemetry section, so there is no `account_list` tool.
    "account_set_enabled": ["principal", "enabled"],
    "account_set_admin": ["principal", "admin"],
    "account_set_logon_rights": ["principal", "deny"],
    "account_create": ["name", "password", "display_name?", "admin?"],
    "account_delete": ["principal", "remove_profile"],
    "account_session_action": ["principal", "action", "warn_seconds?"],
    "password_policy_set": ["min_length?", "max_age_days?", "lockout_threshold?"],
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
        """Drop the cached screenshot for a removed host (ADR-0033)."""

        self._latest.pop(agent_id, None)


def build_health(
    snapshot: dict[str, Any] | None, *, agent_os: str = "windows"
) -> dict[str, Any]:
    """Run health rules over a stored snapshot (or empty when none).

    ``agent_os`` is the agent's OS family; it is forwarded to
    :func:`health_rules.evaluate_snapshot` so a non-Windows agent's Windows-only
    sections are not scored (ADR-0031). Defaults to ``windows`` for callers that
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


def _split_keys(raw: str | None) -> list[str]:
    """Split a comma-separated MCP argument into trimmed, non-empty parts.

    MCP tool arguments stay flat scalars so the generated schema is trivial for
    a model to fill in correctly; category and weekday sets arrive as
    ``"a,b,c"``.
    """

    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _active_key(principal) -> str | None:
    """Per-caller active-agent key so concurrent MCP sessions don't collide."""

    return principal.active_key if principal is not None else None


def _resolve_target(principal, args: dict[str, Any]) -> str:
    """Routing target for a forwarded MCP call (ADR-0038).

    Requires an explicit ``agent_id`` in ``args`` and pops it off — it is
    routing metadata consumed here, never forwarded to the agent, so the wire
    ``request`` frame (and ``docs/protocol.md``'s "argument keys are exact")
    is unaffected. Fails closed (``ToolError``) rather than falling back to any
    shared/sticky selection: remote MCP clients carry no reliable
    per-conversation identifier, so a sticky slot keyed only by credential
    (PAT/OAuth token/session) can be shared by two unrelated concurrent
    conversations and silently clobbered (the reported race). The resolved
    target is always scope-checked since ``agent_id`` is unvalidated client
    input.
    """

    explicit = args.pop("agent_id", None)
    target = str(explicit).strip() if explicit else ""
    if not target:
        raise ToolError(
            "no_agent",
            "agent_id is required: name the target host for this call "
            "(call list_agents/select_agent to find it)",
        )
    _require_scope(principal, target)
    return target


def register_tools(
    mcp: FastMCP,
    *,
    registry: AgentRegistry,
    store: TelemetryStore,
    tunnel: AgentTunnel,
    call_log: CallLog,
    webfilter: WebFilterService | None = None,
    suppression: Any = None,
    ticket_rules: Any = None,
) -> None:
    """Register all MCP tools on ``mcp``."""

    # -- forwarding capability tools --------------------------------------

    forward_logger = logging.getLogger("kenny.tools")

    def make_forwarder(tool_name: str):
        required_os = _OS_SCOPED_TOOLS.get(tool_name)
        min_role = _TOOL_MIN_ROLE.get(tool_name)
        min_timeout_s = _TOOL_MIN_TIMEOUT_S.get(tool_name, 0.0)

        async def forward(args: dict[str, Any] | None = None) -> dict[str, Any]:
            """Forward this capability call to the named agent and return its result."""
            args = args or {}
            principal = _mcp_principal()
            agent_id = _resolve_target(principal, args)
            if min_role is not None:
                _require_role(principal, min_role)

            # OS-scoped shell tools (powershell_exec/shell_exec): refuse the wrong
            # one for this agent's OS before ever forwarding, with a message naming
            # the correct tool (docs/protocol.md § "OS-scoped tools").
            # Skipped when the agent isn't in the registry (e.g. named only from
            # stored telemetry) — the tunnel send fails as offline in that case.
            if required_os is not None:
                agent = registry.get(agent_id)
                if agent is not None and agent.os not in required_os:
                    mirror = _OS_SCOPED_MIRROR.get(tool_name)
                    message = (
                        f"agent {agent_id!r} is {agent.os}; {tool_name} requires "
                        f"{' or '.join(sorted(required_os))}"
                    )
                    if mirror is not None:
                        message += f", use {mirror} instead"
                    forward_logger.info("refused %s -> %s: %s", tool_name, agent_id, message)
                    await call_log.record(agent_id, tool_name, args, ok=False, error=message)
                    raise ToolError("unsupported", message)

            timeout_s = max(float(args.get("timeout_s", 30)), min_timeout_s)
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
            f"Forward `{tool_name}` to a specific agent "
            f"(args: {', '.join(keys) if keys else 'none'}, plus a required `agent_id` "
            "naming the target host — call list_agents/select_agent first to find it)."
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

    @mcp.tool(
        name="select_agent",
        description=(
            "Validate an agent id and remember it as your default (advisory only — "
            "every forwarded capability tool still requires its own `agent_id` "
            "argument naming the target host; this does not route calls for you)."
        ),
    )
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
        if snapshot is not None:
            # Annotate reliability events (category/severity/suspected_cause,
            # ADR-0026) before scoring, so the reliability reason names
            # the dominant pattern here too — not just in the dashboard — and a
            # caller never needs a manual diag_eventlog to judge it. Deferred
            # import avoids a module-load cycle (tools -> chat -> ... -> tools);
            # graceful no-key/failure fallback means this never blocks the tool.
            from .event_categories import annotate_snapshots

            await annotate_snapshots([snapshot])
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

    # -- reliability alarm suppression server-only tools (ADR-0041 / #166) --
    #
    # Server-held operator state, not a per-agent capability -- like
    # webfilter_get/web_activity_query, these never forward a request frame to
    # an agent, so `agent_id` here is an optional scope filter, not a routing
    # target (no `_resolve_target`/ADR-0038 concern). `agent_snapshot` above
    # already carries the `suppressed`/`suppressed_by` markers for free (the
    # TelemetryStore read-path hook), so a caller comparing a fresh breakdown
    # against these rules needs no extra round-trip.

    if suppression is not None:

        @mcp.tool(
            name="reliability_suppression_list",
            description=(
                "List reliability alarm suppression rules (read-only). Without "
                "agent_id, all rules; with it, fleet-wide + that host's rules."
            ),
        )
        async def reliability_suppression_list(agent_id: str | None = None) -> dict[str, Any]:
            principal = _mcp_principal()
            if agent_id:
                _require_scope(principal, agent_id)
                rules = suppression.rules(agent_id)
            else:
                rules = suppression.rules()
                if principal is not None and principal.scoped:
                    rules = [
                        r for r in rules if not r["agent_id"] or r["agent_id"] in principal.hosts
                    ]
            return {"rules": rules}

        @mcp.tool(
            name="reliability_suppression_add",
            description=(
                "Exclude a reliability event pattern (source+event_id) from severity "
                "scoring (state-changing). Counts and heatmaps stay unaffected. "
                "event_id is required; source empty/omitted matches any source with "
                "that id. agent_id empty/omitted suppresses fleet-wide."
            ),
        )
        async def reliability_suppression_add(
            event_id: int,
            source: str | None = None,
            agent_id: str | None = None,
            note: str | None = None,
        ) -> dict[str, Any]:
            principal = _mcp_principal()
            _require_role(principal, "operator")
            if agent_id:
                _require_scope(principal, agent_id)
            try:
                rules = await suppression.add(
                    event_id=event_id,
                    source=source or "",
                    agent_id=agent_id or "",
                    note=note or "",
                    created_by=getattr(principal, "username", "") or "",
                )
            except ValueError as exc:
                raise ToolError("bad_args", str(exc)) from exc
            return {"rules": rules}

        @mcp.tool(
            name="reliability_suppression_remove",
            description="Remove a reliability alarm suppression rule by id (state-changing).",
        )
        async def reliability_suppression_remove(rule_id: str) -> dict[str, Any]:
            principal = _mcp_principal()
            _require_role(principal, "operator")
            removed, rules = await suppression.remove(rule_id)
            return {"ok": True, "removed": removed, "rules": rules}

    # -- auto-ticket rules server-only tools (ticket_rules.py) --------------
    #
    # Server-held operator policy, not a per-agent capability -- like the
    # suppression trio above, this never forwards a request frame to an agent.
    # Operator-only on every tool, including the read: an alert-origin ticket
    # is itself operator-only (its requester_user_id is always None), so a
    # scoped `user` has no legitimate use for the rules that decide when one
    # opens, and listing them would leak fleet host names for no benefit.

    if ticket_rules is not None:

        @mcp.tool(
            name="ticket_rule_list",
            description=(
                "List auto-ticket rules: which alerts open a ticket automatically "
                "(read-only, operator+). Without agent_id, all rules; with it, "
                "fleet-wide + that host's rules."
            ),
        )
        async def ticket_rule_list(agent_id: str | None = None) -> dict[str, Any]:
            principal = _mcp_principal()
            _require_role(principal, "operator")
            if agent_id:
                _require_scope(principal, agent_id)
            return {"rules": ticket_rules.rules(agent_id or None)}

        @mcp.tool(
            name="ticket_rule_set",
            description=(
                "Add (or replace) a rule deciding whether an alert event opens a "
                "ticket (state-changing, operator+). event_type is one of health/"
                "offline/disk_forecast/change; decision is one of open_all/"
                "open_crit/never. section empty/omitted means any section; "
                "agent_id empty/omitted means fleet-wide."
            ),
        )
        async def ticket_rule_set(
            event_type: str,
            decision: str,
            section: str | None = None,
            agent_id: str | None = None,
            note: str | None = None,
        ) -> dict[str, Any]:
            principal = _mcp_principal()
            _require_role(principal, "operator")
            if agent_id:
                _require_scope(principal, agent_id)
            try:
                rules, warnings = await ticket_rules.add(
                    event_type=event_type,
                    decision=decision,
                    section=section or "",
                    agent_id=agent_id or "",
                    note=note or "",
                    created_by=getattr(principal, "username", "") or "",
                )
            except ValueError as exc:
                raise ToolError("bad_args", str(exc)) from exc
            return {"rules": rules, "warnings": warnings}

        @mcp.tool(
            name="ticket_rule_remove",
            description="Remove an auto-ticket rule by id (state-changing, operator+).",
        )
        async def ticket_rule_remove(rule_id: str) -> dict[str, Any]:
            principal = _mcp_principal()
            _require_role(principal, "operator")
            removed, rules = await ticket_rules.remove(rule_id)
            return {"ok": True, "removed": removed, "rules": rules}

    # -- parental-controls (webfilter) server-only tools ------------------

    if webfilter is None:
        return

    async def _webfilter_overview(agent_id: str) -> dict[str, Any]:
        config = await webfilter.get_config(agent_id)
        custom = await webfilter.list_domains(agent_id)
        applied_hash = config.get("applied_hash")
        # An over-cap list is a state the overview has to be able to *show*:
        # failing the read too would leave the operator with an error and no way
        # to see which category to turn off.
        current_hash: str | None = None
        oversize: dict[str, int] | None = None
        try:
            current_hash = await webfilter.current_list_hash(agent_id)
        except ListTooLargeError as exc:
            oversize = {"count": exc.count, "cap": exc.cap}
        return {
            "agent_id": agent_id,
            "config": config,
            "custom": custom,
            "seed_count": len(load_seed()),
            "external": webfilter.cache.stats(),
            "categories": describe_categories(),
            "schedule": await webfilter.schedule_state(agent_id),
            "current_hash": current_hash,
            "oversize": oversize,
            "drift": bool(applied_hash) and applied_hash != current_hash,
        }

    @mcp.tool(
        name="webfilter_get",
        description=(
            "Get the parental-controls config, category set, custom list, schedule "
            "state (which categories are in force now and when they revert), and "
            "drift for an agent."
        ),
    )
    async def webfilter_get(id: str) -> dict[str, Any]:
        _require_scope(_mcp_principal(), id)
        return await _webfilter_overview(id)

    @mcp.tool(
        name="webfilter_set",
        description=(
            "Update parental-controls config, the custom domain list, and/or the "
            "schedule for an agent (state-changing). Toggles: enabled, block_mode, "
            "use_external_adult, use_bypass_protection, doh_policy. Categories: a "
            "comma-separated set of category keys (replaces the enabled set). "
            "Optional add_domain/remove_domain (+action, +domain_category, so an "
            "entry applies only while that category is on). Schedule: pass "
            "window_days ('mon,tue' or 'daily'), window_start and window_end "
            "('21:00'), window_categories and optionally window_label/window_tz to "
            "add a window that adds those categories for that range; or "
            "remove_window with a window id."
        ),
    )
    async def webfilter_set(
        id: str,
        enabled: bool | None = None,
        block_mode: bool | None = None,
        use_external_adult: bool | None = None,
        use_bypass_protection: bool | None = None,
        doh_policy: str | None = None,
        categories: str | None = None,
        add_domain: str | None = None,
        remove_domain: str | None = None,
        action: str | None = None,
        domain_category: str | None = None,
        window_days: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        window_categories: str | None = None,
        window_label: str | None = None,
        window_tz: str | None = None,
        remove_window: str | None = None,
    ) -> dict[str, Any]:
        principal = _mcp_principal()
        _require_role(principal, "operator")
        _require_scope(principal, id)
        try:
            keys = (
                validate_categories(_split_keys(categories))
                if categories is not None
                else None
            )
        except ValueError as exc:
            raise ToolError("bad_args", str(exc)) from exc
        await webfilter.set_config(
            id,
            enabled=enabled,
            block_mode=block_mode,
            use_external_adult=use_external_adult,
            use_bypass_protection=use_bypass_protection,
            doh_policy=doh_policy,
            categories=list(keys) if keys is not None else None,
        )
        if add_domain:
            try:
                await webfilter.add_domain(
                    id, add_domain, action or "block", None, domain_category
                )
            except ValueError as exc:
                raise ToolError("bad_args", str(exc)) from exc
        if remove_domain:
            await webfilter.remove_domain(id, remove_domain)
        if window_days or window_start or window_end or window_categories:
            try:
                await webfilter.add_window(
                    id,
                    days=window_days or "daily",
                    start=window_start,
                    end=window_end,
                    categories=_split_keys(window_categories),
                    label=window_label or "",
                    tz=window_tz,
                )
            except ValueError as exc:
                raise ToolError("bad_args", str(exc)) from exc
        if remove_window:
            await webfilter.remove_window(id, remove_window)
        return await _webfilter_overview(id)

    @mcp.tool(
        name="webfilter_push",
        description=(
            "Push the effective parental-controls block list to an agent (state-changing): "
            "forwards webfilter_apply when block mode is on, else webfilter_clear. The "
            "list reflects any schedule window open right now."
        ),
    )
    async def webfilter_push(id: str) -> dict[str, Any]:
        principal = _mcp_principal()
        _require_role(principal, "operator")
        _require_scope(principal, id)
        config = await webfilter.get_config(id)
        try:
            args = await webfilter.build_apply(id)
        except ListTooLargeError as exc:
            # Refuse here rather than hand the agent a list it rejects with
            # `bad_args` anyway — the operator gets a count and a way out.
            raise ToolError("bad_args", str(exc)) from exc
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

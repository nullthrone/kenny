"""Offering the staged agent version to the agents that are behind it.

The server stages one agent binary per ``(os, arch, channel)``. An agent whose
reported version is older than what it would be handed is *outdated*. This module
answers three things about that, for every surface alike:

* **Who is behind** — :func:`outdated_agents`, a plain version comparison. Nothing
  here needs a model, and nothing here changes a machine.
* **Update one or all of them** — :func:`update_agent` / :func:`update_outdated`,
  built on :func:`~kenny_server.distribution.perform_agent_update`, the same
  call the dashboard's per-agent button makes. Each runs only when a person asked
  for it (a click, or a confirmed Ask kenny call); the agent's own confirm gate and
  kill switch still apply.
* **Tell the operator** — :class:`UpdateOffers`, one notification per staged version
  that lists every agent that has fallen behind, batched so a morning of
  reconnecting PCs is one message, not one per PC.

An offer is not consent. Rolling out unattended is the pinned campaign of
ADR-0040 and nothing here widens it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import agent_release, server_release
from .distribution import (
    ShareLinks,
    StagedBinary,
    agent_binary_path,
    perform_agent_update,
    staged_binary,
)
from .notify import Notification
from .registry import AgentRegistry
from .toolloop import AGENT_UPDATE_STAGED_TOOL, AGENT_UPDATE_STATUS_TOOL, ToolExecutor
from .tunnel import AgentTunnel, ToolError
from .webui.authz import guard, principal_of, visible_ids

logger = logging.getLogger("kenny.agent_updates")

__all__ = [
    "AgentUpdateTools",
    "OFFER_DELAY_S",
    "UpdateOffers",
    "UpdateRefused",
    "build_update_routes",
    "is_behind",
    "outdated_agents",
    "update_agent",
    "update_outdated",
]

#: How long after the first reconnect an offer waits, so the PCs that come back
#: together are announced together.
OFFER_DELAY_S = 90.0


class UpdateRefused(Exception):
    """An update this module will not start; ``code`` is stable, ``message`` is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_behind(current: str | None, staged: str, channel: str = "stable") -> bool:
    """Whether an agent on ``current`` is strictly older than the staged ``staged``.

    A version that does not parse is *unknown*, and unknown is never behind: an
    update is never offered on a guess (same rule as the server's own check).
    """

    if not current or not staged:
        return False
    return server_release.is_newer(staged, str(current), channel=channel)


async def _desired_channel(update_store: Any, agent_id: str) -> str:
    if update_store is None:
        return "stable"
    return await update_store.get_desired_channel(agent_id)


async def outdated_agents(registry: AgentRegistry, update_store: Any) -> list[dict[str, Any]]:
    """Every known agent behind the binary staged for its own ``(os, arch, channel)``.

    Online agents first, then by id. The channel is the agent's *desired* one
    (ADR-0048), so a host an operator just moved to dev is compared with the dev
    binary it is about to receive.
    """

    out: list[dict[str, Any]] = []
    for agent in registry.list():
        current = (agent.meta or {}).get("version")
        channel = await _desired_channel(update_store, agent.agent_id)
        staged = staged_binary(agent.os, agent.arch, channel)
        if staged is None or not is_behind(current, staged.version, channel):
            continue
        out.append(
            {
                "agent_id": agent.agent_id,
                "online": bool(agent.online),
                "os": agent.os,
                "arch": agent.arch,
                "channel": channel,
                "current_version": str(current),
                "staged_version": staged.version,
            }
        )
    out.sort(key=lambda a: (not a["online"], a["agent_id"]))
    return out


async def update_agent(
    *,
    registry: AgentRegistry,
    tunnel: AgentTunnel,
    share_links: ShareLinks,
    update_store: Any,
    agent_id: str,
    require_behind: bool = True,
) -> dict[str, Any]:
    """Send the staged binary to one online agent. Raises :class:`UpdateRefused` or ``ToolError``.

    The agent downloads from a short-lived nonce URL minted here, so neither the
    URL nor the digest ever passes through a model. ``require_behind`` refuses an
    agent that already runs the staged version; the dashboard's own per-agent
    button leaves it off so a person can still reinstall.
    """

    agent = registry.get(agent_id)
    if agent is None:
        raise UpdateRefused("unknown_agent", f"unknown agent {agent_id!r}")
    if not agent.online:
        raise UpdateRefused("offline", f"{agent_id} is offline")
    channel = await _desired_channel(update_store, agent_id)
    staged: StagedBinary | None = staged_binary(agent.os, agent.arch, channel)
    if staged is None:
        raise UpdateRefused(
            "no_staged_binary",
            f"no agent binary is staged for {agent.os}/{agent.arch} ({channel})",
        )
    current = (agent.meta or {}).get("version")
    if require_behind and not is_behind(current, staged.version, channel):
        raise UpdateRefused(
            "up_to_date",
            f"{agent_id} runs {current or 'an unknown version'}; "
            f"the staged version is {staged.version}, which is not newer",
        )
    result = await perform_agent_update(
        tunnel,
        share_links,
        agent_id,
        os_name=agent.os,
        arch=agent.arch,
        version=staged.version,
        binary_path=staged.path,
        sha256=staged.sha256,
    )
    return {
        "agent_id": agent_id,
        "from_version": current,
        "version": staged.version,
        "sha256": staged.sha256,
        "result": result,
    }


async def update_outdated(
    *,
    registry: AgentRegistry,
    tunnel: AgentTunnel,
    share_links: ShareLinks,
    update_store: Any,
    only: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Update every online outdated agent (or those of ``only``), one result per agent.

    One agent failing never stops the others; an offline agent is reported as
    skipped, not as a failure, and gets its offer again when it reconnects.
    """

    wanted = set(only) if only is not None else None
    results: list[dict[str, Any]] = []
    for row in await outdated_agents(registry, update_store):
        agent_id = row["agent_id"]
        if wanted is not None and agent_id not in wanted:
            continue
        if not row["online"]:
            results.append({"agent_id": agent_id, "status": "skipped", "reason": "offline"})
            continue
        try:
            done = await update_agent(
                registry=registry,
                tunnel=tunnel,
                share_links=share_links,
                update_store=update_store,
                agent_id=agent_id,
            )
        except UpdateRefused as exc:
            results.append({"agent_id": agent_id, "status": "skipped", "reason": exc.code})
        except ToolError as exc:
            results.append({"agent_id": agent_id, "status": "failed", "error": exc.message})
        except Exception as exc:  # noqa: BLE001 - one agent must not stop the rest
            results.append({"agent_id": agent_id, "status": "failed", "error": str(exc)})
        else:
            results.append(
                {
                    "agent_id": agent_id,
                    "status": "sent",
                    "from_version": done["from_version"],
                    "version": done["version"],
                }
            )
    return {"results": results, "sent": sum(1 for r in results if r["status"] == "sent")}


class UpdateOffers:
    """Announces outdated agents to the operator, once per agent and staged version.

    Best-effort by design, like every notification (ADR-0027): a failure here is
    logged and never reaches the agent connection or the detection loop. The
    memory of what was already announced lives in the process; after a restart one
    outdated agent can be announced again, which is the safe direction.
    """

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        update_store: Any,
        notifiers: Callable[[], Sequence[Any]],
        delay_s: float = OFFER_DELAY_S,
    ) -> None:
        self._registry = registry
        self._update_store = update_store
        self._notifiers = notifiers
        self._delay_s = delay_s
        self._announced: dict[str, str] = {}
        self._pending: asyncio.Task[None] | None = None

    async def on_agent_connect(self, agent_id: str) -> None:
        """Tunnel hook: schedule one batched offer. Never raises."""

        self.schedule()

    def schedule(self) -> None:
        if self._pending is not None and not self._pending.done():
            return
        self._pending = asyncio.ensure_future(self._after_delay())

    async def _after_delay(self) -> None:
        try:
            await asyncio.sleep(self._delay_s)
            await self.announce()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an offer must never break its caller
            logger.exception("update offer failed")

    async def announce(self) -> Notification | None:
        """Send one notification for agents not yet announced at their staged version."""

        rows = await outdated_agents(self._registry, self._update_store)
        # Forget agents that caught up, so a later release is announced afresh.
        behind = {r["agent_id"] for r in rows}
        for known in [a for a in self._announced if a not in behind]:
            del self._announced[known]
        fresh = [r for r in rows if self._announced.get(r["agent_id"]) != r["staged_version"]]
        if not fresh:
            return None
        lines = [f"{r['agent_id']}: {r['current_version']} -> {r['staged_version']}" for r in fresh]
        staged = sorted({r["staged_version"] for r in fresh})
        note = Notification(
            title=f"{len(fresh)} agent(s) behind the staged version {', '.join(staged)}",
            body="\n".join(lines)
            + "\nUpdate them from the Fleet view, or ask kenny to update the outdated agents.",
            priority="low",
            tags=["arrow_up"],
            kind="change",
            event_type="agent_outdated",
        )
        delivered = False
        for notifier in self._notifiers():
            try:
                await notifier.send(note)
                delivered = True
            except Exception:  # noqa: BLE001 - one dead channel must not hide the rest
                logger.exception(
                    "offer delivery via %s failed", getattr(notifier, "name", type(notifier).__name__)
                )
        if delivered:
            for r in fresh:
                self._announced[r["agent_id"]] = r["staged_version"]
        return note


class AgentUpdateTools:
    """``agent_update_status`` and ``agent_update_staged``: the staged-update tools.

    Registered on Ask kenny's executor and, through :func:`~kenny_server.tools.register_tools`,
    on the MCP endpoint, so a Claude client gets the same way to update an agent as the
    dashboard does instead of the raw ``agent_update`` it cannot fill in.

    The status tool is a read. The update tool is a ``normal_change`` the operator
    confirms like any other, and its ``version`` argument is what they confirm: the
    call refuses unless the staged version for that host is exactly that, so a
    release staged between the confirmation card and the run never ships unseen.
    The model passes neither a URL nor a digest — it cannot know either — and
    never sees them: both come from the staged binary, as in the dashboard.
    """

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        tunnel: AgentTunnel,
        share_links: ShareLinks,
        update_store: Any,
        call_log: Any,
    ) -> None:
        self._registry = registry
        self._tunnel = tunnel
        self._share_links = share_links
        self._update_store = update_store
        self._call_log = call_log

    def register_tools(self, executor: ToolExecutor) -> None:
        executor.register_server_tool(AGENT_UPDATE_STATUS_TOOL, self.status)
        executor.register_server_tool(AGENT_UPDATE_STAGED_TOOL, self.update)

    @staticmethod
    def mcp_session(principal: Any) -> Any:
        """A session stand-in for a caller with no conversation: its principal, no selected agent.

        MCP clients carry no reliable per-conversation identity (ADR-0038), so an update
        over MCP always names its agent; nothing here falls back to a selection.
        """

        return SimpleNamespace(agent_id="", principal=principal)

    def _visible(self, session: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        principal = getattr(session, "principal", None)
        if principal is None or not getattr(principal, "scoped", False):
            return rows
        return [r for r in rows if principal.may_see(r["agent_id"])]

    async def status(self, args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        """Handle ``agent_update_status``: the staged versions and who is behind them."""

        staged: list[dict[str, str]] = []
        for os_name, arch in agent_release.SUPPORTED_TARGETS:
            path = agent_binary_path(os_name, arch)
            if path is not None:
                staged.append(
                    {
                        "os": os_name,
                        "arch": arch,
                        "channel": "stable",
                        "version": agent_release.resolve_agent_version(path),
                    }
                )
        rows = await outdated_agents(self._registry, self._update_store)
        return {"staged": staged, "outdated": self._visible(session, rows)}

    async def update(self, args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        """Handle ``agent_update_staged`` for one agent, after the operator confirmed it."""

        explicit = args.get("agent_id")
        target = (str(explicit).strip() if explicit else "") or str(
            getattr(session, "agent_id", "") or ""
        )
        if not target:
            raise ToolError("no_agent", "no agent selected; select one or pass agent_id")
        principal = getattr(session, "principal", None)
        if principal is not None and not principal.may_see(target):
            raise ToolError("forbidden", "host not in your scope")
        confirmed = str(args.get("version") or "").strip()
        if not confirmed:
            raise ToolError("bad_args", "version is required: the staged version being confirmed")
        actor = getattr(principal, "username", None) if principal is not None else None
        audit_args = {"version": confirmed}
        try:
            agent = self._registry.get(target)
            if agent is not None:
                channel = await _desired_channel(self._update_store, target)
                staged = staged_binary(agent.os, agent.arch, channel)
                if staged is not None and staged.version != confirmed:
                    raise ToolError(
                        "version_mismatch",
                        f"the staged version for {target} is {staged.version}, not {confirmed}; "
                        "check agent_update_status and ask again",
                    )
            try:
                done = await update_agent(
                    registry=self._registry,
                    tunnel=self._tunnel,
                    share_links=self._share_links,
                    update_store=self._update_store,
                    agent_id=target,
                )
            except UpdateRefused as exc:
                raise ToolError(exc.code, exc.message) from None
        except ToolError as exc:
            await self._call_log.record(
                target, AGENT_UPDATE_STAGED_TOOL, audit_args, ok=False, error=exc.message, actor=actor
            )
            raise
        await self._call_log.record(target, AGENT_UPDATE_STAGED_TOOL, audit_args, ok=True, actor=actor)
        return done


def build_update_routes(
    *,
    registry: AgentRegistry,
    tunnel: AgentTunnel,
    share_links: ShareLinks,
) -> list[Route]:
    """``GET /api/agents/outdated`` and ``POST /api/agents/update-outdated`` (operator).

    The update route mints download nonces and pushes a binary, so it is a
    provisioning route like ``/api/agents/{id}/update`` and carries the same role.
    """

    def _store(request: Request) -> Any:
        return getattr(request.app.state, "update_store", None)

    async def outdated(request: Request) -> JSONResponse:
        rows = await outdated_agents(registry, _store(request))
        # A scoped user sees only the hosts they are scoped to.
        principal = principal_of(request)
        if principal is not None:
            allowed = set(visible_ids(principal, [r["agent_id"] for r in rows]))
            rows = [r for r in rows if r["agent_id"] in allowed]
        return JSONResponse({"agents": rows})

    async def update_all(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an empty body means "all of them"
            body = {}
        only = body.get("agents") if isinstance(body, dict) else None
        if only is not None and not (
            isinstance(only, list) and all(isinstance(a, str) for a in only)
        ):
            return JSONResponse({"error": "agents must be a list of agent ids"}, status_code=400)
        result = await update_outdated(
            registry=registry,
            tunnel=tunnel,
            share_links=share_links,
            update_store=_store(request),
            only=only,
        )
        return JSONResponse({"ok": True, **result})

    op = {"min_role": "operator"}
    return [
        Route("/api/agents/outdated", guard(outdated)),
        Route("/api/agents/update-outdated", guard(update_all, **op), methods=["POST"]),
    ]


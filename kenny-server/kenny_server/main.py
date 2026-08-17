"""Compose the whole server into one ASGI app on one port.

Mounts:

* the FastMCP **Streamable HTTP** MCP endpoint at ``/mcp`` (ADR 0006),
* the agent tunnel WebSocket at ``/agent/ws`` (``docs/protocol.md`` § Transport),
* the dashboard ``/api/*`` JSON routes and the static web UI at ``/``.

``build_app`` wires shared singletons (registry, store, tunnel, call log) and
chains the MCP app's lifespan with the telemetry store's connect/prune lifecycle.
``run`` is the ``kenny-server`` script entrypoint. Host/port come from env
(``KENNY_HOST`` / ``KENNY_PORT``); the SQLite path from ``KENNY_DB_PATH``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, AsyncIterator

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route, WebSocketRoute

from . import agent_release
from .alerting import AlertEngine
from .config import Settings
from .auth import (
    OperatorAuthMiddleware,
    build_auth_routes,
    load_operator_token,
    load_operator_tokens,
)
from .backup import BackupManager, apply_pending_restore
from .chat import ChatSessions
from .discord_adapter import DiscordPyGateway, GatewayUnavailable
from .discord_identity import DiscordIdentityStore
from .discord_service import SLASH_COMMANDS, DiscordService
from .distribution import ShareLinks, build_download_routes
from .keystore import KeyStore
from .logging_config import StoreLogHandler, configure_logging, drain_log_queue
from .notify import Notification, NotifierProvider
from .oauth import build_oauth_routes
from .oauthstore import OAuthStore
from .policy import PolicyEngine
from .registry import AgentRegistry
from .reliability_suppression import SuppressionList
from .store import (
    AlertStateStore,
    BackupTargetStore,
    ChatHistoryStore,
    EventStore,
    PolicyStore,
    ReliabilitySuppressionStore,
    SettingsStore,
    TelemetryStore,
    TicketRuleStore,
    UpdateStore,
    WebFilterStore,
)
from .ticket_assistant import TicketAssistant
from .ticket_rules import TicketRuleList
from .ticketstore import TicketStore
from .tickets import TicketService, ticket_sweep_loop
from .tokenstore import AgentTokenStore
from .toolloop import ToolExecutor
from .tools import CallLog, ScreenshotStore, register_tools
from .tunnel import AgentTunnel
from .update_manager import UpdateManager, update_check_loop
from .userstore import UserStore
from .webfilter import ExternalListCache, WebFilterService
from .webui import _anthropic_client, build_api_routes, build_chat_routes
from .webui.authz import guard
from .webui.inbox import build_inbox_routes
from .webui.tickets import build_ticket_routes
from .webui.users import build_user_routes


async def _webfilter_refresh_loop(
    cache: ExternalListCache, settings: Settings, interval_s: int, initial_delay_s: float
) -> None:
    """Periodically refresh the external adult/bypass lists (best-effort)."""

    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            await cache.refresh_all()
        except Exception:  # noqa: BLE001 - never let the loop die
            logging.getLogger("kenny.webfilter").exception("external list refresh failed")
        # Re-read the cadence each pass so a dashboard change retimes the loop.
        interval = settings.get("KENNY_WEBFILTER_REFRESH_SECS")
        await asyncio.sleep(interval if interval and interval > 0 else interval_s)


async def _backup_loop(
    backup_mgr: BackupManager, settings: Settings, interval_s: int, initial_delay_s: float
) -> None:
    """Periodically create a fresh DB snapshot and fan it out (best-effort)."""

    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            await backup_mgr.create("auto")
        except Exception:  # noqa: BLE001 - never let the loop die
            logging.getLogger("kenny.backup").exception("periodic backup failed")
        # Re-read the cadence each pass so a dashboard change retimes the loop.
        interval = settings.get("KENNY_BACKUP_INTERVAL_SECS")
        await asyncio.sleep(interval if interval and interval > 0 else interval_s)


def _guild_ids(raw: Any) -> frozenset[str]:
    """Parse ``KENNY_DISCORD_GUILD_IDS`` into an allowlist (empty = deny all)."""

    return frozenset(part.strip() for part in str(raw or "").split(",") if part.strip())


async def _discord_loop(service: DiscordService) -> None:
    """Connect the gateway and consume its events, isolated from the server.

    Every failure mode ends this task and nothing else: a missing optional
    dependency is a single WARNING (the operator asked for a surface that is not
    installed), and anything else is logged with a traceback. The server, its
    tickets and its dashboard keep running either way.
    """

    log = logging.getLogger("kenny.discord")
    try:
        await service.gateway.start()
    except GatewayUnavailable as exc:
        # Reaching here means the operator asked for the surface and the runtime
        # cannot provide it, so this is a misconfiguration rather than a quiet
        # opt-out — record it where /api/discord/status will show it.
        service.startup_error = str(exc)
        log.warning("Discord surface disabled: %s", exc)
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - never take the server down with the bot
        service.startup_error = f"gateway failed to start: {exc}"
        log.exception("Discord gateway failed to start; the surface stays off")
        return
    # Guild-scoped registration propagates immediately (a global registration
    # can take up to an hour) and is safe to repeat on every startup — Discord
    # treats it as an idempotent bulk replace, so re-registering the same set
    # is a no-op server-side. register_commands is documented to never raise
    # (a failure is logged as a warning by the gateway), but that is a
    # contract on the implementation, not the type -- wrapped here too so a
    # violation of it (any DiscordGateway, present or future) still can't
    # cost the ticket surface below, which matters far more than commands.
    try:
        for guild_id in service.guild_ids:
            commands = list(SLASH_COMMANDS)
            await service.gateway.register_commands(guild_id=guild_id, commands=commands)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never take the ticket surface down over this
        log.exception("registering Discord slash commands failed; continuing without them")
    try:
        await service.run()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never take the server down with the bot
        log.exception("Discord event loop stopped")


def build_app(db_path: str | None = None, *, client_factory: Any = _anthropic_client) -> Starlette:
    """Build and return the composed ASGI application.

    ``client_factory`` constructs the Anthropic client used by both the
    dashboard chat routes and the Discord surface; it is injected so tests need
    no API key (see ``webui._anthropic_client``).
    """

    db_path = db_path or os.environ.get("KENNY_DB_PATH", "kenny.sqlite")

    # Runtime settings: the operator's DB overrides resolve over env then coded
    # defaults. Consumers below are handed ``settings`` so live knobs (alerting,
    # web filter, chat model, log level) take effect without a restart.
    settings_store = SettingsStore(db_path)
    settings = Settings(settings_store)

    token_store = AgentTokenStore(db_path)
    key_store = KeyStore(db_path)
    user_store = UserStore(db_path)
    oauth_store = OAuthStore(db_path)
    registry = AgentRegistry(token_store=token_store, key_store=key_store)
    store = TelemetryStore(db_path)
    event_store = EventStore(db_path)
    # Reliability alarm suppression (ADR-0041 / issue #166): an operator-authored
    # rule table + an in-memory mirror consulted synchronously by every health
    # read. Installed as `store`'s read-path annotator so every consumer of
    # TelemetryStore (alerting, digest, the fleet list, the dashboard, MCP) sees
    # suppression without each of them opting in individually — see
    # reliability_suppression.py.
    suppression_store = ReliabilitySuppressionStore(db_path)
    suppression = SuppressionList(suppression_store)
    store.annotate = suppression.mark
    # Auto-ticket rules (see ticket_rules.py): which alerts open a ticket is operator
    # policy, not a hardcoded predicate. Same shape as the suppression rules
    # above -- an operator-authored table + an in-memory mirror consulted
    # synchronously by the alert engine on every dispatch. An empty table
    # reproduces the coded default exactly (every genuine alert opens
    # a ticket, nothing else does) -- see ticket_rules.py.
    ticket_rule_store = TicketRuleStore(db_path)
    ticket_rules = TicketRuleList(ticket_rule_store)
    # Shared-catalog mirror + operator deny rules (ADR-0020). The engine loads the
    # catalog at construction and never raises if it is missing (fail-open).
    policy_store = PolicyStore(db_path)
    policy_engine = PolicyEngine()
    # Parental controls (ADR-0024): per-host store + external-list cache under a
    # dir derived from the DB path, wrapped in the service the tunnel/API/tools use.
    webfilter_store = WebFilterStore(db_path)
    cache_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    webfilter_cache = ExternalListCache(cache_dir, settings=settings)
    webfilter = WebFilterService(webfilter_store, webfilter_cache)
    # Backup/restore (ADR: server DB backup/restore): a local snapshot dir is
    # always active (that's what solves the Syncthing lock-contention problem);
    # remote fan-out targets are operator-configured via backup_target_store.
    backup_target_store = BackupTargetStore(db_path)
    backup_mgr = BackupManager(
        db_path, backup_target_store, backup_dir=settings.get("KENNY_BACKUP_DIR") or None
    )
    tunnel = AgentTunnel(
        registry,
        store,
        event_store,
        policy_engine=policy_engine,
        policy_store=policy_store,
        webfilter=webfilter,
    )
    call_log = CallLog(event_store=event_store)
    screenshots = ScreenshotStore()
    chat_history_store = ChatHistoryStore(db_path)
    chat_sessions = ChatSessions(store=chat_history_store)
    share_links = ShareLinks()
    # Scheduled update detection + operator-approved rollout (ADR-0040). Built
    # after `tunnel`/`share_links` (it calls through both) but `tunnel` needs
    # its on-connect hook wired the other way around, so the tunnel is
    # patched with the manager's bound method right after construction —
    # avoids a constructor-level cycle between the two.
    update_store = UpdateStore(db_path)
    update_mgr = UpdateManager(
        db_path=db_path,
        store=update_store,
        registry=registry,
        tunnel=tunnel,
        share_links=share_links,
        settings=settings,
    )
    tunnel.on_agent_online = update_mgr.on_agent_connect
    # Tickets: the ITSM record an assisted change hangs off. Store, lifecycle
    # service and Discord identity mapping are built unconditionally — the
    # ticket surface is fully functional on a server that has no Discord at all.
    # The two lifetime knobs are read again after ``settings.load()`` below so a
    # dashboard override applies from this boot.
    ticket_store = TicketStore(db_path)
    ticket_service = TicketService(
        ticket_store,
        approval_ttl_secs=int(settings.get("KENNY_TICKET_APPROVAL_TTL_SECS")),
        autoclose_secs=int(settings.get("KENNY_TICKET_AUTOCLOSE_SECS")),
        stall_nudge_secs=int(settings.get("KENNY_TICKET_STALL_NUDGE_SECS")),
        stall_giveup_secs=int(settings.get("KENNY_TICKET_STALL_GIVEUP_SECS")),
    )
    discord_identities = DiscordIdentityStore(db_path)

    async def open_alert_ticket(note: Notification) -> None:
        """Open the ticket an alert asks for (ADR-0027 stays best-effort).

        ``origin='alert'`` and no requester: nobody asked for it, so it has no
        owner and is operator-only by the ticket API's own listing rule. The
        alerting agent is frozen as the ticket's target the same way a Discord
        requester's host is.
        """

        await ticket_service.create(
            title=note.title,
            origin="alert",
            requester_user_id=None,
            agent_id=note.agent_id,
            priority="high" if note.priority in ("high", "urgent") else "normal",
            category="alert",
            summary=note.body,
            actor="system",
            reason="opened from an alert",
        )

    # Push alerting (ADR-0027): transition detection over the health rules,
    # delivered best-effort on the configured channels (possibly none).
    alert_state = AlertStateStore(db_path)
    # The channels are *not* resolved here. The provider is asked again at every
    # dispatch and reads them through ``settings`` (DB > env > default), so a
    # channel added or cleared in the dashboard applies to the next alert
    # without a restart (ADR-0054). Building it before ``settings.load()`` is
    # deliberate and safe: nothing is read until the first notification.
    notifier_provider = NotifierProvider(settings=settings)
    # Cadence/cooldown/digest are read live from ``settings`` (DB > env >
    # default) on every pass; no env snapshot is baked in here.
    alert_engine = AlertEngine(
        store=store,
        alert_state=alert_state,
        event_store=event_store,
        registry=registry,
        notifier_provider=notifier_provider,
        settings=settings,
        # (store, settings_key) pairs -- only ``store`` (snapshots) has an
        # operator-facing retention setting so far (ADR-0051): it dominates
        # this database's size (~90 KB/row). The rest keep pruning on their
        # own hardcoded default until a key is added for them too.
        prunables=[
            (store, "KENNY_TELEMETRY_RETENTION_DAYS"),
            (event_store, None),
            (webfilter_store, None),
            (ticket_store, None),
            (discord_identities, None),
        ],
        open_ticket=open_alert_ticket,
        ticket_rules=ticket_rules,
    )

    # The ticket assistant (dashboard chat +, if configured, Discord) is built
    # whenever a usable Anthropic client exists — independent of whether a
    # Discord bot token is set. This is the one place both surfaces' turns are
    # actually driven from; a server with no API key gets neither.
    ticket_client: Any = None
    try:
        ticket_client = client_factory()
    except Exception as exc:  # noqa: BLE001 - e.g. no ANTHROPIC_API_KEY
        logging.getLogger("kenny.tickets").info(
            "the ticket assistant is disabled: no usable Anthropic client (%s)", exc
        )
    ticket_assistant: TicketAssistant | None = None
    if ticket_client is not None:
        ticket_assistant = TicketAssistant(
            tickets=ticket_service,
            users=user_store,
            executor=ToolExecutor(
                registry=registry,
                store=store,
                tunnel=tunnel,
                call_log=call_log,
                screenshots=screenshots,
            ),
            client=ticket_client,
            model=str(settings.get("KENNY_CHAT_MODEL")),
            max_turns_per_ticket=int(settings.get("KENNY_DISCORD_MAX_TURNS_PER_TICKET")),
            approval_ttl_secs=int(settings.get("KENNY_TICKET_APPROVAL_TTL_SECS")),
        )

    # Discord bot surface (optional). The service is constructed only when a bot
    # token exists — an env-only secret, so its presence is already known here —
    # and started only when KENNY_DISCORD_ENABLED is set, which is re-read in the
    # lifespan after the operator's DB overrides load. Constructing
    # ``DiscordPyGateway`` imports nothing: discord.py is imported lazily inside
    # ``start()``, so a server without the optional dependency builds normally.
    # It shares the assistant above rather than owning its own model client —
    # ``KENNY_DISCORD_MODEL``, when set, only overrides which model a
    # Discord-driven turn uses (threaded through as a per-call
    # ``model_override``, never mutating the shared assistant's own model).
    discord_token = str(settings.get("KENNY_DISCORD_BOT_TOKEN") or "").strip()
    discord_service: DiscordService | None = None
    if discord_token and ticket_assistant is None:
        logging.getLogger("kenny.discord").warning(
            "Discord surface disabled: no usable Anthropic client"
        )
    if discord_token and ticket_assistant is not None:
        guild_allowlist = _guild_ids(settings.get("KENNY_DISCORD_GUILD_IDS"))
        discord_service = DiscordService(
            gateway=DiscordPyGateway(
                token=discord_token, guild_allowlist=guild_allowlist
            ),
            identities=discord_identities,
            tickets=ticket_service,
            users=user_store,
            executor=ticket_assistant.executor,
            assistant=ticket_assistant,
            guild_ids=guild_allowlist,
            support_channel_id=str(settings.get("KENNY_DISCORD_SUPPORT_CHANNEL_ID")) or None,
            operator_channel_id=str(settings.get("KENNY_DISCORD_OPERATOR_CHANNEL_ID")) or None,
            private_threads=bool(settings.get("KENNY_DISCORD_PRIVATE_THREADS")),
            rate_limit_per_hour=int(settings.get("KENNY_DISCORD_RATE_LIMIT_PER_USER_HOUR")),
            model_override=str(settings.get("KENNY_DISCORD_MODEL") or "").strip() or None,
        )

    mcp = FastMCP("kenny")
    register_tools(
        mcp,
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        webfilter=webfilter,
        suppression=suppression,
        ticket_rules=ticket_rules,
    )
    # mcp_app owns "/mcp" internally and is mounted at the app root below (not
    # re-prefixed with another "/mcp"). A Mount always requires a trailing slash
    # to match its bare prefix (Starlette redirects "/mcp" -> "/mcp/" otherwise,
    # via a 307 that MCP clients are not guaranteed to follow/replay correctly);
    # mounting the already-self-pathed app at root avoids that redirect entirely
    # so a bare POST /mcp — what OAuth discovery, the resource URL, and the 401
    # challenge all advertise — resolves directly on the first request.
    mcp_app = mcp.http_app(path="/mcp")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Apply any staged restore *before* any store opens a connection to the
        # DB file (see backup.py: ~11 stores hold open aiosqlite connections
        # once running, so the swap can only happen here, at the very start).
        applied = apply_pending_restore(db_path)
        if applied:
            logging.getLogger("kenny.backup").info("restored from backup: %s", applied)
        await settings_store.connect()
        # Load operator overrides and re-apply live apply-hooks (e.g. log level)
        # before anything else reads config.
        await settings.load()
        await store.connect()
        await token_store.connect()
        await key_store.connect()
        await user_store.connect()
        await oauth_store.connect()
        await event_store.connect()
        await policy_store.connect()
        await webfilter_store.connect()
        await suppression_store.connect()
        await ticket_rule_store.connect()
        await chat_history_store.connect()
        await alert_state.connect()
        await backup_target_store.connect()
        await update_store.connect()
        await ticket_store.connect()
        await discord_identities.connect()
        # Ticket lifetimes are "live" settings, but the service and the store are
        # constructed before the DB overrides are readable — re-read them here so
        # a dashboard override is in force from the first sweep of this boot.
        ticket_service.approval_ttl_secs = int(settings.get("KENNY_TICKET_APPROVAL_TTL_SECS"))
        ticket_service.autoclose_secs = int(settings.get("KENNY_TICKET_AUTOCLOSE_SECS"))
        ticket_service.stall_nudge_secs = int(settings.get("KENNY_TICKET_STALL_NUDGE_SECS"))
        ticket_service.stall_giveup_secs = int(settings.get("KENNY_TICKET_STALL_GIVEUP_SECS"))
        ticket_store.run_retention_days = int(settings.get("KENNY_TICKET_RETENTION_DAYS"))
        # Same reasoning for telemetry retention (ADR-0051): re-read before the
        # boot-time prune below, so a dashboard override applies from this
        # boot's first sweep instead of only from the next periodic pass.
        store.retention_days = int(settings.get("KENNY_TELEMETRY_RETENTION_DAYS"))
        if applied:
            await event_store.insert_alert(
                agent_id=None,
                message=f"database restored from backup {applied} on boot",
                level="warning",
                fields={"name": applied},
            )
        # Load persisted operator rules into the mirror engine at startup.
        policy_engine.set_operator_rules(await policy_store.list())
        # Load persisted reliability suppression rules into their mirror too
        # (ADR-0041 / issue #166) -- before any health evaluation can run.
        await suppression.load()
        # Load persisted auto-ticket rules before the alert loop
        # below can dispatch a single notification.
        await ticket_rules.load()
        await store.prune()
        await event_store.prune()
        await webfilter_store.prune()
        await oauth_store.prune_expired()
        # Periodically refresh the external adult/bypass lists (ADR-0024). The
        # initial fetch is delayed so short-lived test app instances never reach
        # out; set KENNY_WEBFILTER_REFRESH_SECS=0 to disable entirely. The
        # enable/disable gate is decided here at startup (a "restart" decision);
        # the cadence itself is re-read live inside the loop.
        refresh_secs = int(settings.get("KENNY_WEBFILTER_REFRESH_SECS"))
        webfilter_task: asyncio.Task | None = None
        if refresh_secs > 0:
            initial_delay = float(settings.get("KENNY_WEBFILTER_INITIAL_REFRESH_DELAY"))
            webfilter_task = asyncio.create_task(
                _webfilter_refresh_loop(webfilter_cache, settings, refresh_secs, initial_delay)
            )
        # Alert evaluation loop (ADR-0027). The initial delay keeps short-lived
        # test app instances silent; KENNY_ALERT_INTERVAL_SECS=0 disables.
        alert_secs = int(settings.get("KENNY_ALERT_INTERVAL_SECS"))
        alert_task: asyncio.Task | None = None
        if alert_secs > 0:
            alert_delay = float(settings.get("KENNY_ALERT_INITIAL_DELAY"))
            alert_task = asyncio.create_task(alert_engine.run(alert_secs, alert_delay))
        # Periodic DB backup loop (see backup.py). KENNY_BACKUP_INTERVAL_SECS=0
        # disables entirely (a "restart" decision, like the loops above); the
        # cadence itself is re-read live inside the loop.
        backup_secs = int(settings.get("KENNY_BACKUP_INTERVAL_SECS"))
        backup_task: asyncio.Task | None = None
        if backup_secs > 0:
            backup_delay = float(settings.get("KENNY_BACKUP_INITIAL_DELAY"))
            backup_task = asyncio.create_task(
                _backup_loop(backup_mgr, settings, backup_secs, backup_delay)
            )
        # Scheduled update-detection loop (ADR-0040). KENNY_UPDATE_CHECK_INTERVAL_SECS=0
        # disables entirely (a "restart" decision, like the loops above); the
        # cadence itself is re-read live inside the loop. Detection only records
        # what's available — it never rolls anything out on its own.
        update_check_secs = int(settings.get("KENNY_UPDATE_CHECK_INTERVAL_SECS"))
        update_check_task: asyncio.Task | None = None
        if update_check_secs > 0:
            update_check_delay = float(settings.get("KENNY_UPDATE_CHECK_INITIAL_DELAY"))
            update_check_task = asyncio.create_task(
                update_check_loop(update_mgr, settings, update_check_secs, update_check_delay)
            )
        # Ticket housekeeping loop: expires overdue approval/consent gates and
        # auto-closes resolved tickets. KENNY_TICKET_SWEEP_INTERVAL_SECS=0
        # disables entirely (a "restart" decision, like the loops above); the
        # cadence itself is re-read live inside the loop.
        sweep_secs = int(settings.get("KENNY_TICKET_SWEEP_INTERVAL_SECS"))
        ticket_task: asyncio.Task | None = None
        if sweep_secs > 0:
            sweep_delay = float(settings.get("KENNY_TICKET_SWEEP_INITIAL_DELAY"))
            ticket_task = asyncio.create_task(
                ticket_sweep_loop(ticket_service, settings.get, sweep_secs, sweep_delay)
            )
        # Discord bot surface. Only started when an operator both configured a bot
        # token and enabled the surface; anything else is a one-line info and no
        # task at all. Failures inside the task never reach the server.
        discord_task: asyncio.Task | None = None
        if discord_service is not None and bool(settings.get("KENNY_DISCORD_ENABLED")):
            discord_task = asyncio.create_task(_discord_loop(discord_service))
        else:
            logging.getLogger("kenny.discord").info(
                "Discord surface not started (enabled=%s, token=%s)",
                bool(settings.get("KENNY_DISCORD_ENABLED")),
                "set" if discord_token else "unset",
            )
        # Exposed for tests/introspection: which optional loops this boot started.
        app.state.ticket_task = ticket_task
        app.state.discord_task = discord_task
        # Capture server-side log records onto a bounded queue and persist them
        # via a background drain task (source='server'). See ADR-0017.
        log_handler = StoreLogHandler()
        drain_task = asyncio.create_task(drain_log_queue(log_handler.queue, event_store))
        # Attach to root only: `kenny.*` records propagate up to root, so this one
        # handler captures them once (no duplicate persisted events).
        logging.getLogger().addHandler(log_handler)
        # Best-effort: fetch the prebuilt agent binary from GitHub when configured
        # and not overridden by an operator-placed binary (ADR-0015). Non-fatal.
        if (
            agent_release.github_configured()
            and not os.environ.get("KENNY_AGENT_BINARY", "").strip()
        ):
            try:
                result = await asyncio.to_thread(agent_release.fetch_latest_agent_binary)
                app.state.last_fetch = result
                logging.getLogger("kenny.release").info("agent binary fetch: %s", result.message)
            except Exception as exc:  # noqa: BLE001 - never break startup
                logging.getLogger("kenny.release").warning("agent binary fetch failed: %s", exc)
        # Chain the MCP app's own lifespan (session manager, etc.).
        try:
            async with mcp_app.router.lifespan_context(app):
                yield
        finally:
            logging.getLogger().removeHandler(log_handler)
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task
            if webfilter_task is not None:
                webfilter_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await webfilter_task
            if alert_task is not None:
                alert_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await alert_task
            if backup_task is not None:
                backup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await backup_task
            if update_check_task is not None:
                update_check_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await update_check_task
            if ticket_task is not None:
                ticket_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticket_task
            if discord_task is not None:
                discord_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await discord_task
            if discord_service is not None:
                with contextlib.suppress(Exception):
                    await discord_service.gateway.close()
            await token_store.close()
            await key_store.close()
            await user_store.close()
            await oauth_store.close()
            await store.close()
            await event_store.close()
            await policy_store.close()
            await webfilter_store.close()
            await suppression_store.close()
            await ticket_rule_store.close()
            await chat_history_store.close()
            await alert_state.close()
            await backup_target_store.close()
            await update_store.close()
            await ticket_store.close()
            await discord_identities.close()
            await settings_store.close()

    api_routes = build_api_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        screenshots=screenshots,
        event_store=event_store,
        token_store=token_store,
        policy_store=policy_store,
        policy_engine=policy_engine,
        webfilter=webfilter,
        settings=settings,
        user_store=user_store,
        key_store=key_store,
        alert_state=alert_state,
        webfilter_store=webfilter_store,
        backup_mgr=backup_mgr,
        backup_target_store=backup_target_store,
        update_mgr=update_mgr,
        suppression=suppression,
        ticket_rules=ticket_rules,
        tickets=ticket_service,
        ticket_store=ticket_store,
    )
    user_routes = build_user_routes(
        user_store=user_store, registry=registry, store=store, oauth_store=oauth_store
    )
    chat_routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        sessions=chat_sessions,
        screenshots=screenshots,
        history_store=chat_history_store,
        client_factory=client_factory,
    )
    # The server-hosted copilot drives arbitrary capability tools over the
    # process-global active agent, so it is gated to operator+ (ADR-0033). The
    # scoped ``user`` role uses the structured, per-host dashboard instead.
    chat_routes = [
        Route(
            r.path,
            guard(r.endpoint, min_role="operator"),
            methods=list(r.methods or []),
            name=r.name,
        )
        for r in chat_routes
    ]
    # Ticket/approval/Discord-identity/tool-class routes. Registered on every
    # server: the Discord collaborators are optional and only the two routes that
    # genuinely need a gateway answer 503 without one.
    ticket_routes = build_ticket_routes(
        tickets=ticket_service,
        store=ticket_store,
        identities=discord_identities,
        user_store=user_store,
        discord=discord_service,
        ticket_rules=ticket_rules,
        assistant=ticket_assistant,
    )
    download_routes = build_download_routes(
        registry=registry,
        token_store=token_store,
        tunnel=tunnel,
        share_links=share_links,
        key_store=key_store,
    )
    # The merged ticket/approval/flagged-section inbox (webui/inbox.py) --
    # deliberately its own module and route builder, not folded into
    # build_ticket_routes, so it stays out of webui/tickets.py entirely.
    inbox_routes = build_inbox_routes(
        tickets=ticket_service,
        ticket_store=ticket_store,
        registry=registry,
        telemetry_store=store,
    )

    # `operator_token` is the canonical single token (cookie value, tests);
    # `operator_tokens` is the full accepted set (supports KENNY_OPERATOR_TOKENS).
    operator_token = load_operator_token()
    operator_tokens = load_operator_tokens()

    routes = [
        WebSocketRoute("/agent/ws", tunnel.endpoint),
        *build_auth_routes(operator_tokens, user_store=user_store),
        *build_oauth_routes(oauth_store=oauth_store, user_store=user_store),
        *chat_routes,
        *download_routes,
        *user_routes,
        *ticket_routes,
        *inbox_routes,
        *api_routes,
        # Mounted last so it only catches what nothing above matched.
        Mount("/", app=mcp_app),
    ]

    # Operator auth gates /mcp, /api, and the UI; /agent/ws (agent token) is exempt.
    # The user store resolves per-user PATs and sessions; the OAuth store resolves
    # audience-bound OAuth access tokens (ADR-0037); the shared token stays accepted
    # as a back-compat superuser (ADR-0033).
    middleware = [
        Middleware(
            OperatorAuthMiddleware,
            token=operator_tokens,
            user_store=user_store,
            oauth_store=oauth_store,
        )
    ]

    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    # Expose singletons for tests / introspection.
    app.state.registry = registry
    app.state.settings = settings
    app.state.settings_store = settings_store
    app.state.store = store
    app.state.event_store = event_store
    app.state.token_store = token_store
    app.state.key_store = key_store
    app.state.user_store = user_store
    app.state.oauth_store = oauth_store
    app.state.policy_store = policy_store
    app.state.policy_engine = policy_engine
    app.state.webfilter_store = webfilter_store
    app.state.webfilter = webfilter
    app.state.suppression_store = suppression_store
    app.state.suppression = suppression
    app.state.ticket_rule_store = ticket_rule_store
    app.state.ticket_rules = ticket_rules
    app.state.backup_mgr = backup_mgr
    app.state.backup_target_store = backup_target_store
    app.state.update_store = update_store
    app.state.update_mgr = update_mgr
    app.state.tunnel = tunnel
    app.state.call_log = call_log
    app.state.screenshots = screenshots
    app.state.chat_sessions = chat_sessions
    app.state.chat_history_store = chat_history_store
    app.state.alert_state = alert_state
    app.state.alert_engine = alert_engine
    app.state.ticket_store = ticket_store
    app.state.tickets = ticket_service
    app.state.discord_identities = discord_identities
    app.state.discord_service = discord_service
    app.state.ticket_assistant = ticket_assistant
    # Replaced by the lifespan with the tasks it actually started (if any).
    app.state.ticket_task = None
    app.state.discord_task = None
    # The provider, not a list: a list captured here would be a snapshot of the
    # channels at boot and would quietly disagree with what actually delivers.
    app.state.notifier_provider = notifier_provider
    app.state.share_links = share_links
    app.state.mcp = mcp
    app.state.operator_token = operator_token
    app.state.operator_tokens = operator_tokens
    app.state.last_fetch = None
    return app


def run() -> None:
    """Entrypoint for the ``kenny-server`` script: serve via uvicorn."""

    import uvicorn

    configure_logging()
    host = os.environ.get("KENNY_HOST", "127.0.0.1")
    port = int(os.environ.get("KENNY_PORT", "8000"))
    # Trust X-Forwarded-For from the reverse proxy so per-client logic (e.g. the
    # login rate-limiter) sees the real client IP, not the proxy's. Restrict which
    # upstreams may set it via KENNY_FORWARDED_ALLOW_IPS (default: loopback only).
    forwarded_allow_ips = os.environ.get("KENNY_FORWARDED_ALLOW_IPS", "127.0.0.1")
    # ``log_config=None`` so our dictConfig owns formatting (not uvicorn's default).
    uvicorn.run(
        build_app(),
        host=host,
        port=port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )


if __name__ == "__main__":
    run()

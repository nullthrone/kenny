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
from typing import AsyncIterator

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, WebSocketRoute

from . import agent_release
from .alerting import DEFAULT_COOLDOWN_S, DEFAULT_OFFLINE_AFTER_S, AlertEngine
from .auth import (
    OperatorAuthMiddleware,
    build_auth_routes,
    load_operator_token,
    load_operator_tokens,
)
from .chat import ChatSessions
from .distribution import ShareLinks, build_download_routes
from .keystore import KeyStore
from .logging_config import StoreLogHandler, configure_logging, drain_log_queue
from .notify import load_notifiers
from .policy import PolicyEngine
from .registry import AgentRegistry
from .store import (
    AlertStateStore,
    ChatHistoryStore,
    EventStore,
    PolicyStore,
    TelemetryStore,
    WebFilterStore,
)
from .tokenstore import AgentTokenStore
from .tools import CallLog, ScreenshotStore, register_tools
from .tunnel import AgentTunnel
from .webfilter import ExternalListCache, WebFilterService
from .webui import build_api_routes, build_chat_routes


async def _webfilter_refresh_loop(
    cache: ExternalListCache, interval_s: int, initial_delay_s: float
) -> None:
    """Periodically refresh the external adult/bypass lists (best-effort)."""

    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            await cache.refresh_all()
        except Exception:  # noqa: BLE001 - never let the loop die
            logging.getLogger("kenny.webfilter").exception("external list refresh failed")
        await asyncio.sleep(interval_s)


def build_app(db_path: str | None = None) -> Starlette:
    """Build and return the composed ASGI application."""

    db_path = db_path or os.environ.get("KENNY_DB_PATH", "kenny.sqlite")

    token_store = AgentTokenStore(db_path)
    key_store = KeyStore(db_path)
    registry = AgentRegistry(token_store=token_store, key_store=key_store)
    store = TelemetryStore(db_path)
    event_store = EventStore(db_path)
    # Shared-catalog mirror + operator deny rules (ADR-0021). The engine loads the
    # catalog at construction and never raises if it is missing (fail-open).
    policy_store = PolicyStore(db_path)
    policy_engine = PolicyEngine()
    # Parental controls (ADR-0026): per-host store + external-list cache under a
    # dir derived from the DB path, wrapped in the service the tunnel/API/tools use.
    webfilter_store = WebFilterStore(db_path)
    cache_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    webfilter_cache = ExternalListCache(cache_dir)
    webfilter = WebFilterService(webfilter_store, webfilter_cache)
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
    # Push alerting (ADR-0028): transition detection over the health rules,
    # delivered best-effort via the env-configured channels (possibly none).
    alert_state = AlertStateStore(db_path)
    notifiers = load_notifiers()
    alert_engine = AlertEngine(
        store=store,
        alert_state=alert_state,
        event_store=event_store,
        registry=registry,
        notifiers=notifiers,
        cooldown_s=int(os.environ.get("KENNY_ALERT_COOLDOWN_SECS", str(DEFAULT_COOLDOWN_S))),
        offline_after_s=int(
            os.environ.get("KENNY_ALERT_OFFLINE_AFTER_SECS", str(DEFAULT_OFFLINE_AFTER_S))
        ),
        prunables=[store, event_store, webfilter_store],
        digest_enabled=os.environ.get("KENNY_DIGEST_ENABLED", "1") not in ("0", "false", ""),
        digest_day=os.environ.get("KENNY_DIGEST_DAY", "mon"),
        digest_hour=int(os.environ.get("KENNY_DIGEST_HOUR", "8")),
    )

    mcp = FastMCP("kenny")
    register_tools(
        mcp,
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        webfilter=webfilter,
    )
    mcp_app = mcp.http_app(path="/mcp")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await store.connect()
        await token_store.connect()
        await key_store.connect()
        await event_store.connect()
        await policy_store.connect()
        await webfilter_store.connect()
        await chat_history_store.connect()
        await alert_state.connect()
        # Load persisted operator rules into the mirror engine at startup.
        policy_engine.set_operator_rules(await policy_store.list())
        await store.prune()
        await event_store.prune()
        await webfilter_store.prune()
        # Periodically refresh the external adult/bypass lists (ADR-0026). The
        # initial fetch is delayed so short-lived test app instances never reach
        # out; set KENNY_WEBFILTER_REFRESH_SECS=0 to disable entirely.
        refresh_secs = int(os.environ.get("KENNY_WEBFILTER_REFRESH_SECS", str(24 * 3600)))
        webfilter_task: asyncio.Task | None = None
        if refresh_secs > 0:
            initial_delay = float(
                os.environ.get("KENNY_WEBFILTER_INITIAL_REFRESH_DELAY", "5")
            )
            webfilter_task = asyncio.create_task(
                _webfilter_refresh_loop(webfilter_cache, refresh_secs, initial_delay)
            )
        # Alert evaluation loop (ADR-0028). The initial delay keeps short-lived
        # test app instances silent; KENNY_ALERT_INTERVAL_SECS=0 disables.
        alert_secs = int(os.environ.get("KENNY_ALERT_INTERVAL_SECS", "60"))
        alert_task: asyncio.Task | None = None
        if alert_secs > 0:
            alert_delay = float(os.environ.get("KENNY_ALERT_INITIAL_DELAY", "10"))
            alert_task = asyncio.create_task(alert_engine.run(alert_secs, alert_delay))
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
            await token_store.close()
            await key_store.close()
            await store.close()
            await event_store.close()
            await policy_store.close()
            await webfilter_store.close()
            await chat_history_store.close()
            await alert_state.close()

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
    )
    chat_routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        sessions=chat_sessions,
        screenshots=screenshots,
        history_store=chat_history_store,
    )
    download_routes = build_download_routes(
        registry=registry,
        token_store=token_store,
        tunnel=tunnel,
        share_links=share_links,
        key_store=key_store,
    )

    # `operator_token` is the canonical single token (cookie value, tests);
    # `operator_tokens` is the full accepted set (supports KENNY_OPERATOR_TOKENS).
    operator_token = load_operator_token()
    operator_tokens = load_operator_tokens()

    routes = [
        WebSocketRoute("/agent/ws", tunnel.endpoint),
        Mount("/mcp", app=mcp_app),
        *build_auth_routes(operator_tokens),
        *chat_routes,
        *download_routes,
        *api_routes,
    ]

    # Operator auth gates /mcp, /api, and the UI; /agent/ws (agent token) is exempt.
    middleware = [Middleware(OperatorAuthMiddleware, token=operator_tokens)]

    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    # Expose singletons for tests / introspection.
    app.state.registry = registry
    app.state.store = store
    app.state.event_store = event_store
    app.state.token_store = token_store
    app.state.key_store = key_store
    app.state.policy_store = policy_store
    app.state.policy_engine = policy_engine
    app.state.webfilter_store = webfilter_store
    app.state.webfilter = webfilter
    app.state.tunnel = tunnel
    app.state.call_log = call_log
    app.state.screenshots = screenshots
    app.state.chat_sessions = chat_sessions
    app.state.chat_history_store = chat_history_store
    app.state.alert_state = alert_state
    app.state.alert_engine = alert_engine
    app.state.notifiers = notifiers
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
    # ``log_config=None`` so our dictConfig owns formatting (not uvicorn's default).
    uvicorn.run(build_app(), host=host, port=port, log_config=None)


if __name__ == "__main__":
    run()

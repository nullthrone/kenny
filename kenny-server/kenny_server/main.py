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
from .auth import (
    OperatorAuthMiddleware,
    build_auth_routes,
    load_operator_token,
    load_operator_tokens,
)
from .chat import ChatSessions
from .distribution import ShareLinks, build_download_routes
from .logging_config import StoreLogHandler, configure_logging, drain_log_queue
from .registry import AgentRegistry
from .store import EventStore, TelemetryStore
from .tokenstore import AgentTokenStore
from .tools import CallLog, ScreenshotStore, register_tools
from .tunnel import AgentTunnel
from .webui import build_api_routes, build_chat_routes


def build_app(db_path: str | None = None) -> Starlette:
    """Build and return the composed ASGI application."""

    db_path = db_path or os.environ.get("KENNY_DB_PATH", "kenny.sqlite")

    token_store = AgentTokenStore(db_path)
    registry = AgentRegistry(token_store=token_store)
    store = TelemetryStore(db_path)
    event_store = EventStore(db_path)
    tunnel = AgentTunnel(registry, store, event_store)
    call_log = CallLog(event_store=event_store)
    screenshots = ScreenshotStore()
    chat_sessions = ChatSessions()
    share_links = ShareLinks()

    mcp = FastMCP("kenny")
    register_tools(mcp, registry=registry, store=store, tunnel=tunnel, call_log=call_log)
    mcp_app = mcp.http_app(path="/mcp")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await store.connect()
        await token_store.connect()
        await event_store.connect()
        await store.prune()
        await event_store.prune()
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
            await token_store.close()
            await store.close()
            await event_store.close()

    api_routes = build_api_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        screenshots=screenshots,
        event_store=event_store,
        token_store=token_store,
    )
    chat_routes = build_chat_routes(
        registry=registry,
        store=store,
        tunnel=tunnel,
        call_log=call_log,
        sessions=chat_sessions,
        screenshots=screenshots,
    )
    download_routes = build_download_routes(
        registry=registry,
        token_store=token_store,
        tunnel=tunnel,
        share_links=share_links,
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
    app.state.tunnel = tunnel
    app.state.call_log = call_log
    app.state.screenshots = screenshots
    app.state.chat_sessions = chat_sessions
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

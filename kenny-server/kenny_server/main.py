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

import contextlib
import os
from typing import AsyncIterator

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount, Route, WebSocketRoute

from .registry import AgentRegistry
from .store import TelemetryStore
from .tools import CallLog, register_tools
from .tunnel import AgentTunnel
from .webui import build_api_routes


def build_app(db_path: str | None = None) -> Starlette:
    """Build and return the composed ASGI application."""

    db_path = db_path or os.environ.get("KENNY_DB_PATH", "kenny.sqlite")

    registry = AgentRegistry()
    store = TelemetryStore(db_path)
    tunnel = AgentTunnel(registry, store)
    call_log = CallLog()

    mcp = FastMCP("kenny")
    register_tools(mcp, registry=registry, store=store, tunnel=tunnel, call_log=call_log)
    mcp_app = mcp.http_app(path="/mcp")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await store.connect()
        await store.prune()
        # Chain the MCP app's own lifespan (session manager, etc.).
        async with mcp_app.router.lifespan_context(app):
            yield
        await store.close()

    api_routes = build_api_routes(
        registry=registry, store=store, tunnel=tunnel, call_log=call_log
    )

    routes = [
        WebSocketRoute("/agent/ws", tunnel.endpoint),
        Mount("/mcp", app=mcp_app),
        *api_routes,
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    # Expose singletons for tests / introspection.
    app.state.registry = registry
    app.state.store = store
    app.state.tunnel = tunnel
    app.state.call_log = call_log
    app.state.mcp = mcp
    return app


def run() -> None:
    """Entrypoint for the ``kenny-server`` script: serve via uvicorn."""

    import uvicorn

    host = os.environ.get("KENNY_HOST", "127.0.0.1")
    port = int(os.environ.get("KENNY_PORT", "8000"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    run()

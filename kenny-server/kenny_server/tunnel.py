"""Agent tunnel: the ``/agent/ws`` WebSocket endpoint and request routing.

Flow (see ``docs/protocol.md`` § Transport):

1. The agent opens an outbound WebSocket to ``/agent/ws`` and sends a
   ``register`` frame.
2. The server authenticates and registers the connection (with a ``send_fn``).
3. The server may forward MCP tool calls as ``request`` frames via
   :meth:`AgentTunnel.send_request`, awaiting the matching ``response`` keyed by
   request ``id``.
4. The agent pushes ``telemetry`` frames; these are routed to the store and
   health evaluation.
5. ``ping``/``pong`` keep the connection alive; any inbound frame refreshes the
   agent's ``last_seen``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from .protocol import (
    Log,
    Ping,
    Pong,
    Register,
    Request,
    Response,
    Telemetry,
    dump_frame,
    parse_frame,
)
from .registry import AgentRegistry, AuthError
from .store import EventStore, TelemetryStore

DEFAULT_TIMEOUT_S = 30.0

logger = logging.getLogger("kenny.tunnel")


class ToolError(Exception):
    """Raised when a forwarded tool returns an error response or times out."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AgentTunnel:
    """Owns the WebSocket endpoint and per-agent pending-request futures."""

    def __init__(
        self,
        registry: AgentRegistry,
        store: TelemetryStore,
        event_store: EventStore,
    ) -> None:
        self.registry = registry
        self.store = store
        self.event_store = event_store
        # request_id -> Future[Response]
        self._pending: dict[str, asyncio.Future[Response]] = {}

    # -- server -> agent ---------------------------------------------------

    async def send_request(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Forward a tool call to an agent and await its result.

        Returns the ``result`` dict on success; raises :class:`ToolError` on an
        error response or timeout.
        """

        send_fn = self.registry.send_fn_for(agent_id)
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Response] = loop.create_future()
        self._pending[request_id] = future

        frame = Request(id=request_id, tool=tool, args=args)
        try:
            await send_fn(dump_frame(frame))
            response = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise ToolError("timeout", f"tool {tool} exceeded {timeout_s}s") from exc
        finally:
            self._pending.pop(request_id, None)

        if response.ok:
            return response.result or {}
        err = response.error
        raise ToolError(
            err.code if err else "internal",
            err.message if err else "agent returned an error without detail",
        )

    # -- WebSocket endpoint ------------------------------------------------

    async def endpoint(self, websocket: WebSocket) -> None:
        """Starlette route handler for ``/agent/ws``."""

        await websocket.accept()
        agent_id: str | None = None
        try:
            agent_id = await self._handshake(websocket)
            if agent_id is None:
                return
            await self._serve(websocket, agent_id)
        except WebSocketDisconnect:
            pass
        finally:
            if agent_id is not None:
                self.registry.mark_offline(agent_id)
                self._fail_pending_for_disconnect()
                logger.info("agent %s disconnected", agent_id)

    async def _handshake(self, websocket: WebSocket) -> str | None:
        raw = await websocket.receive_text()
        frame = parse_frame(raw)
        if not isinstance(frame, Register):
            logger.warning(
                "agent handshake rejected: first frame was %s, expected register; "
                "closing 4400",
                type(frame).__name__,
            )
            await websocket.close(code=4400)  # expected register
            return None

        async def send_fn(payload: dict[str, Any]) -> None:
            await websocket.send_json(payload)

        try:
            await self.registry.register_async(
                frame.agent_id, frame.token, frame.meta.model_dump(), send_fn
            )
        except AuthError:
            logger.warning("auth failed for agent %s; closing 4401", frame.agent_id)
            await websocket.close(code=4401)  # unauthorized (non-1000)
            return None
        logger.info("agent %s connected", frame.agent_id)
        return frame.agent_id

    async def _serve(self, websocket: WebSocket, agent_id: str) -> None:
        while True:
            raw = await websocket.receive_text()
            frame = parse_frame(raw)
            self.registry.mark_seen(agent_id)

            if isinstance(frame, Response):
                self._resolve(frame)
            elif isinstance(frame, Telemetry):
                await self.store.insert(
                    frame.agent_id,
                    frame.collected_at,
                    {k: v.model_dump() for k, v in frame.snapshot.items()},
                )
                logger.debug("telemetry from %s at %s", frame.agent_id, frame.collected_at)
            elif isinstance(frame, Log):
                await self.event_store.insert_log(
                    source="agent",
                    agent_id=frame.agent_id,
                    at=frame.at,
                    level=frame.level,
                    target=frame.target,
                    message=frame.message,
                    fields=frame.fields,
                )
            elif isinstance(frame, Ping):
                await websocket.send_json(dump_frame(Pong()))
            elif isinstance(frame, Pong):
                pass  # heartbeat ack; last_seen already refreshed
            elif isinstance(frame, Register):
                # Re-register on the same socket: refresh meta, keep send_fn.
                self.registry.mark_seen(agent_id)
            # Requests never arrive agent->server; ignore defensively.

    # -- response correlation ---------------------------------------------

    def _resolve(self, response: Response) -> None:
        future = self._pending.get(response.id)
        if future is not None and not future.done():
            future.set_result(response)

    def _fail_pending_for_disconnect(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ToolError("internal", "agent disconnected"))

    @staticmethod
    def _is_open(websocket: WebSocket) -> bool:
        return websocket.client_state == WebSocketState.CONNECTED

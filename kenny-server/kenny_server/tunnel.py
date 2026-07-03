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
import base64
import logging
import os
import secrets
import uuid
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from datetime import datetime, timezone

from .keystore import build_transcript
from .policy import PolicyEngine
from .protocol import (
    Auth,
    Challenge,
    Log,
    Ping,
    Policy,
    Pong,
    PolicyRule,
    Register,
    Request,
    Response,
    Telemetry,
    dump_frame,
    parse_frame,
)
from .registry import AgentRegistry, AuthError
from .store import EventStore, PolicyStore, TelemetryStore
from .webfilter import WebFilterService

DEFAULT_TIMEOUT_S = 30.0
HANDSHAKE_TIMEOUT_S = 10.0

logger = logging.getLogger("kenny.tunnel")

# Bound inbound frames so a compromised/malicious agent cannot exhaust server
# memory/disk (CWE-400/770). Two limits, because the DoS surface differs by frame
# kind:
#
# * ``_MAX_FRAME_BYTES`` — a generous *absolute* ceiling applied to every frame
#   before parsing, so no single payload can force an unbounded JSON parse. It is
#   large enough for legitimate ``response`` frames that carry bulk data, notably a
#   ``screen_capture`` result (a full-screen PNG, base64-encoded, routinely runs a
#   few MB). A ``response`` is only ever acted on when its ``id`` matches a request
#   *this server* sent (one outstanding future per request, with a timeout), so it
#   cannot be spammed unsolicited the way a push can — the ceiling is the only bound
#   it needs.
# * ``_MAX_TELEMETRY_BYTES`` / ``_MAX_SECTIONS`` — the strict caps for
#   *unsolicited pushed* frames (``telemetry``, ``log``), applied after parsing once
#   the frame type is known. These keep the tight DoS bound for exactly the frames
#   an agent can push at will. Telemetry sections are sized to stay well within this
#   (see docs/protocol.md).
#
# An offending frame is dropped + logged, never parsed-into-store (byte ceiling) or
# never persisted (per-kind cap). Tune down per deployment.
_MAX_FRAME_BYTES = int(os.environ.get("KENNY_MAX_FRAME_BYTES", str(8 * 1024 * 1024)))
_MAX_TELEMETRY_BYTES = int(os.environ.get("KENNY_MAX_TELEMETRY_BYTES", str(256 * 1024)))
_MAX_SECTIONS = int(os.environ.get("KENNY_MAX_TELEMETRY_SECTIONS", "128"))


def _parse_version(value: str) -> tuple[int, ...]:
    """Parse ``PROTOCOL_VERSION`` strings into comparable component tuples.

    Comparison must be numeric per component, not lexicographic: ``"0.10"`` is
    newer than ``"0.8"`` but compares smaller as a string.
    """

    try:
        return tuple(int(p) for p in value.split("."))
    except ValueError:
        return (0,)


def _signature_path(frame: Register) -> bool:
    """True when the register frame selects the v0.8 signature handshake.

    Selected when ``protocol >= 0.8`` (numeric) and a ``client_nonce`` is
    present (per ``docs/protocol.md`` § Transport / Migration window).
    """

    if frame.client_nonce is None or frame.protocol is None:
        return False
    return _parse_version(frame.protocol) >= (0, 8)


def _token_auth_enabled() -> bool:
    """Whether legacy bearer-token auth is still accepted (migration window)."""

    return os.environ.get("KENNY_ALLOW_TOKEN_AUTH", "1") not in ("0", "false", "")


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
        *,
        policy_engine: PolicyEngine | None = None,
        policy_store: PolicyStore | None = None,
        webfilter: WebFilterService | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.event_store = event_store
        self.policy_engine = policy_engine
        self.policy_store = policy_store
        self.webfilter = webfilter
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

        # Best-effort server mirror (ADR-0021): refuse obviously dangerous calls
        # before forwarding. The agent stays authoritative; this only adds
        # earlier feedback and runs before the pending future / send.
        if self.policy_engine is not None:
            hit = self.policy_engine.check(tool, args)
            if hit is not None:
                _code, reason = hit
                await self.event_store.insert_log(
                    source="server",
                    at=datetime.now(timezone.utc).isoformat(),
                    level="warn",
                    target="kenny.policy",
                    message=f"blocked {tool}: {reason}",
                    agent_id=agent_id,
                    fields={"tool": tool, "reason": reason},
                )
                raise ToolError("blocked", reason)

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

    async def broadcast_policy(self) -> None:
        """Push the current operator deny rules to every online agent.

        Called after an operator changes the rule set (ADR-0021). Per-agent send
        errors are swallowed (logged at debug) so one stale socket can't break a
        fleet-wide broadcast.
        """

        if self.policy_store is None:
            return
        rules = [PolicyRule(**r) for r in await self.policy_store.list()]
        payload = dump_frame(Policy(rules=rules))
        for agent in self.registry.list():
            if not agent.online or agent.send_fn is None:
                continue
            try:
                await agent.send_fn(payload)
            except Exception as exc:  # noqa: BLE001 - one bad socket must not abort
                logger.debug("policy broadcast to %s failed: %s", agent.agent_id, exc)

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

        if _signature_path(frame):
            if not await self._handshake_signed(websocket, frame, send_fn):
                return None
        else:
            if not await self._handshake_token(websocket, frame, send_fn):
                return None

        logger.info("agent %s connected", frame.agent_id)
        # Push the current operator deny rules to the just-connected agent
        # (always, even when empty, so behaviour is deterministic). ADR-0021.
        if self.policy_store is not None:
            try:
                rules = [PolicyRule(**r) for r in await self.policy_store.list()]
                await send_fn(dump_frame(Policy(rules=rules)))
            except Exception as exc:  # noqa: BLE001 - never break the handshake
                logger.debug("policy delivery to %s failed: %s", frame.agent_id, exc)
        return frame.agent_id

    async def _handshake_signed(
        self, websocket: WebSocket, frame: Register, send_fn: Any
    ) -> bool:
        """Run the v0.8 mutual-auth challenge/response. Returns True on success.

        The server signs the transcript (proving its identity to the agent), then
        requires a valid ``auth`` signature from the agent before registering the
        connection. Any failure closes the socket with ``4401`` and returns False.
        """

        key_store = self.registry.key_store
        if key_store is None:
            logger.warning(
                "signature handshake for %s but no key store; closing 4401",
                frame.agent_id,
            )
            await websocket.close(code=4401)
            return False

        try:
            client_nonce = base64.b64decode(frame.client_nonce or "")
        except Exception:  # noqa: BLE001 - malformed base64
            client_nonce = b""
        if len(client_nonce) != 32:
            logger.warning(
                "bad client_nonce from %s; closing 4401", frame.agent_id
            )
            await websocket.close(code=4401)
            return False

        server_nonce = secrets.token_bytes(32)
        transcript = build_transcript(frame.agent_id, client_nonce, server_nonce)
        server_sig = key_store.sign_transcript(transcript)
        await send_fn(
            dump_frame(
                Challenge(
                    server_nonce=base64.b64encode(server_nonce).decode(),
                    server_sig=server_sig,
                )
            )
        )

        # A stalled handshake must not pin the socket indefinitely.
        try:
            reply_raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=HANDSHAKE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.warning("auth timeout for agent %s; closing 4401", frame.agent_id)
            await websocket.close(code=4401)
            return False

        try:
            auth = parse_frame(reply_raw)
        except Exception:  # noqa: BLE001 - malformed frame
            auth = None
        if not isinstance(auth, Auth):
            logger.warning(
                "expected auth frame from %s; closing 4401", frame.agent_id
            )
            await websocket.close(code=4401)
            return False

        try:
            await self.registry.authenticate_signature(
                frame.agent_id, transcript, auth.agent_sig
            )
        except AuthError:
            logger.warning(
                "signature auth failed for agent %s; closing 4401", frame.agent_id
            )
            await websocket.close(code=4401)
            return False

        self.registry.register_signed_async(
            frame.agent_id, frame.meta.model_dump(), send_fn
        )
        return True

    async def _handshake_token(
        self, websocket: WebSocket, frame: Register, send_fn: Any
    ) -> bool:
        """Legacy bearer-token registration (migration window). True on success."""

        if not _token_auth_enabled():
            logger.warning(
                "token auth disabled and no signature material from %s; closing 4401",
                frame.agent_id,
            )
            await websocket.close(code=4401)
            return False
        try:
            await self.registry.register_async(
                frame.agent_id, frame.token or "", frame.meta.model_dump(), send_fn
            )
        except AuthError:
            logger.warning("auth failed for agent %s; closing 4401", frame.agent_id)
            await websocket.close(code=4401)  # unauthorized (non-1000)
            return False
        return True

    async def _serve(self, websocket: WebSocket, agent_id: str) -> None:
        while True:
            raw = await websocket.receive_text()
            # Absolute ceiling: reject any frame too large to safely parse, before
            # parsing/persisting it, so a compromised agent can't exhaust server
            # memory (CWE-400/770). The strict per-kind caps for unsolicited pushes
            # are applied after parsing, below.
            if len(raw) > _MAX_FRAME_BYTES:
                logger.warning(
                    "dropping oversized frame from %s (%d bytes > %d cap)",
                    agent_id,
                    len(raw),
                    _MAX_FRAME_BYTES,
                )
                continue
            frame = parse_frame(raw)
            self.registry.mark_seen(agent_id)

            # Bind pushed frames to the identity proven at the handshake. An agent
            # can only speak for itself, so a frame whose ``agent_id`` differs from
            # the authenticated connection is a spoofing attempt (an agent forging
            # another agent's telemetry/logs/web-activity). Drop it rather than
            # persist data under the forged id (CWE-346 Origin Validation Error).
            frame_agent_id = getattr(frame, "agent_id", None)
            if frame_agent_id is not None and frame_agent_id != agent_id:
                logger.warning(
                    "dropping %s frame from %s: agent_id %r does not match the "
                    "authenticated connection",
                    type(frame).__name__,
                    agent_id,
                    frame_agent_id,
                )
                continue

            if isinstance(frame, Response):
                self._resolve(frame)
            elif isinstance(frame, Telemetry):
                # Strict byte cap for unsolicited pushes (see the constants above):
                # an agent can push telemetry at will, so keep the tight DoS bound
                # here even though the frame already passed the absolute ceiling.
                if len(raw) > _MAX_TELEMETRY_BYTES:
                    logger.warning(
                        "dropping oversized telemetry from %s (%d bytes > %d cap)",
                        agent_id,
                        len(raw),
                        _MAX_TELEMETRY_BYTES,
                    )
                    continue
                if len(frame.snapshot) > _MAX_SECTIONS:
                    logger.warning(
                        "dropping telemetry from %s: %d sections > %d cap",
                        agent_id,
                        len(frame.snapshot),
                        _MAX_SECTIONS,
                    )
                    continue
                snapshot = {k: v.model_dump() for k, v in frame.snapshot.items()}
                # Parental controls (ADR-0026): enrich the web_activity section
                # with server-computed `flagged` before persisting. A webfilter
                # bug must never drop the whole snapshot.
                if self.webfilter is not None and "web_activity" in snapshot:
                    try:
                        snapshot["web_activity"] = await self.webfilter.record_activity(
                            frame.agent_id, snapshot["web_activity"]
                        )
                    except Exception:  # noqa: BLE001 - never lose the snapshot
                        logger.exception(
                            "webfilter record_activity failed for %s", frame.agent_id
                        )
                await self.store.insert(
                    frame.agent_id,
                    frame.collected_at,
                    snapshot,
                )
                logger.debug("telemetry from %s at %s", frame.agent_id, frame.collected_at)
            elif isinstance(frame, Log):
                # Same strict push cap as telemetry: a log frame is unsolicited.
                if len(raw) > _MAX_TELEMETRY_BYTES:
                    logger.warning(
                        "dropping oversized log from %s (%d bytes > %d cap)",
                        agent_id,
                        len(raw),
                        _MAX_TELEMETRY_BYTES,
                    )
                    continue
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

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
   agent's ``last_seen``. A connection that sends no frame at all for
   :data:`HEARTBEAT_TIMEOUT_SECS` is closed and the agent marked offline.
6. Each authenticated connection is one presence session in
   :class:`~kenny_server.store.PresenceStore` (the availability record).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import uuid
from typing import Any, Awaitable, Callable

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from datetime import datetime, timezone

from .config import Settings
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
    ShellPolicy,
    Telemetry,
    dump_frame,
    parse_frame,
)
from .registry import AgentRegistry, AuthError
from .store import EventStore, PolicyStore, PresenceStore, ShellAllowStore, TelemetryStore
from .webfilter import WebFilterService

DEFAULT_TIMEOUT_S = 30.0
HANDSHAKE_TIMEOUT_S = 10.0

#: The agent's ping interval (``HEARTBEAT`` in ``kenny-agent/src/tunnel.rs``;
#: ``tests/test_presence.py`` reads it from there).
HEARTBEAT_SECS = 30
#: No frame of any kind for three missed heartbeats: the connection is dead even
#: if TCP has not noticed (docs/protocol.md § ping/pong).
HEARTBEAT_TIMEOUT_SECS = 3 * HEARTBEAT_SECS
#: Close code for a heartbeat timeout. 4408 mirrors HTTP 408 Request Timeout in
#: the application range (4000-4999), next to the 4400/4401 the handshake uses.
#: The agent treats any close as "reconnect with backoff".
HEARTBEAT_CLOSE_CODE = 4408

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
        shell_allow_store: ShellAllowStore | None = None,
        settings: Settings | None = None,
        webfilter: WebFilterService | None = None,
        on_agent_online: Callable[[str], Awaitable[None]] | None = None,
        after_insert: Callable[[str, dict[str, Any]], Any] | None = None,
        presence: PresenceStore | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.event_store = event_store
        self.policy_engine = policy_engine
        self.policy_store = policy_store
        # The fleet shell execution mode (ADR-0064) is resolved from these two: the
        # mode is a setting, the allow rules are rows. Absent either, the tunnel
        # sends no ``shell`` field and the frame is a pre-0.18 one.
        self.shell_allow_store = shell_allow_store
        self.settings = settings
        self.webfilter = webfilter
        # Optional hook fired (fire-and-forget) after an agent successfully
        # registers, so the update-campaign on-connect rollout (ADR-0040) can
        # decide whether to auto-apply a pinned campaign — without coupling the
        # tunnel to update_manager. Never awaited inline: a slow or failing hook
        # must not delay serving the connection or break the handshake.
        self.on_agent_online = on_agent_online
        # Optional synchronous hook called with ``(agent_id, snapshot)`` right
        # after a telemetry snapshot is stored -- the seam the persisted event
        # classification (ADR-0058) uses to kick its background batch. Never
        # awaited, wrapped in its own guard: ingestion does not depend on it.
        self.after_insert = after_insert
        # Optional presence record (availability). Every write to it is guarded:
        # a presence failure is logged and never reaches the connection.
        self.presence = presence
        # request_id -> Future[Response]
        self._pending: dict[str, asyncio.Future[Response]] = {}
        # request_id -> (agent_id, conn_id) of the connection it was sent on, so a
        # closing socket fails only the requests it owned.
        self._pending_owner: dict[str, tuple[str, int]] = {}

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

        # Best-effort server mirror (ADR-0020): refuse obviously dangerous calls
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

        try:
            send_fn = self.registry.send_fn_for(agent_id)
        except AuthError as exc:
            raise ToolError("offline", f"{agent_id} is not connected") from exc
        agent = self.registry.get(agent_id)
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Response] = loop.create_future()
        self._pending[request_id] = future
        self._pending_owner[request_id] = (agent_id, agent.conn_id if agent else 0)

        frame = Request(id=request_id, tool=tool, args=args)
        try:
            await send_fn(dump_frame(frame))
            response = await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise ToolError("timeout", f"tool {tool} exceeded {timeout_s}s") from exc
        finally:
            self._pending.pop(request_id, None)
            self._pending_owner.pop(request_id, None)

        if response.ok:
            return response.result or {}
        err = response.error
        raise ToolError(
            err.code if err else "internal",
            err.message if err else "agent returned an error without detail",
        )

    async def refresh_shell_policy(self) -> ShellPolicy | None:
        """Resolve the fleet shell execution mode, sync the mirror to it, return it.

        One function, because the policy the agents are pushed and the policy the
        server mirror enforces must not be able to drift: every caller that sends a
        ``policy`` frame resolves through here, and resolving refreshes the mirror.

        Returns ``None`` when the server has nothing to say about shell execution
        (no settings or no store wired, as in tests that build a bare tunnel) — the
        frame then carries no ``shell`` field and is a pre-0.18 one. ADR-0064.
        """

        if self.settings is None or self.shell_allow_store is None:
            return None
        mode = str(self.settings.get("KENNY_SHELL_POLICY_MODE") or "unrestricted")
        rows = await self.shell_allow_store.list()
        if self.policy_engine is not None:
            self.policy_engine.set_shell_policy(mode, rows)
        return ShellPolicy(mode=mode, allow=[PolicyRule(**r) for r in rows])

    async def _policy_frame(self) -> dict[str, Any]:
        """Build the ``policy`` frame every agent is pushed: deny rules plus mode."""

        rules: list[PolicyRule] = []
        if self.policy_store is not None:
            rules = [PolicyRule(**r) for r in await self.policy_store.list()]
        return dump_frame(Policy(rules=rules, shell=await self.refresh_shell_policy()))

    async def broadcast_policy(self) -> None:
        """Push the current deny rules and shell execution mode to every online agent.

        Called after an operator changes either (ADR-0020, ADR-0064). Per-agent send
        errors are swallowed (logged at debug) so one stale socket can't break a
        fleet-wide broadcast.
        """

        if self.policy_store is None and self.shell_allow_store is None:
            return
        payload = await self._policy_frame()
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
        conn_id: int | None = None
        session_id: int | None = None
        end_reason = "disconnect"
        try:
            accepted = await self._handshake_conn(websocket)
            if accepted is None:
                return
            agent_id, conn_id = accepted
            session_id = await self._open_presence(agent_id)
            if self.on_agent_online is not None:
                asyncio.create_task(self._fire_on_agent_online(agent_id))
            end_reason = await self._serve(websocket, agent_id)
        except WebSocketDisconnect:
            pass
        finally:
            if agent_id is not None:
                # A reconnect may already own the agent; then this teardown must
                # leave the live connection's state (and requests) alone.
                if not self.registry.mark_offline(agent_id, conn_id):
                    end_reason = "superseded"
                self._fail_pending_for_disconnect(agent_id, conn_id)
                logger.info("agent %s disconnected (%s)", agent_id, end_reason)
                if session_id is not None:
                    await self._close_presence(agent_id, session_id, end_reason)

    async def _open_presence(self, agent_id: str) -> int | None:
        if self.presence is None:
            return None
        try:
            return await self.presence.open_session(agent_id)
        except Exception:  # noqa: BLE001 - presence must never break the tunnel
            logger.exception("opening presence session for %s failed", agent_id)
            return None

    async def _close_presence(self, agent_id: str, session_id: int, reason: str) -> None:
        if self.presence is None:
            return
        try:
            await self.presence.close_session(session_id, reason)
        except Exception:  # noqa: BLE001 - presence must never break the tunnel
            logger.exception("closing presence session for %s failed", agent_id)

    async def _note_boot(self, agent_id: str, snapshot: dict[str, Any]) -> None:
        if self.presence is None:
            return
        uptime = snapshot.get("uptime")
        boot = uptime.get("boot_time_unix") if isinstance(uptime, dict) else None
        if not isinstance(boot, int) or isinstance(boot, bool):
            return
        try:
            await self.presence.note_boot(agent_id, boot)
        except Exception:  # noqa: BLE001 - presence must never break the tunnel
            logger.exception("recording boot time for %s failed", agent_id)

    async def _fire_on_agent_online(self, agent_id: str) -> None:
        """Run the on-connect hook detached from the handshake/serve path.

        A second safety net beyond ``on_agent_connect``'s own try/except
        (ADR-0040): whatever the hook does, it must never surface into the
        tunnel or delay serving the connection.
        """

        assert self.on_agent_online is not None
        try:
            await self.on_agent_online(agent_id)
        except Exception:  # noqa: BLE001 - a hook failure must never affect the tunnel
            logger.exception("on_agent_online hook failed for %s", agent_id)

    async def _handshake(self, websocket: WebSocket) -> str | None:
        accepted = await self._handshake_conn(websocket)
        return accepted[0] if accepted is not None else None

    async def _handshake_conn(self, websocket: WebSocket) -> tuple[str, int] | None:
        """Run the handshake; on success return ``(agent_id, conn_id)``.

        ``conn_id`` is read right after registration, before anything else is
        awaited, so it names *this* connection even if the agent reconnects while
        the policy frame below is being sent.
        """

        raw = await websocket.receive_text()
        try:
            frame = parse_frame(raw)
        except ValidationError:
            # Malformed JSON or a frame that doesn't match any known shape
            # (pydantic wraps JSON decode errors into ValidationError too, since
            # this goes through validate_json). Nobody is authenticated yet, so
            # this is reachable by anyone who can open a socket to /agent/ws —
            # treat it the same as "first frame was not register": close 4400
            # rather than let the exception propagate out of the handshake.
            logger.warning(
                "agent handshake rejected: first frame was not valid JSON/a known "
                "frame; closing 4400"
            )
            await websocket.close(code=4400)
            return None
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
        registered = self.registry.get(frame.agent_id)
        conn_id = registered.conn_id if registered is not None else 0

        logger.info("agent %s connected", frame.agent_id)
        # Push the current deny rules and shell execution mode to the just-connected
        # agent (always, even when empty, so behaviour is deterministic).
        # ADR-0020, ADR-0064.
        if self.policy_store is not None or self.shell_allow_store is not None:
            try:
                await send_fn(await self._policy_frame())
            except Exception as exc:  # noqa: BLE001 - never break the handshake
                logger.debug("policy delivery to %s failed: %s", frame.agent_id, exc)
        return frame.agent_id, conn_id

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

    async def _serve(self, websocket: WebSocket, agent_id: str) -> str:
        """Serve frames until the connection ends; return why it ended.

        ``"heartbeat_timeout"`` when nothing arrived for
        :data:`HEARTBEAT_TIMEOUT_SECS` (the socket is closed here), otherwise
        ``"disconnect"``.
        """

        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=HEARTBEAT_TIMEOUT_SECS
                )
            except asyncio.TimeoutError:
                logger.info(
                    "agent %s sent nothing for %ss; closing %d",
                    agent_id,
                    HEARTBEAT_TIMEOUT_SECS,
                    HEARTBEAT_CLOSE_CODE,
                )
                try:
                    await websocket.close(code=HEARTBEAT_CLOSE_CODE)
                except Exception:  # noqa: BLE001 - the peer is gone; closing is best-effort
                    logger.debug("closing a timed-out socket for %s failed", agent_id)
                return "heartbeat_timeout"
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
            try:
                frame = parse_frame(raw)
            except ValidationError:
                # Malformed JSON or a frame that doesn't match any known shape
                # (pydantic wraps JSON decode errors into ValidationError too,
                # since this goes through validate_json). An already-authenticated
                # agent can push arbitrary frames at will, so — like the size caps
                # above — drop the one bad frame and keep the tunnel open rather
                # than let the exception tear down the connection.
                logger.warning(
                    "dropping unparseable frame from %s (%d bytes)", agent_id, len(raw)
                )
                continue

            # The host may have been removed from inventory mid-connection
            # (DELETE /api/agent/{id} → inventory.purge_agent → registry.remove).
            # Its token/key are already gone so it can't reconnect; close this live
            # socket too, otherwise it would keep re-populating snapshots and
            # reappear in the fleet list (ADR-0033, fail-closed removal).
            if self.registry.get(agent_id) is None:
                logger.info(
                    "closing connection for %s: removed from inventory", agent_id
                )
                await websocket.close(code=4400)
                return "disconnect"

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
                # Periodic arch reconfirmation (ADR-0036, protocol 0.13): mirror a
                # strictly-recognized os_support.arch into the registry so a
                # long-lived connection stays correct even if register.meta.arch
                # were ever missing or stale. Deliberately not `_norm_arch`-normalized
                # — an unrecognized value must never clobber good data with a guess.
                reported_arch = snapshot.get("os_support", {}).get("arch")
                if reported_arch in ("x86_64", "aarch64"):
                    self.registry.note_arch(frame.agent_id, reported_arch)
                # Periodic channel reconfirmation (ADR-0048, protocol 0.17): the
                # same pattern as the arch mirror above, one release cycle later.
                reported_channel = snapshot.get("os_support", {}).get("channel")
                if reported_channel in ("stable", "dev"):
                    self.registry.note_channel(frame.agent_id, reported_channel)
                # Parental controls (ADR-0024): enrich the web_activity section
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
                try:
                    await self.store.insert(
                        frame.agent_id,
                        frame.collected_at,
                        snapshot,
                    )
                except Exception:  # noqa: BLE001 - a store hiccup must not drop the tunnel
                    # A transient DB error (e.g. SQLite lock contention) on one
                    # snapshot must not tear down the WebSocket: that turns a
                    # momentary hiccup into a reconnect storm. Log and keep serving;
                    # the agent re-pushes on its next interval.
                    logger.exception(
                        "telemetry insert failed for %s; keeping tunnel open", frame.agent_id
                    )
                else:
                    if self.after_insert is not None:
                        try:
                            self.after_insert(frame.agent_id, snapshot)
                        except Exception:  # noqa: BLE001 - a hook bug must not touch the tunnel
                            logger.exception("after_insert hook failed for %s", frame.agent_id)
                    await self._note_boot(frame.agent_id, snapshot)
                    continue
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

    def _fail_pending_for_disconnect(
        self, agent_id: str | None = None, conn_id: int | None = None
    ) -> None:
        """Fail the in-flight requests the closing connection owned.

        Only requests sent on that connection: another agent's calls, and calls
        already sent on a newer connection of the same agent, are still live.
        Without ``agent_id`` every pending request fails.
        """

        for request_id, future in list(self._pending.items()):
            owner = self._pending_owner.get(request_id)
            if agent_id is not None and owner is not None:
                if owner[0] != agent_id or (conn_id is not None and owner[1] != conn_id):
                    continue
            if not future.done():
                future.set_exception(ToolError("internal", "agent disconnected"))


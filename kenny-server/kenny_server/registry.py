"""In-memory agent registry.

Tracks which agents are connected, authenticates their tokens, exposes a
per-agent ``send_fn`` for the tunnel to push frames, and holds the operator's
"active agent" selection for forwarded tool calls.

Tokens come from the ``KENNY_AGENT_TOKENS`` env var (``id=token`` pairs,
comma-separated) or fall back to a hardcoded dev map. This is a deliberately
simple dev auth store; hardening is a later phase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from .keystore import KeyStore
    from .tokenstore import AgentTokenStore

SendFn = Callable[[dict[str, Any]], Awaitable[None]]

_DEV_TOKENS: dict[str, str] = {
    "dev": "dev-token",
    "example-pc": "dev-token-1",
}


def load_tokens() -> dict[str, str]:
    """Load the per-agent token map from env, falling back to the dev map."""

    raw = os.environ.get("KENNY_AGENT_TOKENS", "").strip()
    if not raw:
        return dict(_DEV_TOKENS)
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        agent_id, token = pair.split("=", 1)
        tokens[agent_id.strip()] = token.strip()
    return tokens or dict(_DEV_TOKENS)


@dataclass
class Agent:
    agent_id: str
    meta: dict[str, Any] = field(default_factory=dict)
    online: bool = False
    send_fn: SendFn | None = None
    connected_at: datetime | None = None
    last_seen: datetime | None = None

    @property
    def os(self) -> str:
        """Agent OS family (``windows`` | ``linux`` | ``macos``), lower-cased.

        Read-only view over ``meta["os"]`` (set from ``register.meta.os`` on the
        wire). Legacy agents that never reported an OS default to ``windows`` so
        OS-aware behavior stays backward-compatible (see ADR-0031).
        """

        return str(self.meta.get("os") or "windows").lower()

    @property
    def arch(self) -> str:
        """Normalized CPU arch (``x86_64`` | ``aarch64``).

        Read-only view over ``meta["arch"]`` (set from ``register.meta.arch`` on
        the wire, added in protocol 0.11 to fix #139). Legacy agents that never
        reported one default to ``x86_64``, matching the pre-existing behavior of
        ``distribution._norm_arch``. Imported lazily: ``distribution`` imports
        ``AgentRegistry`` from this module, so a top-level import here would cycle.
        """

        from .distribution import _norm_arch

        return _norm_arch(self.meta.get("arch"))

    @property
    def channel(self) -> str:
        """The agent's actual/built release channel (``stable`` | ``dev``).

        Read-only view over ``meta["channel"]`` (set from ``register.meta.channel``
        on the wire, added in protocol 0.17, ADR-0048). This is what the connected
        binary reports about itself, not what an operator *wants* it to run — that
        desired channel is separate, operator-editable server-side state held in
        ``store.UpdateStore`` (``get_desired_channel``/``set_desired_channel``), the
        same soll/ist split ADR-0040 already uses for version vs. running version.
        Legacy/no-channel agents and any unrecognized value default to ``stable``.
        """

        value = str(self.meta.get("channel") or "stable").lower()
        return value if value in ("stable", "dev") else "stable"

    def to_public(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "online": self.online,
            "os": self.os,
            "meta": self.meta,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


class AuthError(Exception):
    """Raised when an agent presents an unknown id or bad token."""


class AgentRegistry:
    """Tracks agents, their connections, and the active-agent selection."""

    def __init__(
        self,
        tokens: dict[str, str] | None = None,
        token_store: "AgentTokenStore | None" = None,
        key_store: "KeyStore | None" = None,
    ) -> None:
        self._tokens = tokens if tokens is not None else load_tokens()
        self._token_store = token_store
        self._key_store = key_store
        self._agents: dict[str, Agent] = {}
        self._active_agent: str | None = None
        # Per-caller active-agent slots keyed by session/PAT id, so concurrent
        # principals don't clobber each other's selection (ADR-0033). The global
        # ``_active_agent`` remains the fallback for keyless (single-operator /
        # back-compat) callers.
        self._active_by_key: dict[str, str] = {}

    @property
    def token_store(self) -> "AgentTokenStore | None":
        return self._token_store

    @token_store.setter
    def token_store(self, store: "AgentTokenStore | None") -> None:
        self._token_store = store

    @property
    def key_store(self) -> "KeyStore | None":
        return self._key_store

    @key_store.setter
    def key_store(self, store: "KeyStore | None") -> None:
        self._key_store = store

    # -- auth & connection lifecycle ---------------------------------------

    def authenticate(self, agent_id: str, token: str) -> None:
        """Synchronous dev/env-map auth (fallback when no token store is wired)."""

        expected = self._tokens.get(agent_id)
        if expected is None or token != expected:
            raise AuthError(f"authentication failed for agent {agent_id!r}")

    async def authenticate_async(self, agent_id: str, token: str) -> None:
        """Authenticate against the SQLite token store, else the dev/env map.

        The async WebSocket handshake calls this so the hashed per-agent token
        store is consulted; the in-memory dev/env map remains the bootstrap when
        no store is wired (e.g. unit tests that build a bare registry).
        """

        if self._token_store is not None:
            if await self._token_store.verify(agent_id, token):
                return
            raise AuthError(f"authentication failed for agent {agent_id!r}")
        self.authenticate(agent_id, token)

    async def authenticate_signature(
        self, agent_id: str, transcript: bytes, agent_sig: str
    ) -> None:
        """Verify the agent's Ed25519 ``auth`` signature over the transcript.

        Raises :class:`AuthError` when no key store is wired or the signature
        does not verify against the agent's stored (or grace) public key.
        """

        if self._key_store is None:
            raise AuthError("signature auth unavailable: no key store")
        if not await self._key_store.verify_signature(agent_id, transcript, agent_sig):
            raise AuthError(f"signature authentication failed for agent {agent_id!r}")

    async def register_async(
        self,
        agent_id: str,
        token: str,
        meta: dict[str, Any],
        send_fn: SendFn,
    ) -> Agent:
        """Authenticate via the token store (async) and mark the agent online."""

        await self.authenticate_async(agent_id, token)
        return self._mark_online(agent_id, meta, send_fn)

    def register_signed_async(
        self,
        agent_id: str,
        meta: dict[str, Any],
        send_fn: SendFn,
    ) -> Agent:
        """Mark an agent online after its signature already verified.

        The signature path verifies via :meth:`authenticate_signature` before the
        connection is registered, so this only marks the agent online (no token).
        """

        return self._mark_online(agent_id, meta, send_fn)

    def register(
        self,
        agent_id: str,
        token: str,
        meta: dict[str, Any],
        send_fn: SendFn,
    ) -> Agent:
        """Authenticate and mark an agent online with its send function."""

        self.authenticate(agent_id, token)
        return self._mark_online(agent_id, meta, send_fn)

    def _mark_online(
        self, agent_id: str, meta: dict[str, Any], send_fn: SendFn
    ) -> Agent:
        now = datetime.now(timezone.utc)
        agent = self._agents.get(agent_id) or Agent(agent_id=agent_id)
        agent.meta = meta
        agent.online = True
        agent.send_fn = send_fn
        agent.connected_at = now
        agent.last_seen = now
        self._agents[agent_id] = agent
        if self._active_agent is None:
            self._active_agent = agent_id
        return agent

    def mark_seen(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if agent is not None:
            agent.last_seen = datetime.now(timezone.utc)

    def note_arch(self, agent_id: str, arch: str) -> None:
        """Merge a telemetry-reported ``arch`` into the agent's stored meta.

        A periodic reconfirmation of ``register.meta.arch`` (ADR-0036, protocol
        0.13): telemetry pushes every interval for the life of the connection, so
        this self-heals if the registry's copy of ``arch`` were ever missing or
        stale, without waiting for a reconnect. Merges into the existing ``meta``
        dict in place (unlike ``_mark_online``, which replaces it wholesale) so
        other reported fields are untouched. No-op if the agent isn't known.
        """

        agent = self._agents.get(agent_id)
        if agent is not None:
            agent.meta["arch"] = arch

    def note_channel(self, agent_id: str, channel: str) -> None:
        """Merge a telemetry-reported ``channel`` into the agent's stored meta.

        A periodic reconfirmation of ``register.meta.channel`` (ADR-0048, protocol
        0.17), the same pattern as :meth:`note_arch`. Guarded to only accept a
        literal ``"stable"``/``"dev"`` so a malformed value never clobbers good
        data. Merges into the existing ``meta`` dict in place. No-op if the agent
        isn't known.
        """

        if channel not in ("stable", "dev"):
            return
        agent = self._agents.get(agent_id)
        if agent is not None:
            agent.meta["channel"] = channel

    def mark_offline(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if agent is not None:
            agent.online = False
            agent.send_fn = None

    # -- lookups -----------------------------------------------------------

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def list(self) -> list[Agent]:
        return list(self._agents.values())

    def send_fn_for(self, agent_id: str) -> SendFn:
        agent = self._agents.get(agent_id)
        if agent is None or not agent.online or agent.send_fn is None:
            raise AuthError(f"agent {agent_id!r} is not online")
        return agent.send_fn

    # -- active-agent selection -------------------------------------------

    def active_for(self, key: str | None) -> str | None:
        """The active agent for ``key`` (per-caller slot), else the global one."""

        if key is None:
            return self._active_agent
        return self._active_by_key.get(key)

    def select(self, agent_id: str, *, key: str | None = None) -> Agent:
        """Select the active agent. With ``key`` the selection is per-caller;
        without it the process-global slot is set (keyless back-compat)."""

        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"unknown agent {agent_id!r}")
        if key is None:
            self._active_agent = agent_id
        else:
            self._active_by_key[key] = agent_id
        return agent

    def clear(self, key: str) -> None:
        """Drop a per-caller selection (e.g. on logout / session end)."""

        self._active_by_key.pop(key, None)

    def remove(self, agent_id: str) -> bool:
        """Forget an agent entirely (host removed from inventory, ADR-0033).

        Pops it from the registry and evicts it from the global and every
        per-caller active slot. Returns whether it was present.
        """

        existed = self._agents.pop(agent_id, None) is not None
        if self._active_agent == agent_id:
            self._active_agent = None
        for key, selected in list(self._active_by_key.items()):
            if selected == agent_id:
                del self._active_by_key[key]
        return existed

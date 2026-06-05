"""Server-hosted Claude chat: drive a tool-use loop over kenny capabilities.

The operator chats with Claude in the web UI; Claude runs kenny tools on the
fleet. There is no local Claude Desktop — this module owns the Anthropic
tool-use loop server-side.

Two tool families are exposed to Claude (see ``tools.py``):

* **Server-only tools** — ``list_agents``, ``select_agent``, ``fleet_overview``,
  ``agent_health``, ``agent_snapshot`` — read the registry/store directly.
* **Capability tools** — every key in :data:`~kenny_server.tools.CAPABILITY_TOOLS`
  — forwarded to the active agent via ``tunnel.send_request``.

**Confirm-gate.** Tools are classified READ_ONLY vs STATE_CHANGING. Read-only
tools execute automatically. A state-changing ``tool_use`` does *not* execute:
the loop pauses, surfaces a pending-confirmation item to the UI, and only runs
after an explicit operator confirm (default is deny/confirm, never auto-allow).

The Anthropic client is injected (``run_turn(..., client=...)``) so tests pass a
fake client and no real API key is required. Prompt caching is applied to the
system prompt and the tool schemas (they are stable across requests).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from .registry import AgentRegistry
from .store import TelemetryStore
from .tools import (
    CAPABILITY_TOOLS,
    CallLog,
    ScreenshotStore,
    build_health,
)
from .tunnel import AgentTunnel, ToolError

DEFAULT_MODEL = "claude-sonnet-4-6"
_MAX_ITERATIONS = 16

# Server-only tools and their JSON-schema arg keys. These read the registry /
# store and are always READ_ONLY.
SERVER_TOOLS: dict[str, dict[str, Any]] = {
    "list_agents": {
        "description": "List known agents with online state and rolled-up health.",
        "properties": {},
        "required": [],
    },
    "select_agent": {
        "description": (
            "Set the active agent that capability tools forward to. "
            "Call this before any capability tool."
        ),
        "properties": {"id": {"type": "string", "description": "Agent id to make active."}},
        "required": ["id"],
    },
    "fleet_overview": {
        "description": "Per-agent rolled-up health for the whole fleet.",
        "properties": {},
        "required": [],
    },
    "agent_health": {
        "description": "Per-section health status/summary for one agent.",
        "properties": {"id": {"type": "string", "description": "Agent id."}},
        "required": ["id"],
    },
    "agent_snapshot": {
        "description": "Latest stored telemetry snapshot for an agent (optionally one section).",
        "properties": {
            "id": {"type": "string", "description": "Agent id."},
            "section": {"type": "string", "description": "Optional single section name."},
        },
        "required": ["id"],
    },
}

# Capability tools that change state and therefore require operator confirmation.
# Everything else (including all server-only tools) is read-only.
STATE_CHANGING_TOOLS: frozenset[str] = frozenset(
    {
        "powershell_exec",
        "winget_install",
        "winget_uninstall",
        "winget_update",
        "net_dns_flush",
        "net_adapter_reset",
        "agent_update",  # reserved for a future capability
    }
)


def is_state_changing(tool: str) -> bool:
    """True if ``tool`` must be confirmed by the operator before executing."""

    return tool in STATE_CHANGING_TOOLS


_SYSTEM_PROMPT = (
    "You are kenny, an assistant embedded in a remote-admin server. You help a "
    "trusted operator inspect and maintain a small fleet of Windows machines "
    '("agents") by calling tools.\n\n'
    "How to work:\n"
    "- Use the server-only tools (list_agents, fleet_overview, agent_health, "
    "agent_snapshot) to understand fleet state.\n"
    "- Capability tools run on a single agent. Call select_agent first to choose "
    "which machine the capability runs on; mention the agent by name in your reply.\n"
    "- Read-only tools run immediately. State-changing tools (running PowerShell, "
    "installing/uninstalling/updating packages, flushing DNS, resetting an adapter) "
    "require the operator to confirm before they run — propose them, then wait.\n"
    "- Prefer the narrowest tool that answers the question. Explain what you found "
    "in plain language; do not dump raw JSON unless asked.\n"
    "- If a tool returns an error, report it plainly and suggest a next step."
)


def _capability_schema(tool: str, arg_keys: list[str]) -> dict[str, Any]:
    """Build a JSON-schema ``input_schema`` from a CAPABILITY_TOOLS arg list.

    Arg keys ending in ``?`` are optional; ``timeout_s`` is always optional.
    All args are strings except the numeric ``timeout_s`` and ``count``.
    """

    properties: dict[str, Any] = {}
    required: list[str] = []
    for raw in arg_keys:
        optional = raw.endswith("?")
        key = raw[:-1] if optional else raw
        if key in ("timeout_s", "count"):
            properties[key] = {"type": "integer"}
        elif key == "sections":
            properties[key] = {"type": "array", "items": {"type": "string"}}
        else:
            properties[key] = {"type": "string"}
        if not optional:
            required.append(key)
    # Every forwarded call accepts an optional per-call timeout.
    properties.setdefault("timeout_s", {"type": "integer"})
    return {"type": "object", "properties": properties, "required": required}


def build_tool_schemas() -> list[dict[str, Any]]:
    """Anthropic tool schemas for every server-only + capability tool.

    Deterministic order (server tools first, then capability tools in catalog
    order) so the cached prompt prefix is stable across requests.
    """

    schemas: list[dict[str, Any]] = []
    for name, spec in SERVER_TOOLS.items():
        gated = (
            " (state-changing — requires operator confirmation)" if is_state_changing(name) else ""
        )
        schemas.append(
            {
                "name": name,
                "description": spec["description"] + gated,
                "input_schema": {
                    "type": "object",
                    "properties": spec["properties"],
                    "required": spec["required"],
                },
            }
        )
    for name, arg_keys in CAPABILITY_TOOLS.items():
        gated = (
            " (state-changing — requires operator confirmation)" if is_state_changing(name) else ""
        )
        arg_note = f" (args: {', '.join(arg_keys)})" if arg_keys else ""
        schemas.append(
            {
                "name": name,
                "description": f"Run `{name}` on the active agent{arg_note}.{gated}",
                "input_schema": _capability_schema(name, arg_keys),
            }
        )
    return schemas


# Build once; the schema set is stable for the process lifetime.
_TOOL_SCHEMAS = build_tool_schemas()


def _cached_system() -> list[dict[str, Any]]:
    """System prompt as a cacheable block (prompt caching)."""

    return [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _cached_tools() -> list[dict[str, Any]]:
    """Tool schemas with a cache breakpoint on the last definition."""

    tools = [dict(t) for t in _TOOL_SCHEMAS]
    if tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


@dataclass
class PendingCall:
    """A state-changing tool call awaiting operator confirmation."""

    id: str
    tool_use_id: str
    tool: str
    args: dict[str, Any]
    agent_id: str | None

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "agent_id": self.agent_id,
        }


@dataclass
class ChatSession:
    """Server-side conversation state for one chat session."""

    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    # tool_use blocks from the latest assistant turn that we are mid-way through
    # executing (used to resume after a confirmation decision).
    pending: PendingCall | None = None
    # Queued tool_result blocks collected before we hit a confirmation gate.
    _staged_results: list[dict[str, Any]] = field(default_factory=list)
    # tool_use blocks from the current assistant turn not yet executed.
    _queue: list[dict[str, Any]] = field(default_factory=list)


class ChatSessions:
    """In-memory registry of chat sessions keyed by session id."""

    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}

    def get_or_create(self, session_id: str | None) -> ChatSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or uuid.uuid4().hex
        session = ChatSession(id=sid)
        self._sessions[sid] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)


@dataclass
class TurnResult:
    """Structured outcome of a chat turn for the UI."""

    session_id: str
    assistant_text: str
    tool_events: list[dict[str, Any]]
    pending: dict[str, Any] | None
    done: bool

    def to_public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "assistant_text": self.assistant_text,
            "tool_events": self.tool_events,
            "pending": self.pending,
            "done": self.done,
        }


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Normalize an Anthropic content block (SDK object or dict) to a dict."""

    if isinstance(block, dict):
        return block
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}) or {},
        }
    # Fall back to a best-effort serialization.
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


def _assistant_content(response: Any) -> list[dict[str, Any]]:
    return [_block_to_dict(b) for b in getattr(response, "content", [])]


class ChatExecutor:
    """Executes tool calls against the registry/store/tunnel and records them."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        store: TelemetryStore,
        tunnel: AgentTunnel,
        call_log: CallLog,
        screenshots: ScreenshotStore,
    ) -> None:
        self.registry = registry
        self.store = store
        self.tunnel = tunnel
        self.call_log = call_log
        self.screenshots = screenshots

    async def run_server_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "list_agents":
            return await self._list_agents()
        if tool == "select_agent":
            return await self._select_agent(str(args["id"]))
        if tool == "fleet_overview":
            return await self._fleet_overview()
        if tool == "agent_health":
            return await self._agent_health(str(args["id"]))
        if tool == "agent_snapshot":
            return await self._agent_snapshot(str(args["id"]), args.get("section"))
        raise ToolError("unknown_tool", f"unknown server tool {tool!r}")

    async def run_capability(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Forward a capability tool to the active agent (read-only or confirmed)."""

        agent_id = self.registry.require_active()
        timeout_s = float(args.get("timeout_s", 30))
        try:
            result = await self.tunnel.send_request(agent_id, tool, args, timeout_s)
            self.call_log.record(agent_id, tool, args, ok=True)
            if tool == "screen_capture" and isinstance(result, dict) and "image_b64" in result:
                self.screenshots.put(agent_id, result["image_b64"], result.get("format", "png"))
            return result
        except ToolError as exc:
            self.call_log.record(agent_id, tool, args, ok=False, error=exc.message)
            raise

    # -- server-only tool implementations ---------------------------------

    async def _known_ids(self) -> list[str]:
        ids = {a.agent_id for a in self.registry.list()}
        ids.update(await self.store.known_agents())
        return sorted(ids)

    async def _overview(self, agent_id: str) -> dict[str, Any]:
        agent = self.registry.get(agent_id)
        latest = await self.store.latest(agent_id)
        snapshot = latest["snapshot"] if latest else None
        health = build_health(snapshot)
        flagged = [n for n, s in health["sections"].items() if s["status"] in ("warn", "crit")]
        return {
            "agent_id": agent_id,
            "online": bool(agent and agent.online),
            "meta": agent.meta if agent else {},
            "overall": health["overall"],
            "flagged_sections": flagged,
            "collected_at": latest["collected_at"] if latest else None,
        }

    async def _list_agents(self) -> dict[str, Any]:
        ids = await self._known_ids()
        agents = [await self._overview(i) for i in ids]
        return {"active_agent": self.registry.active_agent, "agents": agents}

    async def _select_agent(self, agent_id: str) -> dict[str, Any]:
        try:
            agent = self.registry.select(agent_id)
        except KeyError:
            if agent_id in await self.store.known_agents():
                self.registry._active_agent = agent_id  # noqa: SLF001 (matches tools.py dev path)
                return {"active_agent": agent_id, "online": False}
            raise ToolError("unknown_agent", f"unknown agent {agent_id!r}")
        return {"active_agent": agent.agent_id, "online": agent.online}

    async def _fleet_overview(self) -> dict[str, Any]:
        from . import health_rules

        ids = await self._known_ids()
        agents = [await self._overview(i) for i in ids]
        overall = health_rules.worst(*(a["overall"] for a in agents if a["overall"] != "unknown"))
        return {"overall": overall or "unknown", "agents": agents}

    async def _agent_health(self, agent_id: str) -> dict[str, Any]:
        latest = await self.store.latest(agent_id)
        snapshot = latest["snapshot"] if latest else None
        health = build_health(snapshot)
        return {
            "agent_id": agent_id,
            "collected_at": latest["collected_at"] if latest else None,
            **health,
        }

    async def _agent_snapshot(self, agent_id: str, section: str | None) -> dict[str, Any]:
        latest = await self.store.latest(agent_id)
        if latest is None:
            return {"agent_id": agent_id, "snapshot": None}
        snapshot = latest["snapshot"]
        if section is not None:
            return {
                "agent_id": agent_id,
                "collected_at": latest["collected_at"],
                "section": section,
                "payload": snapshot.get(section),
            }
        return {
            "agent_id": agent_id,
            "collected_at": latest["collected_at"],
            "snapshot": snapshot,
        }


def _image_of(payload: Any) -> tuple[str, str] | None:
    """Return ``(image_b64, format)`` if ``payload`` is a screenshot result, else None."""

    if isinstance(payload, dict) and "image_b64" in payload:
        return payload["image_b64"], payload.get("format", "png")
    return None


def _tool_result_block(tool_use_id: str, payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
    }
    image = None if is_error else _image_of(payload)
    if image is not None:
        image_b64, fmt = image
        # Feed the screenshot to Claude as an image content block so the model
        # actually sees the pixels (not a base64 text blob it can't decode).
        block["content"] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": f"image/{fmt}",
                    "data": image_b64,
                },
            },
            {"type": "text", "text": "screen_capture (png)"},
        ]
    else:
        block["content"] = json.dumps(payload, default=str)
    if is_error:
        block["is_error"] = True
    return block


async def _execute_one(
    executor: ChatExecutor, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Run one tool, returning (result_payload, is_error)."""

    try:
        if tool in SERVER_TOOLS:
            return await executor.run_server_tool(tool, args), False
        return await executor.run_capability(tool, args), False
    except ToolError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}, True


async def _drive(
    session: ChatSession,
    executor: ChatExecutor,
    *,
    client: Any,
    model: str,
) -> TurnResult:
    """Run the tool-use loop until end_turn or a confirmation gate.

    Resumes from ``session._queue`` / ``session._staged_results`` so a confirmed
    tool can continue mid-turn without re-asking the model.
    """

    tool_events: list[dict[str, Any]] = []

    for _ in range(_MAX_ITERATIONS):
        # Drain any queued tool_use blocks from the prior assistant turn.
        while session._queue:
            block = session._queue.pop(0)
            tool = block["name"]
            args = dict(block.get("input") or {})

            if is_state_changing(tool):
                agent_id = executor.registry.active_agent
                session.pending = PendingCall(
                    id=uuid.uuid4().hex,
                    tool_use_id=block["id"],
                    tool=tool,
                    args=args,
                    agent_id=agent_id,
                )
                tool_events.append(
                    {"type": "pending", "tool": tool, "args": args, "agent_id": agent_id}
                )
                # Pause: hold the remaining queue + staged results for resume.
                return TurnResult(
                    session_id=session.id,
                    assistant_text=_latest_text(session),
                    tool_events=tool_events,
                    pending=session.pending.to_public(),
                    done=False,
                )

            payload, is_error = await _execute_one(executor, tool, args)
            event = {"type": "tool_result", "tool": tool, "args": args, "ok": not is_error}
            image = None if is_error else _image_of(payload)
            if image is not None:
                event["image_b64"], event["format"] = image
            tool_events.append(event)
            session._staged_results.append(
                _tool_result_block(block["id"], payload, is_error=is_error)
            )

        # All queued tools ran; if we have staged results, feed them back.
        if session._staged_results:
            session.messages.append({"role": "user", "content": session._staged_results})
            session._staged_results = []

        # Ask the model for the next step.
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_cached_system(),
            tools=_cached_tools(),
            messages=session.messages,
        )
        content = _assistant_content(response)
        session.messages.append({"role": "assistant", "content": content})

        stop_reason = getattr(response, "stop_reason", None)
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        if stop_reason == "tool_use" or tool_uses:
            session._queue = tool_uses
            continue

        # end_turn (or no tools requested): turn complete.
        return TurnResult(
            session_id=session.id,
            assistant_text=_text_of(content),
            tool_events=tool_events,
            pending=None,
            done=True,
        )

    # Iteration cap hit; return what we have.
    return TurnResult(
        session_id=session.id,
        assistant_text=_latest_text(session),
        tool_events=tool_events,
        pending=None,
        done=True,
    )


def _text_of(content: list[dict[str, Any]]) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def _latest_text(session: ChatSession) -> str:
    for msg in reversed(session.messages):
        if msg["role"] == "assistant":
            content = msg["content"]
            if isinstance(content, list):
                return _text_of(content)
    return ""


async def run_turn(
    session: ChatSession,
    user_text: str,
    *,
    executor: ChatExecutor,
    client: Any,
    model: str | None = None,
) -> TurnResult:
    """Send a user message and drive the tool-use loop to completion or a gate.

    ``client`` is injected (real ``anthropic.Anthropic`` in production, a fake in
    tests). Returns a :class:`TurnResult`; if ``pending`` is set the loop paused
    on a state-changing tool and is resumed via :func:`confirm_pending`.
    """

    if session.pending is not None:
        raise RuntimeError("session has a pending confirmation; resolve it first")

    model = model or os.environ.get("KENNY_CHAT_MODEL", DEFAULT_MODEL)
    session.messages.append({"role": "user", "content": user_text})
    return await _drive(session, executor, client=client, model=model)


async def confirm_pending(
    session: ChatSession,
    *,
    approve: bool,
    executor: ChatExecutor,
    client: Any,
    model: str | None = None,
) -> TurnResult:
    """Resolve a pending state-changing call, then resume the tool-use loop.

    On approve the tool executes and its result is fed back; on deny a
    ``denied`` tool_result is fed back so the model can react. Default policy is
    deny — callers must explicitly pass ``approve=True``.
    """

    pending = session.pending
    if pending is None:
        raise RuntimeError("no pending confirmation for this session")

    model = model or os.environ.get("KENNY_CHAT_MODEL", DEFAULT_MODEL)
    session.pending = None

    if approve:
        payload, is_error = await _execute_one(executor, pending.tool, pending.args)
        session._staged_results.append(
            _tool_result_block(pending.tool_use_id, payload, is_error=is_error)
        )
        resume_event = {
            "type": "tool_result",
            "tool": pending.tool,
            "args": pending.args,
            "ok": not is_error,
        }
        image = None if is_error else _image_of(payload)
        if image is not None:
            resume_event["image_b64"], resume_event["format"] = image
    else:
        payload = {"error": {"code": "denied", "message": "operator denied this action"}}
        session._staged_results.append(
            _tool_result_block(pending.tool_use_id, payload, is_error=True)
        )
        resume_event = {
            "type": "denied",
            "tool": pending.tool,
            "args": pending.args,
            "agent_id": pending.agent_id,
        }

    result = await _drive(session, executor, client=client, model=model)
    result.tool_events.insert(0, resume_event)
    return result

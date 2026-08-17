"""The surface-independent tool-use core: catalog, executor, and the loop.

This is the Anthropic tool-use loop with every surface-specific decision lifted
out into a :class:`LoopPolicy`. The loop knows how to talk to the model, run a
tool, feed the result back, pause, and resume. It does **not** know which system
prompt to send, which tools to expose, where a call is routed, or whether a call
may proceed — a policy object answers all four.

That split is what makes a second surface possible without forking the loop: the
dashboard's policy (``chat.FleetPolicy``) holds every state-changing call for
an operator confirmation, and a different surface can hold, deny or allow on its
own terms while the loop's event shapes, ordering and truncation stay identical.

The loop is deliberately duck-typed over its session object. It touches only
``.id``, ``.messages``, ``.agent_id``, ``.pending``, ``._queue`` and
``._staged_results`` — no base class, no ``isinstance`` checks — and it never
imports ``chat.py``, so the dependency runs one way: ``chat`` -> ``toolloop``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass
from typing import Any, Protocol

from .registry import AgentRegistry
from .store import TelemetryStore
from .tool_classes import READ_ONLY, classify, is_state_changing
from .tools import (
    CAPABILITY_TOOLS,
    CallLog,
    ScreenshotStore,
    build_health,
)
from .tunnel import AgentTunnel, ToolError

_MAX_ITERATIONS = 16
# Cap the serialized size of a single tool result fed back to the model. Agent
# telemetry, fs_read file contents, and command output are attacker-influenceable
# (a compromised agent controls them), so bound the volume to limit both context
# blow-up and the surface for second-order prompt injection (CWE-400 / CWE-94 adj.).
_MAX_TOOL_RESULT_CHARS = 100_000

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


# -- gate decisions ---------------------------------------------------------
#
# What a surface may answer when the loop asks "may this call proceed?". The
# three outcomes are the only ones the loop knows how to act on; a surface that
# wants a fourth has to express it as one of these.


@dataclass(frozen=True)
class Allow:
    """Execute the call now."""


@dataclass(frozen=True)
class Deny:
    """Refuse the call; the model is told, in the shape of an error result."""

    code: str
    message: str


@dataclass(frozen=True)
class Hold:
    """Pause the turn until someone decides. ``kind`` names whose decision it is."""

    kind: str  # "operator_approval" | "user_consent"


GateDecision = Allow | Deny | Hold


class LoopPolicy(Protocol):
    """Everything the loop must ask its calling surface.

    Implementations are duck-typed (no registration, no base class). ``session``
    is whatever session object the caller drives the loop with.
    """

    def system_blocks(self, session: Any) -> list[dict[str, Any]]: ...

    def tool_schemas(self) -> list[dict[str, Any]]: ...

    def resolve_target(self, session: Any, tool: str, args: dict[str, Any]) -> str | None: ...

    async def gate(
        self, session: Any, tool: str, args: dict[str, Any], agent_id: str | None
    ) -> GateDecision: ...

    async def on_hold(self, session: Any, pending: "PendingCall") -> None: ...


@dataclass
class PendingCall:
    """A state-changing tool call awaiting operator confirmation."""

    id: str
    tool_use_id: str
    tool: str
    args: dict[str, Any]
    agent_id: str | None
    # Set by the loop from the gate decision. Defaulted so existing construction
    # sites (and rehydrated state) stay valid; carried for surfaces that need to
    # render *why* a call is held, not just that it is.
    tool_class: str = ""
    gate_kind: str = "operator_approval"

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "agent_id": self.agent_id,
        }


# -- tool schemas -----------------------------------------------------------


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
    # Every forwarded call accepts an optional per-call timeout, and an optional
    # agent_id override (ADR-0038): the chat session already tracks a selected
    # agent (the dashboard's "context" pill), so this is only needed to target a
    # different host for one call; omitted, it falls back to the session's
    # selection and fails closed if neither is set.
    properties.setdefault("timeout_s", {"type": "integer"})
    properties.setdefault("agent_id", {"type": "string"})
    return {"type": "object", "properties": properties, "required": required}


def build_tool_schemas(allowed: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """Anthropic tool schemas for every server-only + capability tool.

    Deterministic order (server tools first, then capability tools in catalog
    order) so the cached prompt prefix is stable across requests.

    ``allowed``, when given, narrows the emitted set to those names (a surface
    that exposes only part of the catalog). ``None`` — the default — emits the
    full catalog exactly as before.
    """

    schemas: list[dict[str, Any]] = []
    for name, spec in SERVER_TOOLS.items():
        if allowed is not None and name not in allowed:
            continue
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
        if allowed is not None and name not in allowed:
            continue
        gated = (
            " (state-changing — requires operator confirmation)" if is_state_changing(name) else ""
        )
        arg_note = f" (args: {', '.join(arg_keys)})" if arg_keys else ""
        desc = (
            f"Run `{name}` on the currently selected agent{arg_note}. "
            f"Pass agent_id to target a different host for this one call.{gated}"
        )
        schemas.append(
            {
                "name": name,
                "description": desc,
                "input_schema": _capability_schema(name, arg_keys),
            }
        )
    return schemas


# -- content-block helpers --------------------------------------------------


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


def _tool_result_image(content: Any) -> tuple[str, str] | None:
    """Extract ``(image_b64, format)`` from a tool_result content list, if any."""

    if not isinstance(content, list):
        return None
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image":
            source = part.get("source") or {}
            media_type = source.get("media_type", "image/png")
            return source.get("data", ""), media_type.rsplit("/", 1)[-1]
    return None


def _text_of(content: list[dict[str, Any]]) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def _latest_text(session: Any) -> str:
    for msg in reversed(session.messages):
        if msg["role"] == "assistant":
            content = msg["content"]
            if isinstance(content, list):
                return _text_of(content)
    return ""


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
        text = json.dumps(payload, default=str)
        if len(text) > _MAX_TOOL_RESULT_CHARS:
            dropped = len(text) - _MAX_TOOL_RESULT_CHARS
            text = text[:_MAX_TOOL_RESULT_CHARS] + f"…[truncated {dropped} chars]"
        block["content"] = text
    if is_error:
        block["is_error"] = True
    return block


def _fold_staged_results(session: Any) -> None:
    """Feed ``session._staged_results`` back, clearing it.

    Ordinarily this is the first content of a fresh user turn, so it starts a
    new message. The one exception: a ticket session can have a plain user
    message land on it *between* a hold and its resume (rebuilt from SQLite
    each time — see ``ticket_assistant.session_for``'s and
    ``discord_service.py``'s context-message path) — a third party's message,
    appended and saved with no turn driven, while the gate that will
    eventually stage this ``tool_result`` is still open. Appending
    unconditionally there would produce two consecutive ``"user"`` messages,
    which the Messages API rejects; joining into the existing trailing user
    message keeps the transcript valid instead. A string content is
    normalized to a text block first so the join is always list + list.
    """

    last = session.messages[-1] if session.messages else None
    if last is not None and last.get("role") == "user":
        content = last.get("content")
        if isinstance(content, str):
            last["content"] = [{"type": "text", "text": content}, *session._staged_results]
            session._staged_results = []
            return
        if isinstance(content, list):
            content.extend(session._staged_results)
            session._staged_results = []
            return
    session.messages.append({"role": "user", "content": session._staged_results})
    session._staged_results = []


# -- execution --------------------------------------------------------------


class ToolExecutor:
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

    async def run_server_tool(
        self, tool: str, args: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        if tool == "list_agents":
            return await self._list_agents(session)
        if tool == "select_agent":
            result = await self._select_agent(str(args["id"]))
            if session is not None:
                # The session's own selection is the only state this changes
                # (ADR-0038) — no shared registry slot is written, so concurrent
                # sessions can never clobber each other's target.
                session.agent_id = result.get("active_agent") or session.agent_id
            return result
        if tool == "fleet_overview":
            return await self._fleet_overview()
        if tool == "agent_health":
            return await self._agent_health(str(args["id"]))
        if tool == "agent_snapshot":
            return await self._agent_snapshot(str(args["id"]), args.get("section"))
        raise ToolError("unknown_tool", f"unknown server tool {tool!r}")

    async def run_capability(
        self, tool: str, args: dict[str, Any], *, agent_id: str
    ) -> dict[str, Any]:
        """Forward a capability tool to ``agent_id`` (read-only or confirmed).

        ``agent_id`` is resolved by the caller (:func:`_resolve_chat_target`) —
        the session's selected agent or an explicit per-call override — never
        the process-global registry slot, which is shared by every concurrent
        chat session and would let one session's selection bleed into another's
        forwarded call (ADR-0038).
        """

        if not agent_id:
            raise ToolError("no_agent", "no target agent for this call")
        timeout_s = float(args.get("timeout_s", 30))
        try:
            result = await self.tunnel.send_request(agent_id, tool, args, timeout_s)
            await self.call_log.record(agent_id, tool, args, ok=True)
            if tool == "screen_capture" and isinstance(result, dict) and "image_b64" in result:
                self.screenshots.put(agent_id, result["image_b64"], result.get("format", "png"))
            return result
        except ToolError as exc:
            await self.call_log.record(agent_id, tool, args, ok=False, error=exc.message)
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
        agent_os = agent.os if agent else "windows"
        health = build_health(snapshot, agent_os=agent_os)
        flagged = [n for n, s in health["sections"].items() if s["status"] in ("warn", "crit")]
        return {
            "agent_id": agent_id,
            "online": bool(agent and agent.online),
            "os": agent_os,
            "meta": agent.meta if agent else {},
            "overall": health["overall"],
            "flagged_sections": flagged,
            "collected_at": latest["collected_at"] if latest else None,
        }

    async def _list_agents(self, session: Any = None) -> dict[str, Any]:
        ids = await self._known_ids()
        agents = [await self._overview(i) for i in ids]
        active = session.agent_id if session is not None else None
        return {"active_agent": active, "agents": agents}

    async def _select_agent(self, agent_id: str) -> dict[str, Any]:
        """Validate ``agent_id`` and report it.

        Deliberately does **not** write any registry slot (ADR-0038): the
        session's own ``agent_id`` (set by the caller in :meth:`run_server_tool`)
        is the only state that carries this selection forward, so it can never
        be shared with — or clobbered by — another concurrent session.
        """

        agent = self.registry.get(agent_id)
        if agent is not None:
            return {"active_agent": agent.agent_id, "online": agent.online}
        if agent_id in await self.store.known_agents():
            # Known only from stored telemetry (currently offline/unregistered).
            return {"active_agent": agent_id, "online": False}
        raise ToolError("unknown_agent", f"unknown agent {agent_id!r}")

    async def _fleet_overview(self) -> dict[str, Any]:
        from . import health_rules

        ids = await self._known_ids()
        agents = [await self._overview(i) for i in ids]
        overall = health_rules.worst(*(a["overall"] for a in agents if a["overall"] != "unknown"))
        return {"overall": overall or "unknown", "agents": agents}

    async def _agent_health(self, agent_id: str) -> dict[str, Any]:
        latest = await self.store.latest(agent_id)
        snapshot = latest["snapshot"] if latest else None
        agent = self.registry.get(agent_id)
        health = build_health(snapshot, agent_os=agent.os if agent else "windows")
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


def _resolve_chat_target(session: Any, args: dict[str, Any]) -> str:
    """Routing target for a chat-forwarded capability call (ADR-0038).

    An explicit ``agent_id`` in ``args`` overrides for this one call; otherwise
    falls back to the session's own selection (safe — each chat session is a
    distinct conversation, unlike the process-global registry slot). Pops
    ``agent_id`` off ``args`` so it is routing metadata only and never reaches
    the forwarded tool call. Fails closed when neither is set.
    """

    explicit = args.pop("agent_id", None)
    target = (str(explicit).strip() if explicit else "") or (session.agent_id or "")
    if not target:
        raise ToolError(
            "no_agent",
            "no agent selected for this chat; select an agent in the dashboard "
            "or pass agent_id",
        )
    return target


def _tier_auto_runs(tool: str) -> bool:
    """True when ``tool``'s *tier* is read-only, i.e. nothing has to decide it.

    Read from :func:`~kenny_server.tool_classes.classify` and nothing else, so
    this can never disagree with the tier map, and never varies with a session's
    scope or selected host: the tier is a property of the tool, the gate is a
    property of the calling surface (ADR-0045). Used for the one ``tool_result``
    the loop emits *before* reaching the gate (a routing failure); the events
    emitted after a gate decision report that decision directly instead.
    """

    return classify(tool) == READ_ONLY


async def _execute_one(
    executor: ToolExecutor,
    tool: str,
    args: dict[str, Any],
    *,
    session: Any,
    agent_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run one tool, returning (result_payload, is_error).

    ``agent_id``, when given, is an already-resolved target — used when
    resuming a confirmed state-changing call so the target frozen at gate time
    (:class:`PendingCall.agent_id`) is reused rather than re-resolved (the
    original ``args`` no longer carry any explicit override by then, since
    :func:`_resolve_chat_target` already popped it).
    """

    try:
        if tool in SERVER_TOOLS:
            return await executor.run_server_tool(tool, args, session=session), False
        target = agent_id or _resolve_chat_target(session, args)
        return await executor.run_capability(tool, args, agent_id=target), False
    except ToolError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}, True


# -- the loop ---------------------------------------------------------------


async def drive_events(
    session: Any,
    executor: ToolExecutor,
    *,
    client: Any,
    model: str,
    policy: LoopPolicy,
) -> AsyncIterator[dict[str, Any]]:
    """Run the tool-use loop, yielding structured events as they happen.

    This is the single source of truth for the loop. It yields, in order:

    * ``{"type": "text_delta", "text": ...}`` — one per token as the assistant
      block streams;
    * ``{"type": "tool_result", "tool": ..., "args": ..., "ok": bool,
      "auto_run": bool[, "error": {"code": ..., "message": ...}][, "image_b64",
      "format"]}`` — emitted the moment each tool executes (live); ``error`` is
      present iff ``ok`` is false. ``auto_run`` says the loop ran the call
      without anyone deciding it, which is what separates a read-only call the
      operator only ever sees *after* the fact from a state-changing one they
      had to approve first (the ``pending`` event below). It reports the gate
      outcome, never the scope: a session's selected host cannot change it;
    * ``{"type": "pending", "tool": ..., "args": ..., "agent_id": ...}`` — a call
      the policy held, awaiting a decision;
    * ``{"type": "denied", "tool": ..., "args": ..., "agent_id": ..., "code": ...,
      "message": ...}`` — a call the policy refused outright (the model is told
      and the loop continues);
    * ``{"type": "done", "session_id": ..., "assistant_text": ..., "pending":
      dict|None, "done": bool}`` — terminal, carrying the scalars a
      ``TurnResult`` needs.

    ``chat._drive`` drains this into a ``TurnResult`` for the non-streaming JSON
    endpoints; the SSE endpoints forward the events verbatim. Resumes from
    ``session._queue`` / ``session._staged_results`` so a confirmed tool can
    continue mid-turn without re-asking the model.

    Note: ``stream.text_stream`` performs blocking network reads on the event
    loop — the same tradeoff as the previous blocking ``messages.create()``,
    acceptable for this single-user, self-hosted dashboard.
    """

    for _ in range(_MAX_ITERATIONS):
        # Drain any queued tool_use blocks from the prior assistant turn.
        while session._queue:
            block = session._queue.pop(0)
            tool = block["name"]
            args = dict(block.get("input") or {})

            # Resolve + freeze the routing target now, before any confirm-gate
            # pause, so a dashboard agent switch that happens while a
            # state-changing call awaits confirmation can't retarget it
            # (ADR-0038). Server-only tools name their own host via `id`, if any.
            target: str | None = None
            try:
                target = policy.resolve_target(session, tool, args)
            except ToolError as exc:
                payload = {"error": {"code": exc.code, "message": exc.message}}
                yield {
                    "type": "tool_result",
                    "tool": tool,
                    "args": args,
                    "ok": False,
                    # Routing failed before the gate was consulted, so there is
                    # no decision to report — fall back to the tool's own tier.
                    "auto_run": _tier_auto_runs(tool),
                    "error": payload["error"],
                }
                session._staged_results.append(
                    _tool_result_block(block["id"], payload, is_error=True)
                )
                continue

            decision = await policy.gate(session, tool, args, target)

            if isinstance(decision, Deny):
                # Same shape the confirm path stages for an operator denial: the
                # model sees an error tool_result and can react in the same turn.
                payload = {"error": {"code": decision.code, "message": decision.message}}
                yield {
                    "type": "denied",
                    "tool": tool,
                    "args": args,
                    "agent_id": target,
                    "code": decision.code,
                    "message": decision.message,
                }
                session._staged_results.append(
                    _tool_result_block(block["id"], payload, is_error=True)
                )
                continue

            if isinstance(decision, Hold):
                session.pending = PendingCall(
                    id=uuid.uuid4().hex,
                    tool_use_id=block["id"],
                    tool=tool,
                    args=args,
                    agent_id=target,
                    tool_class=classify(tool),
                    gate_kind=decision.kind,
                )
                # Record the hold BEFORE announcing it. A surface that has to
                # durably store the pending call (to resolve it minutes later, or
                # after a restart) must not depend on the consumer draining this
                # generator — one that breaks on `done` would otherwise show a
                # pending gate that was never persisted. Transient surfaces make
                # this a no-op.
                await policy.on_hold(session, session.pending)
                yield {"type": "pending", "tool": tool, "args": args, "agent_id": target}
                # Pause: hold the remaining queue + staged results for resume.
                yield {
                    "type": "done",
                    "session_id": session.id,
                    "assistant_text": _latest_text(session),
                    "pending": session.pending.to_public(),
                    "done": False,
                }
                return

            payload, is_error = await _execute_one(
                executor, tool, args, session=session, agent_id=target
            )
            event: dict[str, Any] = {
                "type": "tool_result",
                "tool": tool,
                "args": args,
                "ok": not is_error,
                # Reached only on an ``Allow``: the policy let this run with
                # nobody asked. On the dashboard chat that is exactly the
                # read-only tier (``chat.FleetPolicy.gate`` holds both change
                # tiers), which is what the frontend contract states.
                "auto_run": True,
            }
            if is_error:
                event["error"] = payload.get("error")
            image = None if is_error else _image_of(payload)
            if image is not None:
                event["image_b64"], event["format"] = image
            yield event
            session._staged_results.append(
                _tool_result_block(block["id"], payload, is_error=is_error)
            )

        # All queued tools ran; if we have staged results, feed them back.
        if session._staged_results:
            _fold_staged_results(session)

        # Ask the model for the next step, streaming the assistant text token by token.
        with client.messages.stream(
            model=model,
            max_tokens=4096,
            system=policy.system_blocks(session),
            tools=policy.tool_schemas(),
            messages=session.messages,
        ) as stream:
            for text in stream.text_stream:
                yield {"type": "text_delta", "text": text}
            response = stream.get_final_message()
        content = _assistant_content(response)
        session.messages.append({"role": "assistant", "content": content})

        stop_reason = getattr(response, "stop_reason", None)
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        if stop_reason == "tool_use" or tool_uses:
            session._queue = tool_uses
            continue

        # end_turn (or no tools requested): turn complete.
        yield {
            "type": "done",
            "session_id": session.id,
            "assistant_text": _text_of(content),
            "pending": None,
            "done": True,
        }
        return

    # Iteration cap hit; return what we have.
    yield {
        "type": "done",
        "session_id": session.id,
        "assistant_text": _latest_text(session),
        "pending": None,
        "done": True,
    }


def stage_missing_tool_results(
    session: Any, *, exempt: Collection[str] = ()
) -> list[str]:
    """Answer any ``tool_use`` in the trailing assistant message left dangling.

    A session can be rebuilt mid-turn (a restart, a crashed process, an
    abandoned resume) with its trailing message holding one or more
    ``tool_use`` blocks that nothing ever answered — the next model call would
    be rejected outright (a ``tool_use`` with no matching ``tool_result``).
    This finds exactly those ids: not already answered anywhere in
    ``session.messages``, not staged in ``session._staged_results``, not still
    parked in ``session._queue`` (queued for execution, not abandoned), not
    ``session.pending``'s id (a live gate waiting on a decision), and not in
    ``exempt`` (a caller-supplied "leave this one alone" set — notably a gate
    a caller is about to answer itself). Each survivor gets an error
    ``tool_result`` staged for it, so the next model call is always valid.

    Deliberately narrower than :func:`kenny_server.chat.heal_session`: this
    never touches ``session.messages``, ``session._queue`` or
    ``session.pending`` — only ``session._staged_results`` grows. See that
    function's docstring for why the two must not be unified.

    Returns the healed ids, in the order they appear in the trailing message.
    """

    messages = session.messages
    if not messages:
        return []
    last = messages[-1]
    if last.get("role") != "assistant":
        return []
    content = last.get("content")
    if not isinstance(content, list):
        return []
    tool_use_ids = [
        b.get("id") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    if not tool_use_ids:
        return []

    answered: set[str] = set()
    for msg in messages:
        msg_content = msg.get("content")
        if not isinstance(msg_content, list):
            continue
        for block in msg_content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if tool_use_id:
                    answered.add(tool_use_id)
    for block in session._staged_results:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id")
            if tool_use_id:
                answered.add(tool_use_id)
    queued_ids = {
        block.get("id") for block in session._queue if isinstance(block, dict)
    }
    pending_id = session.pending.tool_use_id if session.pending is not None else None
    exempt_set = set(exempt)

    healed: list[str] = []
    for tool_use_id in tool_use_ids:
        if not tool_use_id:
            continue
        if (
            tool_use_id in answered
            or tool_use_id in queued_ids
            or tool_use_id == pending_id
            or tool_use_id in exempt_set
        ):
            continue
        session._staged_results.append(
            _tool_result_block(
                tool_use_id,
                {
                    "error": {
                        "code": "not_completed",
                        "message": (
                            "this call was interrupted and never ran; nothing was "
                            "changed on the machine."
                        ),
                    }
                },
                is_error=True,
            )
        )
        healed.append(tool_use_id)
    return healed


async def apply_confirmation(
    session: Any, *, approve: bool, executor: ToolExecutor
) -> dict[str, Any]:
    """Resolve the pending call (run on approve, feed a denial otherwise).

    Clears ``session.pending``, stages the tool_result block for the resumed
    loop, and returns the ``resume_event`` to surface first to the UI. Shared by
    ``chat.confirm_pending`` and ``chat.confirm_pending_events``.
    """

    pending = session.pending
    assert pending is not None  # callers check before delegating
    session.pending = None

    if approve:
        payload, is_error = await _execute_one(
            executor, pending.tool, pending.args, session=session, agent_id=pending.agent_id
        )
        session._staged_results.append(
            _tool_result_block(pending.tool_use_id, payload, is_error=is_error)
        )
        resume_event: dict[str, Any] = {
            "type": "tool_result",
            "tool": pending.tool,
            "args": pending.args,
            "ok": not is_error,
            # This call was held and explicitly decided; by construction it did
            # not run unattended, whatever its tier.
            "auto_run": False,
        }
        if is_error:
            resume_event["error"] = payload.get("error")
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
            "code": "denied",
            "message": "operator denied this action",
        }
    return resume_event

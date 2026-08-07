# 0009. Server-hosted Claude chat with a confirm-gate

- Status: accepted
- Amended by: [ADR-0045](0045-tiered-tool-classification.md)
- Date: 2026-06-04

## Context and Problem Statement

The operator should be able to drive agents through Claude **without** running a local
Claude Desktop/MCP client — i.e. chat with Claude directly in the kenny web UI, and have
Claude execute capability tools on the selected agent. We need this without giving an LLM
unsupervised power over family PCs.

## Considered Options

- Server-hosted Anthropic tool-use loop in `kenny-server`, with a confirm-gate for
  state-changing tools.
- Have the server act as an MCP client to its own MCP endpoint.
- No server chat (require a local MCP client).

## Decision Outcome

Chosen option: "Server-hosted Anthropic tool-use loop with a confirm-gate". `chat.py`
runs the agentic loop with the Anthropic SDK; tool schemas are generated from the existing
`CAPABILITY_TOOLS` catalog plus the server-only tools, and tool execution reuses
`registry.select` + `AgentTunnel.send_request` (every call audited in `CallLog`).

**Confirm-gate (the key safety property):** tools are classified READ_ONLY vs
STATE_CHANGING. Read-only tools (`diag.*`, `fs.read/list/search/disk_usage`, `winget.list`,
`net.config`, `screen.capture`, `telemetry.collect`, all server-only tools) auto-execute.
State-changing tools (`powershell.exec`, `winget.install/uninstall/update`,
`net.dns_flush`, `net.adapter_reset`, and the reserved `agent.update`) **never** run until
the operator confirms: the loop pauses, surfaces a pending `{tool, args, agent_id}`, and
only executes on explicit approval. Default is deny/confirm — anything not explicitly
read-only is treated as state-changing.

### Consequences

- Good, because no local client is needed and the LLM cannot mutate a PC without a human OK.
- Good, because it reuses the tunnel/registry/audit path and the operator auth on `/api/*`.
- Good, because the Anthropic client is dependency-injected, so the loop is testable without
  an API key; prompt caching is applied to the (stable) system prompt + tool schemas.
- Bad, because chat session state is in-memory (dev-grade), lost on restart — acceptable now.
  **Superseded by [ADR-0025](0025-persistent-chat-history.md): chat history is now persisted to
  SQLite and survives a restart.**

## More Information

- API: `POST /api/chat {session_id?, message}` and `POST /api/chat/confirm {session_id, approve}`,
  returning `{session_id, assistant_text, tool_events[], pending|null, done}`.
- Config: `ANTHROPIC_API_KEY`, model `KENNY_CHAT_MODEL` (default `claude-sonnet-4-6`).
- Implementation: `kenny-server/kenny_server/chat.py`, routes in `webui/`, UI tab in `index.html`.
- Persistence superseded by ADR-0025 (persistent, resumable chat history) — everything else in
  this ADR (confirm-gate model, tool classification) still stands.

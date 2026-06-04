# 0006. MCP over Streamable HTTP between Claude and the server

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

Claude (the operator's client) needs to reach `kenny-server`, which runs in the
cloud, not on the operator's machine. We must choose an MCP transport.

## Considered Options

- Streamable HTTP (remote MCP server over HTTPS)
- stdio via a local shim process that proxies to the server
- SSE (legacy MCP HTTP transport)

## Decision Outcome

Chosen option: "Streamable HTTP". FastMCP serves the MCP endpoint over HTTPS so
Claude connects directly to the remote server with per-operator auth; no local shim
to install or keep running.

### Consequences

- Good, because the server is one ASGI app exposing MCP, the agent WS endpoint, and
  the web UI on a single port.
- Good, because nothing extra runs on the operator's machine.
- Bad, because we must secure the public MCP endpoint (operator auth) — tracked in
  the hardening phase.

## More Information

The Engram note labeled this "MCP (lokal)"; in practice the operator's client is
local but the server is remote, so a remote HTTP transport is required.

# 0008. Operator authentication

- Status: superseded by [ADR-0033](0033-multi-user-authentication.md)
- Amended by: [ADR-0037](0037-oauth2-authorization-server-for-mcp.md)
- Date: 2026-06-04

## Context and Problem Statement

The operator reaches `kenny-server` two ways: Claude over the MCP Streamable-HTTP
endpoint (`/mcp`) and the web dashboard (`/` + `/api`). Until now neither was
authenticated — anyone who could reach the server could drive every tool and read
the fleet dashboard. Agents already authenticate with a per-agent token on
`/agent/ws` ([ADR-0002]/registry), but the operator side was an open gap noted in
[ADR-0006](0006-mcp-streamable-http-transport.md).

## Considered Options

- Single shared **operator bearer token** (env `KENNY_OPERATOR_TOKEN`), enforced by
  one ASGI middleware over `/mcp`, `/api`, and the UI; cookie login for the browser.
- Per-operator identities with a user store / OAuth.
- Delegate entirely to a network layer (Cloudflare Access / Tailscale) and keep the
  app open behind it.

## Decision Outcome

Chosen option: "Single shared operator bearer token". For a family-scale, single (or
few) operator deployment this is the smallest change that closes the gap and is
testable end-to-end:

- A pure-ASGI `OperatorAuthMiddleware` requires a valid bearer token (header) or
  cookie on every HTTP request to `/mcp` and `/api`, and gates the UI (redirect to
  `/login`). It passes WebSocket (`/agent/ws`) and lifespan scopes through untouched,
  and passes authorized requests through unbuffered so MCP streaming/SSE is preserved.
- Claude sends `Authorization: Bearer <token>` to the MCP endpoint. The browser logs
  in at `/login`, which sets an `HttpOnly` cookie carrying the token.
- The token comes from `KENNY_OPERATOR_TOKEN`; an insecure dev fallback is used with a
  loud warning when unset, consistent with the agent-token dev fallback.

### Consequences

- Good, because all three operator surfaces are covered by one mechanism and one
  secret, with tests for each.
- Good, because it composes with a network layer later (defense in depth).
- Bad, because it is a single shared secret (no per-operator identity, no rotation
  story yet) and the login cookie carries the token. Acceptable for the current scale;
  see hardening below.

## More Information

- Transport security is separate and assumed: serve over **`wss`/`https`** in
  production so the token is not sent in clear (the dev fallback and `ws://` are for
  local use only). Set the cookie `secure` flag behind TLS.
- Hardening backlog: per-operator identities, token rotation, and/or fronting with
  Cloudflare Access / Tailscale ([ADR-0003](0003-self-built-websocket-tunnel.md) backlog).
- Implementation: `kenny-server/kenny_server/auth.py`; wired in `main.py`.

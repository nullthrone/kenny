# 0041. OAuth 2.1 authorization server for the MCP connector

- Status: accepted
- Date: 2026-07-21
- Amends: [ADR-0037](0037-multi-user-authentication.md), [ADR-0008](0008-operator-authentication.md), [ADR-0014](0014-auth-hardening.md)

## Context and Problem Statement

Claude reaches `kenny-server` at `/mcp` over remote MCP (Streamable HTTP, ADR-0006).
Until now the operator configured that connection by hand: mint a personal access token
(PAT, ADR-0037) in the dashboard and paste it into the client as an
`Authorization: Bearer <pat>` header. Claude Desktop's "add custom connector" flow does
not want a pasted token — it expects the server to speak the MCP authorization profile:
OAuth 2.1 with Protected Resource Metadata (RFC 9728), Authorization Server Metadata
(RFC 8414), Dynamic Client Registration (RFC 7591), PKCE, and resource-bound tokens
(RFC 8707). Without it, connecting is a manual, error-prone step; with it, the user types
only the server URL, signs in, and approves once.

The question: how does kenny satisfy that OAuth handshake without giving up the in-process
identity model ADR-0037 deliberately chose (it rejected delegating identity to an external
IdP such as OIDC/Cloudflare Access/Tailscale)?

## Considered Options

- **Co-host our own OAuth 2.1 Authorization Server (AS) + Resource Server (RS)** in the
  same app, issuing tokens bound to existing kenny accounts.
- **Delegate to an external IdP** (Google/GitHub/WorkOS/OIDC) as the AS.
- **Use FastMCP's built-in auth providers** instead of the bespoke middleware.
- **Do nothing** — keep pasted PATs as the only path.

## Decision Outcome

Chosen option: **co-host our own AS + RS**. kenny exposes the discovery documents
(`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`), a
registration endpoint (`/register`), an authorization endpoint (`/authorize`) with a
minimal consent screen, a token endpoint (`/token`), and revocation (`/revoke`). The
`/authorize` step reuses the existing session-cookie login (redirecting to `/login?next=`
when signed out) so identity and consent come from the accounts kenny already has.

Access tokens are **opaque** random strings (the same shape and at-rest hashing as PATs),
**audience-bound** to the canonical MCP resource URL (`<public-base>/mcp`), and resolved in
`OperatorAuthMiddleware` exactly like a PAT plus an expiry and audience check. Refresh
tokens rotate, with family-wide revocation on reuse (theft response, OAuth 2.1 §4.3.1).
Every token binds to an account, so the resolved principal carries that account's role and
host scope — the same RBAC as PATs and sessions.

This is **additive**: the PAT bearer path and the legacy shared operator token keep working
unchanged; OAuth is simply the new, recommended way to connect Claude Desktop.

- *External IdP* — rejected, same reasoning as ADR-0037: it moves the household's identity
  and trust off the self-hosted box and adds a hard runtime dependency. Co-hosting keeps
  identity in-process while still speaking standard OAuth to the client.
- *FastMCP native auth providers* — rejected: kenny's auth is a deliberate, hand-rolled
  stdlib design (`security.py`/`auth.py`) integrated with the user store; the MCP endpoint
  is gated by the surrounding `OperatorAuthMiddleware`, not a FastMCP provider. Bolting in
  a provider would fork the credential-resolution path for no gain.
- *JWT / confidential clients* — rejected for now: opaque tokens match the existing PAT
  model, make revocation a single DB write, and avoid signing-key management. Public PKCE
  clients are what Claude Desktop registers as.

### Consequences

- Good: Claude Desktop connects through its native OAuth flow — URL, sign-in, one consent —
  with no token copy-paste; the credential is per-user and revocable.
- Good: no wire-contract impact. This is server-only and does not touch the agent⇄server
  protocol (`PROTOCOL_VERSION`, `docs/protocol.md`, `docs/fixtures/`) or the agent's Ed25519
  handshake, exactly as ADR-0037 was.
- Good: audience validation is now part of principal resolution — kenny only accepts tokens
  minted for its own MCP resource (RFC 8707), closing the token-passthrough class of bugs.
- Bad / cost: a new persistence surface (`oauth_clients`, `oauth_auth_codes`,
  `oauth_access_tokens`, `oauth_refresh_tokens`) and token-lifetime/rotation/revocation
  policy to maintain; three credential types (PAT, session, OAuth) now resolve at the gate.
- Neutral: token lifetimes are tunable via `KENNY_OAUTH_ACCESS_TTL_SECS` (default 1 h) and
  `KENNY_OAUTH_REFRESH_TTL_SECS` (default 30 d).

## More Information

- MCP authorization profile: `modelcontextprotocol.io` (2025-06-18) — RFC 9728, RFC 8414,
  RFC 7591, RFC 8707, OAuth 2.1 + PKCE.
- Amends ADR-0037 (adds OAuth as a third credential type alongside PAT and session,
  reaffirming in-process identity), ADR-0008 and ADR-0014 (extends the operator bearer story
  and the 401 `WWW-Authenticate` challenge, which now carries `resource_metadata` for `/mcp`).
- Implementation: `kenny_server/oauth.py`, `kenny_server/oauthstore.py`,
  `kenny_server/urls.py`, and the wiring in `kenny_server/auth.py` / `main.py`.

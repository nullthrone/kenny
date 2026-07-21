# 0014. Auth hardening: token store, rotation, multi-operator, TLS cookie

- Status: accepted
- Amended by: [ADR-0041](0041-oauth2-authorization-server-for-mcp.md)
- Date: 2026-06-04

## Context and Problem Statement

ADR-0008 shipped deliberately dev-grade auth: agent tokens from an env map and a single
plaintext operator token. The backlog called for per-agent unique tokens with rotation, a
real store, hashed comparison, and a `Secure` login cookie under TLS — needed in particular
because the agent installer download (ADR-0012/WS5) must mint a per-agent token.

## Considered Options

- SQLite-backed, hashed per-agent token store + rotation endpoint; multi-operator tokens;
  `Secure` cookie under TLS. (Incremental hardening, no new infra.)
- Full IdP/OAuth for operators and mTLS for agents.
- Leave as-is and rely solely on a network layer (Tailscale/Cloudflare Access).

## Decision Outcome

Chosen option: incremental hardening in the app, composable with a network layer later.

- **Agent tokens:** `AgentTokenStore` (aiosqlite, same `KENNY_DB_PATH`) stores only
  `sha256(token)`; `verify` is constant-time; `create_or_rotate(agent_id)` mints a
  `secrets.token_urlsafe(32)` and returns the plaintext once. The env/dev map is seeded on
  first connect (`INSERT OR IGNORE`) so existing agents keep working. The `/agent/ws`
  handshake authenticates through the store (`register_async`), preserving the 4401 close.
- **Rotation endpoint:** `POST /api/agents/{id}/token` (operator-auth) returns a fresh token
  once — this is what the installer download calls to bind a per-agent token.
- **Operator tokens:** accept a set — `KENNY_OPERATOR_TOKEN` plus optional comma-separated
  `KENNY_OPERATOR_TOKENS` — compared constant-time across all candidates.
- **Cookie:** `Secure` is set when `KENNY_TLS=1`; otherwise unchanged so local http works.

### Consequences

- Good, because tokens are unique, rotatable, and never stored in clear; the installer has a
  clean mint path; multiple operators are possible.
- Good, because it layers under Tailscale/Cloudflare Access (documented as a Compose `tls`
  profile / deployment variant) for defense in depth.
- Bad, because hashing is `sha256` (fast) not a slow KDF, and the login cookie still carries
  the operator token — acceptable for this scale; a slow KDF / session tokens are future work.

## More Information

- Implementation: `kenny-server/kenny_server/tokenstore.py`, `registry.py`, `tunnel.py`,
  `auth.py`, `webui/__init__.py`, `main.py`. Builds on ADR-0008.

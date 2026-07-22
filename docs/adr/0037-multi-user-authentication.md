# 0037. Multi-user authentication: accounts, roles, sessions, and per-user access tokens

- Status: accepted
- Amended by: [ADR-0041](0041-oauth2-authorization-server-for-mcp.md)
- Date: 2026-07-04

## Context and Problem Statement

kenny assumed a single operator (the family IT person) authenticated by one shared
bearer token (ADR-0008): the login cookie *carried* that token, there were no user
identities, no roles, and no per-request attribution. ADR-0014 added multiple accepted
tokens but still no identities. Households want more than one person at the console: a
partner who can run the whole fleet, and end-users who should reach only *their own*
machines. That needs real accounts, a role model, per-user host scoping, and a way for
Claude (which reaches `/mcp` with a bearer, not a login form) to act as a specific user.
It also must upgrade an existing single-token install with zero manual steps.

This supersedes ADR-0008's "single shared token" decision and completes the
"per-operator identities / rotation" item on ADR-0014's hardening backlog. It is
server-only: it does not touch the agent⇄server wire contract (`docs/protocol.md` +
`docs/fixtures/`), so there is no `PROTOCOL_VERSION` bump and the only agent change is a
tray link to the console.

## Considered Options

- **Accounts + roles + sessions, with per-user personal access tokens (PATs) for the
  MCP/bearer path**, keeping the shared token accepted as a back-compat superuser.
- Keep the shared token for `/mcp` and add accounts only for the web UI (roles govern the
  browser only; Claude stays an unscoped superuser).
- Delegate identity to an external IdP (OIDC/Cloudflare Access/Tailscale) in front of the
  app.

## Decision Outcome

Chosen option: **accounts + roles + sessions + per-user PATs**. For a self-hosted family
deployment this keeps everything in one process with no external dependency, and it makes
role/scope apply to Claude's tool calls too (not just the browser).

- **Identities & storage.** A new `UserStore` (SQLite, same conventions as the other
  stores — own connection, `CREATE TABLE IF NOT EXISTS`) holds `users`, `user_tokens`
  (PATs), `sessions`, and `user_hosts`. Passwords are scrypt hashes (`cryptography`'s KDF;
  no new dependency); optional TOTP is a small dependency-free RFC-6238 implementation.
  User attributes: username + password (required), TOTP + email (optional), plus a
  selectable dog-breed avatar.
- **Two credential paths, one resolver.** `OperatorAuthMiddleware` resolves every HTTP
  request to a `Principal {user_id, role, hosts, session_id|pat_id}` on
  `scope["kenny_principal"]`: a `Bearer` PAT (Claude/MCP) → user; else a session cookie
  carrying an **opaque session id** (browser) → user; else the legacy shared token → a
  synthetic superuser (back-compat). The cookie no longer carries the token itself,
  fixing an ADR-0008 defect.
- **Roles.** `superuser` (everything, incl. user management and core settings),
  `operator` (the whole fleet, but not user management or settings), `user` (only
  assigned hosts — may read and operate on them, may not remove them from inventory).
  Enforced by a `guard` wrapper on the `/api` route table and by `_require_scope`/
  `_require_role` inside the MCP tools; fleet/audit/event reads are filtered to a
  `user`'s hosts. The server-hosted copilot chat is gated to operator+.
- **First-run setup.** When no account exists, `/login` redirects to `/setup`, which
  creates the first user as superuser and signs them in; `/setup` closes once any user
  exists.
- **Per-session active agent.** The registry's process-global active-agent slot becomes
  per-principal (keyed by session/PAT id, with a keyless global fallback), so concurrent
  callers cannot clobber each other's selection or ride it to an unassigned host.
- **Host removal.** A new operator+ `DELETE /api/agent/{id}` purges a host across every
  store (`inventory.purge_agent`); it refuses hosts pinned via `KENNY_AGENT_TOKENS`.

### Consequences

- Good, because there is now real per-user identity, attribution, host scoping, and
  session-based login — with no new runtime dependency and no external IdP.
- Good, because an existing install upgrades seamlessly: tables are created idempotently,
  known hosts are preserved, and the shared `KENNY_OPERATOR_TOKEN(S)` keep working as a
  superuser machine credential until (and after) the admin creates accounts.
- Bad, because the shared token remains a standing superuser back door (deprecated;
  disabling it is future work), and PATs bypass TOTP by nature (a machine can't prompt) —
  so PATs must be treated as password-equivalent (shown once, revocable, `last_used`
  tracked).
- Neutral: TOTP recovery codes are out of scope — a superuser resets a locked-out user's
  TOTP. The copilot chat is operator+ only for now (the `user` role uses the structured,
  per-host dashboard).

## More Information

- Transport security is still assumed and now more important: serve over `wss`/`https`
  (`KENNY_TLS=1`) so passwords, PATs, and session cookies are never sent in clear.
- Supersedes [ADR-0008](0008-operator-authentication.md); amends
  [ADR-0014](0014-auth-hardening.md). Agent tray link relates to
  [ADR-0011](0011-local-remote-control-kill-switch.md)/the tray helper.
- Implementation: `kenny-server/kenny_server/{security,userstore,auth,inventory}.py`,
  `kenny_server/webui/{authz,users}.py`, tool enforcement in `tools.py`, per-session
  active agent in `registry.py`, purge helpers across the stores; wired in `main.py`.
  Agent: `kenny-agent/src/tray.rs`.

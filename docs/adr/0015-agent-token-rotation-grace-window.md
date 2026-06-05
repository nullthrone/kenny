# 0015. Agent token rotation grace window

- Status: accepted
- Date: 2026-06-05

## Context and Problem Statement

ADR-0013 made `create_or_rotate(agent_id)` destructive: it overwrites the single stored
hash, so the previous token stops verifying the instant a new one is minted. Installer
generation mints a token on the operator side at *generation* time
(`distribution.py` `installer` / `public_installer`), decoupled from whether the
installer is ever deployed. The in-place self-update path (ADR-0012) deliberately keeps
the agent's existing `kenny-agent.config.json` (old token) across a binary swap.

The combination is an emergent lockout (issue #10): merely generating an installer for an
*already-connected* agent invalidates its live token. On the next reconnect the agent
presents the now-stale token, the `/agent/ws` handshake raises `AuthError` and closes
with `4401`, and the agent loops on reconnect forever — with no app-level log explaining
why. Each component is "as designed"; the failure is in the seam between them.

## Considered Options

- **A. Dual-token grace window** in the token store: a rotation demotes the old token to
  a previous slot that keeps verifying until the new token is first used or a TTL lapses.
- **B. Rotate at consume, not at generate**: only mint on the `/d/installer/{nonce}`
  fetch. The operator-side `GET .../installer` download path still rotates a live agent,
  so the lockout remains reachable.
- **C. Make `agent.update` token-aware**: carry/refresh the token through the self-update
  flow. Requires a protocol + fixture change and an agent redeploy — the very thing the
  broken scenario (server-only operator action) cannot rely on.

## Decision Outcome

Chosen option: **A**, because it removes the lockout regardless of which path rotated the
token, is **server-only** (no protocol/fixture change, no agent redeploy, so Python/Rust
cannot drift), and leaves the existing installer routes untouched — rotating at
generation is safe once the old token survives a grace window.

`create_or_rotate` now demotes the outgoing hash into `prev_token_sha256` with
`prev_expires_at = now + KENNY_TOKEN_GRACE_SECS` (default 7 days). `verify` accepts the
current token always, and the previous token until either the current token is first seen
here — which retires the previous one immediately — or its grace window lapses. Setting
`KENNY_TOKEN_GRACE_SECS=0` restores the historic instant-invalidation behaviour.

Separately, the `/agent/ws` handshake now logs auth rejections
(`auth failed for agent <id>; closing 4401`) and non-`register` first frames, closing the
observability gap that turned diagnosis into a mock-client investigation.

This amends ADR-0013 (token store / rotation).

### Consequences

- Good, because generating or downloading an installer no longer bricks a live agent; the
  old token keeps working until the new one is actually deployed and used.
- Good, because a `4401` rejection is now a one-line log grep instead of a silent loop.
- Good, because it is contained to `tokenstore.py` + a log line in `tunnel.py`; the wire
  contract, fixtures, and the Rust agent are unchanged.
- Bad, because both the old and new tokens authenticate during the window — a bounded,
  TTL-limited widening of the auth surface, accepted as the cost of not bricking agents.
- Neutral: distinguishing "auth-rejected" from "offline" in the fleet view (issue #10
  fix #5) is left as a follow-up; the handshake log covers the headline gap.

## More Information

- Issue #10. Amends ADR-0013; related: ADR-0011 (distribution), ADR-0012 (self-update).
- Implementation: `kenny-server/kenny_server/tokenstore.py`,
  `kenny-server/kenny_server/tunnel.py`; tests in `tests/test_tokenstore.py`,
  `tests/test_server_e2e.py`.

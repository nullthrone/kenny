# 0053. Guarded minting, open redemption for agent distribution

- Status: accepted
- Boundary moved: the auth model, at the agent-distribution surface. `distribution.py`'s
  routes were the one family in the server mounted outside `webui/authz.py`'s guards; this
  record draws the line between the routes that mint an enrollment path (operator-gated)
  and the routes that redeem one (deliberately unauthenticated), and states which
  credential each side actually rests on.
- Date: 2026-08-17

## Context and Problem Statement

`kenny-server/CLAUDE.md` says operator auth resolves every request to a `Principal` and
that "roles/host-scope are enforced by `webui/authz.py` guards" — "don't open these
surfaces without going through that middleware." Every `/api` route in the server obeyed
that except one module. `distribution.py`'s routes were mounted raw
(`main.py`: `*download_routes`), relying only on the blanket `OperatorAuthMiddleware`,
which admits *any* authenticated principal at *any* role. Concretely, a `user` scoped to a
single machine could call `POST /api/agents/{id}/share-link` for any agent id in the fleet,
or for an id that had never enrolled, and receive a link that mints a working enrollment
token when opened. Minting an enrollment path is provisioning; a scoped user does not
provision.

The obvious fix — wrap everything in `guard()` — is wrong, and wrong in a way that would
have looked correct in review. Three of these routes exist precisely for callers who hold
no operator credential and have no way to obtain one: the `/d/*` downloads (the relative
opening a link that arrived in a message, on a machine that has never talked to this
server) and `/api/agents/{id}/enroll` (the freshly installed agent binary, before it has an
identity). `auth.py::_is_public` already exempts exactly those paths from the operator
middleware, so guarding them would 401 every legitimate caller and break onboarding
outright while appearing to harden it.

The same change lengthens a share link's life from one hour to twenty-four
(`ShareLinkResponse.expires_at` in the frozen frontend contract). A link that travels by
message and is opened whenever its recipient next sits down needs to outlive the operator's
session; an hour does not. That is a longer window for an unguessable secret to sit in
someone's inbox, so it has to be paid for.

## Considered Options

- **Guard every route in the module uniformly.** One rule, no exceptions to remember —
  and it breaks share links, Linux installs and first enrollment.
- **Leave the module unguarded and rely on the middleware.** The status quo: any
  authenticated principal, any role, may provision.
- **Split by what the route does to a credential: minting is guarded, redemption is
  open.** Two rules, each stated where it applies.

## Decision Outcome

Chosen option: **split by minting versus redemption**, because it is the distinction that
actually carries the security, and the uniform rules each get it wrong in one direction.

- **Minting is `guard(min_role="operator")`**, plus `host_param="id"` wherever an agent id
  arrives as a path parameter: `/api/agents/share-link`, `/api/agents/{id}/share-link`,
  `/api/agents/{id}/installer`, `/api/agents/{id}/update`, `/api/agent-binary/fetch`.
  The new body form takes its id from the body, so it has no `host_param` — none is needed,
  because host scope only ever narrows a `user` and `min_role="operator"` already excludes
  one.
- **`/api/agent-binary` stays at the authenticated floor** (`guard()` with the default
  `min_role="user"`). It is a read-only "is a binary on disk" report the fleet view fetches
  on every render for every role. The guard is now explicit rather than absent; the
  behaviour is unchanged.
- **Redemption stays unauthenticated**: `/d/installer/{nonce}`, `/d/install/{nonce}`,
  `/d/binary/{nonce}` and `/api/agents/{id}/enroll`. Their credential is the one their
  caller can actually hold — an unguessable, single-use, TTL-bound nonce (ADR-0012,
  ADR-0030) and the one-time enrollment token (ADR-0022) — each verified inside the
  handler. That they are open is the design, not an oversight, and the module docstring now
  says so at the route table so the next reader does not "fix" it.
- **The 24h window is affordable because the mint is lazy.** No agent token is created when
  a link is minted; `token_store.create_or_rotate` runs at *fetch* time, inside the handler
  that consumes the nonce. A link that is never opened therefore expires having created no
  credential — there is nothing to revoke and no live agent whose token was rotated out from
  under it. The bundled dashboard's path-param form keeps the shorter one-hour TTL; it is
  used by an operator standing at the machine.

### Consequences

- Good, because the last `/api` surface outside `authz.py` is now inside it, and the one
  family of routes that must stay outside says why, next to itself.
- Good, because a scoped `user` can no longer provision an enrollment path for any host,
  in or out of their scope.
- Good, because the mint/redeem split makes the TTL question answerable: the window costs
  only the exposure of an unguessable single-use secret, never a dangling credential.
- Bad, because the module now has two share-link routes with two different TTLs (the
  console's body form at 24h, the bundled dashboard's path form at 1h) until the old
  dashboard is retired.
- **Known gap: an outstanding link does not survive a restart.** `ShareLinks` is an
  in-memory dict, and a deploy or crash invalidates every unredeemed nonce. Against the old
  one-hour window this was nearly invisible; against 24h it is a real failure mode — a link
  sent in the evening can be dead by morning, and its recipient sees only "link invalid or
  expired". It fails safe (a lost nonce is a link that cannot be redeemed, and since nothing
  was minted, nothing dangles; the operator mints another), which is why it is recorded here
  rather than fixed in the same change: durability means a SQLite table and an async
  `create`/`resolve`, which ripples through `perform_agent_update` and `update_manager.py`
  and would have bundled a storage change into an auth change.

## More Information

- ADR-0012 (agent distribution), ADR-0030 (self-elevating bootstrap installer) — the nonce
  as a credential.
- ADR-0022 (enrollment + Ed25519 key binding) — why `/api/agents/{id}/enroll` authenticates
  itself.
- ADR-0014 (token grace window) — why re-sharing a link for a live agent does not brick it.
- ADR-0033 (multi-user auth) — the role hierarchy and `webui/authz.py` guards this brings
  the module under.
- `kenny-server/tests/test_share_link.py` pins all four properties: operator-only minting,
  open redemption, single use, and "an unredeemed link mints no credential".

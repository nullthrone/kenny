# 0022. Mutual agent⇄server authentication via per-agent Ed25519 signatures

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

The agent⇄server tunnel was only half authenticated. The agent proved itself to the
server with a per-agent bearer **token** (a symmetric shared secret, sent in the
`register` frame), but the server proved itself to the agent with **nothing at the
application layer** — only TLS, where the agent dials a `wss://` URL and trusts any
CA-signed certificate for that hostname (ADR-0004). Anyone able to terminate or MITM
that TLS (a rogue/compromised CA, a corporate SSL-inspection proxy, DNS/ARP hijacking
on an untrusted home network) could impersonate the server and push `request` frames
that execute MCP commands on the family PC. The token also offered no server identity
and, being a bearer secret, leaks its full authority if captured.

We want **true mutual authentication** with **per-agent asymmetric keys**: the agent
must verify the server's identity and refuse commands from a spoofed server, and the
server must verify each agent with a key unique to that agent (no shared fleet secret).
ADR-0014 anticipated exactly this as the deferred, "composable" hardening step.

## Considered Options

- **App-layer mutual challenge-response with Ed25519 signatures**, layered over the
  existing WSS tunnel.
- **Mutual TLS (mTLS)**: per-agent client certificates plus a pinned server
  certificate, identity handled at the transport layer.
- **Server-generated agent keypair embedded in the installer** (instead of on-device
  generation).
- **Stay token-only** and rely on a network layer (Tailscale / Cloudflare Access) for
  server identity.

## Decision Outcome

Chosen option: **app-layer mutual challenge-response with Ed25519**.

A three-message handshake runs immediately after connect (`docs/protocol.md`):
`register` (agent → server, carrying `protocol`, a fresh 32-byte `client_nonce`, and
`meta`) → `challenge` (server → agent, a fresh `server_nonce` plus the server's
signature) → `auth` (agent → server, the agent's signature). Both signatures cover one
byte-exact transcript that binds a domain-separation label, `agent_id`, and **both**
nonces, defeating replay and reflection:

```
transcript = "kenny-mutual-auth-v1" || 0x00 || agent_id || 0x00 || client_nonce || 0x00 || server_nonce
```

- **Keys:** each agent generates its own Ed25519 keypair on-device (the private key
  never leaves the machine, persisted in an update-stable location so ADR-0013
  self-update can't lose it). The server holds one server-wide Ed25519 keypair whose
  public half is **pinned in the agent** at install time — this pin is what defeats
  server spoofing. The server stores each agent's public key.
- **Enrollment:** on first run the agent submits its public key once to
  `POST /api/agents/{id}/enroll`, authorized by a one-time enrollment token carried by
  the installer (reusing the existing mint path, ADR-0014). Thereafter only signatures
  authenticate.
- **Migration:** during rollout the server still accepts the legacy token path when
  `KENNY_ALLOW_TOKEN_AUTH=1`; the signature path is selected whenever the agent sends
  `protocol >= "0.8"` and a `client_nonce`. The token path is removed at cutover. This
  reuses the grace-window mindset already in the token store so live agents are never
  bricked.
- **Bump:** `PROTOCOL_VERSION` → `0.8`; new `challenge`/`auth` frames and
  `register.protocol`/`register.client_nonce`; `register.token` becomes optional.
  Golden vectors in `docs/fixtures/vectors/mutual_auth.json` are verified by both Rust
  and Python so the transcript stays byte-identical across the two implementations.

### Consequences

- Good, because the agent now cryptographically verifies the server and runs **no**
  tool until the server's signature over the agent's fresh nonce verifies — a spoofed
  server (even one that fully terminates TLS) cannot issue commands.
- Good, because each agent has a unique key with no bearer secret on the wire; a
  captured `register`/`auth` frame cannot be replayed (nonce-bound) and reveals no key.
- Good, because it keeps ADR-0003/0004 intact (single outbound WSS, one port, dial a
  known URL), is independent of where TLS terminates, and still composes underneath
  Tailscale/Cloudflare for defense in depth.
- Bad, because it adds a hand-rolled crypto handshake whose security rests on
  byte-exact transcript construction across two languages (mitigated by shared golden
  vectors and an anti-spoofing negative test on the agent) and on the one-time
  enrollment token being strictly single-use and TLS-only.
- Bad (rejected alternatives): **mTLS** is the textbook answer and arguably stronger,
  but it pushes identity into the transport/reverse-proxy layer, needs a CA plus
  per-agent certificate issuance/rotation/revocation, and complicates the
  TLS-termination/Tailscale story — conflicting with ADR-0004's "dial a known URL"
  simplicity. **Server-generated keypairs** would let the agent private key transit
  the installer/download path. **Token-only + network layer** gives the agent no
  server identity at all.

## More Information

- Contract: `docs/protocol.md` (§ Transport, `register`/`challenge`/`auth`, § Versioning
  v0.8); fixtures `docs/fixtures/{register,challenge,auth}.json` and golden vectors
  `docs/fixtures/vectors/mutual_auth.json`.
- Implementation: server — `kenny-server/kenny_server/{keystore,protocol,registry,tunnel,distribution,main}.py`;
  agent — `kenny-agent/src/{keys,protocol,config,tunnel}.rs`.
- Builds on ADR-0014 (auth hardening, which deferred this) and ADR-0008 (operator auth,
  unchanged); pairs with ADR-0003/0004 (tunnel and topology).

# 0011. Agent distribution: prebuilt binary + config injection

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

The operator should be able to download a ready-to-run agent from the server GUI (and send
a link to the target user), so a non-technical relative can install kenny-agent on their PC.
The agent needs per-install config (`server` URL, `agent_id`, `token`). How do we produce
and deliver that?

## Considered Options

- **A) Prebuilt binary + config injection.** The release workflow builds **one** signed,
  versioned `kenny-agent.exe` (ADR-0010/WS4). The server serves that prebuilt binary plus a
  generated per-agent bundle (config + service installer) carrying the per-agent token.
- **B) Ephemeral build sidecar.** The server cross-compiles a unique binary per download with
  config embedded at compile time.

## Decision Outcome

Chosen option: **A (prebuilt binary + config injection).**

- Cheap: build once per release, not per download; fast, cacheable downloads; no build
  toolchain / Docker-in-Docker / Windows licensing on the (Free-Tier) server.
- Reproducible and **signable** (one artifact per version) — a prerequisite for safe
  self-update (ADR-0012), which pulls the same prebuilt binary.
- The per-agent token is delivered in the generated config/installer. Embedding the token in
  a unique binary (B) is **not** real secrecy (it is extractable), so B's only real benefit —
  a single self-contained exe — is cosmetic and does not justify its cost/latency/complexity
  (per-download Rust compiles, OOM risk, no pre-signing, rebuild on token rotation).

B is recorded as a deliberately rejected alternative; it can be added later if a compelling
need ever appears.

### Consequences

- Good, because distribution is a static file serve + a small generated config; trivially cheap.
- Good, because the same artifact powers first install and server-triggered self-update.
- Bad, because the token rides in a config file rather than "inside" the binary — acceptable
  (extraction-equivalent), and the token is per-agent, rotatable (ADR-0013), and TLS-protected.

## More Information

- The server mints/rotates the per-agent token via the token store (ADR-0013) when generating
  a bundle. A shareable, **expiring one-time link** lets the target user download without an
  operator login (nonce-gated).
- Implementation: download routes in `kenny-server` (WS5); prebuilt artifact from the release
  workflow (`.github/workflows/release.yml`).

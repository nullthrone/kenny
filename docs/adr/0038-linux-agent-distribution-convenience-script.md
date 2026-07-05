# 0038. Linux agent distribution: convenience install script + server-triggered self-update

- Status: accepted
- Date: 2026-07-05

## Context and Problem Statement

ADR-0035 decided first-class Linux agent support and implemented Phase 1 (the agent registers,
authenticates, heartbeats, and runs the portable tools under a systemd lifecycle —
`kenny-agent/src/setup.rs` + `service.rs`, `linux_impl`). It explicitly deferred **Phase 4 —
distribution**: the release workflow builds only `kenny-agent.exe`, `distribution.py` /
`agent_release.py` know only the Windows binary, and neither the dashboard nor the docs offer
a Linux path. So a Linux agent can run, but there is no supported way to *get it onto a box*
and no way to *keep it current* — `agent_update` is `unsupported` off Windows.

This record fixes the **shape** of Linux distribution and Linux self-update. Two questions:
(1) how does a fresh Linux host obtain, configure, and install the agent, and (2) how are Linux
agents upgraded? Both move the deployment/distribution boundary, so they are recorded here
rather than as an implementation detail.

The Windows onboarding shape (ADR-0033) is a downloaded ZIP whose `setup.bat` self-elevates via
UAC. Linux has no UAC and no double-click culture for servers; the idiomatic equivalent is the
one-line install script (`curl … | sudo sh`) popularised by Docker (`get.docker.com`) and K3s
(`get.k3s.io`).

## Considered Options

- **A) Server-generated per-agent convenience script + server-triggered self-update.** The
  server mints a nonce-gated shell script with the per-agent config (server URL, `agent_id`,
  one-time enroll token, pinned server public key) baked in; the operator (or the target user)
  runs `curl -fsSL <link> | sudo sh`. The script detects the CPU arch, downloads the prebuilt
  Linux binary from the server, and runs `kenny-agent setup`. Upgrades reuse the existing
  `agent_update` frame (ADR-0013): the operator clicks *update*; the agent downloads, verifies,
  atomically swaps the running binary, and asks systemd to restart it.
- **B) Downloadable installer bundle only.** A `.tar.gz` (binary + `install.sh` + config
  sidecar), the direct parallel to the Windows ZIP. Works offline, but the UX is unpack-then-run
  rather than one line, and it does not answer the upgrade question.
- **C) Agent-side autonomous auto-update.** The agent polls a version source on a timer and
  updates itself (Watchtower / `unattended-upgrades` style). Keeps the fleet current with no
  operator action, but introduces a *second*, divergent update philosophy, a new version-check
  contract surface, and rolls updates out without operator review.
- **D) OS package repos (`.deb`/`.rpm` + apt/dnf).** Idiomatic per-distro, but multiplies the
  release matrix and the hosting surface, and the per-agent enroll config still needs a
  side-channel — heavyweight for a self-hosted family tool.

## Decision Outcome

Chosen option: **A — a server-generated per-agent convenience script, plus server-triggered
self-update reusing the `agent_update` frame.**

Rationale:

- It reuses what already exists. The per-agent one-time enrollment token, the `enroll`
  endpoint, the nonce-gated `/d/*` links, and the `agent_update` frame are all OS-neutral
  already; Linux plugs into the same machinery with an `os` dimension rather than a new one.
  The agent's Linux lifecycle (`setup` → `/opt/kenny`, systemd `install`) is already built and
  accepts the needed flags, so the script just calls `kenny-agent setup`.
- It keeps Windows and Linux from drifting. Upgrades stay **server-triggered and
  operator-controlled** on both platforms (ADR-0013) — the same dashboard button, the same
  frame. C is rejected precisely because an autonomous auto-updater would fork that philosophy
  and add a contract surface for cosmetic convenience; it can be added additively later as an
  opt-in if a fleet ever wants it.
- The security posture matches ADR-0012: the enroll token rides in the (nonce-gated,
  single-use, TLS-delivered) script exactly as it rides in the Windows config sidecar —
  extraction-equivalent, per-agent, rotatable. Piping to `sudo sh` is the same trust the
  operator already extends by running any installer as root.

The Linux binary is a **statically linked musl** release artifact (`x86_64` and `aarch64`, the
latter for Raspberry Pi / ARM NAS) so one binary runs on any distro with no glibc/runtime
dependency — the right shape for a script that lands on an unknown box.

Linux self-update is markedly simpler than the Windows path (ADR-0013) and needs **no separate
updater helper**: Linux has no executable file lock and no tray/session-0 process holding the
image, so the running binary is replaced with an atomic `rename()` (the live process keeps its
open inode) and systemd — which owns the lifecycle — restarts the unit cleanly from the swapped
`ExecStart` path. The whole Windows dance of copying a side `updater.exe`, terminating the tray,
and waiting for the image lock to release simply does not apply.

B (offline tarball) is not chosen as the headline but falls out cheaply from the same binary
endpoint and can be added when an air-gapped install is needed. D is rejected as
disproportionate. C is rejected as a divergent, review-bypassing second mechanism.

### Consequences

- Good, because a fresh Linux host is one command to onboard and one dashboard click to
  upgrade — no manual binary handling, no distro-specific packaging.
- Good, because it is additive: no wire-contract change (no new frames/tools/sections, no
  `PROTOCOL_VERSION` bump), Windows behaviour and fixtures are untouched, and everything new
  hangs off an `os` parameter defaulting to `windows`.
- Good, because the static musl binary removes runtime-dependency support burden across the
  distro zoo.
- Bad, because `curl | sudo sh` is a pattern some operators distrust; mitigated by the
  single-use short-TTL nonce, TLS delivery, and the documented manual path (download the binary
  and run `kenny-agent setup` with explicit flags) for anyone who wants to inspect first.
- Bad, because the server now resolves and caches a binary **per OS/arch** (Windows exe + Linux
  musl variants) and the release workflow grows a Linux job — more artifacts to build and host.
- Neutral, because autonomous auto-update (C) and the offline tarball (B) remain open,
  additive follow-ups rather than foreclosed.

## More Information

- Implements ADR-0035 Phase 4. Builds on ADR-0012 (prebuilt binary + config injection),
  ADR-0013 (server-triggered self-update), ADR-0023 (per-agent Ed25519 enrollment), and
  ADR-0033 (self-elevating bootstrap installer — the Windows onboarding shape this parallels).
- Server: per-OS/arch binary resolution and the Linux script generator live in
  `kenny-server/kenny_server/distribution.py` + `agent_release.py`; the release binaries come
  from `.github/workflows/release.yml`. Agent: the Linux self-update arm lives in
  `kenny-agent/src/handlers/agent_update.rs`. Operator-facing docs: `docs/setup.md`.
- Deferred (additive, own follow-up if needed): agent-side autonomous auto-update (C), an
  offline `.tar.gz` bundle (B).

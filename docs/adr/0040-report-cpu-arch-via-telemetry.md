# 0040. Report CPU arch via telemetry; pin arch for fresh Linux installs

- Status: accepted
- Date: 2026-07-21

## Context and Problem Statement

An aarch64 host (`fridolin`) was bricked by a self-update: `trigger_update` resolves
the binary to push from `Agent.arch`, which defaults an *unreported* arch to `x86_64`
(`kenny_server/distribution.py` `_norm_arch`). The agent that got bricked predated
ADR-0038/#139's `register.meta.arch` field, so it never reported its arch on any
channel, and it also predated the agent-side ELF `e_machine` guard — so the wrong
binary was swapped in and the host crash-looped with `Exec format error`.

A first response added a hard `409` refusal in `trigger_update` for any Linux agent
with no reported arch, forcing a reinstall before self-update would proceed. That
approach was rejected: it changes update *behavior* (a UX regression for the common
case) to guard against a case that a blocking check cannot actually fix — an agent
already bricked by shipping without arch-reporting cannot self-report anything, guard
or no guard. The guard only protects agents that already have the capability to avoid
the problem in the first place.

The real gap is narrower: `register` is a **one-time** frame sent once per connection.
If the server's registry state for an agent were ever incomplete or stale (a bug, a
partial write, an agent that reconnects without a clean register), there was no second
channel to reconfirm `arch`. Separately, the dashboard's "Add a PC" flow has no way to
prepare an installer for a specific, already-known target architecture — Linux relies
entirely on `uname -m` auto-detection at curl-time, and the Windows zip build ignores
arch outright (a latent gap if a second Windows target is ever added).

## Considered Options

- **A) Keep the `409` refusal.** Simple, but blocks the primary interaction (update) to
  defend against a case it cannot repair, and does nothing for a genuinely bricked
  legacy agent — it can't call itself out.
- **B) Report arch via telemetry (`os_support` section) as a second, periodic channel,
  merged into the registry on every push; keep `trigger_update` unblocked (its
  pre-existing x86_64-default behavior); add an operator-facing arch-pin at
  installer/share-link generation time for brand-new hosts.** Telemetry already pushes
  every `KENNY_TELEMETRY_INTERVAL_SECS` (default 900s) for the lifetime of the
  connection — reusing it costs no new frame, no new schema (`Section` already accepts
  arbitrary fields), and self-heals if the registry's copy of `arch` were ever wrong or
  missing, without blocking anything.
- **C) A dedicated `system`/`host` telemetry section instead of extending
  `os_support`.** Rejected: `os_support` already owns OS/host identity
  (name/version/build/eol); a second identity section would fragment that without
  adding capability.

## Decision Outcome

Chosen option: **B**.

- `os_support` (`kenny-agent/src/telemetry/collectors/os_support.rs`) gains an `arch`
  field (`crate::util::arch()`, the same normalized value already sent in
  `register.meta.arch`). Purely additive on the wire — `Telemetry.snapshot` is an
  untyped section map on both sides.
- The server merges a **strictly literal** `x86_64`/`aarch64` value from
  `os_support.arch` into the agent's registry `meta` on every telemetry push
  (`AgentRegistry.note_arch`) — deliberately not `_norm_arch`-normalized, so a
  malformed or forged value can never clobber good data with a guessed default.
- `trigger_update` is unchanged from its pre-incident form: unknown arch still defaults
  to `x86_64`, and it never blocks. This is a deliberate acceptance that this class of
  problem is closed by *agents reporting themselves correctly*, not by the server
  refusing to act on incomplete information.
- The dashboard's "Add a PC" flow gains an OS+Arch dropdown, populated only from
  `SUPPORTED_TARGETS` (the (os,arch) combinations we actually ship a binary for). For a
  brand-new Linux host, the chosen arch is **pinned** into the generated install script
  — replacing `uname -m` auto-detection for that flow, since the operator is by
  definition preparing a package for a target whose arch they already know. Windows
  is left as-is (only one target exists today; `agent_binary_path` does not consult
  arch for Windows).

### Consequences

- Good: a second, low-cost, self-refreshing channel for arch — no new frame, no
  blocking, no UX regression on update.
- Good: fresh-install onboarding no longer relies on runtime detection succeeding on
  an unfamiliar box; the operator commits to a known target upfront, and the dropdown
  can never offer a combination we don't actually build.
- Neutral, revises ADR-0038's consequence "no wire-contract change, no `PROTOCOL_VERSION`
  bump" — this change *is* additive to the wire contract (a new telemetry field) and
  does bump `PROTOCOL_VERSION` (0.12 → 0.13), though no frame or tool **schema**
  changes (both `Section` and the pinned-arch install flow slot into existing,
  intentionally-open shapes).
- Bad / explicitly accepted: this does **not** retroactively fix an already-deployed
  pre-0.11 agent (the original `fridolin` case) — an agent that never reports `arch` on
  *either* channel still resolves to the `x86_64` default on update, exactly as before.
  Only reinstalling or updating to an arch-reporting build closes that gap for a given
  host. There is no server-side trick that recovers information an old binary was never
  built to send.

## More Information

- Builds on ADR-0038 (Linux distribution + self-update shape) and the #139 fix
  (`register.meta.arch`, the agent-side ELF `e_machine` guard).
- Server: `kenny_server/registry.py` (`AgentRegistry.note_arch`), `kenny_server/tunnel.py`
  (telemetry-frame handler), `kenny_server/distribution.py` +
  `kenny_server/agent_release.py` (`SUPPORTED_TARGETS`, arch-pinned installer/share-link).
  Agent: `kenny-agent/src/telemetry/collectors/os_support.rs`. Dashboard:
  `kenny_server/webui/index.html` ("Add a PC" widget).

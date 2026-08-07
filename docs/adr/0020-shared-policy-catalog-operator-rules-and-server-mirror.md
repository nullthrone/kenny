# 0020. Shared policy catalog, operator deny rules, and a server-side mirror

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

ADR-0019 introduced a deterministic, always-on safety guard on the agent that refuses
individually dangerous tool calls (`error.code = "blocked"`). It listed three follow-ups,
which this ADR implements:

1. **An append-only operator extension list** — let the operator add deny rules on top of
   the compiled-in built-ins, without ever being able to weaken them.
2. **A server-side mirror** — refuse an obviously dangerous call *before* forwarding it,
   so Claude/the operator get immediate feedback instead of a tunnel round-trip.
3. **Guard hits as an audit signal** — make a refusal visible in the existing event store.

The hard constraint is the repo invariant *"Python and Rust must not drift."* A server
mirror that re-implements the agent's rules in Python is exactly the kind of duplication
that drifts. We need one source of truth that both runtimes consume.

## Considered Options

- **Shared single-source catalog file consumed by both sides (chosen).** One
  `docs/policy/deny_rules.json` of `{id, applies_to, pattern, reason}` rules. The Rust
  agent embeds it at build time (`include_str!`, so the binary stays self-contained); the
  Python server loads the same file for its mirror. No second hand-maintained rule list.
- **Independent Python re-implementation with a parity test.** Less coupling, but two rule
  lists that a test must police forever — the drift the invariant warns against.
- **Operator list as a local endpoint file (like the kill-switch control file).** Rejected
  for *this* change: the operator is the central server-side admin, so a centrally managed,
  tunnel-delivered list matches how operators actually work and surfaces in the dashboard.
- **Guard hits as a new telemetry section / frame.** Rejected: the `log` frame (ADR-0017)
  and event store already are the audit trail; a `tracing::warn!` on each block flows there
  with no contract change.

## Decision Outcome

- **Shared catalog.** `docs/policy/deny_rules.json` is the single source of truth for the
  built-in rules. `applies_to` ∈ {`powershell`, `self_protection`, `path`}; patterns use
  the portable regex subset common to Rust `regex` and Python `re` (no
  backreferences/lookaround). Self-protection patterns carry the literal `kenny-agent` /
  `kenny-agent.control.json` strings; an agent unit test asserts they stay in lockstep with
  the `SERVICE_NAME` / `CONTROL_FILE` constants.
- **Operator rules over the tunnel.** A new `policy` frame (server → agent) carries the
  operator's **append-only** extra deny rules. They are additive to the built-ins (which
  this frame can never weaken or remove). The server persists the list (SQLite), exposes an
  authenticated `/api` route to read/add/remove operator rules, sends the current list on
  each agent `register`, and re-pushes to all connected agents on change. The agent
  recompiles its rule set on each frame; a rule that fails to compile is skipped (logged),
  never fatal. `PROTOCOL_VERSION` bumps `0.5` → `0.6` (additive frame).
- **Server mirror (best-effort).** Before forwarding a capability call, the server runs the
  same catalog + operator rules over the args (`powershell_exec.script`, `fs_*` path/root,
  winget/net string args for self-protection). A hit raises `ToolError("blocked", reason)`
  *without forwarding*. If the catalog file is absent at runtime (e.g. an unusual deploy),
  the mirror **degrades to disabled** with a warning — it is UX, not the boundary. The
  `agent_update` host allowlist stays agent-only (it needs the agent's configured server
  host, which the server cannot self-determine).
- **Audit signal.** On every block, the agent emits a structured `tracing::warn!`
  (forwarded via the `log` frame to the event store, ADR-0017); the server mirror records
  the same via the event store. Both name the matched rule.

### Deployment note

The server image (`kenny-server/Dockerfile`) and `.dockerignore` previously excluded
`docs/`. The build now ships `docs/policy/` so the mirror can load the catalog in the
container; the loader also honours `KENNY_POLICY_CATALOG` and falls back to mirror-disabled
when the file is missing.

### Consequences

- Good: one rule list, consumed by both runtimes — no Python/Rust drift. The agent stays
  the authoritative, self-contained enforcement point; the server mirror only ever *adds*
  earlier feedback and can fail open without weakening safety.
- Good: operators get a central, append-only way to extend the blocklist; built-ins remain
  un-removable; every block is auditable through the existing event store.
- Bad / trade-offs: the agent build now embeds a file from `docs/`; the server build must
  ship `docs/policy/`; the operator list is new persisted state with its own API surface.
  A determined, obfuscated payload can still slip a regex blocklist — unchanged from
  ADR-0019, this is defense-in-depth below auth + confirm-gate + kill-switch, not a sandbox.

## More Information

- Contract: `docs/protocol.md` (`policy` frame, versioning), `docs/fixtures/policy.json`,
  `docs/policy/deny_rules.json`.
- Code: `kenny-agent/src/policy.rs`, `kenny-agent/src/tunnel.rs`,
  `kenny-agent/src/dispatch.rs`, `kenny-agent/src/protocol.rs`;
  `kenny-server/kenny_server/{protocol,tunnel,store,policy,webui,main}.py`.
- Related: [ADR-0019](0019-agent-side-deterministic-tool-guard.md) (the guard),
  [ADR-0011](0011-local-remote-control-kill-switch.md) (kill-switch precedent for
  control state), [ADR-0017](0017-observability-logging-and-event-store.md) (event store /
  `log` frame), [ADR-0005](0005-contract-first-with-golden-fixtures.md) (contract-first).

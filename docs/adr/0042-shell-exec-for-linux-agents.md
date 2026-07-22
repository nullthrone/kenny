# 0042. `shell_exec` for Linux/macOS agents, with a POSIX policy group and a server-side OS guard

- Status: proposed
- Date: 2026-07-22

## Context and Problem Statement

kenny has `powershell_exec` for Windows agents but no first-class shell tool for Linux/macOS
agents. `powershell_exec` has quietly run `sh -c <script>` on non-Windows since its
introduction, as a CI/dev-flow convenience so e2e tests pass on Linux CI without a real
Windows box. That fallback means a Linux agent already executes arbitrary shell commands
today — but (a) under a Windows-named tool the operator and Claude have no reason to expect
runs shell on Linux, and (b) guarded only by the **PowerShell** deny-rule group
(`docs/policy/deny_rules.json`, ADR-0020/0021). There are no POSIX-specific deny rules (`rm -rf
/`, `mkfs`, `dd of=/dev/...`, fork bombs, `systemctl stop kenny-agent`), so on a Linux agent
this fallback is close to unguarded remote code execution — a materially weaker safety
posture than the Windows path has.

ADR-0035 (first-class Linux agent support) explicitly named this as a known design point and
flagged "a neutral `shell_exec` may be added additively later" as future work. This ADR is
that follow-up: add a proper POSIX shell tool, close the PowerShell-catalog/Linux-fallback gap
with a real POSIX policy group, and decide how a caller (Claude, via the MCP tool surface)
should end up calling the *right* one for a given agent's OS.

The server already knows each agent's OS: `Agent.os` (`kenny-server/kenny_server/registry.py`)
is set from `register.meta.os` on connect and surfaced today in `list_agents`/`select_agent`/
`agent_health`. So the routing question is not "can we know the OS" but "who is responsible
for acting on it, and how strictly."

## Considered Options

- **A) Model intelligence only.** Expose both `powershell_exec` and `shell_exec` as ordinary
  MCP tools; rely on the model reading the agent's `os` field (already visible) to call the
  right one. A wrong-OS call falls through to the agent, which returns `unsupported`. Minimal
  diff, no new server logic, but the model is the only thing standing between a wrong-OS call
  and a wasted round-trip (and, before this change, an actually-executed-but-wrong command via
  the old fallback).
- **B) Model intelligence + a deterministic server-side OS guard (chosen).** Same as A, plus:
  before forwarding either tool, the server compares the tool's required OS family against the
  target agent's known `os` and refuses a mismatch itself — `error.code = "unsupported"`,
  message naming the correct tool — without ever sending a `request` frame to the agent. The
  model still does the primary routing (it already has the information); the guard is a cheap,
  deterministic backstop that turns a silent/wasted wrong-OS call into an immediate, actionable
  refusal, and fails safe (skipped, not opened wider) for agents the registry doesn't know yet.
- **C) A single unified `shell_exec` that dispatches internally by OS.** Hide the OS behind one
  tool name; the server or agent picks PowerShell vs. `sh` based on `Agent.os`. Rejected: the
  abstraction is leaky. PowerShell and POSIX shell scripts are not interchangeable — the model
  has to know the target OS to *write* the script regardless of which tool name it calls, so
  collapsing the name buys nothing and would obscure the OS-scoping in tool listings and audit
  logs (`CallLog`, `STATE_CHANGING_TOOLS`) that today key on tool name.

## Decision Outcome

Chosen option: **B — model intelligence for primary routing, plus a deterministic server-side
OS guard**, consistent with kenny's existing guard philosophy (ADR-0020/0021: cheap
deterministic checks as defense-in-depth below the "smart" layer, never a substitute for it).

Concretely:

- New tool **`shell_exec`**: `{command, timeout_s}` → `{stdout, stderr, exit_code}` — the exact
  shape of `powershell_exec`, run via `sh -c <command>` off Windows
  (`kenny-agent/src/handlers/shell.rs`). It is `unsupported` on Windows.
- **`powershell_exec` drops its `sh` fallback.** It is now `unsupported` off Windows
  (`kenny-agent/src/handlers/powershell.rs`) — a clean, symmetric OS-scoped pair with
  `shell_exec` rather than one tool that secretly does double duty. This is a breaking change
  for anything relying on the old Linux fallback (there was no documented/supported use of it
  outside dev/CI, per the contract); the fix is to call `shell_exec` instead.
- **New `posix` deny-rule group** in the shared catalog (`docs/policy/deny_rules.json`,
  `catalog_version` 1 → 2): destructive-command rules (`rm -rf /` and `--no-preserve-root`,
  `mkfs`, `dd`/`shred`/`wipefs` device writes, a fork-bomb pattern, recursive `chmod`/`chown` on
  `/`) plus POSIX self-protection rules (`systemctl stop/disable kenny-agent`, `kill`/`pkill
  kenny-agent`, deleting the `kenny-agent` binary or its control file) added to the existing
  `self_protection` group. Enforced identically by the Rust agent guard (`policy.rs`) and the
  Python server mirror (`policy.py`), per ADR-0021's single-catalog, both-sides-enforce model.
- **Server-side OS guard**: in `kenny_server/tools.py`'s `make_forwarder`, right after the
  existing scope check and before `tunnel.send_request`, the forwarder looks up the target
  agent's `os` and refuses a call to the wrong-OS shell tool with `error.code = "unsupported"`
  and a message naming the correct one. Skipped when the agent isn't in the registry (e.g.
  selected only from stored telemetry) — the tunnel send then fails as offline, which is
  already the correct behavior for an unreachable agent. Legacy agents default to `os =
  "windows"` (the existing `Agent.os` property default), so they keep `powershell_exec` and
  are refused `shell_exec` — the safe direction for the assumption to fail in.
- `PROTOCOL_VERSION` bumped **0.13 → 0.14** (new tool + a breaking change to
  `powershell_exec`'s off-Windows behavior + a new `applies_to` catalog value).

### Consequences

- Good, because Linux/macOS agents get a properly named, properly guarded shell tool instead of
  an accidental, under-guarded one — the POSIX deny-rule group closes a real safety gap that
  predates this ADR.
- Good, because the OS guard gives a wrong-OS call an immediate, actionable refusal (naming the
  right tool) instead of a silent round-trip to the agent or, in the old world, actually running
  the wrong kind of script. It costs one registry lookup per forwarded call and adds no new
  failure mode: an unknown agent just skips the check and fails at the tunnel as before.
- Good, because the guard is additive to — not a replacement for — model-level routing: the
  model still needs to write the right kind of script for the target OS, so it must already
  reason about `os`. This mirrors how ADR-0020/0021's deterministic guard sits below, and does
  not replace, the operator confirm-gate and kill-switch.
- Bad, because `powershell_exec`'s off-Windows semantics change (no more `sh` fallback) — a
  breaking change to a previously-undocumented convenience. Mitigated: the contract never
  described this fallback as supported behavior, and the fix (`shell_exec`) is a one-name swap.
- Bad, because two OS-scoped tool names for "run a script" is one more thing to keep in lockstep
  across `CAPABILITY_TOOLS`, `STATE_CHANGING_TOOLS`, `is_mutating`, and the policy `check()`
  dispatch on both sides — an accepted, precedented cost (the same shape `powershell_exec`
  already carried).
- Neutral, because this is an additive contract change (new tool, new `applies_to` value) plus
  one behavior-breaking change scoped to a single existing tool's non-Windows arm; both sides
  move together and `/contract-check` covers the fixture round-trip.

## More Information

Fulfills the `shell_exec` follow-up explicitly flagged in ADR-0035 (§4, "What we adapt").
Builds on ADR-0016 (underscore tool naming — `shell_exec` follows the same
`family_verb` convention), ADR-0020 (agent-side deterministic safety guard — the model this
POSIX group extends), and ADR-0021 (shared policy catalog + operator rules + server mirror —
the mechanism `posix` rules are enforced through on both sides).

Key files: `docs/protocol.md` (catalog + "OS-scoped tools" + Versioning), `docs/fixtures/
request_shell_exec.json` / `response_shell_exec.json`, `docs/policy/deny_rules.json`,
`kenny-agent/src/handlers/shell.rs` and `handlers/powershell.rs`, `kenny-agent/src/policy.rs`,
`kenny-server/kenny_server/tools.py` (`_OS_SCOPED_TOOLS`, the guard in `make_forwarder`),
`kenny-server/kenny_server/policy.py`.

Follow-up (not in scope here): per-agent tool *visibility* in `list_tools` (hiding the
wrong-OS tool entirely rather than exposing-then-refusing) was considered and rejected for now
— FastMCP's tool registration is static per server process, not per active-agent selection, so
filtering the tool list would need either per-session tool sets or a client-side capability
hint. The refuse-with-a-clear-message guard is a cheaper, immediately available approximation;
revisit if per-agent tool visibility becomes generally useful (e.g. alongside role-based tool
scoping).

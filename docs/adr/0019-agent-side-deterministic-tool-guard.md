# 0019. Agent-side deterministic safety guard for dangerous tool calls

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

kenny drives a family PC through Claude/MCP. Two controls already stand between a request
and execution, but **both are policy decisions, not deterministic refusals**:

- The server-side **confirm-gate** (ADR-0009) asks the operator to approve any
  state-changing tool before it runs. It classifies *which tool* runs, not *what it does*.
- The endpoint user's **kill-switch** (ADR-0011) lets the person at the PC turn *all*
  mutating tools off. It is all-or-nothing and can be switched back on.

Neither inspects the content of a `powershell_exec` script. A single approved script can
still wipe a disk, delete volume shadow copies (the classic ransomware precursor), clear
the event log to cover tracks, disable Defender, create an admin account, or kill the
kenny agent itself. The `fs_*` tools can be pointed at the SAM hive or SSH keys, and
`agent_update` will download a binary from any URL it is handed (the self-update MITM
concern from the security review). We want a **deterministic, always-on** refusal for
individually catastrophic calls that does not depend on a human judging a prompt and
cannot be turned off from the server.

## Considered Options

- **Compiled-in agent-side guard (chosen).** A small policy module the agent consults for
  every tool call before dispatch. Refusals are deterministic, always on, and not
  remotely disableable.
- **Server-side blocklist.** Inspect scripts in `kenny-server` before forwarding. Rejected
  as the *primary* location: the agent is the trust boundary that actually runs the
  command, so the authoritative refusal must live there — even a buggy or compromised
  server must not be able to execute a disk-wipe. (A server-side mirror is a fine future
  addition for earlier/clearer feedback, but it is not the boundary.)
- **Operator/config-supplied blocklist.** Make the rules editable at runtime. Rejected for
  the core rules: a protection function the operator can edit away is not a protection
  function. Built-in and compiled-in maximises determinism and tamper-resistance. (An
  *append-only* extension hook can be added later without weakening the built-ins.)
- **A real sandbox / constrained language mode.** Out of scope and not a contract concern;
  PowerShell remains Turing-complete by design here.

## Decision Outcome

Chosen option: **compiled-in agent-side guard**, in `kenny-agent/src/policy.rs`, called
from `dispatch::run` immediately after the kill-switch check and before any handler.

- `policy::check(tool, args)` returns `Err((ErrorCode::Blocked, reason))` to refuse, where
  the message names the matched rule. Rules are compiled once (`OnceLock`) using the
  `regex` crate. Four rule groups: a **PowerShell blocklist** (disk/partition destruction,
  shadow-copy deletion, event-log clearing, boot-config edits, secure-wipe,
  `-EncodedCommand`, download-and-execute, Defender disable, account/privilege
  escalation); **agent self-protection** (stop/disable/remove the kenny service, kill its
  process, delete its binary, tamper with the kill-switch control file — built from the
  `SERVICE_NAME` and `CONTROL_FILE` constants so they stay in lockstep); an **`fs_*`
  sensitive-path guard** (registry hives, `ntds.dit`, SSH keys, browser credential stores,
  path traversal); and an **`agent_update` host allowlist** (configured server host +
  GitHub release hosts), which composes with — does not replace — the handler's SHA-256
  verification.
- The wire contract gains `blocked` in the `error.code` set (`docs/protocol.md` +
  `docs/fixtures/response_error_blocked.json`), mirrored by `protocol.rs`
  (`ErrorCode::Blocked`) and `protocol.py` (`ErrorCode` literal). `PROTOCOL_VERSION` is
  bumped `0.4` → `0.5` (additive to the error-code set; no frame or tool-schema change).

### Honest scope

A regex blocklist over a Turing-complete shell is a **seatbelt, not a sandbox**. It is
bypassable in principle (string concatenation, aliasing, fetching and running code). The
guard deliberately blocks the cheapest bypass (`-EncodedCommand`) and the obvious
catastrophic foot-guns, which raises the bar substantially, but it is **not** a complete
boundary. The real boundary remains agent/operator auth (ADR-0008/0014) + the confirm-gate
(ADR-0009) + the kill-switch (ADR-0011); this guard sits **below** them as
defense-in-depth. We accept false negatives (a determined, obfuscated payload may slip
through) in exchange for zero false-positive disruption of normal admin work and a
deterministic floor under the human controls.

### Consequences

- Good: catastrophic and self-destructive calls are refused deterministically, with a
  clear `blocked` reason Claude/the dashboard can explain, regardless of approval or
  kill-switch state, and with no remote off-switch.
- Good: portable and `#[cfg]`-free — the rules are string checks that run and are unit
  tested on Linux CI, so there is no Windows-only gap.
- Bad / trade-offs: a blocklist needs maintenance as new dangerous patterns emerge; it can
  produce false negatives (not a sandbox) and, rarely, a false positive that a legitimate
  script must be reworded around. The `agent_update` allowlist needs the server host,
  captured at startup from the `--server` URL.
- Follow-up (not in this ADR): an optional append-only operator extension list; an
  optional server-side mirror for earlier feedback; reporting guard hits as an audit/
  telemetry signal.

## More Information

- Contract: `docs/protocol.md` (`response` error codes, versioning),
  `docs/fixtures/response_error_blocked.json`.
- Code: `kenny-agent/src/policy.rs`, `kenny-agent/src/dispatch.rs`,
  `kenny-agent/src/protocol.rs`, `kenny-server/kenny_server/protocol.py`.
- Related: [ADR-0009](0009-server-hosted-claude-chat.md) (confirm-gate),
  [ADR-0011](0011-local-remote-control-kill-switch.md) (kill-switch),
  [ADR-0013](0013-agent-windows-service-and-self-update.md) /
  [ADR-0015](0015-agent-binary-auto-fetch.md) (self-update + GitHub fetch),
  [ADR-0005](0005-contract-first-with-golden-fixtures.md) (contract-first).

# 0064. The fleet declares what a shell may run

- Status: accepted
- Boundary moved: **the auth model** — `powershell_exec` and `shell_exec` move from
  deny-by-exception to an operator-declared execution mode, so the default verdict for an
  arbitrary command on a managed host is no longer "run it".
- Amends: [ADR-0019](0019-agent-side-deterministic-tool-guard.md),
  [ADR-0020](0020-shared-policy-catalog-operator-rules-and-server-mirror.md)
- Date: 2026-09-20

## Context and Problem Statement

The two shell tools take a command string and run it as the agent's user — root under
systemd, SYSTEM as a Windows service — through `sh -c` / `powershell.exe -Command`. The only
thing between an MCP caller and that shell is the deterministic deny guard of ADR-0019/0020:
a shared catalog of regexes, compiled into the agent and mirrored on the server.

That guard is deny-by-exception, and every rule in it names a *destruction verb*:
`Format-Volume`, `Clear-Disk`, `mkfs`, `dd of=/dev/`, `shred`, `rm -rf /`. Two things follow,
and the second is the one that forced this record.

**It does not address abuse.** Ransomware reads files and writes them back encrypted.
`Get-ChildItem` plus `[System.IO.File]::WriteAllBytes` is the whole of it. Not one catalog
rule fires.

**Even its accident coverage is porous by construction.** `posix_rm_rf_root` matches
`rm -rf /` and not `rm -rf /*`, `find / -delete`, or `python3 -c "shutil.rmtree('/')"`. This
is not a gap to be filled by more rules. `policy.rs` has said so since ADR-0019: *a regex
blocklist over a Turing-complete shell is a seatbelt, not a sandbox.*

A denylist enumerates what is forbidden over an infinite space of equivalent commands. No
number of rules turns that into a boundary. A boundary appears only when the burden of proof
is inverted.

## Considered Options

- **Harden the denylist.** Normalise before matching (decode `-EncodedCommand`, strip quote
  splitting), add rules for `IEX`, `curl | sh`, `certutil -decode`, bulk file access.
- **A fleet-wide execution mode with an allowlist.** The operator declares what may run;
  everything else is refused.
- **A per-host execution mode.** The same, resolved per `agent_id`.
- **An approval gate.** A third verdict, `require_approval`, holding ambiguous commands for a
  human.
- **A parameterised command set.** Retire the free-string shell in favour of named commands
  with typed parameters, executed as argv.

## Decision Outcome

Chosen option: **a fleet-wide execution mode with an allowlist**, carried on the existing
`policy` frame as `policy.shell`, enforced by the agent and mirrored on the server.

`mode` is one of `unrestricted` (run anything the deny rules allow — the default and the
pre-0.18 behaviour), `allowlist` (run only what fully matches an allow rule), or `off`.

Hardening the denylist was rejected as *the* answer, not as a bad idea: it improves nothing
about the case that prompted this, and in `allowlist` mode obfuscation stops mattering —
an encoded command simply matches no allow rule. Doing both would have spent the review's
attention on the half that is not the boundary.

Per-host was rejected for now, not forever; see *What this leaves open*.

An approval gate would put a human in the path of every MCP call and moves the session model,
which is a larger decision than this one and a different one. A parameterised command set is
the honest endgame for "ransomware-proof", but it is a new tool, not a classifier, and it
would retire a capability operators rely on.

Four properties bind every implementation, and each has a case in
`docs/fixtures/vectors/policy_decisions.json`:

1. **Deny outranks allow.** Deny rules are evaluated first and always. An allow rule can
   never lift one from the shared catalog. The catalog stays a floor.
2. **Allow rules match the whole trimmed command, never a substring.** Otherwise an allow
   rule of `Get-Process` admits `Get-Process; rm -rf /` — the control defeated by a
   semicolon. Python uses `re.fullmatch`; Rust wraps each pattern `^(?:…)$` at compile time,
   because `Regex::is_match` is a substring search.
3. **An empty allow list under `allowlist` blocks everything.** Removing the last rule is not
   the same as leaving the mode.
4. **Refusals are `blocked`.** `disabled` stays the agent-local kill switch (ADR-0011).

### What this defends against, and what it does not

It defends against an abused or prompt-injected MCP credential, and against accidental
destruction. It does **not** defend against a compromised kenny server, which is what pushes
the mode, nor against local root on the managed host, which already owns the agent binary.
Recording this is the point: a control whose limits are not written down gets relied on past
them.

That threat model is what makes the role split load-bearing rather than cosmetic. The mode
(`KENNY_SHELL_POLICY_MODE`) and its allow rules are **superuser**; `/api/settings` already is.
Adding a deny rule stays an operator action. A principal that can call `shell_exec` over
`/mcp` must not be able to relax the control that governs it, or the control is circular.

### Consequences

- Good, because the answer to "what may run on these machines" becomes a statement an
  operator makes, not a list of things someone remembered to forbid.
- Good, because the joined decision vectors now pin `policy.py` and `policy.rs` against each
  other. They were hand-written mirrors with no test that failed when they diverged, which
  the root `CLAUDE.md` invariant forbids. Adding a second decision axis without that would
  have doubled the drift surface.
- Good, because the agent persists the applied mode beside its kill-switch control file and
  restores it at startup, so a restart or reconnect no longer runs unrestricted for the
  seconds until the first `policy` frame lands.
- Bad, because `allowlist` mode is only as good as the rules written into it. An allow rule
  of `.*`, or one ending in an unbounded `.*`, gives the shell back. The dashboard warns
  about the empty list; it cannot audit intent.
- Bad, because the server mirror's status is no longer uniform. For an agent that enforces
  the mode it stays UX, as ADR-0020 has it. For an agent predating v0.18 — which parses the
  frame and ignores `shell` — the mirror is the only place the mode is enforced, and it
  covers only what passes through `tunnel.send_request`.
- Neutral: the agent's fallback is deliberately **not** symmetric with ADR-0011. The kill
  switch reads fail-safe-to-on so the machine's owner can always stop remote control; this
  is a restriction, so an absent or unparseable file falls back to `unrestricted` and logs
  loudly rather than refusing everything. An agent that bricks fleet administration over a
  disk hiccup is the worse failure for a self-hosted admin tool, and an attacker who can
  delete that file already owns the binary beside it. What persistence buys is the reconnect
  window, not tamper resistance.

### What this leaves open

A per-agent axis is deferred, not refused, and the shape a later change would take is fixed
here so it is additive:

- **The wire needs nothing.** `policy` is already a per-connection frame. A per-agent
  override changes only how the server resolves what to put in `shell`.
- **The agent stays ignorant of the axis.** It enforces the policy it was handed and holds no
  notion of "fleet" versus "host". `unrestricted` is a default, not a statement about scope.
- **Storage shape**: a fleet default plus an optional per-`agent_id` override, as
  `agent_channel_prefs` and `webfilter_config` already do. `shell_allow_rules` carries a
  reserved `agent_id` column today, empty-string-sentinelled per ADR-0041.
- **Precedence is decided**: most-specific-wins.

One question is deliberately left to its own record: **whether a per-agent override may
widen.** Ordered by permissiveness, `off` < `allowlist` < `unrestricted`. A narrowing-only
override extends the narrow-only principle of ADR-0047 ("a profile only ever narrows; it
grants nothing") and needs no new decision. A widening override turns the fleet setting from
a floor into a suggestion — this boundary, moved back — and so needs one. The real case for
widening, a build host that genuinely needs an unrestricted shell, is acknowledged; it costs
an ADR rather than a commit.

## More Information

- Contract: `docs/protocol.md` § `policy`, `PROTOCOL_VERSION` 0.18. Golden fixture
  `docs/fixtures/policy_shell.json`; decision vectors
  `docs/fixtures/vectors/policy_decisions.json`.
- Enforcement: `kenny-agent/src/policy.rs` (authoritative),
  `kenny-server/kenny_server/policy.py` (mirror, at the `tunnel.send_request` choke point).
- Related: ADR-0009 (operator confirm-gate), ADR-0011 (kill switch), ADR-0045 (tool tiers),
  ADR-0047 (capability profiles), ADR-0032 (settings resolution).

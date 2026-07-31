# 0047. Account governance on Linux

- Status: proposed
- Date: 2026-07-31

## Context and Problem Statement

[ADR-0046](0046-account-governance-local-and-microsoft.md) gave kenny the ability to steer
*who may sign in* — seven account-keyed tools, with `local_accounts` as their inventory. It
was written for Windows, and pinned all seven to `windows` in the server's OS-scope table.

The fleet is not Windows-only. [ADR-0035](0035-linux-agent-support.md) made Linux a
first-class agent target, [ADR-0038](0038-linux-agent-distribution-convenience-script.md)
gave it a distribution path, and `local_accounts` already has a real Linux arm reading
`/etc/passwd` + `/etc/group`. Account governance is the one control that makes every other
control hold (ADR-0046's own argument), and on a Linux home server — where sudo is the whole
security model — it holds even harder than on a family PC.

The question is not *whether* to govern Linux accounts. It is **what shape** that takes, and
the shape is decided by one constraint: the dashboard must not become two worlds. Today it
would become three, because ADR-0046 already had to solve this once for local-vs-Microsoft
accounts and chose "one tool family keyed by account, capability discovered from telemetry".

There is also a live defect to settle. `renderAccountsDetail` has no OS branch at all, so a
Linux host **already** renders the complete Windows verb set — "suspend", "make admin", "no
remote desktop", "built-in Windows accounts cannot be deleted" — and every button fails
server-side with `requires windows`. The dashboard is already one world; it is just lying
about it. Any decision here has to either make that true or make it visibly false.

### Where the layers line up, and where they do not

| Layer | Windows | Linux | Same shape? |
|---|---|---|---|
| **L1 account database** | SAM/LSA | `/etc/passwd`, `/etc/shadow`, `/etc/group` | yes — enable/disable, admin membership, create/delete |
| **L2 session** | WTS (`WTSDisconnectSession`/`WTSLogoffSession`) | `systemd-logind` (`loginctl`) | yes, when a session manager is present |
| **L3 machine policy** | `net accounts` | PAM (`pwquality`, `login.defs`, `faillock`) | yes, per knob — each may be absent |
| **L4 remote sign-in** | two distinct planes (network logon, RDP) | one plane (SSH) | **no** — this is the real asymmetry |

The agent runs as `User=root` under systemd (`kenny-agent/src/service.rs`), so `/etc/shadow`,
`usermod`, `useradd`, `userdel`, `loginctl` and the sshd configuration are all reachable.
Nothing in the Linux arm needs a privilege the agent does not already have.

## Considered Options

- **A second `linux_account_*` tool family.** Rejected for the same reason ADR-0046 rejected
  `local_account_*` / `msaccount_*`: it pushes the OS distinction onto every caller — Claude,
  the dashboard, the operator — for a distinction that only matters at the bottom of the
  stack. It would also mean two dashboard panels, which is precisely the outcome this
  record exists to avoid. Two server tests
  (`test_there_is_no_per_kind_tool_or_kind_argument`,
  `test_no_other_forwarded_tool_silently_acquired_a_role_gate`) already fail loudly if anyone
  tries; they are kept untouched as the guardrail.
- **Keep the tools Windows-scoped, add a read-only Linux inventory.** Rejected: it fixes the
  lying dashboard by admitting defeat, and leaves a Linux server's sudo membership — the
  single most valuable governance lever kenny could pull — permanently out of reach.
- **Widen the OS scope and carry every Linux asymmetry in the existing per-account
  `unsupported` map (chosen).** ADR-0046 built that map for exactly this: "the
  capability-negation shape absorbs future asymmetry without a contract change: adding
  password reset later means one more `unsupported` entry, not a second tool family."

- **A new neutral `remote_shell` deny token for SSH** vs. **reusing `remote_interactive`
  (chosen).** A new token is semantically cleaner in isolation, but it forces the dashboard
  to render a different set of checkboxes per OS — reintroducing the two worlds one layer
  down. `remote_interactive` already means "deny interactive sign-in from elsewhere", which
  is exactly what an SSH deny does. The genuinely absent concept is `network`, and absence is
  what the negation map is for.
- **A new `protected: bool` field so the guard can refuse to touch root** vs. **routing
  root's asymmetry through `unsupported` (chosen).** A new boolean would have been the
  smallest change, but it duplicates information the negation map already has to carry
  (root's three restrictions differ from each other — it cannot be disabled, cannot be
  demoted, cannot be deleted, and each needs its own reason). Reusing `builtin_admin` for
  root was also considered and rejected: it would fire the "built-in Administrator enabled"
  health warning on every Linux host, where root being enabled is not a finding.

## Decision Outcome

Chosen: **the account-governance tool family loses its OS scope; every Linux asymmetry is
published in the per-account `unsupported` map; and the agent's self-protection guard
refuses any action whose verb that map names.** `PROTOCOL_VERSION` bumps `0.15 → 0.16` — no
frame or field shape changes, only documented behaviour, a written-down vocabulary, and one
optional additive key.

That last clause is the load-bearing one. Before this record, `unsupported` was advisory: the
dashboard greyed a control out and the agent refused separately, in its own code. Now
**`guard()` reads the live inventory and refuses exactly what the inventory advertises**, so
the published capability set and the enforced one are the same list by construction. The
guard is already `#[cfg]`-free and runs before the platform split, so this is exercised on
Linux CI — including for the Windows arms.

- **Verb vocabulary written down.** `unsupported` keys are *capability verbs*, not tool
  names (`reset_password` already established that — there is no such tool). The set is
  `set_enabled`, `set_admin`, `deny_network`, `deny_remote_interactive`, `delete`,
  `session_lock`, `session_logoff`, `reset_password`. Splitting the two deny rights and the
  two session actions is deliberate: one tool can be **partly** available, and the UI needs
  that granularity to grey one checkbox and leave the other live.
- **Host capability, not just account kind.** On Windows, `unsupported` is a function of the
  account (`kind`). On Linux it is a function of the account **and the host** — no
  `systemd-logind`, no `sshd`, no `faillock` are all real configurations. The collector
  composes host-level negations with kind-level ones; the wire shape is unchanged because the
  composition happens before serialization.
- **`remote_interactive` = deny SSH**, applied through a kenny-owned
  `/etc/ssh/sshd_config.d/50-kenny-deny.conf` `DenyUsers` block, validated with `sshd -t`
  before reload — never a blind write to a file that can lock out remote administration.
  `network` is negated per account with `no_network_logon_plane`.
- **"Disabled" on Linux means account expiry plus a locked password**, not a locked password
  alone. `usermod -L` leaves SSH **key** authentication working, so a password lock alone
  would report an account as suspended while it is still reachable — the worst possible
  failure mode for this feature. Expiry (`usermod --expiredate 1`) is what actually closes
  the door, and `enabled` is read back from `/etc/shadow` rather than inferred.
- **The admin group is resolved, not assumed** — the first of `sudo`, `wheel`, `admin` that
  exists in `/etc/group`, with removal stripping membership in all of them. This is the
  deterministic analogue of ADR-0046's well-known-SID trick: distro-proof the way the SID is
  locale-proof.
- **root is protected through the map, not through a special case**:
  `set_admin → uid_0_is_always_root` (group membership does not grant or revoke root),
  `set_enabled → uid_0_cannot_be_disabled`, `delete → uid_0`. The guard then refuses all
  three without knowing what root is.
- **A `nologin` shell is reported, not rewritten.** Such an account shows `enabled: false`
  with `set_enabled → nologin_shell`, because kenny changing a login shell is a different
  and more surprising act than clearing an expiry date.
- **`logon_failures` gains a Linux arm** (sshd/PAM failures from the journal, falling back to
  `/var/log/auth.log`) and leaves `WINDOWS_ONLY_SECTIONS`. `network` simply does not occur
  there — SSH is `remote`, console and display-manager attempts are `interactive`. Reporting
  an empty third bucket is honest; inventing a mapping for it would not be.
- **The privacy line does not move again.** ADR-0046 moved it once: identity is named and
  governable, behaviour stays aggregated. Linux changes nothing about that. No home directory
  contents, no command history, no per-user activity — and no source addresses in
  `logon_failures`, exactly as on Windows.
- **The Linux arm shells out with `Command::new` directly**, not through `shell_exec`. The
  `posix` deny-rule catalog (ADR-0020/0021) therefore does not apply to it. That is correct
  rather than an oversight: the deny catalog exists to constrain *operator-supplied*
  commands, and applying it to kenny's own fixed argv would make a policy edit able to break
  governance. Named here so it is a decision.

### Consequences

- Good, because there is one account panel, one tool catalog, one mental model, and one
  audit-log vocabulary across both operating systems. An operator moving between a Windows
  PC and a Linux server sees the same verbs and the same layout, differing only where a
  control is greyed out **with its reason shown**.
- Good, because it fixes a live defect rather than working around it: buttons that today
  render on Linux and cannot work will either work or be visibly unavailable.
- Good, because enforcement and advertisement can no longer drift — the guard reads the same
  map the dashboard renders.
- Good, because on Linux `password_required` and `password_last_set` become real values read
  from `/etc/shadow`, so the existing "admin requires no password" crit rule works on Linux
  for the first time.
- Good, because the negation map now absorbs *host* asymmetry as well as account asymmetry,
  which is what a macOS arm or a directory-backed (SSSD/LDAP) `kind` would need next.
- Bad, because the wire vocabulary stays Windows-flavoured: `remote_interactive` means SSH on
  Linux, and `local_accounts` names a section that includes Microsoft accounts. ADR-0035
  already accepted this cost ("two naming worlds on the wire — an accepted cosmetic cost")
  and rejected a renaming migration as a large breaking change for a cosmetic payoff. The
  same judgment applies here, and the UI is where the vocabulary is made readable.
- Bad, because Linux fragmentation is real and this cannot be fully tested in CI. The sshd
  drop-in directory, `faillock` vs. `pam_tally2`, `pwquality` vs. `cracklib`, and logind's
  presence all vary by distro. The design's answer is to **probe and negate** rather than
  assume — an absent knob becomes an `unsupported` entry, not a silent failure — but the
  probes themselves need verification on real Debian and Fedora hosts before this is relied
  on.
- Bad, because the kill switch still outranks governance (ADR-0046's stance, unchanged): all
  seven tools are mutating, so a user who switches remote control off can still refuse *new*
  measures. Already-applied state persists, and the drift signal remains load-bearing.
- Bad, because `shell_exec` bypasses the whole layer on Linux exactly as `powershell_exec`
  does on Windows. An operator can call `usermod` directly. Not addressed here, for the same
  reason ADR-0046 did not address it.
- Neutral, because `account_create` is now the *less* asymmetric verb on Linux than on
  Windows — there is no Linux equivalent of "a Microsoft account can only be added
  interactively". The asymmetry table is not symmetric, which is the point of publishing it
  as data.

## More Information

- Explicitly **out of scope**: directory-backed identities (SSSD/LDAP/AD-joined hosts — they
  would arrive as a new `kind` with a large `unsupported` map, exactly as ADR-0046 predicted
  for Entra); SELinux/AppArmor confinement; `sudoers` rule editing beyond group membership (a
  drop-in-file editor for `/etc/sudoers.d` is a materially different and more dangerous
  surface); PAM stack editing beyond the three documented knobs; macOS, which keeps returning
  `unsupported`.
- Contract: `docs/protocol.md` § Tool catalog → *Account governance tools* → *Capability
  negation*, § Telemetry sections (`local_accounts`, `logon_failures`), § Versioning
  (`PROTOCOL_VERSION = "0.16"`); fixtures `docs/fixtures/telemetry_snapshot_linux.json` and
  `docs/fixtures/response_*_linux.json`.
- Code (agent): `kenny-agent/src/handlers/accounts.rs` (`guard`, `linux_impl`),
  `src/telemetry/collectors/local_accounts.rs` (`core::HostCapabilities`, `linux_impl`),
  `src/telemetry/collectors/logon_failures.rs` (`core::shape_tokens`, `linux_impl`).
- Code (server): `kenny-server/kenny_server/tools.py` (`_OS_SCOPED_TOOLS`),
  `health_rules.py` (`WINDOWS_ONLY_SECTIONS`), `webui/index.html` (`renderAccountsDetail`).
- Related: [ADR-0046](0046-account-governance-local-and-microsoft.md) (the record this
  extends — its Windows rationale stands unchanged),
  [ADR-0035](0035-linux-agent-support.md) (first-class Linux, additive growth — this is its
  Phase 2/3 for accounts), [ADR-0011](0011-local-remote-control-kill-switch.md),
  [ADR-0020](0020-agent-side-deterministic-tool-guard.md) /
  [ADR-0021](0021-shared-policy-catalog-operator-rules-and-server-mirror.md) (the deny
  catalog this deliberately does not route through),
  [ADR-0024](0024-untrusted-agent-data-in-chat-context.md) (confirm-gate parity),
  [ADR-0026](0026-parental-controls-web-activity-and-webfilter.md) /
  [ADR-0032](0032-screen-time-aggregated-session-minutes.md) (the privacy line, unmoved),
  [ADR-0037](0037-multi-user-authentication.md) (the operator role gate, unchanged).

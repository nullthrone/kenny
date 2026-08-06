# 0042. Account governance across local and Microsoft accounts

- Status: accepted
- Date: 2026-07-31

## Context and Problem Statement

kenny can *see* Windows accounts but cannot *steer* them. The `local_accounts`
collector reads `Get-LocalUser` joined with the Administrators group and a health rule
warns about an enabled built-in Administrator or a password-less admin — and that is
the entire account surface. No tool takes an account as an argument; `dispatch.rs` has
no notion of a subject at all.

That gap undercuts the rest of the product. Every other control kenny has is reversible
by a local administrator: the hosts-file web filter (ADR-0024), the deny-rule guard's
self-protection, the service itself. **Account governance is the layer everything else
rests on** — demoting a child from Administrator is what makes the parental controls
stick.

The operator's fleet is mixed and drifting: Windows 11 setup pushes hard toward
Microsoft accounts, so a design that governs local accounts and treats Microsoft
accounts as a special case would be obsolete on arrival. The requirement is that both
kinds are managed *the same way*, not through parallel code paths that happen to meet
in the UI.

### Why type-agnostic needs no abstraction here

A Microsoft account on a workgroup PC **is** a local SAM entry with a machine-local SID
and profile. `Get-LocalUser` returns it with `PrincipalSource = MicrosoftAccount`;
`Remove-LocalGroupMember -Member "MicrosoftAccount\x@y.com"` works. Below a certain
layer, Windows itself draws no distinction — so we do not have to abstract one away, we
only have to stop introducing one.

| Layer | Type-agnostic | In scope |
|---|---|---|
| **L1 SAM/LSA** | fully | enable/disable, admin membership, deny-logon rights, create/delete |
| **L2 Session** | fully | manual lock / log off |
| **L3 Machine policy** | machine-wide, not per account | password policy (**local accounts only**) |
| **L4 Cloud identity** | not at all | out of scope — see below |

### Where Microsoft closes the door

There is **no administrative API for consumer Microsoft accounts**. Microsoft Graph
covers Entra ID (work/school) identities, not MSAs, and Microsoft states in its own
Learn Q&A that Family Safety exposes no API — its screen time, app limits, web
restrictions, spending controls, reports and "can I have more time?" requests are
reachable only through family.microsoft.com and the Family Safety app.

Structurally unreachable, with no partial coverage and no workaround: MSA password,
MFA, account recovery, every Family Safety function, and the Windows Hello PIN (which
is per-user and TPM/DPAPI-bound even for local accounts).

This is a hard boundary, and the design states it rather than papering over it.

### The second wall is ours

ADR-0024 and ADR-0029 forbid per-user attribution, and both are enforced by tests:
`screen_time` may not carry usernames (each day object must have exactly two keys) and
`local_accounts` may not put SIDs on the wire (the test greps the payload for
`S-1-5-`). Account governance is per-account by definition. That deserves a deliberate
decision, not a quiet field addition.

## Considered Options

- **Two tool families (`local_account_*` / `msaccount_*`).** Rejected: it pushes the
  type distinction onto every caller — Claude, the dashboard, the operator — for a
  distinction Windows does not make at the layer we operate on. It would also encode
  today's account mix into the tool catalog, which is exactly the thing that is
  drifting.
- **One tool family with a `kind` argument the caller must pass.** Rejected: the caller
  would have to know the type before acting, and a wrong `kind` becomes a new failure
  mode for no benefit. The agent can resolve the account itself.
- **One tool family keyed by account, capability discovered from telemetry (chosen).**
  The caller names an account; the agent resolves it. The inventory publishes, per
  account, which verbs are *not* available and why.

- **Wire key: SID / qualified name (`MicrosoftAccount\…`) / SAM name (chosen).** The
  SID is unambiguous but reverses the ADR-0024 no-SIDs-on-the-wire invariant for no
  operational gain. The qualified name carries the MSA email address — the most
  sensitive datum in this whole feature. The SAM name is stable per machine, unique in
  SAM by construction, works for both kinds, and is exactly what the PowerShell
  cmdlets accept.
- **Capability expressed as an allow-list / as a negation (chosen).** Listing
  everything supported per account is verbose on every snapshot and states the boring
  case loudly. Listing only what is *unsupported*, with a reason, keeps the payload
  small and makes the wire format itself say "seamless is the default, asymmetry is the
  named exception".

- **Time enforcement now / deferred (chosen: deferred).** Runtime enforcement
  (warn → lock → log off) is the real compensation for the missing Family Safety API,
  but it needs a scheduler inside the agent, which breaks the stateless-collector
  invariant of ADR-0007. Deferred deliberately, not rejected; `account_session_action`
  ships as an operator-triggered action so the primitive exists and the mechanism can be
  judged before it is automated.

## Decision Outcome

Chosen: **one account-keyed tool family plus an extended inventory, operating on the
SAM/LSA/session layers where Windows is already type-agnostic, with per-account
capability negation carrying the asymmetries that remain.** `PROTOCOL_VERSION` bumps
`0.14 → 0.15` (additive: one section, four fields on an existing section, seven tools).

**The privacy line moves, and moves once.** *Identity is named and governable;
behaviour stays aggregated.* Accounts get names, kinds, admin status and governance
verbs. `screen_time` and `web_activity` are **untouched** — they remain whole-machine,
with their existing tests intact. kenny may know who is allowed to sign in; it still
does not know what any particular person did.

- **Wire key is the SAM name.** `local_accounts.accounts[].name`, already on the wire
  today. Preserves the no-SIDs invariant unchanged.
- **No Microsoft account email addresses on the wire.** A new invariant with the same
  rationale as the SID rule: an MSA address is a globally resolvable,
  credential-adjacent identifier that governance does not need. The inventory reports
  `display` (from `FullName`, which the user chose themselves and is typically a first
  name). Honest residue: when `FullName` is empty the SAM name remains, and for MSA
  accounts that is a five-character prefix of the address — true today already, and
  documented rather than disguised.
- **Inventory extension** — `local_accounts` accounts gain `kind`
  (∈ `local`/`microsoft`/`entra`/`unknown`, from `PrincipalSource`), `display`,
  `deny_logon` (the subset of `network`/`remote_interactive` currently set via LSA),
  and `unsupported` (a map of verb → short reason token; absent verbs are supported).
  The section gains `password_policy`, which carries its own
  `applies_to: "local_only"` so a reader cannot mistake its reach.
- **New `logon_failures` section** — Security event 4625 aggregated per account over
  24 h, with a `types` breakdown (`interactive`/`network`/`remote`) that separates "a
  child tried a parent's password at the console" from "something is hammering RDP".
  No source addresses. Unmatched names collapse into a single `unmatched_count`. This
  attributes an event to a named account, which is a borderline case under the line
  drawn above; it is admitted deliberately because an authentication attempt belongs to
  the identity plane, not the behaviour plane, and it is named here so the boundary
  stays visible.
- **Seven tools**, all account-keyed except the machine-wide last one:
  `account_set_enabled`, `account_set_admin`, `account_set_logon_rights`,
  `account_create`, `account_delete`, `account_session_action`, `password_policy_set`.
  No `account_list` — the inventory is telemetry, refreshable on demand through the
  existing `telemetry_collect`. No `password_policy_get` — that is a section field.
- **Granular tools, not one generic setter.** The audit log records the tool name but
  **not** the arguments. A single `account_set` would leave the audit trail unable to
  distinguish "renamed an account" from "granted administrator", which is precisely the
  distinction worth auditing.
- **Agent-side self-protection, not remotely overridable**, following the
  `webfilter_apply` reserved-set precedent and returning `blocked`: the last enabled
  local administrator can never be disabled, demoted, deleted, or given deny-logon
  rights, and accounts below RID 1000 cannot be deleted. Validation runs *before* the
  `#[cfg]` split so it is exercised on Linux CI.
- **`SeDenyInteractiveLogonRight` is not offered.** Only `network` and
  `remote_interactive` are exposed. The interactive deny can lock out the sole console
  user, and kenny has no remote console to recover with. A deliberate omission, not an
  oversight.
- **Operator role required.** Forwarded tools today require only that the principal can
  see the host. These are materially more dangerous, so `tools.py` gains a per-tool
  minimum-role map — the first per-tool role gate on a forwarded tool. All seven are
  also added to `control::is_mutating` and to the chat confirm-gate, per the gate parity
  ADR-0023 requires.

### Consequences

- Good, because both account kinds are managed through one surface with one mental
  model, and the asymmetry that genuinely remains (`account_create` is local-only) is
  visible in the data instead of hidden in a branch.
- Good, because the strongest available control — removing local administrator rights —
  becomes a first-class, audited, confirm-gated action, which is what makes ADR-0024's
  web filtering hold rather than being trivially undone.
- Good, because the capability-negation shape absorbs future asymmetry without a
  contract change: adding password reset later means one more `unsupported` entry, not
  a second tool family.
- **Bad, because the kill switch outranks governance.** All seven tools are mutating,
  so they are refused with `disabled` when remote control is off — and the tray writes
  that control file as the *standard user* (the installer grants Authenticated Users
  write access to `%ProgramData%\kenny`, which is what lets a non-admin flip it at all).
  A demoted child can therefore still refuse *new* measures. Already-applied SAM/LSA
  state persists. This is ADR-0024's stance unchanged — **monitoring is the guarantee,
  enforcement is best-effort** — and it is why the drift signal is load-bearing rather
  than decorative.
- Bad, because `powershell_exec` bypasses the whole layer: an operator can call
  `Remove-LocalUser` directly and defeat the self-protection set. Deny rules could
  narrow that, but `policy.rs` is right that a regex blocklist over a Turing-complete
  shell is a seatbelt, not a sandbox. Not addressed here.
- Bad, because Family Safety collides invisibly: when a child's MSA belongs to a
  Microsoft family group, Windows enforces that group's screen time itself, and kenny
  can neither read nor influence it. The dashboard says so when `kind == microsoft`.
- Bad, because the password policy reaches only local accounts. Surfaced in the UI, not
  just in the docs — a policy that silently misses half the accounts is worse than none.
- Bad, because four behaviours could not be settled from documentation and need
  verification on real hardware before this is relied on: whether `Disable-LocalUser`
  on an MSA-linked account blocks sign-in including the Hello PIN path; whether
  `Remove-LocalGroupMember` accepts the bare SAM name as `-Member`; whether the LSA
  deny rights apply on Windows Home (where only the MMC snap-in is missing, not the
  API); and what removing an MSA-linked account does to the local profile.

## More Information

- Explicitly out of scope, with reasons: **Family Safety** in its entirety (no API),
  **MSA password / MFA / recovery** (cloud-side, no admin API), **Windows Hello PIN**
  (per-user, TPM-bound), **per-account web filtering** (kenny filters via the hosts
  file, which is machine-wide), **SAM logon hours** and **automated time enforcement**
  (deferred — the latter needs an agent-side scheduler and its own ADR), and
  **intercepting sign-in in real time** (would require a credential provider in the
  winlogon path, where a defect bricks the machine).
- **Entra ID as a third provider** is where cloud-side governance actually exists:
  Microsoft Graph offers full lifecycle management for work/school identities.
  Irrelevant for a family fleet, but it confirms that `kind` is the right axis — a
  future Entra arm slots in by shrinking its `unsupported` map, with no change to the
  tool catalog.
- Contract: `docs/protocol.md` § Tool catalog, § Telemetry sections, § Versioning
  (`PROTOCOL_VERSION = "0.15"`); fixtures `docs/fixtures/request|response_account_*.json`,
  `request|response_password_policy_set.json`, and the `local_accounts` /
  `logon_failures` sections in `docs/fixtures/telemetry_snapshot.json`.
- Code (agent): `kenny-agent/src/handlers/accounts.rs`, `src/dispatch.rs`,
  `src/control.rs`, `src/telemetry/collectors/local_accounts.rs`,
  `src/telemetry/collectors/logon_failures.rs`.
- Code (server): `kenny-server/kenny_server/tools.py`, `health_rules.py`, `diffs.py`,
  `chat.py`, `webui/__init__.py`.
- Extended by [ADR-0043](0043-account-governance-on-linux.md), which drops the Windows OS
  scope of this tool family and routes the Linux asymmetries through the same per-account
  capability-negation map. The rationale recorded here is unchanged by that.
- Related: [ADR-0011](0011-local-remote-control-kill-switch.md) (kill switch),
  [ADR-0019](0019-agent-side-deterministic-tool-guard.md) (deterministic guard),
  [ADR-0023](0023-untrusted-agent-data-in-chat-context.md) (confirm-gate parity),
  [ADR-0024](0024-parental-controls-web-activity-and-webfilter.md) (the stance this
  refines, and the control this one makes stick),
  [ADR-0028](0028-security-and-resilience-telemetry-sections.md) (`local_accounts`
  origin), [ADR-0029](0029-screen-time-aggregated-session-minutes.md) (aggregation rule
  left intact), [ADR-0033](0033-multi-user-authentication.md) (operator roles),
  [ADR-0038](0038-explicit-per-call-agent-targeting.md) (`agent_id` routing).

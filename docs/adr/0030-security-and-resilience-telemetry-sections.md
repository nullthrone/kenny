# 0030. Security-inventory and resilience telemetry sections

- Status: accepted
- Date: 2026-07-02

## Context and Problem Statement

kenny's 26 telemetry sections describe a machine's *state* (disk, Defender, updates,
thermals) but barely its *attack and persistence surface*, and not at all whether its
data would survive a dead disk. For a family fleet the recurring operator questions
are: what got installed on this PC, which browser extensions did the kids add, what is
listening on the network, which scheduled tasks and accounts exist (and who is an
administrator) — and is any backup mechanism alive? With the diff engine (ADR-0029) in
place, inventory sections become doubly valuable: the server can notify on "new since
yesterday" without any agent-side state.

## Considered Options

- **On-demand capability tools only** (`diag_*`-style, operator-triggered). Rejected:
  no history, no diffing, no health rules — the value here is *continuous* inventory.
- **One combined "security" section.** Rejected: sections are the unit of status,
  summary, health rules, drill-down and diff specs; combining them loses all of that
  granularity for no saving.
- **Seven focused telemetry sections, batched into one protocol bump.** Chosen.

## Decision Outcome

Seven additive sections (plus `screen_time`, decided separately in ADR-0031) enter the
contract at **PROTOCOL_VERSION 0.10**: `installed_software`, `browser_extensions`,
`listening_ports`, `scheduled_tasks`, `local_accounts` (security inventory) and
`backup_status`, `net_quality` (resilience). Payload shapes live in
`docs/protocol.md` § Telemetry sections and `docs/fixtures/telemetry_snapshot.json`;
the bump is one changelog line batching all of v0.10's sections.

Data-source decisions that are part of this ADR (they encode hard-won Windows
know-how and must not silently regress):

- `installed_software` reads the **registry Uninstall keys** (HKLM 64+32-bit) — never
  `Win32_Product` (whose enumeration triggers MSI reconfiguration storms) and never
  `winget list` (too slow for the collector probe budget). Per-user HKCU installs are
  invisible to the session-0 service: a documented blind spot, accepted.
- `browser_extensions` reads extension manifests from the same per-user profile
  locations `web_activity` already walks. **Privacy stance inherited from ADR-0026:**
  extensions are deduplicated across users and profiles by `(browser, id)`; no
  per-user attribution goes on the wire.
- `local_accounts` resolves the Administrators group by **SID** `S-1-5-32-544`
  (locale-proof, unlike the localized group name) and marks built-ins by well-known
  RID (-500/-501); full SIDs never go on the wire (minimum identifying tokens).
- `scheduled_tasks` reports only **non-Microsoft** task paths — the surface an
  operator actually reviews — with `total_count` for context.
- `backup_status` reports *evidence*, not configuration it cannot see: restore
  points, the File History service state (per-user File History config is unreadable
  from session 0 → `configured: null`), and OneDrive presence/running.
- `net_quality` is a **stateless point probe** (a few ICMP echoes to the gateway and
  a reference host at collect time) per the stateless-collector rule (ADR-0007);
  rolling link-quality trends, if ever wanted, belong server-side.

Every list is deduplicated, sorted and **capped with a `truncated` flag**
(software 300, extensions/ports/tasks 200), keeping the worst-case snapshot well
inside the 256 KB frame cap. Pure inventory sections always report `status: "ok"`
from the agent; judgment is server-side: health rules for `listening_ports`
(non-loopback remote-access listener ⇒ warn), `local_accounts` (enabled built-in
Administrator/Guest ⇒ warn; enabled admin without password requirement ⇒ crit),
`backup_status` (no living mechanism ⇒ warn) and `net_quality` (poor gateway link ⇒
warn, heavy reference loss ⇒ crit), plus the ADR-0029 diff specs that notify on new
software/extensions/tasks/ports/devices and admin-group changes.

### Consequences

- Good, because the highest-signal family-fleet questions ("what did the kid
  install?", "who is admin?", "is anything backing up?") get continuous,
  diffable, alertable answers with zero operator effort.
- Good, because one batched 0.10 bump keeps the version history readable and ships
  both sides (fixture-validated) in lockstep, per the contract-first rule.
- Bad, because seven more PowerShell probes run per collection cycle; bounded by the
  existing collector thread pool, per-probe budget and caps.
- Bad, because HKCU-installed software and per-user File History configuration are
  invisible from session 0 — accepted, documented blind spots rather than reasons to
  run probes in user sessions.

## More Information

- Related: ADR-0007 (stateless collectors, section status/summary), ADR-0026 (privacy
  stance, profile enumeration), ADR-0029 (diff engine consuming these sections),
  ADR-0031 (`screen_time`, the eighth v0.10 section).
- Contract: `docs/protocol.md` § Telemetry sections (v0.10 block),
  `docs/fixtures/telemetry_snapshot.json`.
- Code: `kenny-agent/src/telemetry/collectors/{installed_software,browser_extensions,listening_ports,scheduled_tasks,local_accounts,backup_status,net_quality}.rs`,
  `kenny-server/kenny_server/health_rules.py`, `kenny-server/kenny_server/diffs.py`.

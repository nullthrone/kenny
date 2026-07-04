# 0034. First-class Linux agent support

- Status: proposed
- Date: 2026-07-04

## Context and Problem Statement

kenny-agent targets Windows PCs in a family setting (ADR-0002): a single Rust binary that
installs as a Windows service (ADR-0013/0033), speaks the outbound WSS wire contract
(`docs/protocol.md`), pushes telemetry, and answers forwarded capability tools. We now want
the agent to run on **Linux clients and servers** too — headless boxes (home server, NAS,
Raspberry Pi) as well as Linux desktops — reporting into the same fleet and answering the
same operator through the same server.

The question is not "can Rust run on Linux" — it obviously can, and CI already builds and
unit-tests the agent on Linux. The question is **architectural**: which of kenny's patterns
carry over unchanged, which are structurally Windows-bound and cannot, what new capability a
Linux target unlocks, and how much of the existing Windows-shaped surface we adapt versus
leave as `unsupported`. That decision moves a structural boundary (a second target OS),
touches the wire contract, and forces both the agent and the server to grow OS-awareness —
so it is recorded here rather than as an implementation detail.

This ADR records the **decision to support Linux and the shape of that support**. It does
**not** land code; it defines the direction, the non-negotiable constraints, and a phased
roadmap so each subsequent implementation step is a routine change reviewed against this
record.

A key finding from surveying the codebase framed the whole decision: kenny is already
markedly multi-OS-aware by construction. The wire contract models a Linux agent today —
`register.meta.os ∈ {windows, linux, macos}` (`docs/protocol.md`), the `unsupported` error
code is documented with "e.g. `winget_list` on a Linux dev build", and every Windows-only
collector already emits an `"n/a on this platform"` stub off Windows. Transport, mutual
Ed25519 auth (ADR-0023), framing, config, the kill switch (ADR-0011), and the safety guard
(ADR-0020) are ungated and Linux-CI-tested. `os_family()` (`kenny-agent/src/util.rs`)
already reports `"linux"` on the wire. So a Linux agent that registers, authenticates,
heartbeats, pushes telemetry stubs, and serves the portable tools is reachable **without any
contract change** — the port is mostly *filling in* the non-Windows arms, not restructuring.

## Considered Options

- **A) First-class Linux agent, additive ("parallel") contract growth.** One codebase, one
  wire contract. Keep every Windows-named tool and telemetry section; on Linux they return
  `unsupported` / `"n/a on this platform"`. Add Linux capability as **new** collectors
  (`#[cfg(target_os = "linux")]` arms) and **new** additive tools/sections where a Linux
  concept has no existing home. Replace the Windows-service lifecycle with a parallel systemd
  path. Detect an interactive desktop at runtime and gate desktop-only features on it.
- **B) Generalize the contract to be OS-neutral.** Rename Windows-specific tools/sections to
  neutral names (`powershell_exec` → `shell_exec`, `winget_*` → `pkg_*`, `defender`/
  `win_update` → generic security/update sections). Cleaner long-term vocabulary, but a broad
  breaking contract change: `PROTOCOL_VERSION` bump, new fixtures, and a coordinated migration
  of both implementations and every stored snapshot's shape.
- **C) Portable subset only.** Ship a Linux build that serves *only* the already-portable
  tools and sections; everything else stays `unsupported` / `n/a` indefinitely. No contract
  change ever, but Linux stays a second-class citizen with no Linux-native telemetry or tools.
- **D) Separate Linux agent codebase/contract.** A distinct daemon with its own protocol.
  Rejected on sight — it duplicates transport, auth, framing, and the safety guard, and
  reintroduces exactly the two-language drift ADR-0005 exists to prevent, now across three
  code paths.

## Decision Outcome

Chosen option: **A — first-class Linux agent with additive ("parallel") contract growth**,
delivered in phases and with runtime desktop detection.

Rationale:

- It preserves the invariant that **the contract is authoritative and the two languages must
  not drift** (`CLAUDE.md`, ADR-0005). Additive growth means existing fixtures keep
  round-tripping unchanged; Windows behavior is untouched; every increment is a versioned,
  reviewable step rather than a big-bang migration.
- It matches the code's existing grain. The `#[cfg(windows)]`-real / `#[cfg(not(windows))]`-
  stub discipline (`kenny-agent/CLAUDE.md`) and the "portable core, OS probe gated" split
  already used by `handlers/webfilter.rs` and `telemetry/collectors/web_activity.rs` are the
  ready-made blueprint for each new Linux capability.
- It keeps Linux honest without a breaking change. `unsupported` and `"n/a on this platform"`
  are the *designed* escape hatches; a Linux agent uses them from day one and we fill in real
  Linux implementations behind them incrementally.
- Generalizing the vocabulary (B) is attractive but premature: it is a large breaking change
  whose payoff is cosmetic while Windows is the only mature target. If a neutral vocabulary
  earns its keep later, it can be introduced additively (e.g. a new `shell_exec` alongside
  `powershell_exec`) under its own ADR. C is rejected as a dead end; D as needless duplication.

"Clients and servers, equally" is handled by **runtime desktop detection**: the agent probes
for an interactive graphical session (e.g. `XDG_SESSION_TYPE` / `DISPLAY` / `loginctl`),
advertises the result in `register.meta`, and enables desktop-only features
(`screen_capture`, the parental-awareness sections) only when a desktop is present — so the
same binary serves a headless server and a Linux desktop without a separate build.

The analysis that backs this decision, in the architect's four framings:

### 1. Patterns we adopt unchanged

Already portable and Linux-CI-tested; carried over as-is:

- **Transport & reconnect** (`src/tunnel.rs`): one outbound WSS via `tokio-tungstenite` +
  rustls, exponential backoff, 30 s heartbeat. No `#[cfg]`.
- **Mutual Ed25519 auth** (`src/keys.rs`, `src/tunnel.rs`, ADR-0023): keygen/sign/verify are
  ungated; the private-key permission lockdown is already `#[cfg(unix)]` (chmod 600).
- **Framing & versioning** (`src/protocol.rs`): all frames + `PROTOCOL_VERSION`, round-tripped
  against `docs/fixtures/`.
- **Dispatch, kill switch, safety guard** (`src/dispatch.rs`, `src/control.rs`,
  `src/policy.rs`, ADR-0011/0020): portable; `os_family()` already reports `"linux"`.
- **Portable collectors** via `sysinfo`/`std`: `disk`, `memory`, `processes`, `network`,
  `uptime` yield real data on Linux immediately; `routing` already has a real Linux path
  (`/proc/net/route`).
- **Portable handlers:** `fs_*`, `net_config`, `diag_processes`, `telemetry_collect`;
  `powershell_exec` already falls back to `sh -c`.
- **The stub discipline itself** is the reusable pattern for everything below.

### 2. What is structurally not possible (or not 1:1)

- **The Windows-service lifecycle** (`src/service.rs`, `src/setup.rs`; SCM, the
  `windows-service` crate, UAC/`ShellExecuteExW`, `%ProgramFiles%\kenny`) has no Linux
  equivalent — it is replaced by a **parallel systemd-unit** model, not adapted.
- **The named-pipe IPC + session-0 model** (`src/screencap_ipc.rs`,
  `src/session_launch_ipc.rs`, `src/tray.rs`; ADR-0018/0022): a session-0 service launching
  apps onto the interactive desktop over a tray-helper pipe. Linux has no session-0 construct;
  a desktop client would need its own mechanism (user systemd unit / D-Bus), and a headless
  server drops it entirely.
- **Windows-native telemetry with no Linux semantics** stays an `n/a` stub on Linux because
  the *field names* are Windows concepts: `defender`/`defender_quarantine` (Microsoft
  Defender), `win_update` (KB numbers), `reliability` (Windows Reliability Index),
  `reboot_pending` (registry flags), `backup_status` (System Restore/File History/OneDrive).
- **Windows-native tools** stay `unsupported` on Linux: `winget_*`, `remotehelp_*` (Quick
  Assist), `agent_update` (Windows service-restart swap), `diag_eventlog`, and the
  registry/DoH half of `webfilter_*`.
- **`screen_capture`** (GDI `BitBlt`) and the desktop-dependent parental sections
  (`web_activity`, `screen_time`) require a graphical user session and are structurally
  unavailable on headless servers.

### 3. What becomes additionally possible (Linux-only value)

Additive, without touching Windows:

- **Server-grade telemetry** irrelevant to a family PC: systemd unit health (failed units),
  journald error/crash summaries (a `reliability` analogue), apt/dnf update & security-update
  counts (a `win_update`/`app_updates` analogue), `listening_ports` via `/proc/net` (cleaner
  than the Windows path), container health (Docker/Podman), SMART via `smartctl`.
- **Existing generically-named sections repopulated from Linux sources:** `firewall`
  (nftables/ufw), `encryption` (LUKS), `local_accounts` (`/etc/passwd` + `/etc/group` +
  sudoers), `time_sync` (`timedatectl`) — same section names, Linux data.
- **Package-management tools** (`pkg_list`/`pkg_install`/…) as the Linux counterpart to
  `winget_*`.
- **Runtime desktop detection** (the "clients and servers, equally" mechanism), enabling
  desktop-only features exactly where a desktop exists.

### 4. What we adapt (portable core, new OS arm)

- **`powershell_exec`** keeps its name but on Linux the "PowerShell" is a shell (`sh -c`,
  already present). Consequence for the guard: the `applies_to: "powershell"` deny rules
  (`docs/policy/deny_rules.json`, ADR-0020/0021) match against *shell* commands on Linux — a
  deliberate design point flagged for the Linux-tools phase (a neutral `shell_exec` may be
  added additively later).
- **`webfilter_*`**: the hosts-splice/hash/validation core is already `#[cfg(windows)]`-free;
  Linux has `/etc/hosts`, so `apply`/`clear` are adaptable (DNS flush via
  `resolvectl flush-caches`; the browser-DoH-registry step drops out).
- **Collector adaptation** as new `#[cfg(target_os = "linux")]` arms beside the Windows arm,
  reusing the section schema where sensible: `services` → systemd, `autostart` →
  systemd/XDG autostart, `scheduled_tasks` → cron/systemd timers, `installed_software` →
  package DB.
- **Path/directory adaptation:** `log_dir()`, `control_path()`, and the key path already have
  a non-Windows arm; production Linux should use `/var/lib/kenny` and `/etc/kenny` rather than
  the current `temp_dir()` dev fallback.

### Server-side consequences (named here, implemented in a later phase)

Real Linux agents force OS-awareness in three server places (`kenny-server/`):

- **`registry.py`** — the `Agent` model has no typed `os` field; `meta["os"]` is used only as
  a display string today. OS-dependent behavior needs it promoted to first class.
- **`health_rules.py`** — Windows-specific rules (`_rule_defender`, `_rule_win_update`,
  `_rule_reboot_pending`, `_rule_backup_status`, and the WinRM/RDP ports in the
  listening-ports rule) must be skipped or replaced for Linux agents, or they will score
  `crit` against `n/a` stubs.
- **Dashboard + `fleet_stats.py`** — `_os_inventory` defaults an unknown OS to `"Windows"`,
  and the UI is pervasively "PC"/Quick-Assist framed. Per-OS labels/icons and per-agent
  tool/section visibility are follow-up work.

### Consequences

- Good, because one codebase and one authoritative contract cover both operating systems;
  Windows behavior and fixtures are untouched, so the change carries near-zero regression risk
  per phase.
- Good, because it builds directly on the existing stub discipline and portable core, so each
  Linux capability is a localized, well-precedented change.
- Good, because Linux is useful from Phase 1 (portable telemetry + tools + systemd) before any
  contract change is needed.
- Bad, because "parallel" growth leaves two naming worlds on the wire (Windows-named tools
  that are `unsupported` on Linux, plus new Linux-named ones) — an accepted cosmetic cost,
  reversible via an additive neutral vocabulary later if it proves worth it.
- Bad, because the server must grow genuine OS-awareness (registry/health/dashboard) that it
  can defer today; until it does, a Linux agent's `n/a` stubs must not be scored as failures.
- Neutral, because Linux-native collectors, additive tools/sections, and any new frame fields
  are still contract changes — each is a `PROTOCOL_VERSION` bump + fixtures + both sides +
  `/contract-check`, exactly as the invariants require.

## More Information

Phased roadmap (each phase reviewed against this ADR; later phases get their own ADR only if
they move a further boundary):

- **Phase 1 — Bring-up, no contract change.** Linux agent registers/authenticates,
  heartbeats, portable collectors report real data, portable tools run; systemd unit as the
  lifecycle; everything else `unsupported`/`n/a`. Covers most of the headless-server use case.
- **Phase 2 — Linux collectors (additive).** systemd/journald/package-update/firewall/
  encryption/local_accounts/time_sync as `target_os = "linux"` arms; server health rules made
  OS-aware. New additive sections bump `PROTOCOL_VERSION` + fixtures + both sides.
- **Phase 3 — Linux tools & desktop features (additive).** `pkg_*`, Linux `webfilter`,
  optional `screen_capture`/parental sections on desktop clients gated by runtime desktop
  detection.
- **Phase 4 — Server OS-awareness & distribution.** Typed `os` field, per-OS dashboard
  labels, and a Linux agent distribution path (`distribution.py`/`agent_release.py` build only
  `kenny-agent.exe` today).

Related records: ADR-0002 (Windows target, Rust agent), ADR-0005 (contract-first, no drift),
ADR-0011 (kill switch), ADR-0013 / ADR-0033 (Windows service + bootstrap installer — the
lifecycle this replaces on Linux), ADR-0018 (screenshots via the tray/session-0 helper),
ADR-0020 / ADR-0021 (safety guard + policy catalog — the PowerShell-vs-shell design point),
ADR-0022 (Quick Assist concierge — Windows-only), ADR-0023 (mutual Ed25519 auth — portable),
ADR-0026 (webfilter — adaptable core), ADR-0031 / ADR-0032 (telemetry sections). The wire
contract lives in `docs/protocol.md` + `docs/fixtures/`.

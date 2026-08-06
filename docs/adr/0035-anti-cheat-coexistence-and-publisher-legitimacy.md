# 0035. Anti-cheat coexistence and publisher legitimacy

- Status: accepted
- Date: 2026-07-18

## Context and Problem Statement

A family PC running the kenny agent also runs games with a kernel-level anti-cheat (the
triggering case: ARC Raiders, which uses Easy Anti-Cheat plus behavioural analysis). The
anti-cheat flagged the agent and the parent had to uninstall it.

kenny does **none** of the "hard" cheat behaviours — it never reads another process's
memory, injects a DLL, hooks an API, loads a driver, or simulates input. It is flagged
*heuristically*, because a few of its legitimate actions look cheat-shaped:

- `screen_capture` takes a full-screen GDI grab (`BitBlt … CAPTUREBLT`) of the desktop —
  of a protected game, that looks like a screen-grab cheat.
- The periodic telemetry enumerates every process and every listening port (joined to the
  owning image) on a fixed timer, regardless of what is running.
- The binary ships **unsigned** and with **no PE identity** (no CompanyName/ProductName/
  version resource, no icon), so to AV/anti-cheat it is an anonymous LocalSystem service
  that self-elevates and self-updates.

The question: can we make kenny robust against these false positives without the parent
having to uninstall it whenever a child plays a protected game?

The load-bearing constraint is what we must **not** do. Anti-cheat engines ban on
**stealth and tampering** signals, not on the mere existence of admin software. Any attempt
to hide, rename, obfuscate, or evade would turn a false-positive *risk* into a real,
defensible detection — and could ban the player's game account (EAC + behavioural AI). So
the only safe direction is the opposite of evasion: be **more** identifiable and **less**
active near a protected game.

## Considered Options

- **Evade detection** — randomize/hide the process and service, pack/obfuscate the binary,
  keep capturing quietly during play. Rejected outright: it is exactly the behaviour
  anti-cheat bans, risks the player's account, and is antithetical to a trusted family tool.
- **Do nothing in the agent; rely only on code signing + vendor allowlisting.** Signing
  helps, but a formal anti-cheat allowlist is not realistically available to a self-hosted,
  non-commercial family tool (EAC allowlists mainstream software/AV vendors; false positives
  are handled reactively via ban appeals). Signing alone also does not stop a full-screen
  capture of the game from looking cheat-shaped.
- **Voluntary coexistence + publisher identity (chosen).** While a protected game runs, the
  agent transparently steps back from its most anti-cheat-visible actions, and the binary
  carries a truthful PE identity (with code signing prepared as a follow-up).

## Decision Outcome

Chosen option: **voluntary, transparent coexistence plus honest publisher identity.** The
agent is made more legitimate and less active near a protected game — never stealthier.

- **Coexistence mode (`kenny-agent/src/coexist.rs`).** A background poller reads the process
  **name list only** (the cheapest `sysinfo` refresh, so it opens no rights-bearing handle
  against the game) and flips a process-global "protected game running" flag when a watched
  anti-cheat process is present. The default watchlist is the anti-cheat processes themselves
  (`EasyAntiCheat.exe`, `EasyAntiCheat_EOS.exe`, `BEService*.exe`, `BEServer.exe`), extendable
  via `KENNY_COEXIST_PROCESSES`. While the flag is set:
  - `screen_capture` is hard-suspended and returns the new **`paused`** error code
    (dispatch gate, after the kill-switch and safety-guard checks).
  - The `processes` and `listening_ports` telemetry sections report a shape-compatible
    "paused" section (no enumeration) instead of listing the machine.
  - The telemetry push interval stretches (`KENNY_COEXIST_TELEMETRY_INTERVAL_SECS`,
    default 3600 s) so the enumeration backs off.
  The step-back is **symmetric and transparent**: identical whether or not anyone is
  watching, it works by genuinely not doing the visible thing, and it is reported (the
  `paused` code, the "paused" section summaries, and an info log on each transition). It
  clears automatically when the game exits. On-demand `diag_processes` is deliberately
  **not** hard-blocked — only the steady-state periodic enumeration is relaxed.

- **`paused` error code (contract change, `PROTOCOL_VERSION` 0.11 → 0.12).** Additive to the
  error-code set, no frame or tool-schema change — the same shape as the `0.5` `blocked`
  addition. It lets the dashboard distinguish "kenny stepped back for a game" from the
  operator kill-switch (`disabled`). Golden fixture: `docs/fixtures/response_error_paused.json`.

- **PE identity (`kenny-agent/build.rs`, via `winresource`).** The Windows binary now carries
  a VERSIONINFO resource (CompanyName, ProductName, FileDescription, OriginalFilename,
  LegalCopyright, FileVersion/ProductVersion from the same source as `KENNY_BUILD_VERSION`)
  and the exe icon. `embed-manifest` keeps owning the `asInvoker` manifest (ADR-0030);
  `winresource` emits no manifest, so there is no duplicate resource. The service name
  `kenny-agent` stays visible in Services.msc — identity is added, never removed.

- **Code signing is prepared, not enabled here.** The Authenticode step in `release.yml` is
  already wired (gated on `WINDOWS_CERT_BASE64`, off by default) and the server ships the exe
  unmodified (ADR-0030/0012) so a signature survives distribution. Turning it on needs an
  external certificate (a modern OV/EV cert requires a hardware/cloud signing key). The
  Authenticode subject must match the VERSIONINFO CompanyName from this ADR. Tracked in
  `docs/setup.md`; not implemented in this change.

### Consequences

- Good, because the agent can stay installed on a gamer's PC: it voluntarily and visibly
  stops the actions an anti-cheat flags while a protected game runs, then resumes.
- Good, because the posture is honest — a signed-ready, named, metadata-bearing process that
  steps back transparently is legitimate; masquerading would be a real, bannable detection.
- Good, because it is additive and reversible: `KENNY_COEXIST_ENABLED=0` restores the old
  behaviour, the watchlist is operator-tunable, and the `paused` code is backward-compatible.
- Bad, because detection is heuristic (a watchlist of anti-cheat process names): a title with
  an unlisted anti-cheat needs `KENNY_COEXIST_PROCESSES` extended, and a screenshot taken in
  the ~5 s poll window before the flag flips could still land. Both are conservative failure
  modes (miss a pause, never a false capture-block that matters).
- Bad, because coexistence protects against *heuristic/behavioural* flags, not a definitive
  kernel-driver signature match; that final assurance still needs code signing and, at most,
  a reactive ban-appeal — neither of which this change delivers on its own.

## More Information

- Relates to ADR-0011 (kill-switch `disabled` — the adjacent "online but refusing" code),
  ADR-0018 (`screen_capture` via the tray — the suspended action), ADR-0019/0020 (the
  dispatch gates this sits beside), ADR-0028/0029 (the process/port/screen-time telemetry it
  relaxes), and ADR-0030/0012 (signature-preserving distribution).
- Contract: `docs/protocol.md` (`paused` error code + `PROTOCOL_VERSION` 0.12) and
  `docs/fixtures/response_error_paused.json`. Run `/contract-check`.
- Implementation: `kenny-agent/src/coexist.rs` (new), `dispatch.rs`, `tunnel.rs`,
  `telemetry/scheduler.rs`, `telemetry/collectors/processes.rs`,
  `telemetry/collectors/listening_ports.rs`, `protocol.rs`, `build.rs`, `Cargo.toml`; server
  `kenny_server/protocol.py` (`paused` in the `ErrorCode` literal). Config via
  `KENNY_COEXIST_ENABLED` / `KENNY_COEXIST_PROCESSES` / `KENNY_COEXIST_POLL_SECS` /
  `KENNY_COEXIST_TELEMETRY_INTERVAL_SECS` (env, per ADR-0032).

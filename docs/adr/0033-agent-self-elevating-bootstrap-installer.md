# 0033. Agent self-elevating bootstrap installer + fixed install location

- Status: accepted
- Date: 2026-07-03

## Context and Problem Statement

A non-technical relative onboards their own PC. Today the dashboard hands them a ZIP
(`kenny-agent.exe` + `install.bat` + `README.txt`, ADR-0012); they must **unzip** it, then
**right-click `install.bat` → "Run as Administrator"**. Double-clicking without elevation
fails silently, and because there is no fixed install location the exe and the
enroll-token-bearing `kenny-agent.config.json` land wherever the ZIP was extracted (usually
`Downloads`). This is the sharpest onboarding friction in the whole system.

We want a one-double-click, one-UAC-prompt install **without** giving up the property
ADR-0013 depends on: the single binary owns its whole lifecycle (stop → swap → restart),
which is what server-triggered self-update needs.

## Considered Options

- **Self-elevating bootstrap inside the binary**: a new `setup` subcommand that elevates via
  UAC at runtime, copies the binary into a fixed `%ProgramFiles%\kenny`, and runs `install`
  from there. Per-agent config rides in a `kenny-agent.setup.json` sidecar next to the exe.
- **External installer (Inno/NSIS/MSI/MSIX)** that registers the service.
- **winget** package.

## Decision Outcome

Chosen option: **self-elevating bootstrap inside the binary**, because it removes the
unzip-and-right-click ritual while staying fully compatible with the ADR-0013 self-update
model. MSI/MSIX want to own versioning and would fight the in-place swap; winget points at a
public release and cannot carry the **per-agent** enroll token + server URL + pinned server
key that the bundle exists to deliver. A bootstrap keeps "the binary owns its lifecycle" and
needs no external toolchain in CI.

- **`setup` subcommand** (Windows; a no-op stub elsewhere). It resolves the connection
  config from CLI flags **or** the `kenny-agent.setup.json` sidecar (flags win), then:
  checks token elevation; if not elevated, relaunches itself elevated via `ShellExecuteExW`
  verb `runas` (the UAC prompt) and waits; once elevated, creates `%ProgramFiles%\kenny`,
  copies the running exe there, and invokes that copied binary's existing `install` — so the
  service `executable_path` and `kenny-agent.config.json` resolve to the fixed location, not
  `Downloads`. After a successful install it deletes the source sidecar (it carries the
  one-time enroll token). The agent's Ed25519 key stays in `%ProgramData%\kenny` (ADR-0023),
  untouched by relocation or self-update.
- **Embedded application manifest stays `asInvoker`** (plus long-path + per-monitor-v2 DPI),
  **not** `requireAdministrator`. The same binary launches the tray in the standard-user
  interactive session (ADR-0011/0018); a require-admin manifest would make that launch fail.
  Elevation is therefore requested programmatically, only on the `setup` path.
- **Bundle shape**: the server ships `kenny-agent.exe` + `setup.bat` (one line:
  `kenny-agent.exe setup`) + `kenny-agent.setup.json` + `README.txt`. The base exe is shipped
  **unmodified**, so its optional Authenticode signature (ADR-0012, `release.yml`) stays
  valid; the per-agent secrets ride in the JSON sidecar rather than being baked into the exe
  (which would break signing) or a secret-bearing `.bat`.

### Consequences

- Good, because onboarding is one double-click and one UAC approval; nothing to unzip, no
  "Run as Administrator" instruction, and a fixed, predictable install location.
- Good, because it composes with ADR-0013 self-update unchanged and needs no MSI/MSIX/winget
  toolchain — the binary still owns its lifecycle.
- Good, because a signed base exe + JSON sidecar keeps code-signing meaningful while still
  delivering per-agent config.
- Bad, because UAC elevation, the copy into Program Files, and the service install can only be
  **runtime**-verified on real Windows; they are compile-verified via
  `cargo check --target x86_64-pc-windows-gnu` and exercised by the self-hosted Windows e2e,
  with the off-Windows path returning "only supported on Windows". Config resolution
  (`resolve_setup_config`) is portable and unit-tested on Linux CI.
- Bad, because the enroll token now sits in a plaintext sidecar next to the exe until setup
  runs; it is single-use, TLS-delivered, and deleted after install — the same trust profile
  as the previous `install.bat`, which also carried it in cleartext.

## More Information

- Relates to ADR-0012 (prebuilt binary + config injection; the sidecar is the injection
  channel) and ADR-0013 (Windows service + self-update; `setup` wraps `install`).
- No wire-contract change: frames, tool schemas, and fixtures are untouched.
- Implementation: `kenny-agent/src/setup.rs`, `config.rs`, `main.rs`, `service.rs` (uninstall
  also removes the install dir), `build.rs` (manifest); server bundle in
  `kenny-server/kenny_server/distribution.py`.

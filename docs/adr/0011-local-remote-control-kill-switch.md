# 0011. Local remote-control kill switch (tray on/off)

- Status: accepted
- Date: 2026-06-05

## Context and Problem Statement

kenny lets an operator drive a family PC remotely through Claude/MCP: alongside
read-only telemetry and diagnostics, the agent runs **mutating** capability tools
(`powershell.exec`, `winget.install|uninstall|update`, `net.dns_flush`,
`net.adapter_reset`, `agent.update`). The person physically at that PC had no way to
say "not right now" — once the agent was online, every tool was forwarded.

We want a local switch the endpoint user controls: a tray icon whose menu can disable
and re-enable remote control at any time. Telemetry must keep flowing (the fleet
dashboard stays useful), but anything that *writes* to the device must be refused while
the switch is off. Two wrinkles shape the design: the agent normally runs as a Windows
**service in session 0**, where a tray icon is invisible to the logged-in user; and the
choice must be **persistent** and default to **on**.

## Considered Options

- **Separate tray process + shared control file (chosen).** A `kenny-agent tray`
  subcommand runs in the interactive user session (auto-started at logon) and writes a
  small JSON control file in a shared, cross-session location; the agent/service reads
  it before every mutating tool.
- **In-process tray.** Spawn the tray from the running agent. Simple, but invisible in
  the normal service (session-0) deployment, and couples a GUI event loop to the async
  tunnel runtime.
- **Server-side gate.** Let the operator/server decide what to forward. This is an
  operator control, not an *endpoint-user* control — it does not give the person at the
  PC a kill switch, which is the whole point.
- **Block everything (incl. reads) when off.** Rejected for now: the stated requirement
  is to block writes while keeping telemetry and read-only diagnostics working.

## Decision Outcome

Chosen option: **separate tray process + shared control file**.

- `kenny-agent/src/control.rs` owns the state: a `kenny-agent.control.json` file
  (`{"remote_control_enabled": bool}`), a `KENNY_CONTROL_FILE` override, and the
  authoritative `is_mutating(tool)` classification. Reads **fail safe to on**: a
  missing/unreadable/corrupt file means enabled, so the agent is operable out of the box
  and a transient error never silently widens the block.
- The agent reads the file in `dispatch::run` *before* routing. A mutating tool while
  the switch is off returns the new `error.code = "disabled"`; telemetry pushes,
  `telemetry.collect`, and read-only tools are untouched.
- On Windows the file lives in `%ProgramData%\kenny\`. `install` creates that directory,
  grants Authenticated Users *modify* via `icacls` (so a standard user's tray can flip
  it while the LocalSystem service reads it), and registers the tray at logon via an
  HKLM `...\Run` value. `uninstall` removes the autostart but leaves the file so the
  user's choice survives a reinstall.
- The tray (`kenny-agent/src/tray.rs`, `#[cfg(windows)]`) is a raw Win32
  `Shell_NotifyIcon` icon + popup menu (toggle / quit) with two embedded icon variants
  so the on/off state is legible at a glance. Off Windows it is a no-op stub.
- The wire contract gains `disabled` in the `error.code` set
  (`docs/protocol.md` + `docs/fixtures/response_error_disabled.json`), mirrored by both
  `protocol.rs` (`ErrorCode::Disabled`) and `protocol.py` (`ErrorCode` literal).

### Consequences

- Good: the endpoint user gets a real, persistent kill switch; telemetry/fleet health is
  unaffected; the block is enforced agent-side regardless of what the server forwards.
- Good: works in the production service (session-0) deployment, not just foreground dev
  runs, because state lives in a file rather than in the agent process.
- Good: a clear `disabled` error lets Claude/the dashboard explain *why* a tool was
  refused instead of surfacing a generic failure.
- Bad / trade-offs: cross-process state needs a ProgramData ACL grant at install time;
  the tray is a second process to ship and keep alive; `agent.update` is treated as
  mutating, so a remote self-update is blocked while the switch is off (intended).
- Follow-up (not in this ADR): report the off-state proactively to the server (e.g.
  `register.meta` or a telemetry section) so the dashboard shows it before a tool is even
  attempted.

## More Information

- Contract: `docs/protocol.md` (`response` error codes), `docs/fixtures/response_error_disabled.json`.
- Code: `kenny-agent/src/control.rs`, `kenny-agent/src/tray.rs`, `kenny-agent/src/dispatch.rs`,
  `kenny-agent/src/service.rs`, `kenny-agent/assets/` (icon source + generator).
- Related: [ADR-0004](0004-agent-initiated-outbound-connection.md) (agent dials out),
  [ADR-0005](0005-contract-first-with-golden-fixtures.md) (contract-first).

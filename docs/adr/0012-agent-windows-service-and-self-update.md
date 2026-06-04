# 0012. Agent as a Windows service + server-triggered self-update

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

The agent must run unattended on a relative's PC (survive reboots, restart on failure) and
the operator must be able to push a new agent version from the server without touching the
machine. We had to pick an install mechanism that also enables server-triggered updates.

## Considered Options

- **Self-install via the `windows-service` crate** (agent subcommands manage the service)
  + a self-update tool (`agent.update`).
- External installer (Inno/NSIS/MSI) that registers the service.
- PowerShell `New-Service` script.

## Decision Outcome

Chosen option: **self-install via `windows-service`**, because the single binary then owns
its whole lifecycle, which is exactly what server-triggered self-update needs (stop → swap
→ restart). All three options can register a service, but only the agent controlling its own
SCM lifecycle makes the in-place update clean.

- Subcommands: `install` / `uninstall` / `run-service` (Windows), plus `run` (default — the
  no-subcommand invocation still runs the foreground tunnel, so existing flows are unchanged).
  `install` writes `kenny-agent.config.json` next to the exe, registers an auto-start service
  with restart-on-failure recovery, and starts it.
- **`agent.update {version, url, sha256}`** (wire contract, PROTOCOL_VERSION 0.2): the agent
  downloads the new binary from `url`, verifies SHA-256 **before** any swap, stages it, replies
  `{ok, staged_version}`, then a detached updater helper stops the service, swaps the binary
  (rename-the-running-exe trick, with rollback), and restarts it. The agent reconnects on the
  new version (`register.meta.version`). Off Windows the tool returns `unsupported`.

### Consequences

- Good, because one binary installs, runs as a service, and updates itself; the operator
  triggers updates from the server, no manual reinstall.
- Good, because the update pulls the same prebuilt, hashed (optionally signed) artifact as
  first install (ADR-0011) — verified before swap.
- Bad, because the SCM integration and the live swap/restart can only be runtime-verified on
  real Windows; they are compile-verified via `cargo check --target x86_64-pc-windows-gnu`
  and covered on the `agent-windows` CI job, with the off-Windows path returning `unsupported`.

## More Information

- Server side: registers `agent.update` as a forwarded tool, serves the binary at `url`
  (ADR-0011/WS5), supplies the `sha256`, and triggers by comparing `register.meta.version`.
- Implementation: `kenny-agent/src/service.rs`, `handlers/agent_update.rs`, `config.rs`,
  `tunnel.rs` (`run_until` graceful stop).

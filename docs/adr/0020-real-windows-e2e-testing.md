# 0020. Real Windows tool-call end-to-end testing

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

The `e2e` CI job runs the **real** compiled agent against the real server, but on
`ubuntu-latest`. There the Windows capability tools never run their actual code: the
`#[cfg(windows)]` discipline (ADR-aligned, see `kenny-agent/CLAUDE.md`) means
`powershell_exec` falls back to `sh` and every Windows-only tool (`diag_services`,
`diag_eventlog`, `diag_autostart`, `winget_*`, `net_dns_flush`, `net_adapter_reset`,
`screen_capture`) returns `unsupported`. So the genuine CIM/WMI, PowerShell, winget
and ipconfig paths — the ones that matter on a family PC — were exercised only by
Rust unit tests, never end-to-end over the real wire protocol.

We want real tool calls against a real Windows agent in CI. The server is
platform-neutral (FastMCP/uvicorn), so server **and** agent can run on the same
Windows runner and reuse the existing `test_integration_e2e.py`.

## Considered Options

- **A. Hosted Windows runners only** (`windows-2022` + `windows-2025`). Free,
  ephemeral, run on every relevant PR. Real PowerShell/CIM/winget, but headless: no
  interactive desktop, single network path, no persistence across reboot.
- **B. Self-hosted runner on a real family PC.** Highest fidelity (interactive
  desktop, persistent service, real winget), but requires maintaining a runner on a
  household machine, with the security and availability cost of running CI there.
- **C. Hybrid:** hosted matrix in regular CI for everything hosted can do honestly,
  plus a manual, opt-in self-hosted workflow for the tools hosted runners
  structurally cannot cover.

A second hosted image only closes gaps that differ *by image* — preinstalled winget
(reliable on `windows-2025`) and OS version. The gaps shared by **all** hosted
runners — no interactive desktop (`screen_capture`, session-0/tray IPC),
self-severing network (`net_adapter_reset`), and no persistence (a real service
`agent_update` across reboot) — are not closed by adding another hosted runner.

## Decision Outcome

Chosen option: "C". A `windows-2022` + `windows-2025` matrix `e2e-windows` job runs
the real integration test on every relevant change; on Windows the test additionally
drives the real `#[cfg(windows)]` paths (`diag_processes`, `diag_services`,
`diag_eventlog`, `net_config`, `net_dns_flush`, and `winget_list` with a runtime
skip where winget is absent). The interactive/destructive tools are documented and
left to a manual, opt-in `e2e-windows-selfhosted.yml` (`workflow_dispatch`,
`runs-on: [self-hosted, windows]`, `KENNY_E2E_FULL=1`) that stays inert until a real
runner is registered. Path-filtering (the `changes` job) keeps these jobs from
running on unrelated changes.

### Consequences

- Good, because the real Windows tool surface is now covered end-to-end on every
  relevant PR, at no extra infrastructure cost, with no wire-contract change.
- Good, because the honest limits of hosted runners are explicit: `screen_capture`,
  `net_adapter_reset` and a real-service `agent_update` are not faked — they wait for
  a self-hosted runner.
- Bad, because the hosted matrix does not match a Windows 10/11 **client** SKU
  (runners are Server) and a clean VM is not representative of a real family PC; the
  self-hosted path is the eventual answer for representativeness.
- Neutral, because `winget` coverage depends on the runner image; the test skips
  cleanly rather than failing where winget is unavailable.

## More Information

Implemented in `.github/workflows/ci.yml` (`e2e-windows`),
`.github/workflows/e2e-windows-selfhosted.yml`, and
`kenny-server/tests/test_integration_e2e.py` (`_assert_windows_tools`). Related:
ADR-0017 (screenshots captured in the user session via the tray) explains why
`screen_capture` needs an interactive desktop and thus a self-hosted runner.

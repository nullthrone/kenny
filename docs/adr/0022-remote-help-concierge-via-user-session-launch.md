# 0022. Orchestrate Windows Quick Assist as a concierge via user-session launch

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

A recurring family-support need is *interactive* remote help: someone takes over the
screen and shows the person at the PC what to do. kenny already exposes `screen_capture`
(read-only, one frame at a time) but nothing that lets a human helper actually drive the
desktop. We want kenny to make a real remote-help session easy to set up, monitor, and
tear down — across **mixed** reachability (home PCs both on a LAN/VPN and behind
NAT/CGNAT across the internet) — without kenny itself becoming a remote-control transport.

Two constraints shape the solution:

1. **Reachability.** A LAN-only mechanism (RDP, RDP-shadow, classic Remote Assistance
   `msra.exe`) cannot reach a PC behind NAT without a tunnel/VPN that kenny does not
   provide. Windows **Quick Assist** brings its own Microsoft relay, NAT traversal, and
   end-to-end encryption, so it works for both cases with **no inbound firewall holes**.
2. **Session 0.** The agent runs as a LocalSystem service in **session 0** with no visible
   desktop ([ADR-0013](0013-agent-windows-service-and-self-update.md)). It therefore
   cannot launch a GUI app like Quick Assist where the user can see it — the same wall the
   screenshot path hit ([ADR-0018](0018-screenshots-captured-in-user-session-via-tray.md)).

The Quick Assist helper side requires a Microsoft-account sign-in and a 6-digit security
code, and the person at the PC must click **Allow**. None of that can — or, in a family
consent setting, *should* — be automated. So kenny's role is a **concierge**: prepare,
launch, verify, and tear down a session around a transport it does not own.

## Considered Options

- **Quick Assist concierge, launched in the user session via the tray (chosen).** kenny
  reports readiness, opens Quick Assist on the interactive desktop, and can stop it. The
  pixels and input ride Quick Assist's own relay.
- **kenny becomes the transport (screen-stream + input injection over its own tunnel).**
  Effectively building a VNC. Large, security-heavy, and duplicates what Quick Assist does
  well; rejected for scope and threat-surface reasons.
- **RDP / RDP-shadow orchestration.** Real over-the-shoulder control, but LAN/VPN only —
  fails the mixed-reachability requirement. Left as a possible later allow-list addition.
- **Classic Remote Assistance (`msra.exe`, Easy Connect / invitation files).** Legacy,
  deprecated, NAT-hostile across the internet, and UI-prompt heavy; little gain over Quick
  Assist.
- **For the session-0 launch: `CreateProcessAsUser` from the service**
  (`WTSGetActiveConsoleSessionId` → `WTSQueryUserToken` → spawn on `winsta0\default`).
  Works, but adds privileged token plumbing and duplicates the user-session presence the
  tray already provides — the same trade-off [ADR-0018](0018-screenshots-captured-in-user-session-via-tray.md)
  weighed for screenshots.

## Decision Outcome

Chosen: **Quick Assist as the transport, kenny as the concierge, with the interactive
launch delegated to the tray helper over an allow-listed named pipe.**

- **Three capability tools** (contract: `docs/protocol.md` + `docs/fixtures/`,
  `PROTOCOL_VERSION` bumped `0.6 → 0.7`):
  - `remotehelp_status` — **read-only** (not gated by the kill switch, like
    `screen_capture`): `{installed, version, internet_ok, interactive_session}`.
  - `remotehelp_start` — **mutating**: opens Quick Assist on the user desktop, returns
    `{launched, pid, note}`; the `note` states the human-in-the-loop steps.
  - `remotehelp_stop` — **mutating**: terminates Quick Assist, returns `{stopped}`.
  Installing Quick Assist when missing reuses the existing `winget_install` tool (Store id
  `9P7BP5VNWKX5`) — no new install tool.
- **User-session launch reuses the ADR-0018 pattern.** A second named pipe
  (`\\.\pipe\kenny-agent-session-launch`, **duplex**) is hosted by the tray helper; the
  session-0 service writes a launch request and reads the reply. Framing is shared with
  the screencap pipe via `kenny-agent/src/ipc.rs`. The tray spawns the app with a plain
  process spawn — it already runs in the interactive session, so no token impersonation is
  needed.
- **The allow-list is the trust boundary.** The tray launches *only*
  `session_launch_ipc::ALLOWED_EXECUTABLES` (initially `quickassist.exe`), enforced on both
  the tray and the service side. The session-0 service can never have the tray start an
  arbitrary program; widening the mechanism means adding a reviewed entry (e.g. `mstsc.exe`
  for a future LAN/VPN path), not changing the plumbing.
- **Kill-switch gating.** `remotehelp_start`/`_stop` are classified mutating in
  `control::is_mutating`, so the endpoint kill switch ([ADR-0011](0011-local-remote-control-kill-switch.md))
  refuses them when remote control is off; `remotehelp_status` keeps working (read-only).
- **Portability.** Off Windows, `start`/`stop` return `error.code = "unsupported"` and
  `status` reports everything not-available, keeping `cargo test`/`cargo build` green on
  Linux CI (the `#[cfg(windows)]` discipline).

### Consequences

- Good: a usable remote-help workflow that works behind NAT and on a LAN alike, with no
  firewall changes and no new transport for kenny to secure. The human consent steps
  (helper sign-in, code, Allow) remain with the people — appropriate for a family setting.
- Good: the launch primitive is generic but **bounded** by the allow-list, and reuses the
  already-load-bearing tray + a proven IPC shape.
- Bad / trade-offs: the tray is now load-bearing for one more capability — if it is not
  running (nobody logged in, or it crashed), `start` returns a clear "no interactive
  session / tray not available" error, and `interactive_session` reports `false`. As with
  ADR-0018, a less-privileged local process could pre-create the well-known pipe name
  (pipe squatting); acceptable for the family-PC threat model, and the allow-list means the
  worst a squatter can do is *decline* or *fake* a Quick Assist launch, not run something
  else. The shared-catalog safety guard ([ADR-0020](0020-agent-side-deterministic-tool-guard.md))
  does not need new rules: `remotehelp_*` carry no operator-supplied script, and the
  launch surface is the allow-list, not a regex blocklist.
- Follow-up (not in this ADR): a `remotehelp` telemetry section + dashboard panel for
  passive per-PC readiness; verifying the launch pipe server's owning SID on connect;
  extending the allow-list to `mstsc /shadow` for LAN/VPN control.

## More Information

- Contract: `docs/protocol.md` § Tool catalog + § Versioning; fixtures
  `docs/fixtures/request|response_remotehelp_{status,start,stop}.json`.
- Code: `kenny-agent/src/handlers/remotehelp.rs`, `kenny-agent/src/session_launch_ipc.rs`,
  `kenny-agent/src/ipc.rs`, `kenny-agent/src/tray.rs`, `kenny-agent/src/control.rs`,
  `kenny-agent/src/dispatch.rs`; server `kenny-server/kenny_server/tools.py`.
- Related: [ADR-0011](0011-local-remote-control-kill-switch.md) (kill switch + tray),
  [ADR-0013](0013-agent-windows-service-and-self-update.md) (session-0 service),
  [ADR-0018](0018-screenshots-captured-in-user-session-via-tray.md) (tray-hosted IPC),
  [ADR-0020](0020-agent-side-deterministic-tool-guard.md) (safety guard).

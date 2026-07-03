# 0018. Screenshots captured in the user session via the tray helper

- Status: accepted
- Date: 2026-06-05

## Context and Problem Statement

The `screen_capture` tool returned a black image in the normal deployment. The cause
is Windows **session 0 isolation**: the agent runs as a LocalSystem service in session 0
(see [ADR-0013](0013-agent-windows-service-and-self-update.md)), which has no visible
desktop. A GDI `BitBlt` only sees the desktop of the *calling* session, so a grab from
the service produces a black frame — regardless of how correct the capture code is.

[ADR-0004](0004-agent-initiated-outbound-connection.md) assumed "the agent runs in the
user's own session — screenshots and input work without a service." In practice the
production agent is a session-0 service, so that assumption does not hold for screen
capture. We need a way to grab the *interactive* desktop and hand the result back to the
session-0 process that owns the tunnel.

A component already lives in the interactive session: the **tray helper**
(`kenny-agent tray`, [ADR-0011](0011-local-remote-control-kill-switch.md)), auto-started
at logon. Until now it only flipped the kill-switch control file. A second problem
surfaced alongside this: the tray icon did not appear right after `install`, because
`install` only registered the HKLM `...\Run` autostart, which fires at the *next* logon
rather than immediately.

## Considered Options

- **Capture in the tray, deliver over a local named pipe (chosen).** The tray (user
  session) hosts a capture responder; the service (session 0) connects as a client and
  fetches the PNG. Reuses the one process already in the right session.
- **Service spawns a capture worker in the active session.** `WTSGetActiveConsoleSessionId`
  → `WTSQueryUserToken` → `CreateProcessAsUser` to launch a short-lived grabber on
  `winsta0\default`. Works, but adds privileged token plumbing and a process spawn per
  capture, duplicating the user-session presence the tray already provides.
- **Run the agent in the user session instead of as a service.** Matches ADR-0004's
  assumption but loses the always-on, pre-logon, auto-restart properties the service was
  chosen for ([ADR-0013](0013-agent-windows-service-and-self-update.md)).

## Decision Outcome

Chosen option: **capture in the tray, deliver over a local named pipe.**

- The GDI grab is factored into `handlers::screenshot::grab_primary_png()` (raw PNG
  bytes), reused by both an interactive foreground run and the tray responder.
  `handlers::screenshot::capture()` routes by session: in **session 0**
  (`ProcessIdToSessionId` == 0) it delegates to the tray; otherwise it grabs directly.
- `kenny-agent/src/screencap_ipc.rs` owns the IPC. Framing is one length-prefixed blob
  (`u32` LE length + PNG); the framing helpers are platform-neutral and unit-tested on
  Linux. On Windows the tray runs `serve()` on a background thread
  (`CreateNamedPipeW(\\.\pipe\kenny-agent-screencap, PIPE_ACCESS_OUTBOUND)` →
  `ConnectNamedPipe` → grab → write frame), and the service runs `capture_via_tray()`
  (`CreateFileW` with a short retry, read frame, base64) with a 5 s deadline.
- The pipe uses the **default security descriptor**: a pipe created by the interactive
  user is connectable by LocalSystem, so the service (SYSTEM) reaches the tray's pipe
  without custom ACLs.
- **Tray visibility fix.** `install` now also starts the tray immediately in the
  installing user's session (in addition to the logon autostart), and the tray takes a
  per-session named-mutex single-instance guard (`Local\kenny-agent-tray`) so the
  immediate start and the autostart cannot stack two icons. The tray calls `FreeConsole`
  at startup so the console-subsystem binary does not flash a console window; `run`/CLI
  keep their console.
- **Kill switch unchanged.** `screen_capture` stays read-only and is *not* gated by the
  endpoint kill switch (`control::is_mutating`), matching prior behavior.
- The **wire contract is unchanged**: the service still returns
  `{image_b64, format:"png"}`; the server/dashboard are unaffected by where the bytes are
  grabbed.

### Consequences

- Good: `screen_capture` returns the real interactive desktop in the production
  session-0 service deployment; foreground/dev runs still grab directly.
- Good: the tray appears immediately after `install`, and the tray is now the single
  user-session host for both the kill switch and screen capture.
- Bad / trade-offs: the tray is now **load-bearing** for a capability, not just the kill
  switch — if it is not running (nobody logged in, or it crashed), `screen_capture`
  returns a clear "no interactive session / tray not available" error instead of a black
  frame. A local, less-privileged process could pre-create the well-known pipe name and
  feed the service a spoofed screenshot (pipe squatting); acceptable for the family-PC
  threat model, and could later be hardened by verifying the server endpoint's owner.
- Follow-up (not in this ADR): multi-monitor / virtual-screen capture.

## Update (2026-07-03): reliability + least-privilege hardening

Two of the trade-offs above were addressed once the tool proved flaky in practice; the
decision and wire contract are unchanged, only the tray/service coupling is tightened.

- **Load-bearing tray now self-heals.** The tray was only (re)launched at service start
  and at logon, so a tray the user closed or that crashed left every `screen_capture`
  failing until the next service restart. The capture path now relaunches it on demand:
  when the pipe is absent, the session-0 service calls its existing
  `launch_tray_in_active_session()` once and retries, using the privilege it already holds
  (no new privilege). The two failure paths are kept distinct — "nobody logged in" (no
  console session / user token) vs. "tray relaunched but its pipe is still unavailable"
  (a tray crashing on start) — so the field can tell them apart. The actual capture still
  runs at the tray's standard-user, medium-integrity level: that is the least privilege
  that does the job.
- **Pipe squatting mitigated.** The pipe is created with an explicit DACL (SDDL
  `D:P(A;;GA;;;SY)(A;;GA;;;<user>)`) admitting only LocalSystem and the interactive user,
  replacing the loose default descriptor; if the descriptor cannot be built the tray falls
  back to the default so capture still works. On connect the service verifies the pipe
  server's owning SID equals the active console user's (`GetNamedPipeServerProcessId` →
  token user SID vs. `WTSQueryUserToken`), and refuses a screenshot from any other
  principal. This closes the follow-up "verifying the server endpoint's owner"; a squatter
  in the *same* user session remains out of scope (same trust level as the tray).

## More Information

- Code: `kenny-agent/src/screencap_ipc.rs`, `kenny-agent/src/handlers/screenshot.rs`,
  `kenny-agent/src/tray.rs`, `kenny-agent/src/service.rs`, `kenny-agent/src/main.rs`.
- Related: [ADR-0004](0004-agent-initiated-outbound-connection.md) (session assumption),
  [ADR-0011](0011-local-remote-control-kill-switch.md) (tray + kill switch),
  [ADR-0013](0013-agent-windows-service-and-self-update.md) (session-0 service).

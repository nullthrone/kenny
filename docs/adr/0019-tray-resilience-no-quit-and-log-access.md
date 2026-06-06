# 0019. Tray resilience: service-relaunch, no user "quit", and log access

- Status: accepted
- Date: 2026-06-06

## Context and Problem Statement

The tray helper (`kenny-agent tray`, [ADR-0010](0010-local-remote-control-kill-switch.md))
is now **load-bearing**: besides flipping the remote-control kill switch it also hosts the
screen-capture responder for the session-0 service ([ADR-0017](0017-screenshots-captured-in-user-session-via-tray.md)).
Yet its context menu still offered **"Beenden"**, and the only ways it ever started were
`install` (once, in the installing session) and the HKLM `...\Run` autostart (next logon).

So once the tray was gone — the user picked *Beenden*, killed it from Task Manager, or it
crashed — there was **no user-friendly way to bring it back** before the next logon, and
in the meantime `screen_capture` and the kill switch were silently degraded. There was
also no convenient way for the person at the PC to see what the agent had been doing
locally; the rolling logs sit in `%ProgramData%\kenny\logs` where a non-technical family
member would not think to look.

## Considered Options

- **Relaunch from the service + drop "quit" + add a log menu item (chosen).** The
  session-0 service relaunches the tray into the active console session on every start, so
  a service restart is the recovery path. The menu loses *Beenden* (closing a load-bearing
  helper is a foot-gun) and gains a read-only "Protokoll anzeigen" that opens the newest
  log in the default editor.
- **Tray watchdog that respawns itself.** A separate watcher process to keep the tray
  alive. Adds yet another always-on process and another thing to install/monitor, to guard
  a helper that the service can already relaunch.
- **Keep "quit" but re-add an autostart on demand.** Leaves the load-bearing process
  user-closable and still offers no recovery within the same session.
- **Open the log directory in Explorer instead of the file.** Simpler, but a folder of
  date-suffixed files is less obviously "the log" than opening the newest one in an editor.

## Decision Outcome

Chosen option: **service-relaunch + no quit + log access**.

- **Service relaunch (`kenny-agent/src/service.rs`).** `run_service_inner` calls
  `launch_tray_in_active_session()` right after reporting *Running*. It resolves the active
  console session (`WTSGetActiveConsoleSessionId`), takes that user's token
  (`WTSQueryUserToken`, available to LocalSystem via `SE_TCB`), and `CreateProcessAsUserW`
  launches `kenny-agent tray` on `winsta0\default` with `CREATE_NO_WINDOW`. The tray's
  single-instance mutex (`Local\kenny-agent-tray`) makes this a no-op when one is already
  running, so it is safe to call unconditionally on every start/restart. Before anyone is
  logged in there is no token — a normal, non-fatal outcome that the logon autostart still
  covers, so the failure is only logged. This is the same `WTSQueryUserToken` →
  `CreateProcessAsUser` mechanism ADR-0017 had *rejected for screen capture* (one spawn per
  capture); used here once per service start it is the right tool.
- **No "quit" (`kenny-agent/src/tray.rs`).** The `Beenden` menu item and its
  `WM_COMMAND`/`DestroyWindow` handling are removed. The tray still exits cleanly on
  `WM_DESTROY` at logoff/shutdown. There is no longer a user-facing way to stop a
  load-bearing helper; an operator who really wants it gone stops/uninstalls the service.
- **Log access.** A read-only **"Protokoll anzeigen"** menu item opens the newest
  `kenny-agent.log*` file (the daily appender suffixes a date) via `ShellExecute "open"`,
  which lands in Notepad on a stock Windows. It falls back to opening the log directory if
  no file has rolled yet. The newest-file selection (`newest_log_file`) is platform-neutral
  and unit-tested on Linux; `log_dir()`/the log prefix are shared from `main.rs`.

### Consequences

- Good: the tray self-heals — a service restart (manual, SCM failure recovery per
  ADR-0012, or reboot) brings it back, so the kill switch and `screen_capture` recover
  without a re-`install` or waiting for the next logon.
- Good: a load-bearing process can no longer be closed by a stray menu click.
- Good: the person at the PC has a one-click view of what kenny is doing locally.
- Bad / trade-offs: the service now carries privileged token plumbing
  (`CreateProcessAsUserW`) it previously avoided; the launched tray inherits the service's
  (LocalSystem) environment, which is fine because the only paths it needs resolve from the
  machine-wide `%ProgramData%`. A user who wants the icon gone for a session can no longer
  remove it themselves.

## More Information

- Code: `kenny-agent/src/service.rs` (`launch_tray_in_active_session`),
  `kenny-agent/src/tray.rs` (menu + `newest_log_file` + `open_logs`),
  `kenny-agent/src/main.rs` (`log_dir`/`LOG_FILE_PREFIX` shared).
- Related: [ADR-0010](0010-local-remote-control-kill-switch.md) (tray + kill switch),
  [ADR-0017](0017-screenshots-captured-in-user-session-via-tray.md) (tray hosts screen
  capture; rejected the per-capture `CreateProcessAsUser`),
  [ADR-0012](0012-agent-windows-service-and-self-update.md) (session-0 service +
  restart-on-failure recovery).

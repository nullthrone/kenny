# Golden fixtures

Canonical on-the-wire frames. **Both** `kenny-server` (Python) and `kenny-agent`
(Rust) load these exact files and assert round-trip (parse → serialize → equal value).
This is the executable form of `docs/protocol.md` and the technical basis for building
the two components in parallel without drift.

Adding/altering a fixture is a contract change — see `docs/protocol.md` § Versioning.

| file                            | frame / payload                                  |
|---------------------------------|--------------------------------------------------|
| `register.json`                 | `register` frame                                 |
| `request_powershell_exec.json`  | `request` frame (`powershell_exec`)              |
| `response_powershell_exec.json` | successful `response` frame                      |
| `request_shell_exec.json`       | `request` frame (`shell_exec`)                   |
| `response_shell_exec.json`      | successful `response` frame (`shell_exec`)       |
| `request_winget_list.json`      | `request` frame (`winget_list`)                  |
| `response_winget_list.json`     | successful `response` frame (`winget_list`)      |
| `request_remotehelp_status.json`  | `request` frame (`remotehelp_status`)          |
| `response_remotehelp_status.json` | successful `response` frame (`remotehelp_status`) |
| `request_remotehelp_start.json`   | `request` frame (`remotehelp_start`)           |
| `response_remotehelp_start.json`  | successful `response` frame (`remotehelp_start`) |
| `request_remotehelp_stop.json`    | `request` frame (`remotehelp_stop`)            |
| `response_remotehelp_stop.json`   | successful `response` frame (`remotehelp_stop`) |
| `response_error_timeout.json`   | error `response` frame                           |
| `response_error_blocked.json`   | error `response` frame (safety-guard `blocked`)  |
| `response_error_disabled.json`  | error `response` frame (kill-switch `disabled`)  |
| `response_error_paused.json`    | error `response` frame (anti-cheat coexistence `paused`) |
| `ping.json` / `pong.json`       | heartbeat frames                                 |
| `telemetry_snapshot.json`       | `telemetry` frame with a representative snapshot |
| `telemetry_snapshot_linux.json` | `telemetry` frame from a Linux agent (partial snapshot: the account-governance sections plus two `n/a` stubs) |
| `request_agent_update.json`     | `request` frame (`agent_update`)                 |
| `response_agent_update.json`    | successful `response` frame (`agent_update`)     |
| `policy.json`                   | `policy` frame (operator append-only deny rules) |
| `log.json`                      | `log` frame (forwarded agent log event)          |
| `request_webfilter_status.json`  | `request` frame (`webfilter_status`)            |
| `response_webfilter_status.json` | successful `response` frame (`webfilter_status`) |
| `request_webfilter_apply.json`   | `request` frame (`webfilter_apply`)             |
| `response_webfilter_apply.json`  | successful `response` frame (`webfilter_apply`) |
| `request_webfilter_clear.json`   | `request` frame (`webfilter_clear`)             |
| `response_webfilter_clear.json`  | successful `response` frame (`webfilter_clear`) |
| `request_account_set_enabled.json`  | `request` frame (`account_set_enabled`)      |
| `response_account_set_enabled.json` | successful `response` frame (`account_set_enabled`) |
| `request_account_set_admin.json`    | `request` frame (`account_set_admin`)        |
| `response_account_set_admin.json`   | successful `response` frame (`account_set_admin`) |
| `request_account_set_logon_rights.json`  | `request` frame (`account_set_logon_rights`) |
| `response_account_set_logon_rights.json` | successful `response` frame (`account_set_logon_rights`) |
| `request_account_create.json`    | `request` frame (`account_create`)              |
| `response_account_create.json`   | successful `response` frame (`account_create`)  |
| `request_account_delete.json`    | `request` frame (`account_delete`)              |
| `response_account_delete.json`   | successful `response` frame (`account_delete`)  |
| `request_account_session_action.json`  | `request` frame (`account_session_action`) |
| `response_account_session_action.json` | successful `response` frame (`account_session_action`) |
| `request_password_policy_set.json`  | `request` frame (`password_policy_set`)      |
| `response_password_policy_set.json` | successful `response` frame (`password_policy_set`) |

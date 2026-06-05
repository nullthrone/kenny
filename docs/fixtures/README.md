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
| `response_error_timeout.json`   | error `response` frame                           |
| `ping.json` / `pong.json`       | heartbeat frames                                 |
| `telemetry_snapshot.json`       | `telemetry` frame with a representative snapshot |
| `request_agent_update.json`     | `request` frame (`agent_update`)                 |
| `response_agent_update.json`    | successful `response` frame (`agent_update`)     |
| `log.json`                      | `log` frame (forwarded agent log event)          |

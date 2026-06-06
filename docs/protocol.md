# kenny Wire Protocol (v0.1)

> **Single source of truth.** This document and the JSON files in `docs/fixtures/`
> define the contract between `kenny-server` (Python) and `kenny-agent` (Rust).
> Both sides validate against the same fixtures. **Do not** copy schemas into
> `CLAUDE.md` — link here instead. Changes to this contract are a synchronization
> point: bump the version, update fixtures, then update both implementations.

## Transport

- `kenny-agent` opens an **outbound** WebSocket (WSS in production) to `kenny-server`
  at `/agent/ws`. The agent never listens for inbound connections.
- All frames are UTF-8 JSON objects, one frame per WebSocket text message.
- Claude talks to `kenny-server` over MCP (Streamable HTTP). That MCP layer is
  separate from this agent⇄server wire protocol; MCP tool calls are translated by
  the server into `request` frames on the tunnel.
- Authentication is asymmetric and out of scope of this wire protocol except for the
  agent's `register.token`: agents authenticate to the server with a per-agent token
  (below); the **operator** authenticates to the server (MCP endpoint + web UI) with a
  separate operator token (see ADR-0008). The server authenticates to the agent via TLS
  (the agent dials a known `wss://` URL).

## Frame envelope

Every frame has a `type` field. Known types:

| type        | direction       | shape (see below)                         |
|-------------|-----------------|-------------------------------------------|
| `register`  | agent → server  | identifies the agent right after connect  |
| `request`   | server → agent  | invoke one capability tool                |
| `response`  | agent → server  | result/error for a `request` (by `id`)    |
| `telemetry` | agent → server  | periodic pushed snapshot (no request)     |
| `log`       | agent → server  | a forwarded structured log event          |
| `ping`      | both            | heartbeat                                 |
| `pong`      | both            | heartbeat reply                           |

### `register` (agent → server)

```json
{
  "type": "register",
  "agent_id": "papa-pc",
  "token": "<api-key>",
  "meta": { "hostname": "PAPA-PC", "os": "windows", "version": "0.1.0" }
}
```

`os` ∈ {`windows`, `linux`, `macos`}. The server authenticates `token` against its
per-agent key store and registers the connection under `agent_id`. On failure the
server closes the socket with a non-1000 code.

### `request` (server → agent)

```json
{
  "type": "request",
  "id": "9f1c0e2a-...",
  "tool": "powershell_exec",
  "args": { "script": "Get-Process | Select -First 5", "timeout_s": 30 }
}
```

`id` is a server-generated UUID. `tool` is one of the names in the tool catalog
below. `args` matches the per-tool schema.

### `response` (agent → server)

Success:

```json
{ "type": "response", "id": "9f1c0e2a-...", "ok": true,
  "result": { "stdout": "...", "stderr": "", "exit_code": 0 } }
```

Error:

```json
{ "type": "response", "id": "9f1c0e2a-...", "ok": false,
  "error": { "code": "timeout", "message": "tool exceeded 30s" } }
```

`error.code` ∈ {`timeout`, `not_found`, `exec_failed`, `unsupported`, `bad_args`,
`internal`, `disabled`, `blocked`}. `unsupported` is returned by an agent that lacks the
capability on its platform (e.g. `winget_list` on a Linux dev build). `disabled` is
returned when the agent is online but the person at the endpoint has switched remote
control **off** locally (via the agent's tray menu): the agent then refuses every
**mutating** tool (`powershell_exec`, `winget_install|uninstall|update`,
`net_dns_flush`, `net_adapter_reset`, `agent_update`) while telemetry and read-only
diagnostics keep working. Remote control is **on** by default and the choice persists
across restarts. See ADR-0011.

`blocked` is returned by the agent's **deterministic, always-on safety guard**: a
compiled-in policy that refuses individually dangerous calls (e.g. a `powershell_exec`
script that deletes volume shadow copies, clears event logs, or disables Defender; an
`fs_read` of the SAM hive; an `agent_update` from a non-allowlisted host) regardless of
operator approval or kill-switch state. Unlike `disabled`, the guard cannot be turned off
remotely and is **not** a substitute for the operator confirm-gate (ADR-0009) or the
local kill-switch (ADR-0011); it is a last-line, defense-in-depth refusal sitting below
them. The `message` names the matched rule. See ADR-0020.

The guard's built-in rules ship as a **shared deny-rule catalog** (`docs/policy/deny_rules.json`):
the agent embeds it at build time and the server loads the same file for an optional
best-effort **mirror** that can refuse a call before forwarding (earlier feedback). The
agent remains the authoritative enforcement point. Operators may add — but never remove —
deny rules on top of the built-ins; those extra rules are delivered to the agent via the
`policy` frame below. See ADR-0021.

### `policy` (server → agent)

After a successful `register` (and again whenever the operator changes the list), the
server pushes the operator's **append-only** extra deny rules to the agent. These are
*additive* to the agent's compiled-in built-ins (which can never be weakened or removed by
this frame). An empty `rules` array clears the operator additions but leaves the built-ins
intact.

```json
{
  "type": "policy",
  "rules": [
    { "id": "op_block_choco", "applies_to": "powershell",
      "pattern": "(?i)\\bchoco\\b", "reason": "operator: block chocolatey" }
  ]
}
```

Each rule has `id` (stable identifier), `applies_to` ∈ {`powershell`, `self_protection`,
`path`}, a `pattern` (regex in the portable subset common to Rust `regex` and Python `re` —
no backreferences/lookaround), and a human-readable `reason`. The agent recompiles its rule
set on each `policy` frame; a rule whose pattern fails to compile is skipped (logged), never
fatal. The same `{id, applies_to, pattern, reason}` shape is used by the shared catalog.

### `telemetry` (agent → server, pushed)

The agent pushes a snapshot on a timer (default every 900 s; the server may send
the interval in a future `register` ack — not in v0.1). A snapshot is a map of
**section name → section payload**. Every section payload carries `status` and
`summary` plus section-specific fields, so the server can aggregate fleet health
without domain logic.

```json
{
  "type": "telemetry",
  "agent_id": "papa-pc",
  "collected_at": "2026-06-04T18:00:00Z",
  "snapshot": {
    "disk": {
      "status": "warn",
      "summary": "C: 91% full",
      "volumes": [
        { "mount": "C:", "total_bytes": 511000000000, "free_bytes": 46000000000, "percent_used": 91 }
      ],
      "top_dirs": [
        { "path": "C:\\Users\\papa\\Videos", "bytes": 120000000000 }
      ]
    },
    "defender": {
      "status": "crit",
      "summary": "Real-time protection OFF",
      "enabled": false,
      "realtime_protection": false,
      "last_scan": "2026-05-01T03:00:00Z",
      "last_scan_type": "quick",
      "last_signature_update": "2026-05-20T06:00:00Z",
      "threats_found": 0,
      "action_needed": true
    }
  }
}
```

A `telemetry_collect` **request** (see tool catalog) returns the *same* snapshot
shape inside `response.result`, optionally restricted to `args.sections`.

### `log` (agent → server, pushed)

The agent forwards its own structured log events (from `tracing`) to the server so
operator-visible events survive when the agent runs as a Windows service and its
stderr is discarded. The agent emits one frame per event for events at or above a
configurable level (`KENNY_LOG_FORWARD_LEVEL`, default `info`); the agent still
writes a fuller record to a local rotating file. Forwarding is best-effort: while the
agent is disconnected, events accumulate in a bounded buffer and the oldest are
dropped under pressure — `log` frames are never retried like a `request`.

```json
{
  "type": "log",
  "agent_id": "papa-pc",
  "at": "2026-06-04T18:00:01Z",
  "level": "warn",
  "target": "kenny_agent::tunnel",
  "message": "tunnel error; backing off",
  "fields": { "error": "connection reset", "backoff_secs": 4 }
}
```

`level` ∈ {`error`, `warn`, `info`, `debug`, `trace`}. `at` is an RFC 3339 / ISO 8601
timestamp. `target` is the emitting module path. `fields` is an optional object of
structured key/values captured from the event (absent when the event has none). The
server persists these alongside its own log records and the tool-call audit (see
ADR-0017); they are never forwarded to an agent.

### `ping` / `pong`

```json
{ "type": "ping" }
{ "type": "pong" }
```

Either side may send `ping`; the peer replies `pong`. The server marks an agent
offline if no frame (any type) arrives within 3 missed intervals.

## Tool catalog

The server exposes each tool as an MCP tool (after `select_agent`); the agent
implements a handler with the same name. Argument keys are exact.

| tool                 | args                          | result (sketch)                              |
|----------------------|-------------------------------|----------------------------------------------|
| `powershell_exec`    | `{script, timeout_s}`         | `{stdout, stderr, exit_code}`                |
| `fs_list`            | `{path}`                      | `{entries:[{name,is_dir,bytes}]}`            |
| `fs_search`          | `{root, pattern}`             | `{matches:[path]}`                           |
| `fs_read`            | `{path}`                      | `{content, truncated}`                       |
| `fs_disk_usage`      | `{}`                          | `{volumes:[...]}`                            |
| `winget_list`        | `{}`                          | `{packages:[{id,name,version,available}]}`   |
| `winget_install`     | `{id}`                        | `{ok, log}`                                  |
| `winget_uninstall`   | `{id}`                        | `{ok, log}`                                  |
| `winget_update`      | `{id?}`                       | `{ok, log}`                                  |
| `diag_processes`     | `{}`                          | `{processes:[{pid,name,cpu,mem_bytes}]}`     |
| `diag_services`      | `{filter?}`                   | `{services:[{name,display,status,start}]}`   |
| `diag_eventlog`      | `{log, count}`                | `{events:[{time,level,source,message}]}`     |
| `diag_autostart`     | `{}`                          | `{entries:[{name,command,location}]}`        |
| `net_config`         | `{}`                          | `{interfaces:[...], dns:[...]}`              |
| `net_dns_flush`      | `{}`                          | `{ok}`                                       |
| `net_adapter_reset`  | `{name}`                      | `{ok}`                                       |
| `screen_capture`     | `{}`                          | `{image_b64, format:"png"}`                  |
| `telemetry_collect`  | `{sections?}`                 | snapshot map (see `telemetry` frame)         |
| `agent_update`       | `{version, url, sha256}`      | `{ok, staged_version}`                       |

`agent_update` is a **server-triggered self-update** (state-changing): the agent
downloads the new binary from `url` (served by the server's download endpoint),
verifies it against `sha256`, stages it, and restarts itself (as a Windows service)
into the new version. The agent answers `{ok, staged_version}` *before* restarting, so
the connection drops and the agent reconnects on the new version (compare
`register.meta.version`). On a non-Windows/dev build the agent returns
`error.code = "unsupported"`.

### Server-only MCP tools (not forwarded to a single agent)

| tool              | args            | purpose                                            |
|-------------------|-----------------|----------------------------------------------------|
| `list_agents`     | `{}`            | known agents + online state + overall health       |
| `select_agent`    | `{id}`          | set the active agent for subsequent forwarded tools |
| `fleet_overview`  | `{}`            | per-agent rolled-up health for the dashboard        |
| `agent_health`    | `{id}`          | per-section status/summary for one agent            |
| `agent_snapshot`  | `{id, section?}`| latest stored snapshot (or one section) for an agent|

## Telemetry sections

Each section payload **must** include `status` ∈ {`ok`, `warn`, `crit`} and a short
`summary` string. Raw fields are section-specific (see `docs/fixtures/telemetry_*`).

**Mandatory:** `disk`, `peripherals`, `network`, `routing`, `processes`, `services`,
`defender`, `win_update`.
**Hardware health:** `disk_smart`, `battery`, `memory`, `thermals` (optional).
**Security & crypto:** `firewall`, `encryption`, `av_thirdparty`, `defender_quarantine`.
**Update & stability:** `reboot_pending`, `os_support`, `reliability`, `app_updates`.
**Operations & daily:** `uptime`, `time_sync`, `printers`, `wifi_quality`, `autostart`.

Health thresholds (e.g. disk used > 80% ⇒ `warn` and ≥ 95% ⇒ `crit`; Defender
real-time protection off ⇒ `crit`; Defender scan older than 14 days ⇒ `warn`) are
evaluated **server-side** in `kenny-server/kenny_server/health_rules.py`. The agent
SHOULD set a reasonable `status` per section, but the server's rules are authoritative
for fleet aggregation. These thresholds are illustrative of the data-driven rules in
`health_rules.py`, which is the source of truth for exact boundaries.

## Versioning

`PROTOCOL_VERSION = "0.6"`. Both implementations expose this constant and include it
nowhere on the wire yet (reserved for a future `register.meta.protocol`). Bump on any
breaking change to a frame or tool schema.

- `0.6` — added the `policy` frame (server → agent) delivering the operator's append-only
  extra deny rules for the safety guard; additive frame, no tool changes. See ADR-0021.
- `0.5` — added the `blocked` error code for the agent's deterministic, always-on safety
  guard; additive to the error-code set, no frame or tool-schema changes. See ADR-0020.
- `0.4` — added the `log` frame (agent → server) for forwarded structured log events;
  additive frame, no tool changes. See ADR-0017.
- `0.3` — renamed every capability tool from dotted (`powershell.exec`) to
  underscore (`powershell_exec`) identifiers so names are valid Anthropic tool
  names (`^[a-zA-Z0-9_-]{1,128}$`); breaking tool-schema change, no frame changes.
- `0.2` — added the `agent_update` tool (server-triggered self-update); no frame changes.
- `0.1` — initial contract.

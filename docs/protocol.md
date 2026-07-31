# kenny Wire Protocol (v0.9)

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
- Authentication on this tunnel is **mutual** and per-agent, using Ed25519 signatures
  layered over the (TLS) transport (ADR-0023). Each agent holds its own Ed25519 keypair
  (private key never leaves the device); the server stores that agent's public key. The
  server holds one server-wide Ed25519 keypair whose public half is **pinned in the
  agent** at install time. Right after connect the two sides run a three-message
  challenge-response (`register` → `challenge` → `auth`, below): the server proves its
  identity to the agent (defeating server spoofing — an attacker who terminates/MITMs TLS
  cannot push `request` frames because it cannot sign the agent's nonce), and the agent
  proves its identity to the server. The **operator** authenticates to the server (MCP
  endpoint + web UI) with a separate operator token (see ADR-0008), unrelated to this
  handshake.
- **Migration window:** during rollout a server may still accept the legacy per-agent
  bearer `register.token` (symmetric) when `KENNY_ALLOW_TOKEN_AUTH=1`; the signature path
  is selected whenever `register.protocol >= "0.8"` and `register.client_nonce` is present.
  The token path is removed at cutover. See ADR-0023 and ADR-0014.

## Frame envelope

Every frame has a `type` field. Known types:

| type        | direction       | shape (see below)                          |
|-------------|-----------------|--------------------------------------------|
| `register`  | agent → server  | identifies the agent right after connect   |
| `challenge` | server → agent  | server's signed nonce (mutual-auth step 2) |
| `auth`      | agent → server  | agent's signature (mutual-auth step 3)     |
| `request`   | server → agent  | invoke one capability tool                 |
| `response`  | agent → server  | result/error for a `request` (by `id`)     |
| `telemetry` | agent → server  | periodic pushed snapshot (no request)      |
| `log`       | agent → server  | a forwarded structured log event           |
| `ping`      | both            | heartbeat                                  |
| `pong`      | both            | heartbeat reply                            |

### `register` (agent → server)

```json
{
  "type": "register",
  "agent_id": "example-pc",
  "protocol": "0.8",
  "client_nonce": "<base64, 32 random bytes>",
  "meta": { "hostname": "EXAMPLE-PC", "os": "windows", "version": "0.1.0", "arch": "x86_64" }
}
```

`os` ∈ {`windows`, `linux`, `macos`}. `arch` ∈ {`x86_64`, `aarch64`} is the agent's
normalized CPU architecture (from `std::env::consts::ARCH`, collapsing `arm64` to
`aarch64`); the server uses it to select the matching self-update binary. Legacy agents
that omit it are treated as `x86_64`. `protocol` is the agent's `PROTOCOL_VERSION`;
`client_nonce` is 32 fresh random bytes (base64) that the server must sign in the
`challenge`. The server looks up `agent_id` and replies with a `challenge` (it never
registers the connection until the agent's `auth` verifies).

`token` (a per-agent bearer secret) is **optional and legacy**: it is only honoured
during the migration window (`KENNY_ALLOW_TOKEN_AUTH=1`) and only when `protocol`/
`client_nonce` are absent, in which case the server authenticates the token against its
per-agent token store and registers immediately (no `challenge`/`auth`). On failure the
server closes the socket with a non-1000 code (`4401`).

### `challenge` (server → agent)

```json
{
  "type": "challenge",
  "server_nonce": "<base64, 32 random bytes>",
  "server_sig": "<base64 Ed25519 signature over the transcript>"
}
```

Sent in reply to a signature-path `register`. `server_sig` is the server's Ed25519
signature, made with the server-wide private key, over the **transcript** (below). The
agent verifies `server_sig` against its **pinned** server public key. If verification
fails — or the frame is not a `challenge` — the agent **aborts the session, sends no
`auth`, dispatches no `request`, and reconnects**. This is the anti-spoofing guarantee:
only the holder of the server private key can answer the agent's fresh nonce.

### `auth` (agent → server)

```json
{
  "type": "auth",
  "agent_sig": "<base64 Ed25519 signature over the transcript>"
}
```

Sent only after the agent has verified `server_sig`. `agent_sig` is the agent's Ed25519
signature, made with its per-agent private key, over the same **transcript**. The server
verifies it against the agent's stored public key; on success it registers the connection
under `agent_id` and proceeds (pushes `policy`, accepts `request` frames). On failure it
closes the socket with `4401`.

#### Transcript (signed by both sides)

Both signatures cover the **same** byte string, constructed identically on both sides
(`0x00` is a single NUL separator byte; nonces are the raw 32 bytes, not their base64):

```
transcript = "kenny-mutual-auth-v1"   (20 ASCII bytes, domain-separation label)
           || 0x00
           || agent_id                (UTF-8 bytes)
           || 0x00
           || client_nonce            (32 raw bytes, from register)
           || 0x00
           || server_nonce            (32 raw bytes, from challenge)
```

Binding both nonces and `agent_id` into both signatures prevents replay and reflection.
Ed25519 public keys, private seeds, signatures, and nonces are exchanged as standard
base64 (with padding). Deterministic golden vectors live in
`docs/fixtures/vectors/mutual_auth.json`; both implementations verify against them so the
transcript stays byte-identical across Rust and Python.

#### Enrollment (first contact)

An agent generates its keypair locally on first run; its public key reaches the server
once via a one-time **enrollment token** carried by the installer (over TLS):
`POST /api/agents/{id}/enroll` with `{ "public_key": "<base64>" }`, authorized by the
enrollment token. The server records the public key bound to `agent_id` (the token is
single-use). Thereafter only signatures authenticate. The installer also carries the
pinned server public key. See ADR-0023.

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

`powershell_exec` and `shell_exec` are OS-specific mirrors of each other — the former
runs a script via `powershell.exe` on Windows, the latter a command via `sh -c` on
Linux/macOS. Each is `unsupported` on the other OS (see below), and the server refuses
to even forward the wrong one for a given agent (see "OS-scoped tools" below); Claude
picks the right one from the agent's `os`, which `list_agents`/`select_agent` already
report.

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
`internal`, `disabled`, `blocked`, `paused`}. `unsupported` is returned by an agent that lacks the
capability on its platform (e.g. `winget_list` on a Linux dev build, `powershell_exec` on
Linux/macOS, or `shell_exec` on Windows). `disabled` is
returned when the agent is online but the person at the endpoint has switched remote
control **off** locally (via the agent's tray menu): the agent then refuses every
**mutating** tool (`powershell_exec`, `shell_exec`, `winget_install|uninstall|update`,
`net_dns_flush`, `net_adapter_reset`, `agent_update`, `webfilter_apply|clear`,
`account_set_enabled|set_admin|set_logon_rights|create|delete|session_action`,
`password_policy_set`) while
telemetry and read-only diagnostics keep working. Remote control is **on** by default and the choice persists
across restarts. See ADR-0011.

#### OS-scoped tools

`powershell_exec` and `shell_exec` each require a specific agent OS family (`windows`
and `linux`/`macos` respectively). Before forwarding such a tool, the
**server**
compares the requested tool's required family against the target agent's known `os`
(from `register.meta.os`, see "Frames" above) and, on a mismatch, refuses the call
itself with `error.code = "unsupported"` and a message naming the correct tool —
without ever sending a `request` frame to the agent. This is a deterministic,
server-side routing guard, not a security boundary: it exists so a wrong-OS call fails
fast with an actionable message instead of round-tripping to the agent (which would
also refuse it, per the paragraph above, once code reaches `dispatch::run`). An agent
unknown to the registry (e.g. selected from stored telemetry only) skips this
pre-check; the call still fails once attempted, either at the tunnel (agent offline) or
at the agent's own OS-gated handler.

The account-governance tools are deliberately **not** OS-scoped (they were, in v0.15).
They are served on every OS family, and what a *particular account on a particular host*
can do is discovered from the `local_accounts` inventory instead — see "Account
governance tools" below. A whole-tool OS scope would have forced the caller to know the
host's OS before acting; the per-account capability map is finer, truer, and already the
mechanism ADR-0046 chose for the local-vs-Microsoft asymmetry.

`paused` is returned when the agent is online but has **voluntarily stepped back** because a
protected game is running on the endpoint (the agent detected the game's anti-cheat process).
To avoid being mistaken for cheating software, the agent suspends its most anti-cheat-visible
tools — today `screen_capture` — while the game runs, and relaxes the periodic process/port
enumeration (those telemetry sections report a `paused` summary and stop listing processes).
Unlike `disabled`, this is automatic and game-scoped rather than an operator toggle, and it
clears the moment the game exits. The step-back is transparent by design: the agent genuinely
stops the visible action and reports it — it never hides, renames, or disguises what it does.
See ADR-0039.

`blocked` is returned by the agent's **deterministic, always-on safety guard**: a
compiled-in policy that refuses individually dangerous calls (e.g. a `powershell_exec`
script that deletes volume shadow copies, clears event logs, or disables Defender; a
`shell_exec` command that runs `rm -rf /` or a fork bomb; an
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

Each rule has `id` (stable identifier), `applies_to` ∈ {`powershell`, `posix`, `self_protection`,
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
  "agent_id": "example-pc",
  "collected_at": "2026-06-04T18:00:00Z",
  "snapshot": {
    "disk": {
      "status": "warn",
      "summary": "C: 91% full",
      "volumes": [
        { "mount": "C:", "total_bytes": 511000000000, "free_bytes": 46000000000, "percent_used": 91 }
      ],
      "top_dirs": [
        { "path": "C:\\Users\\testuser\\Videos", "bytes": 120000000000 }
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
  "agent_id": "example-pc",
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

The server exposes each tool as an MCP tool; the agent implements a handler with the same
name. Argument keys below are exact and are exactly what reaches the agent in the `request`
frame's `args`.

On the MCP surface, every forwarded capability tool additionally accepts an `agent_id`
argument naming the target host (ADR-0042). It is **server-consumed routing metadata**: the
server pops it off the call's arguments to pick which agent connection to send the `request`
frame down, so it is never included in the wire `args` and the agent never sees it — the
table below is unaffected and unchanged. `agent_id` is required on this path (two concurrent
MCP sessions authenticated with the same credential have no other reliable way to stay
isolated); `select_agent` remains available for discovery but no longer decides where a
forwarded call lands.

| tool                 | args                          | result (sketch)                              |
|----------------------|-------------------------------|----------------------------------------------|
| `powershell_exec`    | `{script, timeout_s}`         | `{stdout, stderr, exit_code}`                |
| `shell_exec`         | `{command, timeout_s}`       | `{stdout, stderr, exit_code}`                |
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
| `remotehelp_status`  | `{}`                          | `{installed, version, internet_ok, interactive_session}` |
| `remotehelp_start`   | `{}`                          | `{launched, pid, note}`                      |
| `remotehelp_stop`    | `{}`                          | `{stopped}`                                  |
| `telemetry_collect`  | `{sections?}`                 | snapshot map (see `telemetry` frame)         |
| `agent_update`       | `{version, url, sha256}`      | `{ok, staged_version}`                       |
| `webfilter_status`   | `{}`                          | `{active, entry_count, list_hash, doh_policy, applied_at, supported}` |
| `webfilter_apply`    | `{domains, doh_policy, list_hash}` | `{ok, applied, doh_policy_applied, list_hash, applied_at}` |
| `webfilter_clear`    | `{}`                          | `{ok, removed_entries, doh_policy_cleared}`  |
| `account_set_enabled` | `{principal, enabled}`       | `{ok, principal, kind, enabled}`             |
| `account_set_admin`  | `{principal, admin}`          | `{ok, principal, kind, admin}`               |
| `account_set_logon_rights` | `{principal, deny}`     | `{ok, principal, kind, deny}`                |
| `account_create`     | `{name, password, display_name?, admin?}` | `{ok, principal, kind}`          |
| `account_delete`     | `{principal, remove_profile}` | `{ok, principal, profile_removed}`           |
| `account_session_action` | `{principal, action, warn_seconds?}` | `{ok, principal, action, sessions}` |
| `password_policy_set` | `{min_length?, max_age_days?, lockout_threshold?}` | `{ok, applies_to:"local_only", policy}` |

`agent_update` is a **server-triggered self-update** (state-changing): the agent
downloads the new binary from `url` (served by the server's download endpoint),
verifies it against `sha256`, stages it, and restarts itself (as a Windows service)
into the new version. The agent answers `{ok, staged_version}` *before* restarting, so
the connection drops and the agent reconnects on the new version (compare
`register.meta.version`). On a non-Windows/dev build the agent returns
`error.code = "unsupported"`.

The `remotehelp_*` tools orchestrate Windows **Quick Assist** as a remote-help
*concierge*: kenny prepares and brokers a session but does **not** carry the screen or
input itself (Quick Assist brings its own Microsoft relay, NAT traversal, and
encryption). `remotehelp_status` is **read-only** — it reports whether Quick Assist is
installed (`installed`, `version` from `Get-AppxPackage`), whether the internet is
reachable (`internet_ok`), and whether an interactive user session is present to host the
app (`interactive_session`). `remotehelp_start` and `remotehelp_stop` are **mutating**:
`start` launches Quick Assist **on the interactive user desktop** and answers
`{launched, pid, note}` (the `note` reminds the operator that a human helper must supply
the Quick Assist code and the person at the PC must accept); `stop` terminates Quick
Assist (`{stopped}`) so no session lingers. Because the agent runs as a session-0 service
with no desktop, `start` launches the app via the user-session tray helper over a local
named pipe, restricted to an allow-list of remote-help executables — same delivery
mechanism as `screen_capture` (ADR-0018). On a non-Windows/dev build `start`/`stop`
return `error.code = "unsupported"` and `status` reports everything not-available. See
ADR-0022.

The `webfilter_*` tools implement **parental-controls blocking** (ADR-0026). They are the
enforcement half of the `web_activity` telemetry section (below); the alarm path does not
depend on them. The server owns the per-host list and pre-merges the effective block set into
a flat `domains` array — the agent is a dumb, idempotent enforcer and carries no list logic.

- `webfilter_status` (`{}`) is **read-only** (works under the kill switch, like
  `remotehelp_status`): `{active, entry_count, list_hash, doh_policy:{chrome,edge,firefox}, applied_at, supported}`,
  where `list_hash` is the agent's recomputed hash of the currently applied block (for drift
  detection against the server's intended hash) and `doh_policy` reports the current per-browser
  DNS-over-HTTPS policy state.
- `webfilter_apply` (`{domains, doh_policy, list_hash}`) is **mutating**: it writes `domains`
  as a marker-delimited block (`# kenny-webfilter begin`/`end`, one `0.0.0.0 <domain>` line
  each) into the OS hosts file via atomic replace, and — when `doh_policy == "disable"` — sets
  registry policies turning DNS-over-HTTPS **off** in Chrome, Edge, and Firefox so DoH cannot
  bypass the hosts block; then flushes the DNS cache. `domains` are normalized lowercase host
  names, **hard-capped at 10 000** (the agent returns `bad_args` above the cap rather than
  silently truncating — a server/agent cap mismatch must surface). `doh_policy` ∈
  {`"disable"`, `"leave"`}. `list_hash` is the server's `sha256(sorted domains)[:16]`, echoed
  back in the result and by `webfilter_status`. The agent **refuses** (`blocked`) any list that
  would blackhole a self-protected name (`localhost`, the configured server host, core
  Microsoft-update infrastructure) so a bad list can never sever the tunnel or OS updates.
  Result: `{ok, applied, doh_policy_applied, list_hash, applied_at}`.
- `webfilter_clear` (`{}`) is **mutating**: removes only kenny's marker block and the
  kenny-written DoH policy values, then flushes DNS. Result: `{ok, removed_entries, doh_policy_cleared}`.

On a non-Windows/dev build `apply`/`clear` return `error.code = "unsupported"` and `status`
reports `{active: false, supported: false, ...}`, keeping `cargo test`/`cargo build` green on
Linux CI. See ADR-0026.

#### Account governance tools

The `account_*` tools plus `password_policy_set` govern **who may sign in to a host**
(ADR-0046, extended to Linux by ADR-0047). They are the enforcement half of the
`local_accounts` telemetry section (below), which is also their **inventory**: there is no
`account_list` tool — read the section, and refresh it on demand with
`telemetry_collect {sections:["local_accounts"]}`.

All of them are **mutating** (refused with `disabled` under the kill switch). None of them
is OS-scoped: they are served on Windows and Linux alike, and per-account capability is
read from the inventory (see "Capability negation" below). On an OS with no
implementation (macOS today) the agent answers `unsupported`.

**`principal` is the local account name** — `local_accounts.accounts[].name`. On Windows
that is the SAM name, identical for a local and a Microsoft account; on Linux it is the
`/etc/passwd` login name. Both are unique per machine by construction, and both are exactly
what the platform's own tooling accepts. A Microsoft account on a workgroup PC *is* a SAM
entry with a machine-local SID and profile, so these operations are type-agnostic by
construction rather than by abstraction. Full SIDs and Microsoft-account email addresses
never go on the wire in either direction (ADR-0046).

- `account_set_enabled` (`{principal, enabled}`) — the suspend switch. Works identically for
  both account kinds; disabling a Microsoft-account-backed entry blocks sign-in on this PC
  and leaves the cloud account untouched. On Linux "disabled" means **account expiry plus a
  locked password**, not a locked password alone — a locked password still admits an SSH key.
- `account_set_admin` (`{principal, admin}`) — add to or remove from the administrators
  group. On Windows that group is resolved by the well-known SID `S-1-5-32-544`
  (locale-proof); on Linux it is the first of `sudo`, `wheel`, `admin` that exists in
  `/etc/group`, and removal strips membership in *all* of them.
- `account_set_logon_rights` (`{principal, deny}`) — `deny` is the full desired set, a subset
  of `["network", "remote_interactive"]`. Passing `[]` clears both.
  `SeDenyInteractiveLogonRight` is deliberately **not** exposed: it can lock out
  the sole console user and kenny has no remote console to recover with. On Windows both
  rights are applied via LSA account rights (this works on Windows Home, where only the
  `secpol.msc` snap-in is missing, not the API). On Linux `remote_interactive` denies SSH
  sign-in through a kenny-owned `sshd_config` drop-in, and `network` is reported as
  unsupported per account — Linux has no separate network-logon plane to deny.
- `account_create` (`{name, password, display_name?, admin?}`) — creates a local account. On
  Windows this is the one genuinely asymmetric verb: a Microsoft account can be added to a PC
  only interactively. On Linux there is no asymmetry.
- `account_delete` (`{principal, remove_profile}`) — removes the account entry; for a
  Microsoft-account-backed entry this unlinks the account from this PC and does not touch the
  cloud account. `remove_profile` additionally deletes the user's profile/home directory.
- `account_session_action` (`{principal, action, warn_seconds?}`) — `action` ∈
  {`"lock"`, `"logoff"`}. The agent resolves the principal to its interactive session(s)
  itself and reports which it acted on in `sessions`; there is deliberately **no telemetry
  section listing who is signed in** — this returns proof of effect, not a presence log.
  `warn_seconds` (default 0) shows the user a message first. An account with no session
  succeeds with an empty `sessions` array. On Linux sessions come from `systemd-logind`, and
  `lock` needs a graphical session — a headless host reports it unsupported.
- `password_policy_set` (`{min_length?, max_age_days?, lockout_threshold?}`) — machine-wide,
  omitted keys are left unchanged. The result always carries `applies_to: "local_only"`: on
  Windows because a Microsoft account's password is governed by Microsoft's cloud policy, on
  Linux because the policy is enforced by local PAM and does not reach directory-backed
  (LDAP/SSSD) identities. Consumers must surface that rather than imply full coverage. Where
  the host lacks a knob entirely, the section-level `password_policy.unsupported` says so.

##### Capability negation

What a **particular account on a particular host** can do is published in
`local_accounts.accounts[].unsupported`: a map of *capability verb* → *short reason token*,
listing only what is **not** possible. An absent verb is supported. Reason tokens are stable
identifiers for consumers to localize, never display strings.

The verb vocabulary — capability names, not tool names, so one tool can be partly available:

| verb | gates |
|---|---|
| `set_enabled` | `account_set_enabled` |
| `set_admin` | `account_set_admin` |
| `deny_network` | the `network` member of `account_set_logon_rights`'s `deny` set |
| `deny_remote_interactive` | the `remote_interactive` member of that set |
| `delete` | `account_delete` |
| `session_lock` | `account_session_action` with `action: "lock"` |
| `session_logoff` | `account_session_action` with `action: "logoff"` |
| `session_warn` | `account_session_action`'s `warn_seconds` — the *warning*, not the action |
| `reset_password` | advisory only — kenny has no password-reset tool |

Splitting `account_set_logon_rights` and `account_session_action` into two verbs each is
deliberate: a tool can be **partly** available, and a consumer needs that granularity to
disable one checkbox and leave the other live.

Reason tokens, and where they come from:

| token | source | meaning |
|---|---|---|
| `password_in_cloud` | account kind | a Microsoft/Entra password is not on this machine |
| `kind_unknown` | account kind | the probe could not determine the account kind |
| `root_account` | Linux account | uid 0 — group membership neither grants nor revokes root |
| `admin_via_sudoers` | Linux account | admin rights come from `/etc/sudoers.d`, which kenny never writes |
| `nologin_shell` | Linux account | the account has no login shell, and kenny does not rewrite shells |
| `no_network_logon_concept` | Linux host | Linux has no per-account network-logon plane to deny |
| `no_sshd` | Linux host | no SSH daemon, so there is no remote sign-in to deny |
| `sshd_no_include` | Linux host | `sshd_config` has no `Include` line, and kenny will not add one |
| `no_logind` | Linux host | no `systemd-logind`, so sessions cannot be enumerated |
| `no_graphical_session` | Linux host | headless — a session can be ended but not locked |
| `no_admin_group` | Linux host | none of `sudo`/`wheel`/`admin` exists |
| `shadow_unreadable` | Linux host | the agent cannot read `/etc/shadow` (not running as root) |
| `no_user_notification_channel` | Linux host | no way to warn the signed-in user first |

The machine-wide `password_policy` carries the same idiom one level up in its own optional
`unsupported` map, with its own tokens `no_pwquality` and `pam_faillock_not_enabled`.

The agent enforces a **self-protection set** it will not let an operator override, refusing
with `blocked`: the last enabled local administrator cannot be disabled, demoted, deleted, or
given deny-logon rights; built-in accounts (Windows RID 500/501) cannot be deleted; **and any
action whose verb the inventory reports as `unsupported` for that account is refused**. The
last rule is what keeps the published capability map and the enforced one from drifting apart
— it is the same list, read at call time. This mirrors the `webfilter_apply` reserved-name
rule: a governance call must never be able to lock everyone out of the machine.

### Server-only MCP tools (not forwarded to a single agent)

| tool              | args            | purpose                                            |
|-------------------|-----------------|----------------------------------------------------|
| `list_agents`     | `{}`            | known agents + online state + overall health       |
| `select_agent`    | `{id}`          | validate an agent id and report it as a default (advisory only — does not route forwarded calls, see ADR-0042) |
| `fleet_overview`  | `{}`            | per-agent rolled-up health for the dashboard        |
| `agent_health`    | `{id}`          | per-section status/summary for one agent            |
| `agent_snapshot`  | `{id, section?}`| latest stored snapshot (or one section) for an agent|
| `webfilter_get`   | `{id}`          | one host's parental-controls config + custom list   |
| `webfilter_set`   | `{id, ...}`     | edit a host's config/toggles or add/remove a domain |
| `webfilter_push`  | `{id}`          | build the effective block set and forward `webfilter_apply`/`clear` |
| `web_activity_query` | `{id, hours?, flagged_only?}` | observed/flagged domains for one host  |
| `reliability_suppression_list` | `{agent_id?}` | reliability alarm suppression rules (fleet-wide + host) |
| `reliability_suppression_add` | `{event_id, source?, agent_id?, note?}` | exclude a `(source, event_id)` pattern from severity scoring |
| `reliability_suppression_remove` | `{rule_id}` | remove a suppression rule |

The `webfilter_*` server-only tools manage the per-host list and trigger a push; they wrap
the forwarded `webfilter_apply`/`webfilter_clear` capability tools (ADR-0026). `webfilter_set`
and `webfilter_push` are state-changing (they pass the operator confirm-gate, ADR-0009).

The `reliability_suppression_*` server-only tools manage the suppression-rule table behind
the Reliability card (ADR-0045); `_add`/`_remove` are state-changing (ADR-0009). They forward
nothing to an agent — `agent_id` is an optional scope filter, not a routing target.

## Telemetry sections

Each section payload **must** include `status` ∈ {`ok`, `warn`, `crit`} and a short
`summary` string. Raw fields are section-specific (see `docs/fixtures/telemetry_*`).

**Mandatory:** `disk`, `peripherals`, `network`, `routing`, `processes`, `services`,
`defender`, `win_update`.
**Hardware health:** `disk_smart`, `battery`, `memory`, `thermals` (optional).
**Security & crypto:** `firewall`, `encryption`, `av_thirdparty`, `defender_quarantine`.
**Update & stability:** `reboot_pending`, `os_support`, `reliability`, `app_updates`.
**Operations & daily:** `uptime`, `time_sync`, `printers`, `wifi_quality`, `autostart`.
**Parental controls:** `web_activity`, `screen_time`.
**Security inventory:** `installed_software`, `browser_extensions`, `listening_ports`,
`scheduled_tasks`, `local_accounts`.
**Resilience:** `backup_status`, `net_quality`.

The `web_activity` section reports the **host names** a PC has been reaching in a rolling
window (default 24 h), observed from the OS DNS client cache and per-user browser history
(host names only — never full URLs, page titles, or which user visited). It is bounded:
domains are deduplicated and capped (250, `last_seen` desc, `truncated` beyond), well inside
the telemetry frame cap. The agent always reports `status: "ok"` — it holds no list and does not
judge; the server matches observed domains against that host's per-host list and is
authoritative (see ADR-0026). The section payload the agent sends:

```json
"web_activity": {
  "status": "ok",
  "summary": "42 domains observed (24h)",
  "window_hours": 24,
  "sources": ["dns_cache", "browser_history"],
  "domains": [
    { "domain": "example.com", "first_seen": "2026-06-04T09:12:00Z",
      "last_seen": "2026-06-04T17:40:00Z", "hits": 7, "sources": ["dns_cache", "browser_history"] }
  ],
  "truncated": false,
  "browser_profiles_read": 3,
  "errors": []
}
```

On telemetry insert the server annotates the *stored* payload with a `flagged` array (the
matches against the host's list, with category and timestamps) that the `web_activity` health
rule consumes. That annotation is **server-internal and not part of this wire contract** — the
agent never sends `flagged`. Off Windows the section is the standard `n/a on this platform`
stub with empty `sources`/`domains`.

The `reliability` section reports **what** is going wrong, not just how many errors there are:
a breakdown of the Error/Critical entries in the System + Application event logs over a rolling
window (default 7 days), grouped by Windows source + event id. Each group carries a real sample
message, its level, a total count, when it was last seen, and a per-day histogram. The list is
bounded (top ~20 groups by count, `truncated` beyond; `sample` capped ~200 chars). The section
payload the agent sends:

```json
"reliability": {
  "status": "warn",
  "summary": "192 error/critical events in 7d",
  "stability_index": 6.8,
  "recent_crashes": 192,
  "window_days": 7,
  "events": [
    { "source": "Application Error", "event_id": 1000, "level": "error", "count": 84,
      "sample": "Faulting application name: chrome.exe, version 126.0.0.0 ...",
      "last_seen": "2026-07-01T20:14:33Z",
      "by_day": { "2026-06-27": 10, "2026-06-28": 12 } }
  ],
  "truncated": false
}
```

`stability_index` (Windows Reliability Index, 0–10, or `null`) and `recent_crashes` (the total
count = sum of the groups' counts) are retained. On the read path the server annotates each
group with a friendly `category`, a `severity` (`benign`/`notable`/`serious`/`unknown`), and a
short `suspected_cause` (via the connected LLM, cached) — used both for the dashboard's
reliability heatmaps and to drive the health rule's crit/warn scoring by pattern, not raw
volume. If the operator has suppressed this exact `(source, event_id)` pattern (ADR-0045), the
read path additionally stamps `suppressed: true` and a `suppressed_by` descriptor, excluding
the group from severity scoring while leaving its count untouched; unlike the LLM annotation
above, suppression needs no API key and so is stamped on every read path, including
`agent_snapshot`. These fields are all **server-internal and not part of this wire
contract** — the agent never sends them (see ADR-0028, ADR-0045). Off Windows the section is
the `n/a on this platform` stub with `events: []`.

### Security-inventory, resilience, and parental-awareness sections (v0.10)

Added at v0.10 (see ADR-0031, ADR-0032). All are additive; off Windows each is the
standard `n/a on this platform` stub with empty lists. Inventory lists are deduplicated,
sorted, and capped (with a `truncated` flag) so a section can never blow the telemetry
frame cap (unsolicited pushes are held to a tighter byte cap than correlated tool
responses; see the tunnel's inbound limits). The agent reports `status: "ok"` for pure inventory sections — judgment (health
rules and cross-snapshot diffing) is server-side.

- **`installed_software`** — machine-wide program inventory from the registry Uninstall
  keys (HKLM 64+32-bit; *not* `Win32_Product`, which triggers MSI reconfiguration, and
  not `winget list`, which is too slow for the probe budget). System components are
  filtered out; per-user (HKCU) installs are not visible to the session-0 service and
  are a documented blind spot. Cap 300.

  ```json
  "installed_software": {
    "status": "ok", "summary": "142 programs installed",
    "apps": [ { "name": "7-Zip 24.08 (x64)", "version": "24.08", "publisher": "Igor Pavlov", "install_date": "2026-03-11" } ],
    "count": 142, "truncated": false
  }
  ```

- **`browser_extensions`** — extensions installed in Chromium-family browsers
  (Chrome/Edge manifest dirs) and Firefox (`extensions.json`), read from the same
  per-user profile locations as `web_activity`. **Privacy:** deduplicated across users
  and profiles by `(browser, id)` — no per-user attribution on the wire. Cap 200.

  ```json
  "browser_extensions": {
    "status": "ok", "summary": "9 extensions across 2 browsers",
    "extensions": [ { "browser": "chrome", "id": "cjpalhdlnbpafiamejdnhcphjbkeiagm", "name": "uBlock Origin", "version": "1.58.0" } ],
    "count": 9, "truncated": false, "profiles_read": 3, "errors": []
  }
  ```

- **`listening_ports`** — TCP listeners and UDP endpoints joined with the owning
  process image name, deduplicated by `(proto, port, process)`, wildcard binds first.
  Cap 200.

  ```json
  "listening_ports": {
    "status": "ok", "summary": "12 listening ports",
    "ports": [ { "proto": "tcp", "port": 445, "address": "0.0.0.0", "pid": 4, "process": "System" } ],
    "count": 12, "truncated": false
  }
  ```

- **`scheduled_tasks`** — non-Microsoft scheduled tasks (`TaskPath` not under
  `\Microsoft\`), i.e. the persistence surface an operator actually reviews;
  `total_count` reports the full count for context. Cap 200.

  ```json
  "scheduled_tasks": {
    "status": "ok", "summary": "4 non-Microsoft tasks (312 total)",
    "tasks": [ { "path": "\\", "name": "OneDrive Update", "state": "Ready", "action": "%LocalAppData%\\OneDrive\\Update\\OneDriveSetup.exe", "run_as": "kid-pc\\kid", "last_result": 0, "next_run": "2026-06-05T03:00:00Z" } ],
    "count": 4, "total_count": 312, "truncated": false
  }
  ```

- **`local_accounts`** — local users plus administrators-group membership. On Windows the
  group is resolved by SID (`S-1-5-32-544`, locale-proof) and built-ins are marked via the
  well-known RID (`builtin_admin` -500, `builtin_guest` -501); **full SIDs never go on the
  wire** (minimum identifying tokens, ADR-0026 stance). On Linux the source is
  `/etc/passwd` + `/etc/group` + `/etc/shadow` (+ a read-only scan of `/etc/sudoers.d`):
  `is_admin` is uid 0 or membership in `sudo`/`wheel`/`admin`. `builtin_admin` is set for
  **root**, which is factually the built-in, undeletable administrator — so the same guard
  rule that protects Windows RID 500 protects it. `builtin_guest` is always `false` on
  Linux; it has no analogue. Accounts with uid 1–999 and `nobody` are not reported at all.
  Consumers must read `builtin_admin` together with the agent's OS: an *enabled* built-in
  administrator is a finding on Windows (RID 500 ships disabled) and is simply how Linux
  works. `password_required` mirrors the
  `UF_PASSWD_NOTREQD` flag on Windows and an empty `/etc/shadow` password field on Linux —
  it means *a blank password is permitted*, **not** *the account has no password*.
  `password_last_set` (nullable RFC3339 UTC) disambiguates: it is set the moment a password
  is ever chosen, and `null` only for a genuinely password-less account. Health rules must
  consult both, not `password_required` alone.

  Since v0.15 this section is also the **inventory for the `account_*` governance tools**
  (ADR-0046), and each account carries four more fields:

  - `kind` ∈ `local` | `microsoft` | `entra` | `unknown`, from `PrincipalSource` on Windows.
    A Microsoft account is a SAM entry like any other — the section name still fits, and
    `name` is the governance key for every kind. Every Linux account is `local`.
  - `display` — `FullName` (Windows) or the first GECOS field (Linux) if the account has
    one, else `name`. **Microsoft-account email addresses never go on the wire** (ADR-0046,
    same rationale as the SID rule). Residual imprecision, stated rather than hidden: with
    no `FullName` the fallback is the SAM name, which Windows derives from the first five
    characters of the address.
  - `deny_logon` — the sign-in restrictions currently in force, a subset of
    `["network", "remote_interactive"]` — LSA account rights on Windows, an sshd
    `DenyUsers` drop-in on Linux (where only `remote_interactive` is settable).
    `SeDenyInteractiveLogonRight` is not reported because it is not settable through kenny.
  - `unsupported` — the per-account capability negation map; the verb and reason vocabulary
    is defined under "Capability negation" in the tool catalog above.

  The section-level `password_policy` is machine-wide and carries its own
  `applies_to: "local_only"` — Microsoft accounts follow Microsoft's cloud password policy,
  and on Linux the policy is local PAM and does not reach directory-backed identities.
  Any field may be `null` when the probe could not read it; an optional `unsupported` map
  names the fields this host cannot set at all.

  ```json
  "local_accounts": {
    "status": "ok", "summary": "3 accounts, 1 admin",
    "accounts": [ { "name": "kid", "display": "Kid", "kind": "microsoft", "enabled": true, "is_admin": false, "password_required": true, "password_last_set": "2026-02-20T18:30:00Z", "last_logon": "2026-06-04T15:02:00Z", "builtin_admin": false, "builtin_guest": false, "deny_logon": ["remote_interactive"], "unsupported": { "reset_password": "password_in_cloud" } } ],
    "admins": ["papa"], "count": 3,
    "password_policy": { "applies_to": "local_only", "min_length": 8, "max_age_days": 0, "lockout_threshold": 10 }
  }
  ```

- **`logon_failures`** — failed sign-in attempts aggregated per account over a 24 h window.
  The source is Windows Security event 4625, or on Linux the sshd/PAM authentication
  failures in the journal (falling back to `/var/log/auth.log`). Reported per *named
  account* because an authentication attempt belongs to the identity plane that ADR-0046
  governs, not to the behaviour plane that stays aggregated — `screen_time` and
  `web_activity` are unaffected by that decision. Attempts against names with no matching
  local account collapse into `unmatched_count` (a mistyped or probed username is not an
  account). **No source addresses**: `types` (⊆ `interactive` | `network` | `remote`)
  already separates "someone tried a password at the console" from "something is hammering
  RDP", which is what the distinction is for. On Linux `network` does not occur — SSH is
  `remote`, console/display-manager attempts are `interactive`, and there is no third plane
  to report. `accounts` is capped at 50 entries, sorted by descending count.

  ```json
  "logon_failures": {
    "status": "ok", "summary": "3 failed logons in 24h",
    "window_hours": 24,
    "accounts": [ { "name": "papa", "count": 3, "types": ["interactive"] } ],
    "unmatched_count": 0, "count": 3, "truncated": false
  }
  ```

- **`backup_status`** — evidence that *any* backup mechanism is alive: System Restore
  (enabled + restore-point count/latest), the File History service state (per-user File
  History *configuration* is unreadable from session 0, so `configured` may be `null`),
  and OneDrive presence/running. All sub-objects are best-effort and nullable.

  ```json
  "backup_status": {
    "status": "ok", "summary": "restore point 2d ago; OneDrive running",
    "restore_points": { "enabled": true, "count": 5, "latest": "2026-06-02T11:30:00Z" },
    "file_history": { "service_state": "stopped", "configured": null },
    "onedrive": { "installed": true, "running": true }
  }
  ```

- **`net_quality`** — a stateless probe of link quality at collection time: a handful
  of ICMP echoes to the default gateway and to a reference host (default `1.1.1.1`,
  agent-side override `KENNY_NET_QUALITY_REF_HOST`). `latency_ms` is the median and is
  `null` at 100 % loss.

  ```json
  "net_quality": {
    "status": "ok", "summary": "gateway 2ms, internet 14ms",
    "gateway": { "host": "192.168.1.1", "latency_ms": 2.0, "loss_percent": 0 },
    "reference": { "host": "1.1.1.1", "latency_ms": 14.0, "loss_percent": 0 },
    "samples": 5, "errors": []
  }
  ```

- **`screen_time`** — aggregated interactive minutes per calendar day for the **whole
  machine** over the last 7 days, derived from logon/logoff (and, where readable,
  lock/unlock) events. **Privacy (ADR-0032):** no usernames, no per-user split, no app
  names, no timestamps finer than the day bucket; each day is clamped to [0, 1440].
  The agent recomputes the window on every push (stateless); the server's daily
  history provides longer trends. The agent always reports `status: "ok"` — kenny
  reports, parents judge.

  ```json
  "screen_time": {
    "status": "ok", "summary": "3.4h today, 24h over 7 days",
    "window_days": 7,
    "days": [ { "date": "2026-06-04", "active_minutes": 204 } ],
    "source": "eventlog", "errors": []
  }
  ```

Health thresholds (e.g. disk used > 80% ⇒ `warn` and ≥ 95% ⇒ `crit`; Defender
real-time protection off ⇒ `crit`; Defender scan older than 14 days ⇒ `warn`) are
evaluated **server-side** in `kenny-server/kenny_server/health_rules.py`. The agent
SHOULD set a reasonable `status` per section, but the server's rules are authoritative
for fleet aggregation. These thresholds are illustrative of the data-driven rules in
`health_rules.py`, which is the source of truth for exact boundaries.

## Versioning

`PROTOCOL_VERSION = "0.16"`. Both implementations expose this constant; from v0.8 the
agent puts it on the wire in `register.protocol` to select the mutual-auth handshake
(compare versions **numerically per component**, not lexically — `"0.10"` is newer than
`"0.9"`). Bump on any breaking change to a frame or tool schema.

- `0.16` — account governance is no longer Windows-only (ADR-0047). The seven `account_*`
  /`password_policy_set` tools **lose their OS scope**: the server forwards them to Linux
  agents too, and the agent serves them from `/etc/passwd`/`/etc/shadow`/`/etc/group`,
  `usermod`/`useradd`/`userdel`, an sshd `DenyUsers` drop-in, `loginctl`, and
  pwquality/login.defs/faillock. `logon_failures` gained a Linux arm (sshd/PAM failures from
  the journal) and is no longer skipped for non-Windows agents. No frame or field shape
  changes: every Linux asymmetry is published in the existing per-account `unsupported` map,
  whose verb and reason vocabulary is now written down explicitly (see "Capability
  negation"), and the agent's self-protection guard refuses any action whose verb that map
  names — so the published capability set and the enforced one cannot drift. Additive
  extras: an optional `password_policy.unsupported` map, and `password_required` /
  `password_last_set` now carrying real values on Linux. Existing Windows payloads and
  fixtures are byte-identical.
- `0.15` — added account governance across local **and** Microsoft accounts (ADR-0046):
  seven tools (`account_set_enabled`, `account_set_admin`, `account_set_logon_rights`,
  `account_create`, `account_delete`, `account_session_action`, `password_policy_set`), four
  new per-account fields on `local_accounts` (`kind`, `display`, `deny_logon`,
  `unsupported`) plus a section-level `password_policy`, and a new `logon_failures`
  telemetry section. Additive throughout — no frame changes, and existing `local_accounts`
  consumers keep working since every addition is a new key. The governance key is the SAM
  account name, which both account kinds share; full SIDs and Microsoft-account email
  addresses stay off the wire. `screen_time` and `web_activity` are deliberately unchanged:
  identity became nameable, behaviour did not (ADR-0026, ADR-0032 hold).
- `0.14` — added the `shell_exec` tool, a POSIX mirror of `powershell_exec` for
  Linux/macOS agents (`{command, timeout_s}` → `{stdout, stderr, exit_code}`, run via
  `sh -c`). `powershell_exec` now returns `unsupported` on non-Windows agents instead of
  falling back to `sh` — the two tools are now a clean OS-scoped pair, and the server
  refuses to forward either to the wrong agent OS before it ever reaches the agent (see
  "OS-scoped tools" above). The shared deny-rule catalog gained a `posix` `applies_to`
  group with POSIX-destructive rules (`rm -rf /`, `mkfs`, `dd`/`shred`/`wipefs` device
  writes, fork bombs, recursive `chmod`/`chown` on `/`) alongside POSIX
  self-protection rules (`systemctl stop/disable kenny-agent`, `kill`/`pkill
  kenny-agent`), mirroring the existing PowerShell/self-protection groups (ADR-0020/0021).
  Breaking change for `powershell_exec`'s off-Windows behavior (dev/CI scripts relying on
  the old `sh` fallback must switch to `shell_exec`); additive new tool otherwise. See
  ADR-0035 (Linux agent support), which flagged this as future work.
- `0.13` — added `arch` to the `os_support` telemetry section, mirroring
  `register.meta.arch`. The server merges a reported value into the agent's stored
  `arch` on every telemetry push, giving it a periodic, self-refreshing signal in
  addition to the one-time value from `register` — so a long-lived connection stays
  correct even if the initial `register.meta.arch` were ever missing or stale. Additive:
  no frame/tool-schema changes, `Section` already accepts arbitrary fields. See
  ADR-0040. This does not retroactively fix an already-deployed pre-0.11 agent that
  never reports `arch` on either channel — only updating/reinstalling to an
  arch-reporting build does.
- `0.12` — added the `paused` error code for anti-cheat coexistence: while a protected game
  is running on the endpoint, the agent voluntarily suspends its most anti-cheat-visible tools
  (today `screen_capture`) and relaxes the process/port telemetry sections, returning `paused`
  instead of acting. Additive to the error-code set, no frame or tool-schema changes. See ADR-0039.
- `0.11` — added `register.meta.arch` (∈ `x86_64`/`aarch64`) so server-triggered
  `agent_update` selects the binary matching the agent's CPU, fixing aarch64 Linux
  agents being bricked by a mis-routed x86_64 push (#139). Additive and backward
  compatible: legacy agents that omit `arch` still register and default to `x86_64`.
  The Linux updater also now verifies the downloaded binary's ELF `e_machine` matches
  the host before the atomic swap, so a mis-routed binary is rejected instead of
  overwriting the working exe.
- `0.10` — added the `installed_software`, `browser_extensions`, `listening_ports`,
  `scheduled_tasks`, `local_accounts`, `backup_status`, `net_quality`, and `screen_time`
  telemetry sections (security inventory, resilience, parental awareness); additive
  sections only, no frame or tool changes. See ADR-0031, ADR-0032.
  - Refinement: `local_accounts` accounts gained a nullable `password_last_set` field so
    health rules can tell a genuinely password-less admin from one that merely has the
    `UF_PASSWD_NOTREQD` flag set (blank password permitted but a real password present).
    Additive and backward compatible — no version bump.
- `0.9` — added the `webfilter_status`, `webfilter_apply`, and `webfilter_clear` tools and
  the `web_activity` telemetry section for parental-controls observability and on-demand web
  filtering; additive tools + section, no frame changes. See ADR-0026.
- `0.8` — mutual agent⇄server authentication via per-agent Ed25519 signatures: added the
  `challenge` (server → agent) and `auth` (agent → server) frames and the
  `register.protocol` / `register.client_nonce` fields; `register.token` becomes optional
  (legacy, migration-window only). Breaking handshake change. See ADR-0023.
- `0.7` — added the `remotehelp_status`, `remotehelp_start`, and `remotehelp_stop` tools
  (orchestrate Windows Quick Assist as a remote-help concierge); additive tools, no frame
  changes. See ADR-0022.
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

# kenny — User Guide

This guide is for the **operator**: the person who watches the family's Windows PCs and runs
commands on them through kenny. For installing and hosting the server, see
[`setup.md`](setup.md).

## What kenny gives you

- A **fleet dashboard** showing every PC's health at a glance (a traffic light per machine).
- A **drill-down** per PC: telemetry sections, a health trend, and a tool-call log.
- Two ways to act on a PC: talk to **Claude** (which calls kenny's tools), either from a local
  Claude client over MCP or from the **chat built into the dashboard** — no local client needed.
- One-click **agent installer download** (and a shareable link) plus **server-triggered updates**.

## The pieces

```mermaid
flowchart LR
  Operator(("Operator"))
  subgraph Server["kenny-server (cloud)"]
    UI["Dashboard + Chat<br/>/ , /api/*"]
    MCP["MCP endpoint<br/>/mcp"]
    Tunnel["Agent tunnel<br/>/agent/ws"]
    Store[("Telemetry store<br/>SQLite")]
  end
  Claude["Claude<br/>(local client, optional)"]
  Agent["kenny-agent<br/>(Windows PC)"]

  Operator -->|"https + login"| UI
  Operator -->|"chat"| UI
  Operator --> Claude -->|"MCP (Bearer token)"| MCP
  UI --> Tunnel
  MCP --> Tunnel
  Tunnel <-->|"WSS, agent dials out"| Agent
  Agent -->|"telemetry push"| Store
  Store --> UI
```

The agent always **dials out** to the server (NAT/firewall friendly) and authenticates with its
own per-agent token; you authenticate to the server with the **operator token**.

## Signing in

1. Open the server in a browser (e.g. `https://kenny.example.com/`).
2. You are redirected to `/login`. Enter the **operator token** (set by whoever runs the server as
   `KENNY_OPERATOR_TOKEN`). A cookie keeps you signed in; `/logout` clears it.

> The same operator token is what a local Claude client sends as `Authorization: Bearer <token>` to
> the `/mcp` endpoint.

## The fleet view

Each PC is a tile with a status dot:

| Dot | Status | Meaning |
|-----|--------|---------|
| 🟢 | `ok` | nothing flagged |
| 🟡 | `warn` | something needs attention (e.g. disk > 80 %, aging battery) |
| 🔴 | `crit` | acute problem (e.g. Defender real-time protection off, disk ≥ 95 %) |
| ⚪ | `unknown`/offline | no recent telemetry / agent not connected |

The header shows the **worst-of** health across the whole fleet. Click a tile to drill in.

## The agent drill-down

- **Sections** — each telemetry section with its status, a one-line summary, and the server's health
  rule reason. Click a section tile to open a popup with its details rendered as readable tables and
  fields (no raw JSON).
- **Health trend** — recent snapshots as colored bars.
- **Tool-call log** — every tool run against this agent (ok / error), for audit. The fleet-wide log
  below the cards is searchable (by time, PC, or tool) and paged.
- Action buttons: **refresh now**, **reinstall**, **re-share**, **update agent**. Onboarding a *new*
  PC uses the **Add a PC** panel (**installer** / **share link**); from a PC's drill-down,
  **reinstall** / **re-share** re-provision that existing PC (rotating its token).

Telemetry sections cover: disk & SMART, memory, CPU/thermals, uptime, network & routing, Wi‑Fi,
Defender (+ quarantine), third-party AV, firewall, BitLocker, Windows Update & app updates,
reboot-pending, OS support/EOL, services, autostart, peripherals, printers, battery, reliability,
and time sync. Health thresholds are evaluated **server-side** (authoritative); the agent also sets
a reasonable per-section status.

## Running commands on a PC

### Option A — the dashboard chat (no local client)

Open the **chat** tab and ask in plain language (“is Defender on for papa-pc?”, “free up disk on
oma-laptop”). Claude picks the right kenny tools and runs them.

```mermaid
sequenceDiagram
  actor Op as Operator
  participant Chat as Dashboard chat
  participant Claude as Claude (server-side)
  participant Agent as kenny-agent
  Op->>Chat: "Update 7-Zip on papa-pc"
  Chat->>Claude: message + tool catalog
  Claude-->>Chat: read-only tool calls (diag/telemetry)
  Chat->>Agent: forwarded (auto-run)
  Agent-->>Chat: results
  Claude-->>Chat: wants winget_install (state-changing)
  Chat-->>Op: ⚠ confirmation required (tool + args)
  Op->>Chat: confirm
  Chat->>Agent: winget_install
  Agent-->>Chat: result
  Claude-->>Op: summary
```

**Confirm-gate:** read-only tools (diagnostics, `fs_read`/`list`/`search`, `telemetry_collect`,
`*_list`, `screen_capture`) run automatically. Anything **state-changing** —
`powershell_exec`, `winget_install/uninstall/update`, `net_dns_flush`, `net_adapter_reset`,
`agent_update` — pauses for your explicit confirmation before it runs. Every call is recorded in the
tool-call log.

### Option B — a local Claude client over MCP

Point any MCP client at `https://<server>/mcp` with the operator token as a bearer credential. The
same tools are available; `select_agent` chooses the target PC.

### Tool catalog

| Family | Tools | Changes state? |
|--------|-------|----------------|
| Shell | `powershell_exec` | ✅ |
| Packages | `winget_list` · `winget_install` · `winget_uninstall` · `winget_update` | install/uninstall/update ✅ |
| Files | `fs_list` · `fs_search` · `fs_read` · `fs_disk_usage` | read-only |
| Diagnostics | `diag_processes` · `diag_services` · `diag_eventlog` · `diag_autostart` | read-only |
| Network | `net_config` · `net_dns_flush` · `net_adapter_reset` | dns_flush/adapter_reset ✅ |
| Screen | `screen_capture` | read-only |
| Telemetry | `telemetry_collect` | read-only |
| Agent mgmt | `agent_update` | ✅ |
| Server-only | `list_agents` · `select_agent` · `fleet_overview` · `agent_health` · `agent_snapshot` | read-only |

### The local kill switch (endpoint user)

The person sitting at a managed PC can switch remote control **off** at any time from
the kenny tray icon (notification area) → **Fernsteuerung aktiv**. While off, the agent
refuses every state-changing tool above (the ✅ rows) and a forwarded call comes back
with `error.code = "disabled"`; **telemetry and all read-only tools keep working**, so
the fleet view stays live. Remote control is **on by default** and the choice persists
across restarts. The tray icon shows the state at a glance (normal Kenny = on, greyed
with a red slash = off). To re-enable, open the menu and toggle it back on. See
[ADR-0010](adr/0010-local-remote-control-kill-switch.md).

## Adding a PC to the fleet

From the **Add a PC** panel (left of the console), **installer** gives you a ZIP (the agent binary +
an `install.bat` pre-filled with the server URL, the agent's id, and a freshly minted token). Or use
**share link** to send the target user a one-time, expiring download link they can open without your
login. For a PC that already exists, the drill-down's **reinstall** / **re-share** buttons do the
same for that agent id (rotating its token, so the old install stops reporting).

```mermaid
sequenceDiagram
  actor Op as Operator
  participant UI as Dashboard
  participant User as Target user (Windows)
  participant Agent as kenny-agent
  participant Server as kenny-server
  Op->>UI: download installer / share link
  UI-->>Op: ZIP (exe + install.bat + token)
  Op-->>User: send link / ZIP
  User->>Agent: run install.bat (as Admin)
  Agent->>Agent: register as auto-start Windows service
  Agent->>Server: connect (WSS) + register (token)
  Agent-->>Server: first telemetry snapshot
  Server-->>UI: new agent appears online
```

The installer registers the agent as an **auto-starting Windows service** (restart-on-failure), so
it survives reboots. To remove it: `kenny-agent.exe uninstall`.

## Updating an agent

**update agent** pushes a server-triggered self-update. The agent downloads the new binary from the
server, verifies its SHA-256 **before** swapping, then a helper stops the service, replaces the
binary (with rollback on failure), and restarts it.

```mermaid
sequenceDiagram
  actor Op as Operator
  participant Server as kenny-server
  participant Agent as kenny-agent (service)
  Op->>Server: update agent
  Server->>Agent: agent_update {version, url, sha256}
  Agent->>Server: download new binary (url)
  Agent->>Agent: verify sha256, stage
  Agent-->>Server: {ok, staged_version}
  Agent->>Agent: helper: stop → swap → restart
  Agent->>Server: reconnect on new version
```

## Good habits for operators

- Treat `powershell_exec` and the `winget`/`net` write tools as real admin power — confirm
  deliberately. Screenshots and `fs_read` can expose private content; use them sparingly.
- Telemetry summaries come from the agent; the dashboard is for **your** family's machines only.
- If a PC shows `crit`, open it and read the section reason before acting.
- Rotate a PC's token (re-download the installer) if you suspect a leaked token.

See [`setup.md`](setup.md) for hosting, TLS, environment variables, and releases.

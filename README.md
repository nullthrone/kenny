# kenny

Self-hosted remote administration **and fleet monitoring** for Windows PCs in a family
setting, operated through **Claude** (MCP) and a web **dashboard**.

```
Operator ─> Claude ─MCP(HTTPS)─> kenny-server (cloud) ─WSS tunnel─> kenny-agent (Windows PC)
                                      │                                 ├─ PowerShell / Win32 / winget
Operator ─HTTPS dashboard ────────────┘                                 ├─ filesystem, screenshot
                                                                        └─ telemetry collectors
```

- **kenny-server** (Python / FastMCP): stable MCP endpoint for Claude, the agent tunnel,
  the telemetry store (SQLite), and the operator dashboard. One ASGI app, one port.
- **kenny-agent** (Rust, single binary): runs on each Windows PC, dials **out** to the
  server (NAT/firewall friendly), executes tool calls in the user's session, and pushes
  periodic health snapshots.

## How it fits together

- The agent⇄server **wire contract** is the single source of truth:
  [`docs/protocol.md`](docs/protocol.md) + [`docs/fixtures/`](docs/fixtures). Both
  components round-trip the golden fixtures so they cannot drift.
- Why things are the way they are: architecture decisions in
  [`docs/adr/`](docs/adr) (MADR).
- Coding conventions for agents/humans: [`CLAUDE.md`](CLAUDE.md) and the per-component
  `CLAUDE.md` files (deliberately free of architecture/contract duplication).

## Develop

```bash
# server
cd kenny-server && pip install -e ".[dev]" && pytest

# agent (builds on Linux too, via cfg fallbacks)
cd kenny-agent && cargo test && cargo build
```

Helper commands inside Claude Code: `/new-adr`, `/add-tool`, `/add-collector`,
`/contract-check`, `/e2e`.

## Authentication

- **Agent → server:** per-agent token in the `register` frame (`KENNY_AGENT_TOKENS`).
- **Operator → server:** one operator bearer token (`KENNY_OPERATOR_TOKEN`) gates the
  MCP endpoint, the `/api` routes, and the web UI (browser logs in at `/login`). Claude
  sends `Authorization: Bearer <token>`. See `docs/adr/0008-operator-authentication.md`.
- **Server → agent:** TLS — run the server behind **`wss`/`https`** in production; the
  agent dials a known `wss://…/agent/ws` URL. `ws://` and the dev token fallbacks are
  for local use only.

## Status

Early bootstrap. The wire contract and project skeleton exist; the two components are
implemented against the contract. Operator and agent auth are in place (single-token,
dev-grade); per-identity auth, rotation, TLS hardening, and packaging come later
(see `docs/adr/`).

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

## Status

Early bootstrap. The wire contract and project skeleton exist; the two components are
implemented against the contract. Auth/TLS hardening and packaging come later
(see `docs/adr/`).

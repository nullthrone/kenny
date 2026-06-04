# kenny

kenny is a self-hosted remote-admin and fleet-monitoring system for Windows PCs in a
family setting, operated through Claude (MCP) and a web dashboard.

## Why is it built this way?

Architecture and rationale live in **`docs/adr/`** (MADR). Read them there — they are
not duplicated here. Start with `docs/adr/0001-use-madr-and-record-decisions.md`.

## Repo map

- `docs/protocol.md` + `docs/fixtures/` — the agent⇄server wire **contract** (single
  source of truth). Frame and tool schemas live here, nowhere else.
- `kenny-server/` — Python/FastMCP server: MCP endpoint for Claude, agent tunnel,
  telemetry store, web dashboard. See `kenny-server/CLAUDE.md`.
- `kenny-agent/` — Rust single-binary agent on the Windows PC. See `kenny-agent/CLAUDE.md`.
- `docs/adr/` — architecture decisions.
- `.claude/` — subagents, slash commands, skills.

## Invariants (do not violate)

- **The contract is authoritative.** Never change frame/tool shapes in only one
  language. Change `docs/protocol.md` + `docs/fixtures/` first, then both sides.
- **Python and Rust must not drift.** Both round-trip the golden fixtures; run
  `/contract-check` after touching the protocol.
- **Record significant decisions as an ADR** (`/new-adr`). Architecture explanations
  belong in ADRs, not in any CLAUDE.md.
- **`#[cfg(windows)]` discipline** in the agent: Windows-only code is gated and has a
  portable fallback so CI/dev on Linux stays green.
- **This file carries no volatile or redundant content.** If a line can go stale when
  code or architecture changes, it belongs at its source (ADR or contract), not here.

## Build & test

- Server: `cd kenny-server && pytest`
- Agent: `cd kenny-agent && cargo test && cargo build`
- End-to-end smoke test: `/e2e`

## Skills & commands

`/new-adr`, `/add-tool`, `/add-collector`, `/contract-check`, `/e2e`, `/security-audit`. Skills:
`kenny-protocol`, `kenny-add-capability`, `kenny-telemetry`.

# kenny

kenny is a self-hosted remote-admin and fleet-monitoring system for Windows PCs in a
family setting, operated through Claude (MCP) and a web dashboard.

## Language

Respond to the architect in the language they write in, but keep everything committed to
the repository strictly in English — code, comments, docs, commit messages, PR titles and
bodies, and identifiers.

## Why is it built this way?

Architecture and rationale live in **`docs/adr/`** (MADR). Read them there — they are
not duplicated here. Start with `docs/adr/0001-use-madr-and-record-decisions.md`.

## When (not) to write an ADR

Write an ADR when a decision is **architectural** — hard to reverse, cross-cutting, or
moving a structural boundary: language/runtime choices, the wire contract or
`PROTOCOL_VERSION`, the network/trust topology, the auth model, the storage/observability
model, the deployment/distribution shape, or the agent/session model.

Do **not** write an ADR for an **implementation detail** — a localized coding choice, a
bug fix, a refactor, dashboard/UI layout, a test/CI tweak, naming, or anything contained
to one file/component that leaves the contract and the boundaries unchanged. Record those
in the commit message and, where it helps a reader, a code comment.

Rule of thumb: if it touches `docs/protocol.md`/fixtures, moves both server and agent at
once, or a maintainer would be surprised to find it silently reverted — it's an ADR. If
reverting it is a routine pull request, it isn't.

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
- **Record architectural decisions as an ADR** (`/new-adr`) — see *When (not) to write an
  ADR* above. Architecture explanations belong in ADRs, not in any CLAUDE.md.
- **`#[cfg(windows)]` discipline** in the agent: Windows-only code is gated and has a
  portable fallback so CI/dev on Linux stays green.
- **This file carries no volatile or redundant content.** If a line can go stale when
  code or architecture changes, it belongs at its source (ADR or contract), not here.

## Build & test

- Server: `cd kenny-server && pytest`
- Agent: `cd kenny-agent && cargo test && cargo build`
- End-to-end smoke test: `/e2e`

## Skills & commands

`/new-adr`, `/add-tool`, `/add-collector`, `/contract-check`, `/e2e`, `/security-review`. Skills:
`kenny-protocol`, `kenny-add-capability`, `kenny-telemetry`.

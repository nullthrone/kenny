# kenny

kenny is a self-hosted remote-admin, fleet-monitoring and ticketing system for the Windows
and Linux machines you administer, operated through Claude (MCP), a web dashboard, and an
optional Discord bot.

## Language

Respond to the architect in the language they write in, but keep everything committed to
the repository strictly in English — code, comments, docs, commit messages, PR titles and
bodies, and identifiers.

## Why is it built this way?

Architecture and rationale live in **`docs/adr/`** (MADR). Read them there — they are
not duplicated here. Start with `docs/adr/0001-use-madr-and-record-decisions.md`.

## When (not) to write an ADR

An ADR is the only kind of record kenny keeps. Write one when the change moves a structural
boundary — language/runtime, the wire contract's shape, the network/trust topology, the auth
model, the storage/observability model, the deployment/distribution shape, the agent/session
model — and name it in the record's `Boundary moved:` line.

If you cannot name one it is not an ADR: a localized choice, a bug fix, a refactor, UI
layout, a test/CI tweak, naming, a dependency and an additive contract field all belong in
the code and the commit message. Records are ~1 page, numbered gap-free `0001..N`.

## Repo map

- `docs/protocol.md` + `docs/fixtures/` — the agent⇄server wire **contract** (single
  source of truth). Frame and tool schemas live here, nowhere else.
- `kenny-server/` — Python/FastMCP server: MCP endpoint for Claude, agent tunnel,
  telemetry store, web dashboard. See `kenny-server/CLAUDE.md`.
- `kenny-agent/` — Rust single-binary agent on each managed host. See `kenny-agent/CLAUDE.md`.
- `docs/adr/` — architecture decisions.
- `.claude/` — subagents, slash commands, skills.

## Invariants (do not violate)

- **The contract is authoritative.** Never change frame/tool shapes in only one
  language. Change `docs/protocol.md` + `docs/fixtures/` first, then both sides.
- **Python and Rust must not drift.** Both round-trip the golden fixtures; run
  `/contract-check` after touching the protocol.
- **Every seam two places must agree on gets a test that fails when they diverge.**
  The contract is the best-known seam, not the only one: the server's tool tiers against
  the agent's `is_mutating`, the runtime extras in `pyproject.toml` against what the
  `Dockerfile` installs, a module against the interface its caller assumed. Test the seam
  **joined** — a test that passes because the other half is not wired up yet is not
  evidence that the two halves fit.
- **Record architectural decisions as an ADR** (`/new-adr`) — see *When (not) to write an
  ADR* above. Architecture explanations belong in ADRs, not in any CLAUDE.md.
- **`#[cfg(windows)]` discipline** in the agent: Windows-only code is gated and has a
  portable fallback so CI/dev on Linux stays green.
- **Every document states what holds now.** Write each line so it reads correctly to
  someone who does not know what it replaced: the rule, the constraint, the trade-off
  that still binds. What changed and why it moved is the commit message's job, and for a
  boundary the ADR's. A line that can go stale when code or architecture changes belongs
  at its source (ADR or contract), not in a CLAUDE.md.

## Build & test

- Dashboard: `npm --prefix kenny-web install && npm --prefix kenny-web run build` (Vite + React + TypeScript);
  `npm --prefix kenny-web run typecheck` and `npm --prefix kenny-web test` for checks.
  Contributors only — end users get the prebuilt UI shipped in the server.
- Server: `cd kenny-server && pytest` — exactly that; `python -m pytest` adds the working
  directory to `sys.path` and hides import errors CI will hit.
- Agent: `cd kenny-agent && cargo test && cargo build`
- End-to-end smoke test: `/e2e`

## Skills & commands

`/new-adr`, `/add-tool`, `/add-collector`, `/contract-check`, `/e2e`, `/security-review`. Skills:
`kenny-protocol`, `kenny-add-capability`, `kenny-telemetry`.

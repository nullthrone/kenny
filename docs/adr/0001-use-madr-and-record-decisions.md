# 0001. Record architecture decisions using MADR

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

kenny is built "Claude-first": coding agents and humans need a durable, single
place to learn *why* the system is shaped the way it is. `CLAUDE.md` files are
deliberately kept free of architecture and volatile detail, so that rationale must
live somewhere stable and versioned.

## Considered Options

- MADR (Markdown Any Decision Records) in `docs/adr/`
- A single growing `ARCHITECTURE.md`
- Only commit messages / PR descriptions

## Decision Outcome

Chosen option: "MADR in `docs/adr/`", because each decision is an immutable,
reviewable unit with context and consequences, and superseding is explicit.

### Consequences

- Good, because `CLAUDE.md` can simply point here for all "why" questions.
- Good, because decisions are diffable and survive refactors.
- Bad, because it adds the discipline of writing an ADR for significant changes
  (mitigated by the `/new-adr` slash command).

## More Information

Architecture and rationale are read from these ADRs, never duplicated in `CLAUDE.md`.

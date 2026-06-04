# 0005. Contract-first development with golden fixtures

- Status: accepted
- Date: 2026-06-04

## Context and Problem Statement

The wire protocol spans Python and Rust ([ADR-0002](0002-python-server-rust-agent.md)).
We want to build both components in parallel without the two drifting apart, and we
want a cheap, automatable way to catch drift.

## Considered Options

- Prose spec only (`protocol.md`), trust both sides to match
- Code-generate types from a schema (e.g. JSON Schema / protobuf)
- Prose spec + hand-written **golden JSON fixtures** both sides round-trip

## Decision Outcome

Chosen option: "Prose spec + golden fixtures". `docs/protocol.md` is authoritative
prose; `docs/fixtures/*.json` are canonical frames that both `protocol.py` and
`protocol.rs` parse, re-serialize, and assert equal. This is the executable contract
and lets each side develop against a mock counterpart.

### Consequences

- Good, because parallel work is safe: agree on fixtures, then build independently.
- Good, because drift fails a test (and `/contract-check`) instead of failing in prod.
- Bad, because adding a tool/section means touching fixtures first (intended friction).

## More Information

Codegen was rejected for v0.1 to keep the toolchain simple; revisit if the schema
grows large. Enforced by the `contract-guard` subagent and `/contract-check`.

---
name: kenny-protocol
description: How to read and safely extend the kenny agent⇄server wire contract. Use when touching frames, tool schemas, or telemetry sections, or when server and agent disagree.
---

# kenny wire protocol

The contract is **`docs/protocol.md` + `docs/fixtures/`**. It is the single source of
truth; `protocol.py` (Pydantic) and `protocol.rs` (serde) are implementations of it.

## Invariants

- One JSON object per WebSocket message. Frame types: `register`, `request`, `response`,
  `telemetry`, `ping`, `pong`.
- `request`/`response` are correlated by `id` (server-generated UUID).
- `telemetry` is **pushed** by the agent, not requested. `telemetry.collect` returns the
  same snapshot shape on demand.
- Every telemetry section payload carries `status` ∈ {ok, warn, crit} and a `summary`
  string. Health thresholds are authoritative on the server (`health_rules.py`).
- Error codes: `timeout`, `not_found`, `exec_failed`, `unsupported`, `bad_args`, `internal`.

## Changing the contract (synchronization point)

1. Edit `docs/protocol.md` and add/adjust fixtures in `docs/fixtures/`.
2. Update **both** `protocol.py` and `protocol.rs`; keep field names and optionality identical.
3. Bump `PROTOCOL_VERSION` on any breaking change.
4. Run `/contract-check`. Never let one language change shape without the other.

A contract change is owned by the orchestrator, not by a single component subagent.

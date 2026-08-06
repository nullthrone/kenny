---
description: Add a capability tool end-to-end (contract → fixtures → server → agent → tests)
argument-hint: <tool.name>
---

Add the capability tool **$ARGUMENTS** across all layers, in this order (contract first):

1. **Contract** — add the tool to the catalog table in `docs/protocol.md` with its `args`
   and `result` shape. This is a contract change; note it for the version bump.
2. **Fixtures** — add a `request_*.json` and `response_*.json` under `docs/fixtures/` and
   list them in `docs/fixtures/README.md`.
3. **Server** — register the MCP tool in `kenny-server/kenny_server/tools.py` (requires
   an explicit per-call `agent_id` per ADR-0038, forwards a `request` frame, returns the
   `response`). Classify it in `control.rs::is_mutating` and, if it is state-changing,
   in `chat.py`'s confirm-gate — the two must stay in lockstep (ADR-0023).
4. **Agent** — add a handler in `kenny-agent/src/handlers/` and wire it in `dispatch.rs`,
   with `#[cfg(windows)]` real impl + `#[cfg(not(windows))]` fallback.
5. **Tests** — fixture round-trip on both sides; a server test via the mock agent.
6. Run `/contract-check`, then `pytest` and `cargo test`.

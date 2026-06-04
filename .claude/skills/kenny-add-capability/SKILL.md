---
name: kenny-add-capability
description: Step-by-step recipe to add a new remote-admin capability tool to kenny across contract, server, and agent. Use whenever adding or changing a tool like powershell.exec, fs.*, winget.*, diag.*, net.*.
---

# Add a capability tool

A capability tool is a single named action Claude can invoke that the server forwards to
the active agent. Adding one touches three layers; always go **contract → server → agent**.
The `/add-tool <name>` command automates the sequence; this skill explains the pattern.

## Layers

1. **Contract** (`docs/protocol.md` catalog + `docs/fixtures/`): define `args` and the
   `result` sketch; add `request_<tool>.json` / `response_<tool>.json` fixtures.
2. **Server** (`kenny-server/kenny_server/tools.py`): register an MCP tool with the exact
   catalog name. It must require an active agent (`select_agent`), build a `request` frame,
   await the correlated `response`, and surface `error` as a tool error.
3. **Agent** (`kenny-agent/src/handlers/<tool>.rs` + `dispatch.rs`): implement the handler
   keyed by the tool name. Real impl behind `#[cfg(windows)]` (usually PowerShell via
   `std::process::Command` or `windows-rs`); `#[cfg(not(windows))]` fallback returns either
   a portable result or `error.code = "unsupported"`.

## Conventions

- Tool names are dotted and identical everywhere (`winget.install`, not `wingetInstall`).
- Long-running actions respect a `timeout_s` arg and return `error.code = "timeout"`.
- Keep results structured (objects), not pre-formatted strings, so the UI/Claude can render.

## Done when

Fixtures round-trip on both sides, a server test exercises the tool via the mock agent,
`/contract-check` is clean, and `pytest` + `cargo test` pass.

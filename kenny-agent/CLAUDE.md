# kenny-agent conventions

Rust, async (tokio), ships as a single binary. Opens one outbound WSS connection to
the server and never listens. The wire contract is `../docs/protocol.md` +
`../docs/fixtures/` — read it there, do not restate schemas here.

## Conventions

- `protocol.rs` holds `serde` structs/enums for every frame and has round-trip tests
  against `../docs/fixtures/`. Frame shapes change only when the contract changes.
- A capability is a handler in `handlers/` keyed by the tool name from the catalog.
  `dispatch.rs` maps `tool` → handler. Unknown/unsupported tools return
  `error.code = "unsupported"`.
- Telemetry: `telemetry/scheduler.rs` pushes on a timer (default 900 s); each file in
  `telemetry/collectors/` produces one section with `status` + `summary` + raw fields.
- **`#[cfg(windows)]` discipline:** Windows-only code (PowerShell/CIM/WMI, `windows-rs`)
  is gated; provide a `#[cfg(not(windows))]` fallback (e.g. `sysinfo`, `sh`, or an
  `unsupported` result) so `cargo test`/`cargo build` pass on Linux CI.
- Format/lint with `rustfmt` + `clippy`.

## Testing without the real server

Round-trip the fixtures and/or run against a **mock server** that sends `request`
frames and asserts `response`/`telemetry` frames. The Python server is not needed for
agent tests.

## Don't

- Don't put architecture rationale here — that's `../docs/adr/`.
- Don't copy the tool/frame schemas here — that's the contract.

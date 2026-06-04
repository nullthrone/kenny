---
description: Add a telemetry collector end-to-end (section schema → fixture → agent collector → health rule → dashboard)
argument-hint: <section_name>
---

Add the telemetry section **$ARGUMENTS** across all layers, contract first:

1. **Contract** — document the section under `docs/protocol.md` § Telemetry sections,
   including its raw fields (besides the required `status` + `summary`).
2. **Fixture** — add the section to `docs/fixtures/telemetry_snapshot.json` (or a dedicated
   `telemetry_<section>.json`) so both sides can round-trip it.
3. **Agent** — add `kenny-agent/src/telemetry/collectors/<section>.rs` returning
   `status` + `summary` + raw fields, gated with `#[cfg(windows)]` and a portable fallback;
   register it with the scheduler.
4. **Server** — add a threshold in `kenny-server/kenny_server/health_rules.py` and make sure
   `store.py` persists the section and the dashboard surfaces it.
5. **Tests** — fixture round-trip + a health-rule unit test.
6. Run `/contract-check`, then `pytest` and `cargo test`.

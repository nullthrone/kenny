---
description: Verify kenny-server and kenny-agent both match docs/protocol.md and the golden fixtures
---

Check the wire contract is honored on both sides. Do not change the contract.

1. Run the fixture round-trip tests:
   - `cd kenny-server && pytest -k fixtures -q`
   - `cd kenny-agent && cargo test fixtures -q`
2. Cross-check the catalog in `docs/protocol.md` against `kenny-server/kenny_server/tools.py`
   and `kenny-agent/src/dispatch.rs`: every tool present on both sides, names identical.
3. Cross-check the telemetry section list against `kenny-agent/src/telemetry/collectors/`
   and `kenny-server/kenny_server/health_rules.py`.
4. Report drift / missing / cosmetic findings. If anything is out of sync, fix the
   **implementations** (never the contract) or escalate if the contract itself is wrong.

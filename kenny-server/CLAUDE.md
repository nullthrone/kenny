# kenny-server conventions

Python 3.11+, FastMCP + Starlette/uvicorn, one ASGI app on one port (MCP endpoint,
`/agent/ws`, web UI). The wire contract is `../docs/protocol.md` + `../docs/fixtures/` —
read it there, do not restate schemas here.

## Conventions

- `protocol.py` holds Pydantic models for every frame and validates against
  `../docs/fixtures/` in tests. Frame shapes change only when the contract changes.
- A capability tool is an MCP tool that requires `select_agent`, forwards a `request`
  frame to the active agent, and returns its `response`. Keep tool names identical to
  the catalog in the contract.
- Telemetry read-paths (`fleet_overview`, `agent_health`, `agent_snapshot`) read from
  `store.py`; health thresholds live only in `health_rules.py`.
- Operator auth (MCP + `/api` + UI) resolves each request to a `Principal` in `auth.py`
  (`OperatorAuthMiddleware`) — a per-user PAT (Bearer), a session cookie, or the legacy
  shared token (back-compat superuser); `/agent/ws` is exempt (agents use their own
  token). Roles/host-scope are enforced by `webui/authz.py` guards and `tools.py`. See
  ADR-0037 (supersedes ADR-0008). Don't open these surfaces without going through that
  middleware.
- Type-hint everything; keep I/O async. Format/lint with `ruff`.

## Testing without the real agent

Test against a **mock agent**: a coroutine that connects to `/agent/ws`, registers,
and replays `../docs/fixtures/` responses and a telemetry push. The Rust agent is not
needed for server tests.

## Don't

- Don't put architecture rationale here — that's `../docs/adr/`.
- Don't copy the tool/frame schemas here — that's the contract.

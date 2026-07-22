---
description: Run the local end-to-end smoke test (server + agent + one tool call)
---

Run kenny end-to-end on this machine (a Linux dev agent is fine):

1. Start `kenny-server` locally (uvicorn/FastMCP) on a free localhost port, in the background.
2. Start `kenny-agent` against `ws://localhost:<port>/agent/ws` with a dev agent id/token,
   in the background.
3. Drive the MCP endpoint (MCP client or HTTP): `list_agents` → `select_agent("dev")` →
   `shell_exec {command:"echo hi"}` (the agent registers as `os: linux` on a Linux dev
   machine, and `powershell_exec` is now `unsupported` there); assert the result contains
   `hi`.
4. Trigger `telemetry_collect` and confirm a snapshot is stored and `fleet_overview()`
   shows the agent with a health status.
5. Tear down both background processes. Report pass/fail with the captured output.

"""Real Rust<->Python end-to-end test (no mock agent).

Runs the composed server on uvicorn and spawns the **actual** compiled
`kenny-agent` binary, which dials `/agent/ws`, registers, serves a forwarded
`powershell.exec` (the agent's `sh` fallback on Linux), and pushes a telemetry
snapshot. Asserts the round-trip through the real wire protocol on both sides.

Skipped unless the agent binary is available: set ``KENNY_AGENT_BIN`` to its path,
or build it (``cd kenny-agent && cargo build``) so the default debug path exists.
Set ``KENNY_E2E=1`` to force-fail (instead of skip) when the binary is missing.
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kenny_server.main import build_app

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BIN = REPO_ROOT / "kenny-agent" / "target" / "debug" / "kenny-agent"


def _agent_bin() -> Path | None:
    env = os.environ.get("KENNY_AGENT_BIN")
    candidate = Path(env) if env else _DEFAULT_BIN
    return candidate if candidate.exists() else None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_Server":
        self._task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.should_exit = True
        if self._task is not None:
            await self._task


@pytest.mark.asyncio
async def test_real_agent_end_to_end(tmp_path) -> None:
    binary = _agent_bin()
    if binary is None:
        msg = f"agent binary not found (looked at {_DEFAULT_BIN}); build with `cargo build`"
        if os.environ.get("KENNY_E2E") == "1":
            pytest.fail(msg)
        pytest.skip(msg)

    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e.sqlite"))
    token = app.state.operator_token

    async with _Server(app, port):
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--server",
            f"ws://127.0.0.1:{port}/agent/ws",
            "--agent-id",
            "dev",
            "--token",
            "dev-token",
            "--telemetry-interval-secs",
            "2",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "RUST_LOG": "warn"},
        )
        try:
            # Wait for the real agent to dial in and register.
            registry = app.state.registry
            for _ in range(100):  # ~10s
                agent = registry.get("dev")
                if agent is not None and agent.online:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("real agent did not register within 10s")

            transport = StreamableHttpTransport(
                f"http://127.0.0.1:{port}/mcp/mcp",
                headers={"Authorization": f"Bearer {token}"},
            )
            async with Client(transport) as client:
                await client.call_tool("select_agent", {"id": "dev"})
                res = await client.call_tool(
                    "powershell.exec",
                    {"args": {"script": "echo hi", "timeout_s": 20}},
                )
                # The Linux fallback runs `sh -c "echo hi"`.
                assert res.data["exit_code"] == 0
                assert "hi" in res.data["stdout"]

                # The agent pushes telemetry on its first tick; wait for it.
                for _ in range(50):  # ~10s
                    fleet = (await client.call_tool("fleet_overview", {})).data
                    dev = next(
                        (a for a in fleet["agents"] if a["agent_id"] == "dev"), None
                    )
                    if dev is not None and dev["collected_at"]:
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise AssertionError("no telemetry snapshot arrived from the agent")

                assert dev["online"] is True
                # Real Linux collectors produce a valid overall health (any level).
                assert dev["overall"] in ("ok", "warn", "crit")
        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

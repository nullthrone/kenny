"""End-to-end test with a mock agent (no real Rust agent needed).

Runs the composed ASGI app on uvicorn on an ephemeral port, connects a mock
agent over the ``/agent/ws`` WebSocket that registers as ``dev`` and replays
fixture responses plus one telemetry push, then drives the MCP tools via the
FastMCP HTTP client:

* ``select_agent`` + a forwarded ``powershell_exec`` (assert the result), and
* push telemetry, then assert ``fleet_overview`` shows the agent.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
import uvicorn
import websockets
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kenny_server.main import build_app

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


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


class MockAgent:
    """Connects to /agent/ws, registers, and replays fixtures on demand."""

    def __init__(self, ws_url: str, agent_id: str, token: str) -> None:
        self.ws_url = ws_url
        self.agent_id = agent_id
        self.token = token
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.ws = await websockets.connect(self.ws_url)
        await self.ws.send(
            json.dumps(
                {
                    "type": "register",
                    "agent_id": self.agent_id,
                    "token": self.token,
                    "meta": {"hostname": "DEV-PC", "os": "linux", "version": "0.1.0"},
                }
            )
        )
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            frame = json.loads(raw)
            if frame.get("type") == "request":
                await self._handle_request(frame)
            elif frame.get("type") == "ping":
                await self.ws.send(json.dumps({"type": "pong"}))

    async def _handle_request(self, frame: dict) -> None:
        assert self.ws is not None
        tool = frame["tool"]
        if tool == "powershell_exec":
            result = _fixture("response_powershell_exec.json")["result"]
        elif tool == "telemetry_collect":
            result = _fixture("telemetry_snapshot.json")["snapshot"]
        else:
            await self.ws.send(
                json.dumps(
                    {
                        "type": "response",
                        "id": frame["id"],
                        "ok": False,
                        "error": {"code": "unsupported", "message": tool},
                    }
                )
            )
            return
        await self.ws.send(
            json.dumps({"type": "response", "id": frame["id"], "ok": True, "result": result})
        )

    async def push_telemetry(self) -> None:
        assert self.ws is not None
        frame = _fixture("telemetry_snapshot.json")
        frame["agent_id"] = self.agent_id
        await self.ws.send(json.dumps(frame))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.ws is not None:
            await self.ws.close()


@pytest.mark.asyncio
async def test_e2e_forward_and_telemetry(tmp_path) -> None:
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev", "dev-token")
        await agent.start()
        # Give the server a moment to process registration.
        await asyncio.sleep(0.1)

        # The MCP endpoint now requires the operator bearer token.
        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            tools = {t.name for t in await client.list_tools()}
            assert "powershell_exec" in tools
            assert "select_agent" in tools
            assert "fleet_overview" in tools

            # Select the agent and forward a powershell_exec call.
            await client.call_tool("select_agent", {"id": "dev"})
            res = await client.call_tool(
                "powershell_exec", {"args": {"script": "Get-Process", "timeout_s": 30}}
            )
            assert res.data["exit_code"] == 0
            assert "Handles" in res.data["stdout"]

            # Push telemetry, then assert fleet_overview shows the agent + crit health.
            await agent.push_telemetry()
            await asyncio.sleep(0.15)
            fleet = (await client.call_tool("fleet_overview", {})).data
            ids = {a["agent_id"] for a in fleet["agents"]}
            assert "dev" in ids
            dev = next(a for a in fleet["agents"] if a["agent_id"] == "dev")
            assert dev["online"] is True
            assert dev["overall"] == "crit"
            assert "defender" in dev["flagged_sections"]

        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_bad_token_rejected(tmp_path, caplog) -> None:
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e2.sqlite"))
    async with _Server(app, port):
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/agent/ws")
        await ws.send(
            json.dumps(
                {
                    "type": "register",
                    "agent_id": "dev",
                    "token": "WRONG",
                    "meta": {"hostname": "X", "os": "linux", "version": "0.1.0"},
                }
            )
        )
        with pytest.raises(websockets.ConnectionClosed):
            await ws.recv()
    # The handshake now logs the rejection instead of failing silently (issue #10).
    assert any(
        "auth failed for agent" in r.getMessage() for r in caplog.records
    )


async def _register_once(ws_url: str, agent_id: str, token: str) -> bool:
    """Open /agent/ws, send one register frame, return True if it stays open.

    A successful handshake leaves the socket open; an ``AuthError`` closes it
    with 4401. We probe by sending a ping and seeing whether a pong comes back.
    """

    ws = await websockets.connect(ws_url)
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "register",
                    "agent_id": agent_id,
                    "token": token,
                    "meta": {"hostname": "X", "os": "linux", "version": "0.1.0"},
                }
            )
        )
        await ws.send(json.dumps({"type": "ping"}))
        try:
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            return False
        return reply.get("type") == "pong"
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_e2e_rotation_grace_window_keeps_live_agent(tmp_path) -> None:
    """Rotating an installer token must not instantly brick a live agent (#10)."""

    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e3.sqlite"))
    ws_url = f"ws://127.0.0.1:{port}/agent/ws"
    async with _Server(app, port):
        # The agent is provisioned and connected with its current token.
        t1 = await app.state.token_store.create_or_rotate("thomas-pc")
        assert await _register_once(ws_url, "thomas-pc", t1) is True

        # Operator generates a new installer -> token rotates server-side.
        t2 = await app.state.token_store.create_or_rotate("thomas-pc")

        # The live agent, still holding t1, reconnects and is NOT locked out.
        assert await _register_once(ws_url, "thomas-pc", t1) is True

        # Once the new installer is deployed and t2 is used, t1 is retired.
        assert await _register_once(ws_url, "thomas-pc", t2) is True
        assert await _register_once(ws_url, "thomas-pc", t1) is False

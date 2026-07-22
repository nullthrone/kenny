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
import base64
import json
import socket
from pathlib import Path

import pytest
import uvicorn
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kenny_server.keystore import build_transcript
from kenny_server.main import build_app

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"
VECTORS = json.loads((FIXTURES_DIR / "vectors" / "mutual_auth.json").read_text())
# A deterministic server seed so the mock agent can pin the public key.
SERVER_SEED_B64 = VECTORS["server_seed_b64"]
SERVER_PUBLIC_KEY_B64 = VECTORS["server_public_key_b64"]


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
    """Connects to /agent/ws and replays fixtures on demand.

    Performs the full v0.8 mutual-auth handshake by default (register +
    client_nonce + protocol, verify the server's challenge, send a signed auth);
    set ``signed=False`` to use the legacy token path (migration window).
    """

    def __init__(
        self,
        ws_url: str,
        agent_id: str,
        token: str = "",
        *,
        signed: bool = True,
        private_key: Ed25519PrivateKey | None = None,
        server_public_key_b64: str = SERVER_PUBLIC_KEY_B64,
        bad_sig: bool = False,
        os: str = "windows",
    ) -> None:
        self.ws_url = ws_url
        self.agent_id = agent_id
        self.token = token
        self.signed = signed
        self.bad_sig = bad_sig
        self.os = os
        self._private_key = private_key or Ed25519PrivateKey.generate()
        self._server_pub = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(server_public_key_b64)
        )
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None

    @property
    def public_key_b64(self) -> str:
        pub = self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(pub).decode()

    async def start(self) -> None:
        self.ws = await websockets.connect(self.ws_url)
        if self.signed:
            await self._signed_handshake()
        else:
            await self.ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "agent_id": self.agent_id,
                        "token": self.token,
                        "meta": {"hostname": "DEV-PC", "os": self.os, "version": "0.1.0"},
                    }
                )
            )
        self._task = asyncio.create_task(self._loop())

    async def _signed_handshake(self) -> None:
        assert self.ws is not None
        client_nonce = b"\x11" * 32
        await self.ws.send(
            json.dumps(
                {
                    "type": "register",
                    "agent_id": self.agent_id,
                    "protocol": "0.8",
                    "client_nonce": base64.b64encode(client_nonce).decode(),
                    "meta": {"hostname": "DEV-PC", "os": self.os, "version": "0.1.0"},
                }
            )
        )
        challenge = json.loads(await self.ws.recv())
        assert challenge["type"] == "challenge", challenge
        server_nonce = base64.b64decode(challenge["server_nonce"])
        transcript = build_transcript(self.agent_id, client_nonce, server_nonce)
        # Anti-spoofing: verify the server's signature against the pinned key.
        self._server_pub.verify(base64.b64decode(challenge["server_sig"]), transcript)
        if self.bad_sig:
            agent_sig = base64.b64encode(b"\x00" * 64).decode()
        else:
            agent_sig = base64.b64encode(self._private_key.sign(transcript)).decode()
        await self.ws.send(json.dumps({"type": "auth", "agent_sig": agent_sig}))

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
        elif tool == "shell_exec":
            result = _fixture("response_shell_exec.json")["result"]
        elif tool == "telemetry_collect":
            result = _fixture("telemetry_snapshot.json")["snapshot"]
        elif tool == "screen_capture":
            # A real full-screen PNG, base64-encoded, routinely runs a few MB —
            # far past the telemetry cap but within the absolute frame ceiling.
            result = {"image_b64": "A" * (4 * 1024 * 1024), "format": "png"}
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

    async def push_telemetry_with_arch(self, arch: str) -> None:
        """Push telemetry carrying an ``os_support.arch`` field (ADR-0040).

        The golden fixture has no ``os_support`` section, so inject a minimal one
        — only ``status``/``summary``/``arch`` matter for this path.
        """

        assert self.ws is not None
        frame = _fixture("telemetry_snapshot.json")
        frame["agent_id"] = self.agent_id
        frame["snapshot"]["os_support"] = {
            "status": "ok",
            "summary": "test",
            "arch": arch,
        }
        await self.ws.send(json.dumps(frame))

    async def push_oversized_telemetry(self) -> None:
        """Send a telemetry frame whose payload exceeds the server's size cap."""

        assert self.ws is not None
        frame = _fixture("telemetry_snapshot.json")
        frame["agent_id"] = self.agent_id
        # Inflate a section summary past KENNY_MAX_TELEMETRY_BYTES (but well under
        # the absolute frame ceiling, so it is parsed and then dropped by kind).
        section = next(iter(frame["snapshot"]))
        frame["snapshot"][section]["summary"] = "x" * (600 * 1024)
        await self.ws.send(json.dumps(frame))

    async def push_huge_telemetry(self) -> None:
        """Send a telemetry frame past the absolute frame ceiling (dropped pre-parse)."""

        assert self.ws is not None
        frame = _fixture("telemetry_snapshot.json")
        frame["agent_id"] = self.agent_id
        # Exceed KENNY_MAX_FRAME_BYTES (8 MiB): rejected before parsing.
        section = next(iter(frame["snapshot"]))
        frame["snapshot"][section]["summary"] = "x" * (9 * 1024 * 1024)
        await self.ws.send(json.dumps(frame))

    async def push_log(self) -> None:
        assert self.ws is not None
        frame = _fixture("log.json")
        frame["agent_id"] = self.agent_id
        await self.ws.send(json.dumps(frame))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.ws is not None:
            await self.ws.close()


@pytest.mark.asyncio
async def test_e2e_forward_and_telemetry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        # Enroll the agent's public key before the signed handshake.
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        # Give the server a moment to process registration.
        await asyncio.sleep(0.1)

        # The MCP endpoint now requires the operator bearer token.
        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            tools = {t.name for t in await client.list_tools()}
            assert "powershell_exec" in tools
            assert "shell_exec" in tools
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
async def test_e2e_shell_exec_forwards_on_linux_agent(tmp_path, monkeypatch) -> None:
    """``shell_exec`` forwards normally to a Linux-registered agent."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "shell.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev", os="linux")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            await client.call_tool("select_agent", {"id": "dev"})
            res = await client.call_tool(
                "shell_exec", {"args": {"command": "uname -a", "timeout_s": 30}}
            )
            assert res.data["exit_code"] == 0

        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_os_guard_refuses_wrong_shell_tool(tmp_path, monkeypatch) -> None:
    """The server's OS guard refuses the wrong shell tool for the agent's OS
    before ever forwarding a `request` frame (docs/protocol.md § "OS-scoped
    tools"): `shell_exec` on a Windows agent, `powershell_exec` on a Linux
    agent."""

    from fastmcp.exceptions import ToolError

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "os_guard.sqlite"))

    async with _Server(app, port):
        windows_agent = MockAgent(
            f"ws://127.0.0.1:{port}/agent/ws", "win-pc", os="windows"
        )
        await app.state.key_store.enroll("win-pc", windows_agent.public_key_b64)
        await windows_agent.start()

        linux_agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "linux-pc", os="linux")
        await app.state.key_store.enroll("linux-pc", linux_agent.public_key_b64)
        await linux_agent.start()

        await asyncio.sleep(0.1)

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            # shell_exec on a Windows agent is refused, naming powershell_exec.
            await client.call_tool("select_agent", {"id": "win-pc"})
            with pytest.raises(ToolError, match="shell_exec"):
                await client.call_tool("shell_exec", {"args": {"command": "echo hi"}})

            # powershell_exec on a Linux agent is refused, naming shell_exec.
            await client.call_tool("select_agent", {"id": "linux-pc"})
            with pytest.raises(ToolError, match="powershell_exec"):
                await client.call_tool(
                    "powershell_exec", {"args": {"script": "Get-Process"}}
                )

        await windows_agent.stop()
        await linux_agent.stop()


@pytest.mark.asyncio
async def test_e2e_telemetry_arch_mirrors_into_registry(tmp_path, monkeypatch) -> None:
    """A telemetry push carrying ``os_support.arch`` (ADR-0040, protocol 0.13)
    updates the registry's stored arch for that agent — the periodic second
    channel alongside the one-time ``register.meta.arch``."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "arch.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        # register (v0.8 handshake, no arch field on this fixture) leaves the
        # backward-compat default in place.
        assert app.state.registry.get("dev").arch == "x86_64"

        await agent.push_telemetry_with_arch("aarch64")
        await asyncio.sleep(0.15)
        assert app.state.registry.get("dev").arch == "aarch64"

        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_oversized_telemetry_dropped(tmp_path, monkeypatch) -> None:
    """An oversized telemetry frame is dropped (not stored) and the connection
    survives, so a following normal push is still persisted."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "oversized.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        # Oversized frame: dropped, nothing stored.
        await agent.push_oversized_telemetry()
        await asyncio.sleep(0.15)
        assert await app.state.store.latest("dev") is None

        # The socket is still alive: a normal push afterwards is persisted.
        await agent.push_telemetry()
        await asyncio.sleep(0.15)
        assert await app.state.store.latest("dev") is not None

        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_large_screenshot_response_passes(tmp_path, monkeypatch) -> None:
    """A large ``screen_capture`` response (past the telemetry cap, within the
    absolute frame ceiling) is delivered — not dropped like an unsolicited push."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "screenshot.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            await client.call_tool("select_agent", {"id": "dev"})
            res = await client.call_tool("screen_capture", {"args": {}})
            # The multi-MB response frame round-trips instead of timing out.
            assert res.data["format"] == "png"
            assert len(res.data["image_b64"]) == 4 * 1024 * 1024

        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_huge_frame_dropped(tmp_path, monkeypatch) -> None:
    """A frame past the absolute ceiling is dropped before parsing; the socket
    survives so a following normal push is still persisted."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "huge.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        # Past the 8 MiB ceiling: dropped pre-parse, nothing stored.
        await agent.push_huge_telemetry()
        await asyncio.sleep(0.15)
        assert await app.state.store.latest("dev") is None

        # The socket is still alive: a normal push afterwards is persisted.
        await agent.push_telemetry()
        await asyncio.sleep(0.15)
        assert await app.state.store.latest("dev") is not None

        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_log_frame_persisted_and_connect_logged(tmp_path, monkeypatch) -> None:
    """An agent ``log`` frame is persisted (kind='log', source='agent'); the
    tunnel also records a server-side connect log event."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e_log.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)
        await agent.push_log()
        await asyncio.sleep(0.15)

        logs = await app.state.event_store.query(kind="log", agent_id="dev")
        assert any(
            e["source"] == "agent"
            and e["level"] == "warn"
            and e["target"] == "kenny_agent::tunnel"
            and e["message"] == "tunnel error; backing off"
            and e["fields"] == {"error": "connection reset", "backoff_secs": 4}
            for e in logs
        )

        # The server-side connect event is captured by the drain task.
        await asyncio.sleep(0.1)
        server_logs = await app.state.event_store.query(kind="log")
        assert any(
            e["source"] == "server" and "connected" in (e["message"] or "")
            for e in server_logs
        )

        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_bad_token_rejected(tmp_path, caplog, monkeypatch) -> None:
    # Legacy token path (migration window): explicitly enabled.
    monkeypatch.setenv("KENNY_ALLOW_TOKEN_AUTH", "1")
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


@pytest.mark.asyncio
async def test_e2e_legacy_token_auth_succeeds(tmp_path, monkeypatch) -> None:
    """A legacy token-only register still registers when KENNY_ALLOW_TOKEN_AUTH=1."""

    monkeypatch.setenv("KENNY_ALLOW_TOKEN_AUTH", "1")
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e_legacy.sqlite"))
    async with _Server(app, port):
        agent = MockAgent(
            f"ws://127.0.0.1:{port}/agent/ws", "dev", "dev-token", signed=False
        )
        await agent.start()
        await asyncio.sleep(0.1)
        assert app.state.registry.get("dev").online is True
        await agent.stop()


@pytest.mark.asyncio
async def test_e2e_bad_signature_rejected(tmp_path, monkeypatch) -> None:
    """An agent that sends a bad agent_sig is closed 4401 and gets no request."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e_badsig.sqlite"))
    ws_url = f"ws://127.0.0.1:{port}/agent/ws"
    async with _Server(app, port):
        key = Ed25519PrivateKey.generate()
        pub = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
        await app.state.key_store.enroll("dev", pub)

        ws = await websockets.connect(ws_url)
        client_nonce = b"\x22" * 32
        await ws.send(
            json.dumps(
                {
                    "type": "register",
                    "agent_id": "dev",
                    "protocol": "0.8",
                    "client_nonce": base64.b64encode(client_nonce).decode(),
                    "meta": {"hostname": "X", "os": "linux", "version": "0.1.0"},
                }
            )
        )
        challenge = json.loads(await ws.recv())
        assert challenge["type"] == "challenge"
        # Send a deliberately invalid agent signature.
        await ws.send(
            json.dumps({"type": "auth", "agent_sig": base64.b64encode(b"\x00" * 64).decode()})
        )
        # The socket is closed 4401; no `request` frame is ever delivered.
        with pytest.raises(websockets.ConnectionClosed) as exc_info:
            await ws.recv()
        assert exc_info.value.rcvd.code == 4401
        # The registry never marked the agent online.
        agent = app.state.registry.get("dev")
        assert agent is None or agent.online is False


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
        # On a successful handshake the server now also pushes a `policy` frame
        # (ADR-0021) before any pong; a real agent drains inbound non-request
        # frames, so skip anything that isn't the pong we're probing for.
        try:
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                if reply.get("type") == "pong":
                    return True
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            return False
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_e2e_rotation_grace_window_keeps_live_agent(tmp_path, monkeypatch) -> None:
    """Rotating an installer token must not instantly brick a live agent (#10)."""

    monkeypatch.setenv("KENNY_ALLOW_TOKEN_AUTH", "1")
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "e2e3.sqlite"))
    ws_url = f"ws://127.0.0.1:{port}/agent/ws"
    async with _Server(app, port):
        # The agent is provisioned and connected with its current token.
        t1 = await app.state.token_store.create_or_rotate("example-pc-2")
        assert await _register_once(ws_url, "example-pc-2", t1) is True

        # Operator generates a new installer -> token rotates server-side.
        t2 = await app.state.token_store.create_or_rotate("example-pc-2")

        # The live agent, still holding t1, reconnects and is NOT locked out.
        assert await _register_once(ws_url, "example-pc-2", t1) is True

        # Once the new installer is deployed and t2 is used, t1 is retired.
        assert await _register_once(ws_url, "example-pc-2", t2) is True
        assert await _register_once(ws_url, "example-pc-2", t1) is False


@pytest.mark.asyncio
async def test_e2e_spoofed_agent_id_is_dropped(tmp_path, monkeypatch) -> None:
    """A frame whose agent_id != the authenticated connection is dropped, not
    persisted under the forged id (kenny-sec:tunnel/frame-agent-id-spoofing)."""

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "spoof.sqlite"))

    async with _Server(app, port):
        # Authenticate ONLY as "dev" via the full Ed25519 mutual-auth handshake.
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "dev")
        await app.state.key_store.enroll("dev", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        # Push telemetry + a log frame forged as another agent id.
        telemetry = _fixture("telemetry_snapshot.json")
        telemetry["agent_id"] = "victim-pc"
        await agent.ws.send(json.dumps(telemetry))
        logframe = _fixture("log.json")
        logframe["agent_id"] = "victim-pc"
        await agent.ws.send(json.dumps(logframe))
        await asyncio.sleep(0.2)

        # The forged id must have no stored telemetry or logs, and must not appear
        # among known agents.
        assert await app.state.store.latest("victim-pc") is None
        assert "victim-pc" not in await app.state.store.known_agents()
        assert await app.state.event_store.query(kind="log", agent_id="victim-pc") == []

        # The socket survives: a correctly-attributed push still persists.
        telemetry["agent_id"] = "dev"
        await agent.ws.send(json.dumps(telemetry))
        await asyncio.sleep(0.2)
        assert await app.state.store.latest("dev") is not None

        await agent.stop()

"""The disk-cleanup and service-restart tools (contract v0.23) are wired end to end.

Covers the server plumbing around ``fs_cleanup_scan``, ``fs_cleanup`` and
``service_restart``: the forwarding catalog, OS scope, role floor, timeout floor,
tier, the chat schema, and the MCP registration -- joined to the golden fixtures
and the cleanup-target vector rather than to a copy of them.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from test_server_e2e import SERVER_SEED_B64, MockAgent, _free_port, _Server

from kenny_server import tool_classes, toolloop
from kenny_server.main import build_app
from kenny_server.tool_classes import NORMAL_CHANGE, READ_ONLY, classify, is_state_changing
from kenny_server.tools import (
    _OS_SCOPED_TOOLS,
    _TOOL_MIN_ROLE,
    _TOOL_MIN_TIMEOUT_S,
    CAPABILITY_TOOLS,
    forward_timeout_s,
    supports_tool,
)

DOCS = Path(__file__).resolve().parents[2] / "docs"
FIXTURES = DOCS / "fixtures"

CLEANUP_TOOLS = ("fs_cleanup_scan", "fs_cleanup")
NEW_TOOLS = (*CLEANUP_TOOLS, "service_restart")
ARG_KEY = {"fs_cleanup_scan": "target", "fs_cleanup": "target", "service_restart": "name"}
MUTATING = ("fs_cleanup", "service_restart")


def _targets_vector() -> dict[str, list[dict[str, object]]]:
    return json.loads((FIXTURES / "vectors" / "fs_cleanup_targets.json").read_text())


# -- catalog, scope, role, timeout, tier ----------------------------------------


@pytest.mark.parametrize("tool", NEW_TOOLS)
def test_the_tool_forwards_one_string_argument(tool: str) -> None:
    assert CAPABILITY_TOOLS[tool] == [ARG_KEY[tool]]


@pytest.mark.parametrize("tool", NEW_TOOLS)
def test_the_args_match_the_golden_request_fixture(tool: str) -> None:
    request = json.loads((FIXTURES / f"request_{tool}.json").read_text())
    assert request["tool"] == tool
    assert sorted(request["args"]) == sorted(CAPABILITY_TOOLS[tool])


@pytest.mark.parametrize("tool", NEW_TOOLS)
def test_served_on_windows_and_linux_and_refused_on_macos(tool: str) -> None:
    assert _OS_SCOPED_TOOLS[tool] == frozenset({"windows", "linux"})
    assert supports_tool(tool, "windows") and supports_tool(tool, "linux")
    assert not supports_tool(tool, "macos")


def test_only_the_mutating_tools_need_the_operator_role() -> None:
    for tool in MUTATING:
        assert _TOOL_MIN_ROLE[tool] == "operator"
    # The scan deletes nothing and sits at the default role, like `fs_read`.
    assert "fs_cleanup_scan" not in _TOOL_MIN_ROLE


def test_the_timeout_floors_are_pinned() -> None:
    assert _TOOL_MIN_TIMEOUT_S["fs_cleanup"] == 180
    assert _TOOL_MIN_TIMEOUT_S["fs_cleanup_scan"] == 120
    assert _TOOL_MIN_TIMEOUT_S["service_restart"] == 120


@pytest.mark.parametrize("tool", NEW_TOOLS)
def test_a_short_caller_timeout_is_raised_to_the_floor(tool: str) -> None:
    assert forward_timeout_s(tool, {}) == _TOOL_MIN_TIMEOUT_S[tool]
    assert forward_timeout_s(tool, {"timeout_s": 5}) == _TOOL_MIN_TIMEOUT_S[tool]
    assert forward_timeout_s(tool, {"timeout_s": 600}) == 600


def test_tiers() -> None:
    assert classify("fs_cleanup_scan") == READ_ONLY
    assert not is_state_changing("fs_cleanup_scan")
    for tool in MUTATING:
        assert classify(tool) == NORMAL_CHANGE
        assert is_state_changing(tool)
    assert "fs_cleanup_scan" in tool_classes.READ_ONLY_TOOLS


# -- chat schema ----------------------------------------------------------------


@pytest.mark.parametrize("tool", NEW_TOOLS)
def test_the_chat_schema_has_the_string_argument_required(tool: str) -> None:
    schema = toolloop._capability_schema(tool, CAPABILITY_TOOLS[tool])
    key = ARG_KEY[tool]
    assert schema["properties"][key] == {"type": "string"}
    assert schema["required"] == [key]
    assert "agent_id" in schema["properties"]


@pytest.mark.parametrize("tool", NEW_TOOLS)
def test_the_chat_catalog_offers_the_tool(tool: str) -> None:
    names = {s["name"] for s in toolloop.build_tool_schemas()}
    assert tool in names


# -- the cleanup-target vector ---------------------------------------------------


def _protocol_targets() -> dict[str, list[str]]:
    """Target names from the "Windows targets" / "Linux targets" tables in protocol.md."""

    text = (DOCS / "protocol.md").read_text(encoding="utf-8")
    section = text.split("#### Cleanup targets", 1)[1].split("#### Service restart", 1)[0]
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in section.splitlines():
        if line.startswith("Windows targets"):
            current = "windows"
        elif line.startswith("Linux targets"):
            current = "linux"
        elif current and (m := re.match(r"\| `([a-z_]+)` \|", line)):
            found.setdefault(current, []).append(m.group(1))
    return found


def test_the_vector_names_exactly_the_targets_the_contract_documents() -> None:
    vector = {os_: [e["target"] for e in entries] for os_, entries in _targets_vector().items()}
    assert vector == _protocol_targets()


def test_the_vector_is_well_formed() -> None:
    vector = _targets_vector()
    assert set(vector) == {"windows", "linux"}
    for entries in vector.values():
        names = [e["target"] for e in entries]
        assert names and len(names) == len(set(names))
        assert all(re.fullmatch(r"[a-z][a-z_]*", n) for n in names)
        assert all(isinstance(e["min_age_days"], int) and e["min_age_days"] >= 0 for e in entries)


@pytest.mark.parametrize("tool", CLEANUP_TOOLS)
def test_the_golden_request_names_a_catalog_target(tool: str) -> None:
    request = json.loads((FIXTURES / f"request_{tool}.json").read_text())
    known = {e["target"] for entries in _targets_vector().values() for e in entries}
    assert request["args"]["target"] in known


# -- MCP registration and forwarding ------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_lists_the_tools_and_forwards_them(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "cleanup.sqlite"))

    async with _Server(app, port):
        agents = {}
        for agent_id, os_ in (("win-pc", "windows"), ("lin-pc", "linux"), ("mac-pc", "macos")):
            agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", agent_id, os=os_)
            await app.state.key_store.enroll(agent_id, agent.public_key_b64)
            await agent.start()
            agents[agent_id] = agent
        await asyncio.sleep(0.1)

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            listed = {t.name: t for t in await client.list_tools()}
            for tool in NEW_TOOLS:
                assert tool in listed, f"{tool} is not registered for MCP"
                assert ARG_KEY[tool] in (listed[tool].description or "")

            scan = await client.call_tool(
                "fs_cleanup_scan",
                {"args": {"target": "temp_system", "agent_id": "win-pc"}},
            )
            assert scan.data["target"] == "temp_system"
            assert scan.data["complete"] is True

            cleaned = await client.call_tool(
                "fs_cleanup", {"args": {"target": "tmp", "agent_id": "lin-pc"}}
            )
            assert "files_removed" in cleaned.data

            restarted = await client.call_tool(
                "service_restart", {"args": {"name": "Spooler", "agent_id": "win-pc"}}
            )
            assert restarted.data["ok"] is True

            # macOS answers `unsupported`; the server says so before sending a frame.
            for tool in NEW_TOOLS:
                with pytest.raises(ToolError, match="macos"):
                    await client.call_tool(
                        tool, {"args": {ARG_KEY[tool]: "x", "agent_id": "mac-pc"}}
                    )

        for agent in agents.values():
            await agent.stop()

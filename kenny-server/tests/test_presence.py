"""The presence record and the tunnel's liveness (availability).

* ``PresenceStore``: runs, sessions, boots, the one-time boot backfill, retention.
* The tunnel closes a connection that sends nothing for three heartbeats and
  marks the agent offline -- and the server's timeout is built on the agent's
  real ping interval, read from the Rust source.
* A reconnect that lands before the old socket dies is not undone by the old
  socket's teardown.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from kenny_server import tunnel as tunnel_module
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, PresenceStore, TelemetryStore
from kenny_server.tunnel import AgentTunnel, ToolError

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
BOOT = 1780322400  # the uptime section of docs/fixtures/telemetry_snapshot.json
BOOT_ISO = "2026-06-01T14:00:00+00:00"
_TUNNEL_RS = Path(__file__).resolve().parents[2] / "kenny-agent" / "src" / "tunnel.rs"
_FIXTURES = Path(__file__).resolve().parents[2] / "docs" / "fixtures"


@pytest.fixture
async def presence(tmp_path):
    store = PresenceStore(str(tmp_path / "p.sqlite"))
    await store.connect()
    yield store
    await store.close()


def ts(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# -- the heartbeat seam ----------------------------------------------------------


def test_server_heartbeat_matches_the_agents_ping_interval() -> None:
    source = _TUNNEL_RS.read_text(encoding="utf-8")
    match = re.search(r"const HEARTBEAT: Duration = Duration::from_secs\((\d+)\);", source)
    assert match, f"HEARTBEAT not found in {_TUNNEL_RS}"
    assert int(match.group(1)) == tunnel_module.HEARTBEAT_SECS
    assert tunnel_module.HEARTBEAT_TIMEOUT_SECS == 3 * tunnel_module.HEARTBEAT_SECS


def test_fixture_uptime_boot_is_the_one_these_tests_expect() -> None:
    snapshot = json.loads((_FIXTURES / "telemetry_snapshot.json").read_text())["snapshot"]
    assert snapshot["uptime"]["boot_time_unix"] == BOOT
    assert datetime.fromtimestamp(BOOT, timezone.utc).isoformat() == BOOT_ISO


# -- PresenceStore -----------------------------------------------------------------


async def test_start_run_closes_a_crashed_runs_sessions_at_its_last_sign_of_life(
    presence: PresenceStore,
) -> None:
    await presence.start_run(ts(0))
    await presence.open_session("pc1", ts(1))
    late = await presence.open_session("pc2", ts(10.5))
    await presence.touch_run(ts(10))
    # Crash: no stop_run. The next process starts later.
    await presence.start_run(ts(30))

    rows = await presence.sessions_many(["pc1", "pc2"], ts(-60).isoformat(), ts(60).isoformat())
    assert rows["pc1"] == [
        {
            "connected_at": ts(1).isoformat(),
            "disconnected_at": ts(10).isoformat(),
            "end_reason": "server_stopped",
        }
    ]
    # Connected after the last touch: ends where it began, never before.
    assert rows["pc2"][0]["disconnected_at"] == ts(10.5).isoformat()
    assert late > 0
    runs = await presence.runs(ts(-60).isoformat(), ts(60).isoformat())
    assert [(r["started_at"], r["last_alive_at"], r["current"]) for r in runs] == [
        (ts(0).isoformat(), ts(10).isoformat(), False),
        (ts(30).isoformat(), ts(30).isoformat(), True),
    ]
    assert await presence.ledger_epoch() == ts(0).isoformat()


async def test_stop_run_ends_open_sessions_and_a_later_close_is_a_no_op(
    presence: PresenceStore,
) -> None:
    await presence.start_run(ts(0))
    sid = await presence.open_session("pc1", ts(1))
    await presence.stop_run(ts(5))
    assert await presence.close_session(sid, "disconnect", ts(6)) is False
    [row] = await presence.sessions("pc1", ts(0).isoformat(), ts(10).isoformat())
    assert row["disconnected_at"] == ts(5).isoformat()
    assert row["end_reason"] == "server_stopped"


async def test_close_session_records_the_reason(presence: PresenceStore) -> None:
    await presence.start_run(ts(0))
    sid = await presence.open_session("pc1", ts(1))
    assert await presence.close_session(sid, "heartbeat_timeout", ts(2)) is True
    [row] = await presence.sessions("pc1", ts(0).isoformat(), ts(10).isoformat())
    assert row["end_reason"] == "heartbeat_timeout"
    with pytest.raises(ValueError):
        await presence.close_session(sid, "exploded", ts(3))


async def test_sessions_are_windowed_by_overlap(presence: PresenceStore) -> None:
    await presence.start_run(ts(0))
    a = await presence.open_session("pc1", ts(1))
    await presence.close_session(a, "disconnect", ts(2))
    await presence.open_session("pc1", ts(5))  # still open
    rows = await presence.sessions("pc1", ts(3).isoformat(), ts(10).isoformat())
    assert [r["connected_at"] for r in rows] == [ts(5).isoformat()]
    assert rows[0]["disconnected_at"] is None
    first = await presence.first_session_many(["pc1", "pc2"])
    assert first == {"pc1": ts(1).isoformat()}


async def test_note_boot_dedupes_within_120_seconds(presence: PresenceStore) -> None:
    assert await presence.note_boot("pc1", BOOT) is True
    assert await presence.note_boot("pc1", BOOT + 2) is False
    # A fresh store instance has no cache: the DB check alone must dedupe.
    presence._last_boot.clear()
    assert await presence.note_boot("pc1", BOOT - 119) is False
    assert await presence.note_boot("pc1", BOOT + 3600) is True
    assert await presence.note_boot("pc2", BOOT) is True
    boots = await presence.boots_many(["pc1", "pc2"], "2026-01-01", "2027-01-01")
    assert boots == {
        "pc1": [BOOT_ISO, "2026-06-01T15:00:00+00:00"],
        "pc2": [BOOT_ISO],
    }


@pytest.mark.parametrize("value", [None, "1780322400", 1.5e9, True, 0, -5, 10**12])
async def test_note_boot_ignores_implausible_values(presence: PresenceStore, value: Any) -> None:
    assert await presence.note_boot("pc1", value) is False
    assert await presence.boots("pc1", "1970-01-01", "9999-01-01") == []


async def test_boot_backfill_reads_stored_snapshots_once(tmp_path) -> None:
    db = str(tmp_path / "bf.sqlite")
    telemetry = TelemetryStore(db)
    await telemetry.connect()
    for boot in (BOOT, BOOT + 1, BOOT + 5000):
        await telemetry.insert(
            "pc1", "2026-06-02T00:00:00Z", {"uptime": {"status": "ok", "summary": "", "boot_time_unix": boot}}
        )
    await telemetry.insert("pc2", "2026-06-02T00:00:00Z", {"cpu": {"status": "ok", "summary": ""}})
    presence = PresenceStore(db)
    await presence.connect()
    try:
        assert await presence.backfill_boots_from_snapshots() == 2
        assert await presence.boots("pc1", "2026-01-01", "2027-01-01") == [
            BOOT_ISO,
            datetime.fromtimestamp(BOOT + 5000, timezone.utc).isoformat(),
        ]
        await presence.delete_agent("pc1")
        # Flagged done: it never runs again, even with the rows gone.
        assert await presence.backfill_boots_from_snapshots() == 0
        assert await presence.boots("pc1", "2026-01-01", "2027-01-01") == []
    finally:
        await presence.close()
        await telemetry.close()


async def test_prune_clips_the_record_to_the_retention_window(presence: PresenceStore) -> None:
    now = ts(0) + timedelta(days=40)
    await presence.start_run(ts(0))
    old = await presence.open_session("pc1", ts(1))
    await presence.close_session(old, "disconnect", ts(2))
    await presence.open_session("pc1", ts(0) + timedelta(days=5))  # still open
    await presence.note_boot("pc1", int(ts(0).timestamp()), now=now)
    assert await presence.note_boot("pc1", int((now - timedelta(days=1)).timestamp()), now=now)

    deleted = await presence.prune(now=now, retention_days=30)
    cutoff = (now - timedelta(days=30)).isoformat()
    assert deleted == 2  # the old closed session + the old boot
    rows = await presence.sessions("pc1", "2000-01-01", "2100-01-01")
    assert [(r["connected_at"], r["disconnected_at"]) for r in rows] == [(cutoff, None)]
    # The current run straddles the cutoff: clipped, never deleted.
    assert await presence.ledger_epoch() == cutoff
    assert len(await presence.boots("pc1", "2000-01-01", "2100-01-01")) == 1


async def test_telemetry_received_times_are_windowed_and_batched(tmp_path) -> None:
    telemetry = TelemetryStore(str(tmp_path / "rt.sqlite"))
    await telemetry.connect()
    try:
        for i, agent in enumerate(("pc1", "pc1", "pc2", "pc1")):
            await telemetry.insert(
                agent, ts(i).isoformat(), {}, received_at=ts(i).isoformat()
            )
        assert await telemetry.received_times("pc1", ts(0).isoformat(), ts(3).isoformat()) == [
            ts(0).isoformat(),
            ts(1).isoformat(),
        ]
        many = await telemetry.received_times_many(
            ["pc1", "pc2", "pc3"], ts(0).isoformat(), ts(10).isoformat()
        )
        assert many == {
            "pc1": [ts(0).isoformat(), ts(1).isoformat(), ts(3).isoformat()],
            "pc2": [ts(2).isoformat()],
        }
        assert await telemetry.first_collected_many(["pc1", "pc2", "pc3"]) == {
            "pc1": ts(0).isoformat(),
            "pc2": ts(2).isoformat(),
        }
    finally:
        await telemetry.close()


# -- the tunnel ------------------------------------------------------------------


class _QueueSocket:
    """An in-process ``/agent/ws`` peer: frames in through a queue, sends recorded."""

    CLOSE = object()

    def __init__(self) -> None:
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed_code: int | None = None

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        item = await self.inbound.get()
        if item is self.CLOSE:
            raise WebSocketDisconnect(1000)
        return item

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    def register(self, agent_id: str, token: str) -> None:
        self.inbound.put_nowait(
            json.dumps(
                {
                    "type": "register",
                    "agent_id": agent_id,
                    "token": token,
                    "meta": {"hostname": agent_id, "os": "windows", "version": "0.1.0"},
                }
            )
        )


@pytest.fixture
async def bench(tmp_path):
    db = str(tmp_path / "tunnel.sqlite")
    telemetry, events, presence = TelemetryStore(db), EventStore(db), PresenceStore(db)
    for s in (telemetry, events, presence):
        await s.connect()
    await presence.start_run()
    registry = AgentRegistry(tokens={"pc1": "tok"})
    tunnel = AgentTunnel(registry, telemetry, events, presence=presence)
    yield tunnel
    for s in (telemetry, events, presence):
        await s.close()


async def _until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _sessions(tunnel: AgentTunnel, agent_id: str) -> list[dict[str, Any]]:
    return await tunnel.presence.sessions(agent_id, "2000-01-01", "2100-01-01")


async def test_a_silent_agent_is_closed_after_the_heartbeat_timeout(bench, monkeypatch) -> None:
    monkeypatch.setattr(tunnel_module, "HEARTBEAT_TIMEOUT_SECS", 0.2)
    ws = _QueueSocket()
    ws.register("pc1", "tok")
    await asyncio.wait_for(bench.endpoint(ws), timeout=5)

    assert ws.closed_code == tunnel_module.HEARTBEAT_CLOSE_CODE
    assert bench.registry.get("pc1").online is False
    [session] = await _sessions(bench, "pc1")
    assert session["end_reason"] == "heartbeat_timeout"
    assert session["disconnected_at"] is not None


async def test_any_frame_resets_the_heartbeat_timeout(bench, monkeypatch) -> None:
    monkeypatch.setattr(tunnel_module, "HEARTBEAT_TIMEOUT_SECS", 0.3)
    ws = _QueueSocket()
    ws.register("pc1", "tok")
    task = asyncio.create_task(bench.endpoint(ws))
    for _ in range(5):
        await asyncio.sleep(0.15)
        ws.inbound.put_nowait(json.dumps({"type": "ping"}))
    assert not task.done() and bench.registry.get("pc1").online is True
    ws.inbound.put_nowait(_QueueSocket.CLOSE)
    await asyncio.wait_for(task, timeout=2)
    [session] = await _sessions(bench, "pc1")
    assert session["end_reason"] == "disconnect"


async def test_telemetry_with_uptime_records_the_boot(bench) -> None:
    frame = json.loads((_FIXTURES / "telemetry_snapshot.json").read_text())
    frame["agent_id"] = "pc1"
    ws = _QueueSocket()
    ws.register("pc1", "tok")
    ws.inbound.put_nowait(json.dumps(frame))
    ws.inbound.put_nowait(json.dumps(frame))
    ws.inbound.put_nowait(_QueueSocket.CLOSE)
    await asyncio.wait_for(bench.endpoint(ws), timeout=5)
    assert await bench.presence.boots("pc1", "2026-01-01", "2027-01-01") == [BOOT_ISO]


async def test_a_broken_presence_store_never_breaks_the_tunnel(bench) -> None:
    await bench.presence.close()  # every presence call now raises
    frame = json.loads((_FIXTURES / "telemetry_snapshot.json").read_text())
    frame["agent_id"] = "pc1"
    ws = _QueueSocket()
    ws.register("pc1", "tok")
    ws.inbound.put_nowait(json.dumps(frame))
    ws.inbound.put_nowait(json.dumps({"type": "ping"}))
    ws.inbound.put_nowait(_QueueSocket.CLOSE)
    await asyncio.wait_for(bench.endpoint(ws), timeout=5)
    assert {"type": "pong"} in ws.sent
    assert await bench.store.latest("pc1") is not None
    await bench.presence.connect()  # let the fixture close it


async def test_a_reconnect_is_not_undone_by_the_old_sockets_teardown(bench) -> None:
    old, new = _QueueSocket(), _QueueSocket()
    old.register("pc1", "tok")
    old_task = asyncio.create_task(bench.endpoint(old))
    await _until(lambda: bench.registry.get("pc1") is not None)
    first_conn = bench.registry.get("pc1").conn_id

    new.register("pc1", "tok")
    new_task = asyncio.create_task(bench.endpoint(new))
    await _until(lambda: bench.registry.get("pc1").conn_id != first_conn)

    # A request in flight on the new connection.
    request = asyncio.create_task(bench.send_request("pc1", "net_config", {}, timeout_s=2))
    await _until(lambda: len(new.sent) == 1)

    # The old socket finally dies.
    old.inbound.put_nowait(_QueueSocket.CLOSE)
    await asyncio.wait_for(old_task, timeout=2)

    agent = bench.registry.get("pc1")
    assert agent.online is True, "the old socket's teardown took the new connection offline"
    assert bench.registry.send_fn_for("pc1") is not None
    assert not request.done(), "the old socket's teardown failed the new connection's request"

    new.inbound.put_nowait(
        json.dumps(
            {"type": "response", "id": new.sent[0]["id"], "ok": True, "result": {"ok": 1}}
        )
    )
    assert await asyncio.wait_for(request, timeout=2) == {"ok": 1}

    sessions = await _sessions(bench, "pc1")
    assert [s["end_reason"] for s in sessions] == ["superseded", None]

    new.inbound.put_nowait(_QueueSocket.CLOSE)
    await asyncio.wait_for(new_task, timeout=2)
    assert bench.registry.get("pc1").online is False
    assert [s["end_reason"] for s in await _sessions(bench, "pc1")] == [
        "superseded",
        "disconnect",
    ]


async def test_a_disconnect_fails_only_its_own_requests(bench) -> None:
    bench.registry._tokens["pc2"] = "tok2"  # noqa: SLF001
    one, two = _QueueSocket(), _QueueSocket()
    one.register("pc1", "tok")
    two.register("pc2", "tok2")
    t1 = asyncio.create_task(bench.endpoint(one))
    t2 = asyncio.create_task(bench.endpoint(two))
    await _until(lambda: bench.registry.get("pc1") is not None and bench.registry.get("pc2") is not None)
    r1 = asyncio.create_task(bench.send_request("pc1", "net_config", {}, timeout_s=2))
    r2 = asyncio.create_task(bench.send_request("pc2", "net_config", {}, timeout_s=2))
    await _until(lambda: len(one.sent) == 1 and len(two.sent) == 1)

    one.inbound.put_nowait(_QueueSocket.CLOSE)
    await asyncio.wait_for(t1, timeout=2)
    with pytest.raises(ToolError):
        await asyncio.wait_for(r1, timeout=2)
    assert not r2.done()

    two.inbound.put_nowait(
        json.dumps({"type": "response", "id": two.sent[0]["id"], "ok": True, "result": {}})
    )
    assert await asyncio.wait_for(r2, timeout=2) == {}
    two.inbound.put_nowait(_QueueSocket.CLOSE)
    await asyncio.wait_for(t2, timeout=2)


# -- the tool surfaces -----------------------------------------------------------


def test_every_host_naming_server_tool_is_pinned_on_a_ticket() -> None:
    """A server tool that takes the host in ``id`` must be pinned to the ticket's
    frozen target, or a scoped requester could read any host through it."""

    from kenny_server.ticket_assistant import _HOST_ARG_TOOLS, EXCLUDED_TOOLS
    from kenny_server.toolloop import SERVER_TOOLS

    naming = {
        name
        for name, spec in SERVER_TOOLS.items()
        if "id" in spec.get("properties", {}) and name not in EXCLUDED_TOOLS
    }
    assert "agent_availability" in naming
    assert naming <= _HOST_ARG_TOOLS


async def _seeded(tmp_path):
    db = str(tmp_path / "tools.sqlite")
    telemetry, presence = TelemetryStore(db), PresenceStore(db)
    await telemetry.connect()
    await presence.connect()
    now = datetime.now(timezone.utc)
    await presence.start_run(now - timedelta(hours=2))
    sid = await presence.open_session("pc1", now - timedelta(hours=2))
    await presence.close_session(sid, "disconnect", now - timedelta(hours=1))
    await presence.note_boot("pc1", int((now - timedelta(hours=3)).timestamp()))
    await telemetry.insert("pc1", (now - timedelta(hours=2)).isoformat(), {})
    return telemetry, presence


async def test_mcp_and_chat_executor_return_the_same_summary(tmp_path) -> None:
    from fastmcp import Client, FastMCP
    from fastmcp.exceptions import ToolError as McpToolError

    from kenny_server.tools import CallLog, ScreenshotStore, register_tools
    from kenny_server.toolloop import ToolExecutor

    telemetry, presence = await _seeded(tmp_path)
    events = EventStore(str(tmp_path / "tools.sqlite"))
    await events.connect()
    registry = AgentRegistry(tokens={})
    tunnel = AgentTunnel(registry, telemetry, events)
    try:
        mcp = FastMCP("t")
        register_tools(
            mcp, registry=registry, store=telemetry, tunnel=tunnel,
            call_log=CallLog(), presence=presence,
        )
        executor = ToolExecutor(
            registry=registry, store=telemetry, tunnel=tunnel, call_log=CallLog(),
            screenshots=ScreenshotStore(), presence=presence,
        )
        async with Client(mcp) as client:
            via_mcp = (await client.call_tool("agent_availability", {"id": "pc1"})).data
            with pytest.raises(McpToolError):
                await client.call_tool("agent_availability", {"id": "pc1", "days": 31})
        via_chat = await executor.run_server_tool("agent_availability", {"id": "pc1", "days": 7})
        # Both end "now", a moment apart: compare what does not move with it.
        for key in ("online_pct", "ledger_since", "boots", "outages_truncated"):
            assert via_mcp[key] == via_chat[key], key
        assert [o["start"] for o in via_mcp["outages"]] == [o["start"] for o in via_chat["outages"]]
        assert via_mcp["online_pct"] == 50.0
        [outage] = via_mcp["outages"]
        assert outage["approx"] is False and 3590 <= outage["duration_secs"] <= 3610
        assert len(via_mcp["boots"]) == 1
        with pytest.raises(ToolError) as excinfo:
            await executor.run_server_tool("agent_availability", {"id": "pc1", "days": 0})
        assert excinfo.value.code == "bad_args"
    finally:
        await events.close()
        await presence.close()
        await telemetry.close()

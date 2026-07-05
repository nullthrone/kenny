"""Removing a host mid-connection closes its live socket (#127, ADR-0037).

A still-connected agent whose host was purged must not keep re-populating its
snapshots (which would make it reappear in the fleet list). The ``_serve`` loop
drops the connection as soon as the agent is no longer registered.
"""

from __future__ import annotations

import json

import pytest

from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore
from kenny_server.tunnel import AgentTunnel


class _FakeWebSocket:
    """Yields one telemetry frame, then records the close code."""

    def __init__(self, frame: str) -> None:
        self._frames = [frame]
        self.closed_code: int | None = None

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise AssertionError("receive_text called after the socket should have closed")

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


@pytest.mark.asyncio
async def test_serve_drops_a_removed_agent(tmp_path) -> None:
    store = TelemetryStore(str(tmp_path / "t.sqlite"))
    events = EventStore(str(tmp_path / "t.sqlite"))
    await store.connect()
    await events.connect()
    registry = AgentRegistry(tokens={})
    tunnel = AgentTunnel(registry, store, events)

    # The agent is NOT registered (simulating a purge via registry.remove).
    frame = json.dumps(
        {
            "type": "telemetry",
            "agent_id": "gone-pc",
            "collected_at": "2026-07-01T00:00:00+00:00",
            "snapshot": {},
        }
    )
    ws = _FakeWebSocket(frame)

    await tunnel._serve(ws, "gone-pc")

    # The socket was closed (4400) and nothing was persisted for the removed host.
    assert ws.closed_code == 4400
    assert await store.latest("gone-pc") is None
    assert "gone-pc" not in await store.known_agents()

    await store.close()
    await events.close()

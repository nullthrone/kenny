"""Token accounting: ``UsageMeter`` and its opt-in use by ``drive_events``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

from kenny_server.chat import ChatExecutor, FleetPolicy, FleetSession
from kenny_server.registry import AgentRegistry
from kenny_server.store import TelemetryStore
from kenny_server.toolloop import UsageMeter, drive_events
from kenny_server.tools import CallLog, ScreenshotStore
from kenny_server.tunnel import AgentTunnel


class _UsageResponse(_Response):
    def __init__(self, content: list[Any], stop_reason: str, usage: Any = None) -> None:
        super().__init__(content, stop_reason)
        if usage is not None:
            self.usage = usage


def _usage(i: Any = 0, o: Any = 0, cr: Any = 0, cc: Any = 0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=i,
        output_tokens=o,
        cache_read_input_tokens=cr,
        cache_creation_input_tokens=cc,
    )


def _executor(store: TelemetryStore) -> ChatExecutor:
    registry = AgentRegistry(tokens={})
    return ChatExecutor(
        registry=registry,
        store=store,
        tunnel=AgentTunnel(registry, store, None),
        call_log=CallLog(),
        screenshots=ScreenshotStore(),
    )


async def _drain(session: Any, client: Any, tmp_path) -> list[dict[str, Any]]:
    store = TelemetryStore(db_path=str(tmp_path / "u.sqlite"))
    await store.connect()
    try:
        return [
            ev
            async for ev in drive_events(
                session,
                _executor(store),
                client=client,
                model="claude-test",
                policy=FleetPolicy(),
            )
        ]
    finally:
        await store.close()


def test_meter_add_and_to_dict_tolerate_missing_and_none() -> None:
    m = UsageMeter()
    m.add(_usage(10, 5, 3, 2))
    m.add(SimpleNamespace(input_tokens=1, output_tokens=None))  # missing/None -> 0
    m.add(None)
    m.add(object())
    assert m.to_dict() == {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_read_tokens": 3,
        "cache_creation_tokens": 2,
    }


async def test_meter_sums_across_two_model_round_trips(tmp_path) -> None:
    client = FakeAnthropic(
        [
            _UsageResponse(
                [tool_use_block("t1", "fleet_overview", {})], "tool_use", _usage(100, 20, 5, 7)
            ),
            _UsageResponse([text_block("All green.")], "end_turn", _usage(150, 30, 60, 0)),
        ]
    )
    session = FleetSession(id="s")
    session.messages.append({"role": "user", "content": "status?"})
    session.usage = UsageMeter()  # type: ignore[attr-defined]
    events = await _drain(session, client, tmp_path)
    assert events[-1]["type"] == "done"
    assert session.usage.to_dict() == {  # type: ignore[attr-defined]
        "input_tokens": 250,
        "output_tokens": 50,
        "cache_read_tokens": 65,
        "cache_creation_tokens": 7,
    }
    # The event stream is unchanged: no event carries usage.
    assert not any("usage" in ev for ev in events)


async def test_message_without_usage_counts_zero(tmp_path) -> None:
    client = FakeAnthropic([_Response([text_block("hi")], "end_turn")])
    session = FleetSession(id="s")
    session.messages.append({"role": "user", "content": "hello"})
    session.usage = UsageMeter()  # type: ignore[attr-defined]
    await _drain(session, client, tmp_path)
    assert session.usage.to_dict() == UsageMeter().to_dict()  # type: ignore[attr-defined]


async def test_session_without_usage_is_untouched(tmp_path) -> None:
    client = FakeAnthropic([_UsageResponse([text_block("hi")], "end_turn", _usage(9, 9))])
    session = FleetSession(id="s")
    session.messages.append({"role": "user", "content": "hello"})
    events = await _drain(session, client, tmp_path)
    assert events[-1]["type"] == "done"
    assert not hasattr(session, "usage")


def test_ticket_session_new_fields_default_to_none() -> None:
    from kenny_server.auth import Principal
    from kenny_server.ticket_assistant import TicketSession

    s = TicketSession(
        id="t",
        principal=Principal(user_id=1, username="u", role="operator"),
        agent_id=None,
        allowed_tools=frozenset(),
    )
    assert s.usage is None and s.audit_actor is None and s.agent_run_id is None

"""Offering the staged agent version: who is behind, update one or all, tell the operator."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from starlette.testclient import TestClient

from kenny_server import agent_updates
from kenny_server.agent_updates import (
    CopilotUpdates,
    UpdateOffers,
    UpdateRefused,
    is_behind,
    outdated_agents,
    update_agent,
    update_outdated,
)
from kenny_server.distribution import ShareLinks
from kenny_server.main import build_app
from kenny_server.registry import AgentRegistry
from kenny_server.tool_classes import NORMAL_CHANGE, READ_ONLY, classify
from kenny_server.toolloop import (
    COPILOT_UPDATE_TOOLS,
    SERVER_TOOLS,
    ToolExecutor,
    build_tool_schemas,
)
from kenny_server.tunnel import ToolError

BINARY = b"MZ staged kenny-agent 2.7.0"
SHA = hashlib.sha256(BINARY).hexdigest()


@pytest.fixture(autouse=True)
def staged(tmp_path, monkeypatch):
    """Stage a Windows binary tagged 2.7.0 and no Linux one."""

    p = tmp_path / "kenny-agent.exe"
    p.write_bytes(BINARY)
    (tmp_path / "kenny-agent.exe.version").write_text("2.7.0")
    monkeypatch.setenv("KENNY_AGENT_BINARY", str(p))
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX", raising=False)
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    return p


class FakeUpdateStore:
    def __init__(self, channels: dict[str, str] | None = None) -> None:
        self.channels = channels or {}

    async def get_desired_channel(self, agent_id: str) -> str:
        return self.channels.get(agent_id, "stable")


class FakeTunnel:
    """Records the wire frames; an agent id in ``fail`` answers with an error."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = fail or set()

    async def send_request(self, agent_id: str, tool: str, args: dict, timeout_s: float):
        if agent_id in self.fail:
            raise ToolError("disabled", "remote control is off")
        self.sent.append({"agent_id": agent_id, "tool": tool, "args": dict(args)})
        return {"ok": True}


async def _noop(_frame):
    return None


def _registry(**agents: dict[str, Any]) -> AgentRegistry:
    """``name=dict(version=..., os=..., online=...)`` -> a registry with those agents."""

    reg = AgentRegistry(tokens={})
    for name, spec in agents.items():
        meta = {"os": spec.get("os", "windows"), "arch": "x86_64"}
        if spec.get("version") is not None:
            meta["version"] = spec["version"]
        reg.register_signed_async(name.replace("_", "-"), meta, _noop)
        if not spec.get("online", True):
            reg.mark_offline(name.replace("_", "-"))
    return reg


# -- who is behind -----------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "staged", "behind"),
    [
        ("2.5.1", "2.7.0", True),
        ("2.6.0", "2.7.0", True),
        ("2.7.0", "2.7.0", False),
        ("2.8.0", "2.7.0", False),  # never a downgrade
        (None, "2.7.0", False),  # unknown is never behind
        ("garbage", "2.7.0", False),
        ("2.5.1", "", False),
    ],
)
def test_is_behind(current, staged, behind):
    assert is_behind(current, staged) is behind


async def test_outdated_agents_lists_only_agents_behind_the_staged_version():
    reg = _registry(
        linus_pc={"version": "2.5.1"},
        thomas_pc={"version": "2.6.0"},
        current_pc={"version": "2.7.0"},
        unknown_pc={},
        maria_pc={"version": "2.5.1", "online": False},
        # No Linux binary is staged, so a Linux agent has nothing to be behind.
        penguin={"version": "2.5.1", "os": "linux"},
    )
    rows = await outdated_agents(reg, FakeUpdateStore())

    assert [r["agent_id"] for r in rows] == ["linus-pc", "thomas-pc", "maria-pc"]  # online first
    assert rows[0] == {
        "agent_id": "linus-pc",
        "online": True,
        "os": "windows",
        "arch": "x86_64",
        "channel": "stable",
        "current_version": "2.5.1",
        "staged_version": "2.7.0",
    }
    assert rows[2]["online"] is False


async def test_an_agent_on_the_dev_channel_is_compared_with_the_dev_binary(tmp_path, monkeypatch):
    # Only a stable 2.7.0 is staged; the dev-desired agent has no dev binary, so
    # it is not offered the stable one.
    reg = _registry(dev_pc={"version": "2.5.1"})
    rows = await outdated_agents(reg, FakeUpdateStore({"dev-pc": "dev"}))
    assert rows == []


# -- update one ----------------------------------------------------------------


async def test_update_agent_sends_the_staged_artifact_with_a_minted_url(staged):
    reg = _registry(linus_pc={"version": "2.5.1"})
    tunnel, links = FakeTunnel(), ShareLinks()

    done = await update_agent(
        registry=reg,
        tunnel=tunnel,
        share_links=links,
        update_store=FakeUpdateStore(),
        agent_id="linus-pc",
    )

    (frame,) = tunnel.sent
    assert frame["tool"] == "agent_update"
    assert frame["args"]["version"] == "2.7.0"
    assert frame["args"]["sha256"] == SHA
    url = frame["args"]["url"]
    assert url.startswith("https://kenny.example.com/d/binary/")
    # The nonce in that URL serves exactly the staged file, for this agent.
    entry = links.resolve_entry(url.rsplit("/", 1)[1], "binary", consume=False)
    assert entry is not None and entry.agent_id == "linus-pc"
    assert done["from_version"] == "2.5.1" and done["version"] == "2.7.0"


@pytest.mark.parametrize(
    ("agent", "code"),
    [
        ({"version": "2.7.0"}, "up_to_date"),
        ({"version": "2.5.1", "online": False}, "offline"),
        ({"version": "2.5.1", "os": "linux"}, "no_staged_binary"),
    ],
)
async def test_update_agent_refuses_what_it_should_not_send(agent, code):
    reg = _registry(target=agent)
    tunnel = FakeTunnel()
    with pytest.raises(UpdateRefused) as exc:
        await update_agent(
            registry=reg,
            tunnel=tunnel,
            share_links=ShareLinks(),
            update_store=FakeUpdateStore(),
            agent_id="target",
        )
    assert exc.value.code == code
    assert tunnel.sent == []


async def test_update_agent_refuses_an_unknown_agent():
    with pytest.raises(UpdateRefused) as exc:
        await update_agent(
            registry=_registry(),
            tunnel=FakeTunnel(),
            share_links=ShareLinks(),
            update_store=FakeUpdateStore(),
            agent_id="ghost",
        )
    assert exc.value.code == "unknown_agent"


# -- update all that are behind ------------------------------------------------


async def test_update_outdated_bundles_and_one_failure_does_not_stop_the_rest():
    reg = _registry(
        linus_pc={"version": "2.5.1"},
        thomas_pc={"version": "2.6.0"},
        broken_pc={"version": "2.6.0"},
        current_pc={"version": "2.7.0"},
        maria_pc={"version": "2.5.1", "online": False},
    )
    tunnel = FakeTunnel(fail={"broken-pc"})

    out = await update_outdated(
        registry=reg, tunnel=tunnel, share_links=ShareLinks(), update_store=FakeUpdateStore()
    )

    by_agent = {r["agent_id"]: r for r in out["results"]}
    assert by_agent["linus-pc"]["status"] == "sent"
    assert by_agent["thomas-pc"]["status"] == "sent"
    assert by_agent["broken-pc"]["status"] == "failed"
    assert by_agent["maria-pc"] == {"agent_id": "maria-pc", "status": "skipped", "reason": "offline"}
    assert "current-pc" not in by_agent  # already current: not touched, not listed
    assert out["sent"] == 2
    assert sorted(f["agent_id"] for f in tunnel.sent) == ["linus-pc", "thomas-pc"]


async def test_update_outdated_can_be_narrowed_to_some_agents():
    reg = _registry(linus_pc={"version": "2.5.1"}, thomas_pc={"version": "2.6.0"})
    tunnel = FakeTunnel()
    out = await update_outdated(
        registry=reg,
        tunnel=tunnel,
        share_links=ShareLinks(),
        update_store=FakeUpdateStore(),
        only=["thomas-pc"],
    )
    assert [r["agent_id"] for r in out["results"]] == ["thomas-pc"]
    assert [f["agent_id"] for f in tunnel.sent] == ["thomas-pc"]


# -- the routes ------------------------------------------------------------------


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def test_routes_list_and_update_outdated_agents(tmp_path, monkeypatch):
    app = build_app(db_path=str(tmp_path / "u.sqlite"))
    sent: list[tuple[str, str, dict]] = []

    async def fake_send(self, agent_id, tool, args, timeout_s=30):
        sent.append((agent_id, tool, dict(args)))
        return {"ok": True}

    monkeypatch.setattr(type(app.state.tunnel), "send_request", fake_send)
    app.state.registry.register_signed_async(
        "linus-pc", {"os": "windows", "arch": "x86_64", "version": "2.5.1"}, _noop
    )
    app.state.registry.register_signed_async(
        "thomas-pc", {"os": "windows", "arch": "x86_64", "version": "2.7.0"}, _noop
    )
    with TestClient(app) as c:
        assert c.get("/api/agents/outdated").status_code == 401
        assert c.post("/api/agents/update-outdated").status_code == 401

        h = _bearer(app)
        listed = c.get("/api/agents/outdated", headers=h).json()["agents"]
        assert [a["agent_id"] for a in listed] == ["linus-pc"]

        r = c.post("/api/agents/update-outdated", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["sent"] == 1
        assert [(a, t) for a, t, _ in sent] == [("linus-pc", "agent_update")]

        bad = c.post("/api/agents/update-outdated", headers=h, json={"agents": "linus-pc"})
        assert bad.status_code == 400


# -- Ask kenny: the two tools ------------------------------------------------------


class RecordingCallLog:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record(self, agent_id, tool, args, *, ok, error=None, actor=None, **_kw):
        self.rows.append({"agent_id": agent_id, "tool": tool, "args": args, "ok": ok, "error": error})


def _copilot(reg: AgentRegistry, tunnel: FakeTunnel, log: RecordingCallLog) -> CopilotUpdates:
    return CopilotUpdates(
        registry=reg,
        tunnel=tunnel,
        share_links=ShareLinks(),
        update_store=FakeUpdateStore(),
        call_log=log,
    )


class _Session:
    agent_id = "linus-pc"
    principal = None


async def test_the_status_tool_names_the_staged_version_and_who_is_behind():
    reg = _registry(linus_pc={"version": "2.5.1"}, thomas_pc={"version": "2.7.0"})
    out = await _copilot(reg, FakeTunnel(), RecordingCallLog()).status({}, session=_Session())
    assert {"os": "windows", "arch": "x86_64", "channel": "stable", "version": "2.7.0"} in out["staged"]
    assert [a["agent_id"] for a in out["outdated"]] == ["linus-pc"]


async def test_the_update_tool_sends_the_staged_binary_and_audits_it():
    reg = _registry(linus_pc={"version": "2.5.1"})
    tunnel, log = FakeTunnel(), RecordingCallLog()
    out = await _copilot(reg, tunnel, log).update({"version": "2.7.0"}, session=_Session())

    assert out["version"] == "2.7.0"
    (frame,) = tunnel.sent
    assert frame["args"]["sha256"] == SHA and frame["args"]["url"].startswith("https://")
    assert log.rows == [
        {"agent_id": "linus-pc", "tool": "agent_update_staged", "args": {"version": "2.7.0"},
         "ok": True, "error": None}
    ]


async def test_the_update_tool_refuses_a_version_other_than_the_one_confirmed():
    reg = _registry(linus_pc={"version": "2.5.1"})
    tunnel, log = FakeTunnel(), RecordingCallLog()
    with pytest.raises(ToolError) as exc:
        await _copilot(reg, tunnel, log).update({"version": "2.6.0"}, session=_Session())
    assert exc.value.code == "version_mismatch"
    assert tunnel.sent == []  # nothing shipped that the operator did not confirm
    assert log.rows[0]["ok"] is False


async def test_the_update_tool_needs_a_version_and_a_target():
    cu = _copilot(_registry(linus_pc={"version": "2.5.1"}), FakeTunnel(), RecordingCallLog())
    with pytest.raises(ToolError) as exc:
        await cu.update({}, session=_Session())
    assert exc.value.code == "bad_args"

    class NoSelection:
        agent_id = ""
        principal = None

    with pytest.raises(ToolError) as exc:
        await cu.update({"version": "2.7.0"}, session=NoSelection())
    assert exc.value.code == "no_agent"


async def test_a_scoped_principal_cannot_update_a_host_outside_its_scope():
    class Scoped:
        scoped = True
        username = "kid"

        def may_see(self, agent_id: str) -> bool:
            return agent_id == "own-pc"

    class Session:
        agent_id = "linus-pc"
        principal = Scoped()

    reg = _registry(linus_pc={"version": "2.5.1"})
    tunnel = FakeTunnel()
    with pytest.raises(ToolError) as exc:
        await _copilot(reg, tunnel, RecordingCallLog()).update({"version": "2.7.0"}, session=Session())
    assert exc.value.code == "forbidden"
    assert tunnel.sent == []


# -- the seams: tiers, catalogs, surfaces ----------------------------------------------


def test_the_tools_are_in_the_catalog_with_the_right_tiers():
    assert COPILOT_UPDATE_TOOLS == {"agent_update_status", "agent_update_staged"}
    assert COPILOT_UPDATE_TOOLS <= set(SERVER_TOOLS)
    assert classify("agent_update_status") == READ_ONLY
    # The change half is confirm-gated on the dashboard like the raw update.
    assert classify("agent_update_staged") == NORMAL_CHANGE
    schemas = {s["name"]: s for s in build_tool_schemas()}
    assert "requires operator confirmation" in schemas["agent_update_staged"]["description"]
    assert schemas["agent_update_staged"]["input_schema"]["required"] == ["version"]
    # The model is never asked for the two values it cannot know.
    assert set(schemas["agent_update_staged"]["input_schema"]["properties"]) == {"version", "agent_id"}


def test_no_ticket_session_and_no_steward_can_hold_the_tools():
    from kenny_server.stewards.builder import tool_refusal
    from kenny_server.stewards.catalog import COPILOT_ONLY_TOOLS
    from kenny_server.ticket_assistant import EXCLUDED_TOOLS, TRIAGE_TOOLS

    assert COPILOT_UPDATE_TOOLS <= EXCLUDED_TOOLS
    assert COPILOT_UPDATE_TOOLS <= COPILOT_ONLY_TOOLS
    assert not (COPILOT_UPDATE_TOOLS & TRIAGE_TOOLS)
    for tool in COPILOT_UPDATE_TOOLS:
        assert tool_refusal(tool) is not None


async def test_the_executor_routes_the_tools_to_the_handler():
    """Joined: registered on a real executor, not just callable on the class."""

    from kenny_server.store import EventStore, TelemetryStore
    from kenny_server.tools import CallLog, ScreenshotStore
    from kenny_server.tunnel import AgentTunnel

    reg = _registry(linus_pc={"version": "2.5.1"})
    store = TelemetryStore(db_path=":memory:")
    tunnel = AgentTunnel(reg, store, EventStore(db_path=":memory:"))
    executor = ToolExecutor(
        registry=reg,
        store=store,
        tunnel=tunnel,
        call_log=CallLog(event_store=EventStore(db_path=":memory:")),
        screenshots=ScreenshotStore(),
    )
    _copilot(reg, FakeTunnel(), RecordingCallLog()).register_tools(executor)

    out = await executor.run_server_tool("agent_update_status", {}, session=_Session())
    assert [a["agent_id"] for a in out["outdated"]] == ["linus-pc"]


# -- telling the operator ----------------------------------------------------------------


class Channel:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, note):
        self.sent.append(note)


async def test_one_batched_offer_per_staged_version():
    reg = _registry(linus_pc={"version": "2.5.1"}, thomas_pc={"version": "2.6.0"})
    ch = Channel()
    offers = UpdateOffers(
        registry=reg, update_store=FakeUpdateStore(), notifiers=lambda: [ch], delay_s=0
    )

    first = await offers.announce()
    assert first is not None and len(ch.sent) == 1
    assert "linus-pc: 2.5.1 -> 2.7.0" in ch.sent[0].body
    assert "thomas-pc: 2.6.0 -> 2.7.0" in ch.sent[0].body
    assert ch.sent[0].event_type == "agent_outdated"

    assert await offers.announce() is None  # nothing new: no second message
    assert len(ch.sent) == 1

    # A newly reconnecting agent that is behind is announced alone, not the others again.
    reg2 = _registry(
        linus_pc={"version": "2.5.1"}, thomas_pc={"version": "2.6.0"}, anna_pc={"version": "2.4.0"}
    )
    offers._registry = reg2  # noqa: SLF001
    await offers.announce()
    assert len(ch.sent) == 2
    assert "anna-pc" in ch.sent[1].body and "linus-pc" not in ch.sent[1].body


async def test_an_agent_that_caught_up_is_announced_again_for_the_next_release():
    reg = _registry(linus_pc={"version": "2.5.1"})
    ch = Channel()
    offers = UpdateOffers(
        registry=reg, update_store=FakeUpdateStore(), notifiers=lambda: [ch], delay_s=0
    )
    await offers.announce()
    offers._registry = _registry(linus_pc={"version": "2.7.0"})  # noqa: SLF001 - it updated
    assert await offers.announce() is None
    offers._registry = _registry(linus_pc={"version": "2.5.1"})  # noqa: SLF001 - behind again
    assert await offers.announce() is not None
    assert len(ch.sent) == 2


async def test_without_a_channel_nothing_is_marked_announced():
    reg = _registry(linus_pc={"version": "2.5.1"})
    offers = UpdateOffers(
        registry=reg, update_store=FakeUpdateStore(), notifiers=lambda: [], delay_s=0
    )
    await offers.announce()
    ch = Channel()
    offers._notifiers = lambda: [ch]  # noqa: SLF001 - a channel is configured later
    assert await offers.announce() is not None  # still owed to the operator
    assert len(ch.sent) == 1


async def test_a_dead_channel_does_not_raise_or_hide_the_others():
    class Dead:
        name = "dead"

        async def send(self, note):
            raise RuntimeError("down")

    ok = Channel()
    reg = _registry(linus_pc={"version": "2.5.1"})
    offers = UpdateOffers(
        registry=reg, update_store=FakeUpdateStore(), notifiers=lambda: [Dead(), ok], delay_s=0
    )
    await offers.announce()
    assert len(ok.sent) == 1


async def test_connect_hook_never_raises_and_batches():
    reg = _registry(linus_pc={"version": "2.5.1"})
    ch = Channel()
    offers = UpdateOffers(
        registry=reg, update_store=FakeUpdateStore(), notifiers=lambda: [ch], delay_s=0.01
    )
    await offers.on_agent_connect("linus-pc")
    await offers.on_agent_connect("linus-pc")  # a second connect inside the window adds nothing
    import asyncio

    await asyncio.sleep(0.1)
    assert len(ch.sent) == 1


def test_module_exports_what_main_wires():
    for name in agent_updates.__all__:
        assert hasattr(agent_updates, name)


# -- joined: the real chat loop and its confirm-gate ---------------------------------------


async def test_chat_holds_the_update_at_the_confirm_gate_until_the_operator_approves(tmp_path):
    """The change tier reaches the dashboard gate for a *server* tool too, and what
    runs after the approval is the staged artifact, not anything the model supplied."""

    from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

    from kenny_server.chat import ChatExecutor, FleetSession, confirm_pending, run_turn
    from kenny_server.store import TelemetryStore
    from kenny_server.tools import ScreenshotStore

    store = TelemetryStore(db_path=str(tmp_path / "chat.sqlite"))
    await store.connect()
    try:
        reg = _registry(linus_pc={"version": "2.5.1"})
        tunnel = FakeTunnel()
        executor = ChatExecutor(
            registry=reg,
            store=store,
            tunnel=tunnel,  # type: ignore[arg-type]
            call_log=RecordingCallLog(),  # type: ignore[arg-type]
            screenshots=ScreenshotStore(),
        )
        CopilotUpdates(
            registry=reg,
            tunnel=tunnel,  # type: ignore[arg-type]
            share_links=ShareLinks(),
            update_store=FakeUpdateStore(),
            call_log=RecordingCallLog(),
        ).register_tools(executor)

        session = FleetSession(id="upd")
        session.agent_id = "linus-pc"
        client = FakeAnthropic(
            [
                _Response(
                    [tool_use_block("t1", "agent_update_staged", {"version": "2.7.0"})], "tool_use"
                ),
                _Response([text_block("Linus-PC is updating to 2.7.0.")], "end_turn"),
            ]
        )

        held = await run_turn(session, "update linus-pc", executor=executor, client=client)
        assert held.done is False and held.pending is not None
        assert held.pending["tool"] == "agent_update_staged"
        assert held.pending["args"] == {"version": "2.7.0"}
        assert tunnel.sent == []  # nothing shipped before the approval

        done = await confirm_pending(session, approve=True, executor=executor, client=client)
        assert done.done is True
        (frame,) = tunnel.sent
        assert frame["tool"] == "agent_update"
        assert frame["args"]["sha256"] == SHA
        assert frame["args"]["version"] == "2.7.0"
    finally:
        await store.close()

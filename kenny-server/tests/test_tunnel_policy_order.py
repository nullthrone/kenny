"""The first ``policy`` frame reaches an agent before the agent becomes routable.

The handshake authenticates, sends the agent its ``policy`` frame, and only then
marks the connection online in the registry — so no forwarded request (for
example ``telemetry_collect``) can reach the agent ahead of its deny rules,
shell mode and ``collect`` gate (ADR-0020, ADR-0064, ADR-0069). Covered on both
handshake paths, since each authenticates separately.
"""

from __future__ import annotations

import base64
import json
import secrets

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kenny_server.keystore import KeyStore, build_transcript
from kenny_server.registry import AgentRegistry
from kenny_server.store import EventStore, TelemetryStore, WebFilterStore
from kenny_server.tunnel import AgentTunnel
from kenny_server.webfilter import WebFilterService
from test_server_e2e import SERVER_SEED_B64
from test_webfilter import _StubCache

_META = {"hostname": "h", "os": "windows", "version": "1"}


class _HandshakeSocket:
    """Plays the agent side of either handshake and records what the server sent.

    Every ``send_json`` notes whether the agent was already routable at that
    moment, which is the ordering under test.
    """

    def __init__(self, registry: AgentRegistry, *, signed: bool) -> None:
        self.registry = registry
        self.signed = signed
        self.key = Ed25519PrivateKey.generate()
        self.client_nonce = secrets.token_bytes(32)
        self.sent: list[tuple[dict, bool]] = []
        self.closed_code: int | None = None
        self._step = 0

    @property
    def public_key_b64(self) -> str:
        raw = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode()

    async def receive_text(self) -> str:
        self._step += 1
        if self._step == 1:
            frame: dict = {"type": "register", "agent_id": "pc1", "meta": _META}
            if self.signed:
                frame["protocol"] = "0.20"
                frame["client_nonce"] = base64.b64encode(self.client_nonce).decode()
            else:
                frame["token"] = "tok"
            return json.dumps(frame)
        if self._step == 2 and self.signed:
            challenge = self.sent[-1][0]
            transcript = build_transcript(
                "pc1", self.client_nonce, base64.b64decode(challenge["server_nonce"])
            )
            sig = base64.b64encode(self.key.sign(transcript)).decode()
            return json.dumps({"type": "auth", "agent_sig": sig})
        raise AssertionError("unexpected receive_text")

    async def send_json(self, payload: dict) -> None:
        agent = self.registry.get("pc1")
        self.sent.append((payload, bool(agent is not None and agent.online)))

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


@pytest.fixture
async def parts(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    db = str(tmp_path / "order.sqlite")
    store, events, wf, keys = (
        TelemetryStore(db), EventStore(db), WebFilterStore(db), KeyStore(db)
    )
    for s in (store, events, wf, keys):
        await s.connect()
    registry = AgentRegistry(tokens={"pc1": "tok"}, key_store=keys)
    service = WebFilterService(wf, _StubCache())
    tunnel = AgentTunnel(registry, store, events, webfilter=service)
    yield tunnel, registry, service, keys
    for s in (store, events, wf, keys):
        await s.close()


def _policies(ws: _HandshakeSocket) -> list[tuple[dict, bool]]:
    return [(p, online) for p, online in ws.sent if p["type"] == "policy"]


@pytest.mark.parametrize("signed", [True, False], ids=["signature", "token"])
async def test_policy_is_delivered_before_the_agent_is_routable(parts, signed: bool) -> None:
    tunnel, registry, _service, keys = parts
    ws = _HandshakeSocket(registry, signed=signed)
    await keys.enroll("pc1", ws.public_key_b64)

    assert await tunnel._handshake(ws) == "pc1"

    assert ws.closed_code is None
    policies = _policies(ws)
    assert len(policies) == 1
    frame, online_when_sent = policies[0]
    assert online_when_sent is False
    assert frame["collect"] == {"web_activity": False}
    assert registry.get("pc1").online is True


async def test_a_change_during_the_handshake_is_re_sent(parts, monkeypatch) -> None:
    """A config change between building the frame and registering cannot reach
    the agent through the registry, so the handshake re-sends a moved frame."""

    tunnel, registry, service, _keys = parts
    ws = _HandshakeSocket(registry, signed=False)
    original = tunnel._policy_frame
    builds = 0

    async def _frame(agent_id: str) -> dict:
        nonlocal builds
        builds += 1
        if builds == 2:  # the re-check after registering: a change landed meanwhile
            await service.store.set_config("pc1", enforcement="log_only")
        return await original(agent_id)

    monkeypatch.setattr(tunnel, "_policy_frame", _frame)

    assert await tunnel._handshake(ws) == "pc1"
    policies = _policies(ws)
    assert [p["collect"]["web_activity"] for p, _ in policies] == [False, True]
    assert [online for _, online in policies] == [False, True]


async def test_an_unchanged_frame_is_not_sent_twice(parts) -> None:
    tunnel, registry, _service, _keys = parts
    ws = _HandshakeSocket(registry, signed=False)
    assert await tunnel._handshake(ws) == "pc1"
    assert len(_policies(ws)) == 1


async def test_a_policy_failure_never_breaks_the_handshake(parts, monkeypatch) -> None:
    tunnel, registry, service, _keys = parts

    async def boom(agent_id):
        raise RuntimeError("config read failed")

    monkeypatch.setattr(service, "get_config", boom)
    ws = _HandshakeSocket(registry, signed=False)

    assert await tunnel._handshake(ws) == "pc1"
    assert ws.closed_code is None
    assert _policies(ws) == []
    assert registry.get("pc1").online is True

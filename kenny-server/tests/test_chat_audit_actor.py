"""The dashboard copilot's forwarded calls are audited under the operator who drove them.

Joined through the real app: first-run setup creates a real principal, the
request goes through ``OperatorAuthMiddleware`` and ``/api/chat/stream``, the
fake model asks for a read-only capability tool, and the audit row the
``CallLog`` persists must name that principal.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from test_chat import FakeAnthropic, _Response, text_block, tool_use_block

from kenny_server.main import build_app


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def test_copilot_read_only_call_is_audited_under_the_logged_in_operator(tmp_path) -> None:
    # One shared client: the factory is consulted by several features, and the
    # scripted two-step turn must be served in order whoever asks first.
    client = FakeAnthropic(
        [
            _Response(
                [tool_use_block("tu1", "fs_list", {"path": "C:\\", "password": "hunter2"})],
                "tool_use",
            ),
            _Response([text_block("Listed.")], "end_turn"),
        ]
    )
    app = build_app(db_path=str(tmp_path / "audit.sqlite"), client_factory=lambda: client)

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        return {"entries": []}

    app.state.tunnel.send_request = fake_send_request  # type: ignore[assignment]

    with TestClient(app) as c:
        r = c.post(
            "/setup",
            data={"username": "admin", "password": "pw-123456"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        r = c.post("/api/chat/stream", json={"message": "list C:", "agent_id": "pc1"})
        assert r.status_code == 200
        assert '"ok": true' in r.text or '"ok":true' in r.text

        entries = c.portal.call(app.state.call_log.list)

    [entry] = [e for e in entries if e["tool"] == "fs_list"]
    assert entry["agent_id"] == "pc1"
    assert entry["actor"] == "admin"
    assert entry["run_id"] is None
    # Redaction applies on the way into the audit trail, not just in the helper.
    assert entry["args"] == {"path": "C:\\", "password": "[redacted]"}

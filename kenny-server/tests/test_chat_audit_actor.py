"""The dashboard copilot's forwarded calls are audited under the operator who drove them.

Joined through the real app: first-run setup creates a real principal, the
request goes through ``OperatorAuthMiddleware`` and ``/api/chat/stream``, the
fake model asks for a read-only capability tool, and the audit row the
``CallLog`` persists must name that principal.
"""

from __future__ import annotations

import asyncio
import threading

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


def test_two_operators_on_one_session_are_each_audited_as_themselves(tmp_path) -> None:
    """Two concurrent turns on one session id, by two operators.

    Operator one's turn makes a call, then waits inside the tunnel while
    operator two's whole turn runs on the same session; then operator one's
    turn makes a second call. Both of one's calls must be audited as one: an
    actor stored on the shared session would have been overwritten by two's
    request in between.
    """

    client = FakeAnthropic(
        [
            # one's first model call
            _Response([tool_use_block("a1", "fs_list", {"path": "C:\\one-1"})], "tool_use"),
            # two's turn, run entirely while one waits
            _Response([tool_use_block("b1", "fs_list", {"path": "C:\\two"})], "tool_use"),
            _Response([text_block("Two done.")], "end_turn"),
            # one resumes
            _Response([tool_use_block("a2", "fs_list", {"path": "C:\\one-2"})], "tool_use"),
            _Response([text_block("One done.")], "end_turn"),
        ]
    )
    app = build_app(db_path=str(tmp_path / "audit2.sqlite"), client_factory=lambda: client)
    one_waiting = threading.Event()
    release_one = threading.Event()

    async def fake_send_request(agent_id, tool, args, timeout_s):  # type: ignore[no-untyped-def]
        if args.get("path") == "C:\\one-1":
            one_waiting.set()
            while not release_one.is_set():
                await asyncio.sleep(0.01)
        return {"entries": []}

    app.state.tunnel.send_request = fake_send_request  # type: ignore[assignment]

    def pat_for(c: TestClient, username: str) -> str:
        users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
        return c.post(f"/api/users/{users[username]['id']}/pats", json={"label": "t"}).json()[
            "token"
        ]

    with TestClient(app) as c:
        r = c.post(
            "/setup", data={"username": "admin", "password": "pw-123456"}, follow_redirects=False
        )
        assert r.status_code == 303
        for name in ("one", "two"):
            assert c.post(
                "/api/users", json={"username": name, "password": "pw-123456", "role": "operator"}
            ).status_code == 201
        headers = {name: {"Authorization": f"Bearer {pat_for(c, name)}"} for name in ("one", "two")}
        body = {"session_id": "shared-session", "agent_id": "pc1"}
        results: dict[str, str] = {}

        def drive_one() -> None:
            r = c.post("/api/chat/stream", json={**body, "message": "one"}, headers=headers["one"])
            results["one"] = r.text

        thread = threading.Thread(target=drive_one)
        thread.start()
        try:
            assert one_waiting.wait(timeout=10)
            r = c.post("/api/chat/stream", json={**body, "message": "two"}, headers=headers["two"])
            assert r.status_code == 200 and "Two done." in r.text
        finally:
            release_one.set()
            thread.join(timeout=10)
        assert "One done." in results["one"]
        entries = c.portal.call(app.state.call_log.list)

    by_path = {e["args"]["path"]: e["actor"] for e in entries if e["tool"] == "fs_list"}
    assert by_path == {"C:\\one-1": "one", "C:\\two": "two", "C:\\one-2": "one"}

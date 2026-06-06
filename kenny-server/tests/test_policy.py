"""ADR-0021: shared-catalog mirror, operator PolicyStore, and the operator API.

The PolicyEngine here mirrors the agent's deterministic guard (best-effort UX,
fail-open). These tests do not depend on the Rust agent; they exercise the
catalog loaded from ``docs/policy/deny_rules.json`` plus operator-set rules.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kenny_server.main import build_app
from kenny_server.policy import PolicyEngine
from kenny_server.store import PolicyStore


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


# -- PolicyEngine mirror ----------------------------------------------------


def test_engine_blocks_destructive_powershell() -> None:
    engine = PolicyEngine()
    hit = engine.check("powershell_exec", {"script": "Format-Volume -DriveLetter D"})
    assert hit is not None
    code, reason = hit
    assert code == "blocked"
    assert "Format-Volume" in reason


def test_engine_blocks_sam_hive_fs_read() -> None:
    engine = PolicyEngine()
    hit = engine.check(
        "fs_read", {"path": r"C:\Windows\System32\config\SAM"}
    )
    assert hit is not None
    assert hit[0] == "blocked"


def test_engine_normalises_forward_slashes_for_path() -> None:
    engine = PolicyEngine()
    # Forward-slash path should still hit the path rule after / -> \ normalisation.
    hit = engine.check("fs_read", {"path": "C:/Windows/System32/config/SYSTEM"})
    assert hit is not None


def test_engine_permits_benign_call() -> None:
    engine = PolicyEngine()
    assert engine.check(
        "powershell_exec", {"script": "Get-Process | Select-Object -First 5"}
    ) is None
    assert engine.check("fs_read", {"path": r"C:\Users\papa\notes.txt"}) is None


def test_engine_does_not_mirror_agent_update() -> None:
    engine = PolicyEngine()
    # agent_update host allowlist is agent-only; the mirror always returns None.
    assert engine.check(
        "agent_update",
        {"version": "9.9.9", "url": "http://evil.example/x", "sha256": "deadbeef"},
    ) is None


def test_engine_enforces_operator_rule() -> None:
    engine = PolicyEngine()
    # Benign before the operator rule is set.
    assert engine.check("powershell_exec", {"script": "choco install foo"}) is None
    engine.set_operator_rules(
        [
            {
                "id": "op_block_choco",
                "applies_to": "powershell",
                "pattern": r"(?i)\bchoco\b",
                "reason": "operator: block chocolatey",
            }
        ]
    )
    hit = engine.check("powershell_exec", {"script": "choco install foo"})
    assert hit is not None
    assert hit[1] == "operator: block chocolatey"


def test_engine_bad_operator_pattern_is_skipped_not_fatal() -> None:
    engine = PolicyEngine()
    # An invalid regex must be skipped (logged), not raise.
    engine.set_operator_rules(
        [{"id": "bad", "applies_to": "powershell", "pattern": "(", "reason": "x"}]
    )
    assert engine.check("powershell_exec", {"script": "anything"}) is None


def test_engine_self_protection_concatenates_winget_args() -> None:
    engine = PolicyEngine()
    # winget args are concatenated and matched against self_protection.
    hit = engine.check("winget_uninstall", {"id": "kenny-agent.exe"})
    assert hit is not None
    assert hit[0] == "blocked"


# -- PolicyStore round-trip -------------------------------------------------


@pytest.mark.asyncio
async def test_policy_store_add_list_remove(tmp_path) -> None:
    store = PolicyStore(str(tmp_path / "policy.sqlite"))
    await store.connect()
    try:
        assert await store.list() == []
        await store.add(
            id="op1", applies_to="powershell", pattern=r"\bfoo\b", reason="r1"
        )
        await store.add(
            id="op2", applies_to="path", pattern=r"\\bar\\", reason="r2"
        )
        rules = await store.list()
        assert [r["id"] for r in rules] == ["op1", "op2"]
        assert rules[0] == {
            "id": "op1",
            "applies_to": "powershell",
            "pattern": r"\bfoo\b",
            "reason": "r1",
        }
        # INSERT OR REPLACE: same id updates in place.
        await store.add(
            id="op1", applies_to="powershell", pattern=r"\bbaz\b", reason="r1b"
        )
        rules = await store.list()
        op1 = next(r for r in rules if r["id"] == "op1")
        assert op1["pattern"] == r"\bbaz\b"
        # Remove.
        assert await store.remove("op1") is True
        assert await store.remove("nope") is False
        assert {r["id"] for r in await store.list()} == {"op2"}
    finally:
        await store.close()


# -- Operator API -----------------------------------------------------------


def test_policy_api_add_list_remove(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api.sqlite"))
    with TestClient(app) as c:
        # GET shows built-ins from the catalog and no operator rules yet.
        body = c.get("/api/policy/rules", headers=_bearer(app)).json()
        assert any(r["id"] == "ps_format_volume" for r in body["builtin"])
        assert body["operator"] == []

        # POST adds an operator rule.
        resp = c.post(
            "/api/policy/rules",
            headers=_bearer(app),
            json={
                "id": "op_block_choco",
                "applies_to": "powershell",
                "pattern": r"(?i)\bchoco\b",
                "reason": "operator: block chocolatey",
            },
        )
        assert resp.status_code == 200
        assert [r["id"] for r in resp.json()["operator"]] == ["op_block_choco"]

        # GET now lists it and the engine enforces it via the mirror.
        body = c.get("/api/policy/rules", headers=_bearer(app)).json()
        assert [r["id"] for r in body["operator"]] == ["op_block_choco"]
        assert app.state.policy_engine.check(
            "powershell_exec", {"script": "choco install x"}
        ) is not None

        # DELETE removes it.
        resp = c.request(
            "DELETE", "/api/policy/rules/op_block_choco", headers=_bearer(app)
        )
        assert resp.status_code == 200
        assert resp.json()["removed"] is True
        body = c.get("/api/policy/rules", headers=_bearer(app)).json()
        assert body["operator"] == []
        assert app.state.policy_engine.check(
            "powershell_exec", {"script": "choco install x"}
        ) is None


def test_policy_api_bad_pattern_is_400(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_bad.sqlite"))
    with TestClient(app) as c:
        resp = c.post(
            "/api/policy/rules",
            headers=_bearer(app),
            json={
                "id": "bad",
                "applies_to": "powershell",
                "pattern": "(unterminated",
                "reason": "x",
            },
        )
        assert resp.status_code == 400


def test_policy_api_bad_applies_to_is_400(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "api_bad2.sqlite"))
    with TestClient(app) as c:
        resp = c.post(
            "/api/policy/rules",
            headers=_bearer(app),
            json={
                "id": "bad",
                "applies_to": "nonsense",
                "pattern": r"\bfoo\b",
                "reason": "x",
            },
        )
        assert resp.status_code == 400

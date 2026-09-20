"""ADR-0020: shared-catalog mirror, operator PolicyStore, and the operator API.

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


def test_engine_blocks_destructive_shell() -> None:
    engine = PolicyEngine()
    hit = engine.check("shell_exec", {"command": "rm -rf /"})
    assert hit is not None
    code, reason = hit
    assert code == "blocked"
    assert "rm -rf" in reason


def test_engine_blocks_posix_self_protection() -> None:
    engine = PolicyEngine()
    hit = engine.check("shell_exec", {"command": "systemctl stop kenny-agent"})
    assert hit is not None
    assert hit[0] == "blocked"


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
    assert engine.check("fs_read", {"path": r"C:\Users\testuser\notes.txt"}) is None
    assert engine.check("shell_exec", {"command": "uname -a"}) is None


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


def test_engine_blocks_destructive_command_smuggled_in_net_arg() -> None:
    engine = PolicyEngine()
    # net_adapter_reset interpolates the adapter name into a PowerShell command on
    # the agent; a destructive command hidden in the name must be caught by the
    # powershell catalog (not just self_protection), mirroring the agent guard.
    hit = engine.check(
        "net_adapter_reset",
        {"name": "Ethernet'; Format-Volume -DriveLetter D -Force; '"},
    )
    assert hit is not None
    assert hit[0] == "blocked"
    assert "Format-Volume" in hit[1]
    # Same for a destructive command smuggled into a winget arg.
    assert engine.check(
        "winget_install", {"id": "x; vssadmin delete shadows /all"}
    ) is not None
    # A benign adapter name still passes.
    assert engine.check("net_adapter_reset", {"name": "Ethernet"}) is None
    assert engine.check("net_adapter_reset", {"name": "Wi-Fi"}) is None


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
        assert any(r["id"] == "posix_rm_rf_root" for r in body["builtin"])
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


# -- fleet shell execution mode (ADR-0064) ----------------------------------


def _engine(mode: str = "unrestricted", allow: list[dict] | None = None) -> PolicyEngine:
    engine = PolicyEngine()
    engine.set_shell_policy(mode, allow or [])
    return engine


def test_shell_mode_defaults_to_unrestricted() -> None:
    # A fresh engine must not refuse what the fleet has not been configured to
    # refuse: the mirror is not allowed to be stricter than the policy.
    assert PolicyEngine().shell_mode() == "unrestricted"
    assert PolicyEngine().check("shell_exec", {"command": "uname -a"}) is None


def test_shell_mode_off_blocks_both_shells_and_nothing_else() -> None:
    engine = _engine("off")
    assert engine.check("shell_exec", {"command": "uname -a"}) is not None
    assert engine.check("powershell_exec", {"script": "Get-Process"}) is not None
    assert engine.check("winget_list", {}) is None


def test_shell_allowlist_matches_the_whole_command_only() -> None:
    # The anchoring property. A substring match here would turn every allow
    # entry into a prefix onto which an attacker can append anything.
    engine = _engine("allowlist", [
        {"id": "a", "applies_to": "posix", "pattern": "uname -a", "reason": "ok"}
    ])
    assert engine.check("shell_exec", {"command": "uname -a"}) is None
    assert engine.check("shell_exec", {"command": "  uname -a\n"}) is None
    assert engine.check("shell_exec", {"command": "uname -a; rm -rf /home"}) is not None
    assert engine.check("shell_exec", {"command": "echo hi && uname -a"}) is not None
    assert engine.check("shell_exec", {"command": "uname -a\nid"}) is not None


def test_shell_allowlist_empty_blocks_everything() -> None:
    engine = _engine("allowlist")
    assert engine.check("shell_exec", {"command": "uname -a"}) is not None


def test_shell_allowlist_never_lifts_a_deny_rule() -> None:
    # Deny outranks allow, whatever the operator writes in the allowlist.
    engine = _engine("allowlist", [
        {"id": "a", "applies_to": "posix", "pattern": ".*", "reason": "everything"}
    ])
    hit = engine.check("shell_exec", {"command": "rm -rf /"})
    assert hit is not None and "rm -rf" in hit[1]


def test_shell_allow_entries_are_per_shell() -> None:
    engine = _engine("allowlist", [
        {"id": "a", "applies_to": "posix", "pattern": "uname -a", "reason": "ok"}
    ])
    assert engine.check("powershell_exec", {"script": "uname -a"}) is not None


def test_shell_allow_ignores_entries_for_surfaces_without_a_command() -> None:
    engine = _engine("allowlist", [
        {"id": "a", "applies_to": "path", "pattern": "uname -a", "reason": "bogus"},
        {"id": "b", "applies_to": "self_protection", "pattern": "uname -a", "reason": "x"},
    ])
    assert engine.check("shell_exec", {"command": "uname -a"}) is not None


def test_shell_allowlist_fails_closed_on_missing_or_non_string_args() -> None:
    engine = _engine("allowlist", [
        {"id": "a", "applies_to": "posix", "pattern": "uname -a", "reason": "ok"}
    ])
    assert engine.check("shell_exec", {}) is not None
    assert engine.check("shell_exec", {"command": 42}) is not None


def test_unknown_shell_mode_falls_back_to_unrestricted() -> None:
    # Neither guessing stricter (which takes fleet administration down over a
    # typo) nor guessing looser (a policy nobody set): fall back to the default.
    engine = _engine("nonsense")
    assert engine.shell_mode() == "unrestricted"
    assert engine.check("shell_exec", {"command": "uname -a"}) is None


# -- shell allowlist API + the pushed frame (ADR-0064) ----------------------


def _setup_admin(c) -> None:
    r = c.post(
        "/setup", data={"username": "admin", "password": "pw-123456"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_shell_allow_api_round_trip(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "shell_api.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        assert c.get("/api/policy/shell-allow", headers=h).json() == {
            "mode": "unrestricted",
            "allow": [],
        }
        resp = c.post(
            "/api/policy/shell-allow",
            headers=h,
            json={
                "id": "al_uname",
                "applies_to": "posix",
                "pattern": "uname -a",
                "reason": "read kernel version",
            },
        )
        assert resp.status_code == 200
        assert [r["id"] for r in resp.json()["allow"]] == ["al_uname"]

        c.put(
            "/api/settings/KENNY_SHELL_POLICY_MODE",
            headers=h,
            json={"value": "allowlist"},
        )
        body = c.get("/api/policy/shell-allow", headers=h).json()
        assert body["mode"] == "allowlist"

        assert c.delete("/api/policy/shell-allow/al_uname", headers=h).json()["allow"] == []


def test_shell_allow_api_rejects_bad_input(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "shell_api_bad.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        base = {"id": "x", "applies_to": "posix", "pattern": "ok", "reason": "r"}
        for patch, why in (
            ({"id": ""}, "id is required"),
            # An allow rule is matched against a command string, so the two
            # surfaces that have none cannot carry one.
            ({"applies_to": "path"}, "path has no command string"),
            ({"applies_to": "self_protection"}, "nor does self_protection"),
            ({"applies_to": "nonsense"}, "unknown group"),
            ({"pattern": "(unclosed"}, "uncompilable pattern"),
            ({"pattern": ""}, "pattern is required"),
            ({"reason": ""}, "reason is required"),
        ):
            resp = c.post("/api/policy/shell-allow", headers=h, json={**base, **patch})
            assert resp.status_code == 400, f"{patch} should be rejected: {why}"


def test_deny_rules_accept_posix(tmp_path) -> None:
    # The contract, the mirror and the agent guard all carry the `posix` group;
    # the API validator used to omit it, so no Linux shell rule could be added.
    app = build_app(db_path=str(tmp_path / "posix_rule.sqlite"))
    with TestClient(app) as c:
        resp = c.post(
            "/api/policy/rules",
            headers=_bearer(app),
            json={
                "id": "op_no_curl",
                "applies_to": "posix",
                "pattern": r"\bcurl\b",
                "reason": "operator: no ad-hoc downloads",
            },
        )
        assert resp.status_code == 200
        assert [r["id"] for r in resp.json()["operator"]] == ["op_no_curl"]


def test_shell_policy_is_superuser_only(tmp_path) -> None:
    # The control that governs shell_exec must not be relaxable by a principal
    # that can call shell_exec. An operator may still ADD a deny rule.
    app = build_app(db_path=str(tmp_path / "shell_rbac.sqlite"))
    with TestClient(app) as c:
        _setup_admin(c)
        assert c.post("/api/users", json={
            "username": "op", "password": "pw-123456", "role": "operator"}
        ).status_code == 201
        uid = {u["username"]: u["id"] for u in c.get("/api/users").json()["users"]}["op"]
        op_pat = c.post(f"/api/users/{uid}/pats", json={"label": "t"}).json()["token"]

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {op_pat}"}
        assert c.get("/api/policy/shell-allow", headers=h).status_code == 403
        assert c.post("/api/policy/shell-allow", headers=h, json={}).status_code == 403
        assert c.delete("/api/policy/shell-allow/x", headers=h).status_code == 403
        assert c.put(
            "/api/settings/KENNY_SHELL_POLICY_MODE", headers=h, json={"value": "off"}
        ).status_code == 403
        # Adding a deny rule stays an operator action.
        assert c.post("/api/policy/rules", headers=h, json={
            "id": "op_x", "applies_to": "posix", "pattern": r"\bfoo\b", "reason": "r"}
        ).status_code == 200


async def test_policy_frame_carries_the_shell_policy(tmp_path) -> None:
    # The frame the agents get and the policy the mirror enforces come from one
    # resolver, so they cannot drift.
    app = build_app(db_path=str(tmp_path / "shell_frame.sqlite"))
    with TestClient(app):
        tunnel = app.state.tunnel
        frame = await tunnel._policy_frame()
        assert frame["shell"] == {"mode": "unrestricted", "allow": []}

        await app.state.shell_allow_store.add(
            id="al_uname", applies_to="posix", pattern="uname -a", reason="r"
        )
        await app.state.settings.set("KENNY_SHELL_POLICY_MODE", "allowlist")
        frame = await tunnel._policy_frame()
        assert frame["shell"]["mode"] == "allowlist"
        assert [r["id"] for r in frame["shell"]["allow"]] == ["al_uname"]
        # Resolving refreshed the mirror in the same pass.
        assert tunnel.policy_engine.shell_mode() == "allowlist"
        assert tunnel.policy_engine.check("shell_exec", {"command": "id"}) is not None
        assert tunnel.policy_engine.check("shell_exec", {"command": "uname -a"}) is None

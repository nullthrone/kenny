"""The shipped shell allow rules (docs/policy/shell_allow_defaults.json).

Three seams, each tested joined:

- **The file and the server.** :func:`load_shell_allow_defaults` must yield every rule
  in the file; one it silently drops is a rule the operator believes they have.
- **The server and the agent.** The vectors in
  ``docs/fixtures/vectors/shell_allow_defaults.json`` run here against the mirror and in
  ``kenny-agent/src/policy.rs`` (``shell_allow_defaults_vectors``) against the guard.
- **The store, the API and the pushed frame.** A new database is seeded once; reset
  restores the file and reaches the agents.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kenny_server.main import build_app
from kenny_server.policy import PolicyEngine, load_shell_allow_defaults
from kenny_server.store import ShellAllowStore

REPO = Path(__file__).resolve().parents[2]
DEFAULTS_FILE = REPO / "docs" / "policy" / "shell_allow_defaults.json"
VECTORS_FILE = REPO / "docs" / "fixtures" / "vectors" / "shell_allow_defaults.json"

_FILE_RULES = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))["rules"]
_CASES = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))["cases"]


def _engine() -> PolicyEngine:
    engine = PolicyEngine()
    engine.set_shell_policy("allowlist", load_shell_allow_defaults())
    return engine


def _command(case: dict) -> str:
    return next(iter(case["args"].values()))


def _bearer(app) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.operator_token}"}


# -- the file ------------------------------------------------------------------


def test_loader_yields_every_rule_in_file_order() -> None:
    assert load_shell_allow_defaults() == [
        {k: r[k] for k in ("id", "applies_to", "pattern", "reason")} for r in _FILE_RULES
    ]


def test_rules_are_well_formed() -> None:
    ids = [r["id"] for r in _FILE_RULES]
    assert len(ids) == len(set(ids)), "duplicate id: reset would keep only one"
    for r in _FILE_RULES:
        assert r["applies_to"] in ("posix", "powershell"), r["id"]
        assert r["reason"].strip(), r["id"]
        # \s matches a newline, and both shells run the second line as a second
        # command; tokens must be separated by a literal space.
        assert "\\s" not in r["pattern"], f"{r['id']}: use a literal space, not \\s"
        assert "\\w" not in r["pattern"], f"{r['id']}: \\w is Unicode; spell the ASCII class"
        assert r["pattern"].startswith("(?i)") == (r["applies_to"] == "powershell"), (
            f"{r['id']}: PowerShell is case-insensitive, sh is not"
        )


# -- the vectors (joined with kenny-agent) --------------------------------------


def test_vectors_file_is_present() -> None:
    assert _CASES


@pytest.mark.parametrize("case", _CASES, ids=lambda c: _command(c)[:60])
def test_shell_allow_default_vectors(case: dict) -> None:
    hit = _engine().check(case["tool"], case["args"])
    verdict = "blocked" if hit is not None else "allow"
    assert verdict == case["expect"], (
        f"{_command(case)!r}: expected {case['expect']}, got {verdict}"
        f"{' (' + hit[1] + ')' if hit else ''} - {case['why']}"
    )


def test_every_shipped_rule_is_exercised_by_an_allow_vector() -> None:
    # A rule no vector admits is a rule nobody has checked still does what its
    # reason says.
    unused = set()
    allowed = [(c["tool"], _command(c).strip()) for c in _CASES if c["expect"] == "allow"]
    for rule in load_shell_allow_defaults():
        engine = PolicyEngine()
        engine.set_shell_policy("allowlist", [rule])
        tool = "shell_exec" if rule["applies_to"] == "posix" else "powershell_exec"
        key = "command" if tool == "shell_exec" else "script"
        if not any(t == tool and engine.check(t, {key: c}) is None for t, c in allowed):
            unused.add(rule["id"])
    assert not unused, f"no allow vector exercises {sorted(unused)}"


def test_no_rule_backtracks_catastrophically() -> None:
    # The mirror matches on the server's event loop, so a pattern with overlapping
    # alternatives under a repetition would let one crafted command stall the server.
    # Each allowed command is grown by repeating its own tokens, then made to fail
    # at the very end, which forces the engine through every split it can find.
    engine = _engine()
    worst = 0.0
    for case in _CASES:
        if case["expect"] != "allow":
            continue
        cmd = _command(case)
        toks = cmd.split(" ")
        for probe in (
            " ".join(toks + toks[1:] * 12) + " ;",
            cmd + (" " + toks[-1]) * 40 + " ;",
            cmd + " | Where-Object {$_.a -eq 1}" * 25 + " ;",
            cmd + " | head -n 1" * 30 + " ;",
        ):
            key = next(iter(case["args"]))
            start = time.perf_counter()
            engine.check(case["tool"], {key: probe})
            worst = max(worst, time.perf_counter() - start)
    assert worst < 0.25, f"a crafted command took {worst:.2f}s to reject"


# -- the store ------------------------------------------------------------------


async def test_new_database_is_seeded_once(tmp_path) -> None:
    rules = load_shell_allow_defaults()
    store = ShellAllowStore(str(tmp_path / "seed.sqlite"))
    await store.connect()
    try:
        assert store.created
        assert await store.seed_defaults(rules, seed=True)
        assert [r["id"] for r in await store.list()] == [r["id"] for r in rules]

        # An operator removes one; a restart must not bring it back.
        await store.remove(rules[0]["id"])
        assert not await store.seed_defaults(rules, seed=True)
        assert rules[0]["id"] not in [r["id"] for r in await store.list()]
    finally:
        await store.close()

    reopened = ShellAllowStore(str(tmp_path / "seed.sqlite"))
    await reopened.connect()
    try:
        assert not reopened.created
    finally:
        await reopened.close()


async def test_seeding_never_mixes_into_an_operator_list(tmp_path) -> None:
    store = ShellAllowStore(str(tmp_path / "curated.sqlite"))
    await store.connect()
    try:
        await store.add(id="al_mine", applies_to="posix", pattern="uname -a", reason="r")
        assert not await store.seed_defaults(load_shell_allow_defaults(), seed=True)
        assert [r["id"] for r in await store.list()] == ["al_mine"]
    finally:
        await store.close()


async def test_declined_seed_is_still_the_one_offer(tmp_path) -> None:
    # An existing database in allowlist mode with an empty list: main.py declines,
    # and the decision sticks even if the mode changes later.
    store = ShellAllowStore(str(tmp_path / "declined.sqlite"))
    await store.connect()
    try:
        assert not await store.seed_defaults(load_shell_allow_defaults(), seed=False)
        assert not await store.seed_defaults(load_shell_allow_defaults(), seed=True)
        assert await store.list() == []
    finally:
        await store.close()


async def test_reset_replaces_the_whole_list_in_file_order(tmp_path) -> None:
    rules = load_shell_allow_defaults()
    store = ShellAllowStore(str(tmp_path / "reset.sqlite"))
    await store.connect()
    try:
        await store.add(id="al_mine", applies_to="posix", pattern="id", reason="r")
        await store.add(
            id=rules[0]["id"], applies_to=rules[0]["applies_to"], pattern="changed", reason="r"
        )
        await store.reset_to_defaults(rules, created_by="admin")
        assert await store.list() == rules
    finally:
        await store.close()


# -- the API and the pushed frame ----------------------------------------------


def test_new_server_starts_with_the_shipped_rules(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "fresh.sqlite"))
    with TestClient(app) as c:
        body = c.get("/api/policy/shell-allow", headers=_bearer(app)).json()
        assert body["mode"] == "unrestricted"
        assert body["allow"] == load_shell_allow_defaults()
        assert body["defaults"] == load_shell_allow_defaults()


async def test_reset_restores_defaults_and_reaches_the_agents(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "reset_api.sqlite"))
    with TestClient(app) as c:
        h = _bearer(app)
        for r in c.get("/api/policy/shell-allow", headers=h).json()["allow"]:
            c.delete(f"/api/policy/shell-allow/{r['id']}", headers=h)
        c.post("/api/policy/shell-allow", headers=h, json={
            "id": "al_mine", "applies_to": "posix", "pattern": "cat /etc/shadow", "reason": "r"})
        c.put("/api/settings/KENNY_SHELL_POLICY_MODE", headers=h, json={"value": "allowlist"})

        resp = c.post("/api/policy/shell-allow/reset", headers=h)
        assert resp.status_code == 200
        assert resp.json()["allow"] == load_shell_allow_defaults()

        frame = await app.state.tunnel._policy_frame()
        assert [r["id"] for r in frame["shell"]["allow"]] == [
            r["id"] for r in load_shell_allow_defaults()
        ]
        engine = app.state.tunnel.policy_engine
        assert engine.check("shell_exec", {"command": "cat /etc/shadow"}) is not None
        assert engine.check("shell_exec", {"command": "uname -a"}) is None


def test_reset_is_superuser_only(tmp_path) -> None:
    app = build_app(db_path=str(tmp_path / "reset_rbac.sqlite"))
    with TestClient(app) as c:
        assert c.post(
            "/setup", data={"username": "admin", "password": "pw-123456"},
            follow_redirects=False,
        ).status_code == 303
        assert c.post("/api/users", json={
            "username": "op", "password": "pw-123456", "role": "operator"}
        ).status_code == 201
        uid = {u["username"]: u["id"] for u in c.get("/api/users").json()["users"]}["op"]
        op_pat = c.post(f"/api/users/{uid}/pats", json={"label": "t"}).json()["token"]

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {op_pat}"}
        assert c.post("/api/policy/shell-allow/reset", headers=h).status_code == 403

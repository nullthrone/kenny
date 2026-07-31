"""Account governance across local and Microsoft accounts (ADR-0046).

Three things are worth pinning down here, and they are the three that would
silently rot:

1. **Gate parity.** Every ``account_*`` tool has to appear in the forwarding
   catalog, the Windows OS scope, the operator-role map, and the chat
   confirm-gate. The agent enforces its own copy in ``control::is_mutating``;
   ADR-0024 requires the server to agree, and nothing but a test makes the two
   lists stay in step.
2. **Type-agnosticism is structural.** There must be no per-kind tool and no
   ``kind`` argument anywhere in the catalog — the whole design rests on the
   caller never having to know whether an account is local or Microsoft.
3. **The health rules** read the new sections the way the contract describes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kenny_server.chat import STATE_CHANGING_TOOLS, is_state_changing
from kenny_server.diffs import diff_section
from kenny_server.health_rules import LOGON_FAILURES_WARN, evaluate_section
from kenny_server.tools import (
    _OS_SCOPED_TOOLS,
    _TOOL_MIN_ROLE,
    _TOOL_MIN_TIMEOUT_S,
    CAPABILITY_TOOLS,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "docs" / "fixtures"

ACCOUNT_TOOLS = (
    "account_set_enabled",
    "account_set_admin",
    "account_set_logon_rights",
    "account_create",
    "account_delete",
    "account_session_action",
    "password_policy_set",
)


# --- gate parity -------------------------------------------------------------


@pytest.mark.parametrize("tool", ACCOUNT_TOOLS)
def test_account_tool_is_registered_scoped_and_gated(tool: str) -> None:
    assert tool in CAPABILITY_TOOLS, f"{tool} missing from the forwarding catalog"
    # SAM/LSA/session work has no POSIX equivalent, so a Linux agent must be
    # refused before a frame is ever sent.
    assert _OS_SCOPED_TOOLS.get(tool) == "windows"
    # Deciding who may sign in is operator authority, unlike the rest of the
    # forwarded catalog where seeing the host is enough.
    assert _TOOL_MIN_ROLE.get(tool) == "operator"
    # Mutating on the agent, therefore confirm-gated in chat (ADR-0024 parity).
    assert is_state_changing(tool), f"{tool} must require operator confirmation"
    assert tool in STATE_CHANGING_TOOLS


def test_no_other_forwarded_tool_silently_acquired_a_role_gate() -> None:
    """The role map is the exception, not a creeping default.

    If a tool is added here it should be a deliberate decision recorded in an
    ADR, not something that drifted in — so the set is asserted exactly.
    """

    assert set(_TOOL_MIN_ROLE) == set(ACCOUNT_TOOLS)


def test_session_action_gets_a_timeout_that_survives_its_warning() -> None:
    """The agent may warn the signed-in user and wait (capped at 60 s) before
    acting, so the 30 s default would time out a perfectly healthy call."""

    assert _TOOL_MIN_TIMEOUT_S["account_session_action"] >= 90


@pytest.mark.parametrize("tool", ACCOUNT_TOOLS)
def test_account_tool_has_golden_fixtures(tool: str) -> None:
    request = FIXTURES_DIR / f"request_{tool}.json"
    response = FIXTURES_DIR / f"response_{tool}.json"
    assert request.exists() and response.exists()
    payload = json.loads(request.read_text())
    assert payload["tool"] == tool
    # Fixture args must be a subset of the catalog's declared keys (optional
    # keys are declared with a trailing "?").
    declared = {key.rstrip("?") for key in CAPABILITY_TOOLS[tool]}
    assert set(payload["args"]) <= declared, f"{tool} fixture uses undeclared args"


# --- type-agnosticism --------------------------------------------------------


def test_there_is_no_per_kind_tool_or_kind_argument() -> None:
    """The load-bearing property: one tool family for both account kinds.

    A ``local_account_*``/``msaccount_*`` split, or a ``kind`` argument the
    caller has to supply, would push the distinction back onto every caller —
    which is exactly what ADR-0046 rejected. The agent resolves the account
    itself; the caller passes a SAM name and nothing else.
    """

    for name, keys in CAPABILITY_TOOLS.items():
        assert not name.startswith(("local_account", "msaccount", "microsoft_account"))
        assert "kind" not in {k.rstrip("?") for k in keys}, name

    # And every account-scoped verb takes the same key, so a caller can drive
    # them uniformly. `account_create` is the documented exception: it names a
    # new account rather than addressing an existing one.
    for tool in ACCOUNT_TOOLS:
        if tool in ("account_create", "password_policy_set"):
            continue
        assert CAPABILITY_TOOLS[tool][0] == "principal", tool


def test_account_list_is_not_a_tool() -> None:
    """The inventory is telemetry (``local_accounts``), refreshable on demand
    through ``telemetry_collect`` — a second source of truth would drift."""

    assert "account_list" not in CAPABILITY_TOOLS


# --- health rules ------------------------------------------------------------


def test_logon_failures_warns_per_account_not_on_the_total() -> None:
    """Twenty failures across five accounts is a household forgetting
    passwords; twenty against one account is somebody working at it."""

    spread = {
        "status": "ok",
        "summary": "",
        "window_hours": 24,
        "accounts": [
            {"name": f"u{i}", "count": 4, "types": ["interactive"]} for i in range(5)
        ],
        "unmatched_count": 0,
        "count": 20,
    }
    assert evaluate_section("logon_failures", spread)["status"] == "ok"

    focused = {
        "status": "ok",
        "summary": "",
        "window_hours": 24,
        "accounts": [
            {"name": "papa", "count": LOGON_FAILURES_WARN, "types": ["interactive"]}
        ],
        "unmatched_count": 0,
        "count": LOGON_FAILURES_WARN,
    }
    result = evaluate_section("logon_failures", focused)
    assert result["status"] == "warn"
    assert "papa" in result["reason"]


def test_logon_failures_warns_on_unknown_usernames() -> None:
    """Attempts against names that are not accounts here are spraying or a
    scanner — never a household member mistyping their own name."""

    payload = {
        "status": "ok",
        "summary": "",
        "window_hours": 24,
        "accounts": [],
        "unmatched_count": LOGON_FAILURES_WARN,
        "count": LOGON_FAILURES_WARN,
    }
    result = evaluate_section("logon_failures", payload)
    assert result["status"] == "warn"
    assert "unknown" in result["reason"]


def test_logon_failures_never_escalates_to_crit() -> None:
    payload = {
        "status": "ok",
        "summary": "",
        "window_hours": 24,
        "accounts": [{"name": "papa", "count": 5000, "types": ["remote"]}],
        "unmatched_count": 5000,
        "count": 10000,
    }
    # A failed sign-in is not, by itself, a compromised machine: kenny reports
    # and the parent judges.
    assert evaluate_section("logon_failures", payload)["status"] == "warn"


def test_local_accounts_flags_an_admin_holding_deny_rights() -> None:
    payload = {
        "status": "ok",
        "summary": "",
        "accounts": [
            {
                "name": "papa",
                "kind": "local",
                "enabled": True,
                "is_admin": True,
                "password_required": True,
                "password_last_set": "2026-01-15T09:00:00Z",
                "deny_logon": ["network"],
            }
        ],
        "admins": ["papa"],
        "count": 1,
    }
    result = evaluate_section("local_accounts", payload)
    assert result["status"] == "warn"
    assert "papa" in result["reason"]


def test_local_accounts_stays_ok_for_a_clean_mixed_household() -> None:
    """A Microsoft account is not, by itself, a finding."""

    payload = {
        "status": "ok",
        "summary": "",
        "accounts": [
            {
                "name": "papa",
                "kind": "local",
                "enabled": True,
                "is_admin": True,
                "password_required": True,
                "password_last_set": "2026-01-15T09:00:00Z",
                "deny_logon": [],
            },
            {
                "name": "kid",
                "kind": "microsoft",
                "enabled": True,
                "is_admin": False,
                "password_required": True,
                "password_last_set": "2026-02-20T18:30:00Z",
                "deny_logon": ["remote_interactive"],
                "unsupported": {"reset_password": "password_in_cloud"},
            },
        ],
        "admins": ["papa"],
        "count": 2,
    }
    assert evaluate_section("local_accounts", payload)["status"] == "ok"


# --- drift -------------------------------------------------------------------


def test_diff_reports_the_governance_changes_that_matter() -> None:
    """Enforcement is best-effort by design (the kill switch and a local admin
    can both undo it), so noticing the undo is the part that actually holds."""

    def account(name, **over):
        base = {
            "name": name,
            "kind": "local",
            "enabled": True,
            "is_admin": False,
            "deny_logon": [],
        }
        base.update(over)
        return base

    old = {"accounts": [account("papa", is_admin=True), account("kid", deny_logon=["network"])]}
    new = {
        "accounts": [
            account("papa", is_admin=True),
            # The child got admin back and the deny rights were cleared...
            account("kid", is_admin=True),
            # ...and a Microsoft account appeared that was not there before.
            account("newcomer", kind="microsoft"),
        ]
    }
    changes = diff_section("local_accounts", old, new)
    by_key = {c["key"]: c for c in changes}

    assert by_key["newcomer"]["kind"] == "added"
    assert "kind=microsoft" in by_key["newcomer"]["detail"]

    kid = by_key["kid"]
    assert kid["kind"] == "changed"
    assert "is_admin" in kid["detail"]
    assert "deny_logon" in kid["detail"]

    # An unchanged account produces no noise.
    assert "papa" not in by_key


def test_diff_reports_a_local_account_being_linked_to_microsoft() -> None:
    """Nothing else in kenny would surface this."""

    old = {"accounts": [{"name": "kid", "kind": "local", "enabled": True, "is_admin": False}]}
    new = {"accounts": [{"name": "kid", "kind": "microsoft", "enabled": True, "is_admin": False}]}
    changes = diff_section("local_accounts", old, new)
    assert len(changes) == 1
    assert changes[0]["kind"] == "changed"
    assert "kind: local -> microsoft" in changes[0]["detail"]


# --- forwarding, against the mock agent --------------------------------------


@pytest.mark.asyncio
async def test_account_tool_forwards_to_a_windows_agent(tmp_path, monkeypatch) -> None:
    """The whole path: MCP call -> request frame -> agent -> result."""

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from kenny_server.main import build_app

    from test_server_e2e import SERVER_SEED_B64, MockAgent, _free_port, _Server

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "accounts.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "kid-pc", os="windows")
        await app.state.key_store.enroll("kid-pc", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            result = await client.call_tool(
                "account_set_admin",
                {"args": {"principal": "kid", "admin": False, "agent_id": "kid-pc"}},
            )
            payload = json.loads(result.content[0].text)
            assert payload["ok"] is True
            assert payload["principal"] == "kid"
            # The result names the account kind so a caller can see what it just
            # changed without a second round trip — same tool either way.
            assert payload["kind"] == "microsoft"

        await agent.stop()


@pytest.mark.asyncio
async def test_account_tool_refused_on_a_linux_agent(tmp_path, monkeypatch) -> None:
    """Refused by the server's OS guard before a frame is sent. Unlike the
    shell pair there is no mirror tool to point at, so the message just says
    the host is the wrong OS."""

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.exceptions import ToolError

    from kenny_server.main import build_app

    from test_server_e2e import SERVER_SEED_B64, MockAgent, _free_port, _Server

    monkeypatch.setenv("KENNY_SERVER_PRIVATE_KEY", SERVER_SEED_B64)
    port = _free_port()
    app = build_app(db_path=str(tmp_path / "accounts_linux.sqlite"))

    async with _Server(app, port):
        agent = MockAgent(f"ws://127.0.0.1:{port}/agent/ws", "nas", os="linux")
        await app.state.key_store.enroll("nas", agent.public_key_b64)
        await agent.start()
        await asyncio.sleep(0.1)

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={"Authorization": f"Bearer {app.state.operator_token}"},
        )
        async with Client(transport) as client:
            with pytest.raises(ToolError, match="requires windows"):
                await client.call_tool(
                    "account_set_enabled",
                    {"args": {"principal": "kid", "enabled": False, "agent_id": "nas"}},
                )

        await agent.stop()

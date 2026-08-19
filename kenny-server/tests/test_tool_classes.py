"""The tool tier map: no behaviour change, exhaustive, fail-closed, in step.

``tool_classes.py`` replaced the binary ``chat.STATE_CHANGING_TOOLS`` frozenset
with a three-tier classification. Four things have to hold, and all four would
rot silently:

1. **Nothing became auto-executable.** The set of non-``read_only`` tools must
   equal the old hand-maintained frozenset, byte for byte. The old set is copied
   into this file as a frozen literal — deriving it from the code under test
   would assert nothing.
2. **The map covers every catalog.** A tool added to ``CAPABILITY_TOOLS`` or
   registered in ``tools.py`` without a tier here would only be caught at
   runtime, by the fail-closed default.
3. **Unknown fails closed**, so that runtime default is the safe one.
4. **The agent agrees.** ADR-0023 requires the server's gate to match
   ``control::is_mutating``; nothing but a test keeps the two lists in step.
"""

from __future__ import annotations

from kenny_server import chat
from kenny_server.tool_classes import (
    NORMAL_CHANGE,
    PROFILES,
    READ_ONLY,
    REDACTED_OUTPUT,
    SENSITIVE_TOOLS,
    STANDARD_CHANGE,
    TOOL_CLASSES,
    classify,
    is_state_changing,
    profile_allows,
)
from kenny_server.tools import CAPABILITY_TOOLS

# A frozen literal copy of ``chat.STATE_CHANGING_TOOLS`` as it stood before the
# tier map existed. Deliberately hand-written: it is the baseline, so it must not
# be derived from anything the code under test can change.
LEGACY_STATE_CHANGING = frozenset(
    {
        "powershell_exec",
        "shell_exec",
        "winget_install",
        "winget_uninstall",
        "winget_update",
        "net_dns_flush",
        "net_adapter_reset",
        "remotehelp_start",
        "remotehelp_stop",
        "agent_update",
        "webfilter_apply",
        "webfilter_clear",
        "webfilter_set",
        "webfilter_push",
        "reliability_suppression_add",
        "reliability_suppression_remove",
        "ticket_rule_set",
        "ticket_rule_remove",
        "account_set_enabled",
        "account_set_admin",
        "account_set_logon_rights",
        "account_create",
        "account_delete",
        "account_session_action",
        "password_policy_set",
    }
)

# State-changing tools that did not exist when ``LEGACY_STATE_CHANGING`` was
# written. Each one is a deliberate addition, not a reclassification.
SINCE_LEGACY = frozenset(
    {
        # Records a triage verdict and, when the server's preconditions hold,
        # resolves the ticket (kenny_server/triage.py).
        "ticket_triage_verdict",
    }
)

# The MCP-only server tools registered in ``tools.py`` (neither forwarded
# capabilities nor part of ``chat.SERVER_TOOLS``).
MCP_ONLY_SERVER_TOOLS = frozenset(
    {
        "webfilter_get",
        "webfilter_set",
        "webfilter_push",
        "web_activity_query",
        "reliability_suppression_list",
        "reliability_suppression_add",
        "reliability_suppression_remove",
        "ticket_rule_list",
        "ticket_rule_set",
        "ticket_rule_remove",
    }
)

# A frozen literal copy of ``kenny-agent/src/control.rs::is_mutating``. Same
# technique as ``test_account_governance.py``: the agent is a separate build, so
# the only way the two classifications stay in step is a copy asserted here.
AGENT_MUTATING = (
    "powershell_exec",
    "shell_exec",
    "winget_install",
    "winget_uninstall",
    "winget_update",
    "net_dns_flush",
    "net_adapter_reset",
    "remotehelp_start",
    "remotehelp_stop",
    "agent_update",
    "webfilter_apply",
    "webfilter_clear",
    "account_set_enabled",
    "account_set_admin",
    "account_set_logon_rights",
    "account_create",
    "account_delete",
    "account_session_action",
    "password_policy_set",
)


def test_no_behaviour_change_vs_legacy() -> None:
    """The tier map must not have made a single tool auto-executable.

    ``LEGACY_STATE_CHANGING`` is the pre-ADR-0045 list, so it can only be
    compared against tools that existed then: a tool added later is not evidence
    that an old one was reclassified, which is the regression this guards. New
    names are named in :data:`SINCE_LEGACY` and subtracted, so adding one stays
    a visible edit here rather than a silently widening assertion.
    """

    current = {t for t in TOOL_CLASSES if classify(t) != READ_ONLY}
    assert current - SINCE_LEGACY == LEGACY_STATE_CHANGING
    assert current >= SINCE_LEGACY, "a tool in SINCE_LEGACY is no longer state-changing"
    # And the re-derived frozenset ``chat`` still exports agrees with it.
    assert chat.STATE_CHANGING_TOOLS - SINCE_LEGACY == LEGACY_STATE_CHANGING


def test_catalog_exhaustive() -> None:
    """Every tool name in the repo carries an explicit tier."""

    catalog = set(chat.SERVER_TOOLS) | set(CAPABILITY_TOOLS) | set(MCP_ONLY_SERVER_TOOLS)
    missing = catalog - set(TOOL_CLASSES)
    assert not missing, f"tools with no tier: {sorted(missing)}"
    # And no tier entry names a tool that does not exist anywhere.
    stale = set(TOOL_CLASSES) - catalog
    assert not stale, f"tiers for unknown tools: {sorted(stale)}"


def test_every_tier_is_one_of_the_three() -> None:
    assert set(TOOL_CLASSES.values()) <= {READ_ONLY, STANDARD_CHANGE, NORMAL_CHANGE}


def test_unknown_fails_closed() -> None:
    """An unclassified name is treated as the most consequential tier."""

    assert classify("no_such_tool") == NORMAL_CHANGE
    assert is_state_changing("no_such_tool") is True


def test_standard_change_is_the_short_deliberate_list() -> None:
    """Widening the routine tier must be a deliberate, reviewed edit."""

    assert {t for t, c in TOOL_CLASSES.items() if c == STANDARD_CHANGE} == {
        "net_dns_flush",
        "net_adapter_reset",
        "remotehelp_start",
        "remotehelp_stop",
        "winget_update",
        "webfilter_push",
        # Moves a ticket to `resolved` — reversible by construction (a reopen
        # window plus a `resolved -> in_progress` transition), and it has to run
        # without a decision because the session that calls it has nobody in it
        # to make one. `normal_change` would hold for an operator who is not
        # coming, parking the ticket on an open gate. See tool_classes.py.
        "ticket_triage_verdict",
    }


def test_agent_mutating_parity() -> None:
    """Everything the agent calls mutating is state-changing here (ADR-0023)."""

    for tool in AGENT_MUTATING:
        assert tool in TOOL_CLASSES, f"{tool} is mutating on the agent but has no tier"
        assert classify(tool) != READ_ONLY, f"{tool} must not be classified read-only"


def test_agent_read_only_tools_stay_read_only() -> None:
    """The other half of the parity: what the agent serves under the kill switch."""

    for tool in (
        "webfilter_status",
        "telemetry_collect",
        "fs_list",
        "diag_processes",
        "net_config",
        "screen_capture",
        "remotehelp_status",
    ):
        assert classify(tool) == READ_ONLY


def test_sensitive_and_redacted_sets_name_real_tools() -> None:
    """Both privacy sets are unused in this wave; they must still be truthful."""

    assert SENSITIVE_TOOLS <= set(TOOL_CLASSES)
    assert REDACTED_OUTPUT <= set(TOOL_CLASSES)
    # Sensitivity is orthogonal to the tier: looking at someone's screen changes
    # nothing and is still the most invasive thing in the catalog.
    assert classify("screen_capture") == READ_ONLY


def test_profile_only_narrows() -> None:
    """A profile subtracts; no profile at all is the current allow-all behaviour."""

    for tool in ("powershell_exec", "shell_exec", "fs_read", "webfilter_clear", "screen_capture"):
        assert profile_allows(None, tool) is True
    assert profile_allows(None, "no_such_tool") is True

    denied_for_basic = (
        "powershell_exec",
        "shell_exec",
        "fs_read",
        "webfilter_clear",
        "web_activity_query",
    )
    for tool in denied_for_basic:
        assert profile_allows("self-service-basic", tool) is False
    for tool in TOOL_CLASSES:
        if tool.startswith("account_"):
            assert profile_allows("self-service-basic", tool) is False

    # Every profile is a subset of the catalog, and they nest: basic ⊆ power-user
    # ⊆ operator, so "upgrading" a caller can never remove a tool.
    assert PROFILES["self-service-basic"] <= PROFILES["power-user"] <= PROFILES["operator"]
    assert PROFILES["operator"] == frozenset(TOOL_CLASSES)
    # The self-service tier reaches nothing beyond the routine change tier.
    assert all(classify(t) != NORMAL_CHANGE for t in PROFILES["self-service-basic"])


def test_unknown_profile_allows_nothing() -> None:
    """A typo'd profile name must not silently widen access."""

    assert profile_allows("no-such-profile", "fleet_overview") is False

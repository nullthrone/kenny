"""The agent's diagnostic budget and the server's forwarding timeout stay in order.

A Windows diagnostic (``diag_services``, ``diag_eventlog``, ``diag_autostart``)
runs a query the agent bounds at ``DIAG_BUDGET``. The server waits for the
answer at least ``_TOOL_MIN_TIMEOUT_S`` for that tool. The agent's limit must be
the shorter one: then a slow query ends in the agent's own ``timeout`` error,
which names what timed out, instead of the server giving up first with nothing
to say. The two live in two languages, so this reads the agent's constant from
its source rather than trusting a copy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kenny_server.tools import _TOOL_MIN_TIMEOUT_S, forward_timeout_s

_DIAGNOSTICS_RS = (
    Path(__file__).resolve().parents[2] / "kenny-agent" / "src" / "handlers" / "diagnostics.rs"
)
_DIAG_TOOLS = ("diag_services", "diag_eventlog", "diag_autostart")


_HANDLERS = _DIAGNOSTICS_RS.parent
_CLEANUP_RS = _HANDLERS / "cleanup.rs"
_SERVICES_RS = _HANDLERS / "services.rs"


def _agent_budget_s(source_file: Path, name: str) -> int:
    """Read ``pub const <name>: Duration = Duration::from_secs(N);`` from agent source."""

    assert source_file.exists(), f"{source_file} not found; the agent budget cannot be read"
    source = source_file.read_text(encoding="utf-8")
    match = re.search(
        rf"pub const {name}: Duration = Duration::from_secs\((\d+)\);", source
    )
    assert match, f"{name} not found in {source_file}"
    return int(match.group(1))


def _agent_diag_budget_s() -> int:
    return _agent_budget_s(_DIAGNOSTICS_RS, "DIAG_BUDGET")


@pytest.mark.parametrize("tool", _DIAG_TOOLS)
def test_the_agent_gives_up_on_a_diagnostic_before_the_server_does(tool: str) -> None:
    budget = _agent_diag_budget_s()
    # Headroom for PowerShell start-up and the round trip over the tunnel.
    assert _TOOL_MIN_TIMEOUT_S[tool] >= budget + 10, (tool, budget)


# Tool -> (agent source file, budget constant). The agent bounds each of these
# calls itself; the server must outwait that bound, or a slow call ends in the
# server's generic timeout instead of the agent's own, more specific error.
_BUDGETS = {
    "fs_cleanup": (_CLEANUP_RS, "CLEANUP_BUDGET"),
    "fs_cleanup_scan": (_CLEANUP_RS, "SCAN_BUDGET"),
    "service_restart": (_SERVICES_RS, "RESTART_BUDGET"),
}


@pytest.mark.parametrize("tool", sorted(_BUDGETS))
def test_the_agent_gives_up_on_cleanup_and_restart_before_the_server_does(tool: str) -> None:
    source_file, name = _BUDGETS[tool]
    budget = _agent_budget_s(source_file, name)
    # The agent answers `timeout` at most WORKER_GRACE after the budget when a
    # worker is stuck in the kernel; the server must still be waiting then.
    grace = _agent_budget_s(_CLEANUP_RS, "WORKER_GRACE")
    assert _TOOL_MIN_TIMEOUT_S[tool] >= budget + grace + 5, (tool, name, budget, grace)
    assert _TOOL_MIN_TIMEOUT_S[tool] >= budget + 10, (tool, name, budget)


@pytest.mark.parametrize("tool", _DIAG_TOOLS)
def test_a_diagnostic_waits_at_least_its_floor_whatever_the_caller_asks(tool: str) -> None:
    assert forward_timeout_s(tool, {}) == _TOOL_MIN_TIMEOUT_S[tool]
    assert forward_timeout_s(tool, {"timeout_s": 5}) == _TOOL_MIN_TIMEOUT_S[tool]
    assert forward_timeout_s(tool, {"timeout_s": 300}) == 300


def test_a_tool_without_a_floor_keeps_the_callers_timeout() -> None:
    assert forward_timeout_s("fs_list", {}) == 30
    assert forward_timeout_s("fs_list", {"timeout_s": 5}) == 5
    with pytest.raises(ValueError):
        forward_timeout_s("fs_list", {"timeout_s": "soon"})


def test_an_oversized_timeout_s_raises_value_error_not_overflow_error() -> None:
    # `timeout_s` is unvalidated caller input (an MCP tool arg or LLM tool-call
    # argument); JSON has no int-size limit, and `float()` on a huge-but-valid
    # int raises OverflowError, not the TypeError/ValueError both call sites'
    # `except` clauses catch to fail the call closed instead of crashing.
    with pytest.raises(ValueError):
        forward_timeout_s("fs_list", {"timeout_s": 10**400})

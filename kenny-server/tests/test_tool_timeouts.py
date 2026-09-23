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


def _agent_diag_budget_s() -> int:
    source = _DIAGNOSTICS_RS.read_text(encoding="utf-8")
    match = re.search(
        r"pub const DIAG_BUDGET: Duration = Duration::from_secs\((\d+)\);", source
    )
    assert match, f"DIAG_BUDGET not found in {_DIAGNOSTICS_RS}"
    return int(match.group(1))


@pytest.mark.parametrize("tool", _DIAG_TOOLS)
def test_the_agent_gives_up_on_a_diagnostic_before_the_server_does(tool: str) -> None:
    budget = _agent_diag_budget_s()
    # Headroom for PowerShell start-up and the round trip over the tunnel.
    assert _TOOL_MIN_TIMEOUT_S[tool] >= budget + 10, (tool, budget)


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

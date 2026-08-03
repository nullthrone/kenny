"""Seam test: the real-agent e2e test's own timeout must cover its own work.

`tests/test_integration_e2e.py` legitimately runs minutes on a Windows runner
(cold PowerShell/CIM collectors), while the suite-wide `timeout = 90` in
`pyproject.toml` exists to catch a *hung* test fast (see that setting's
comment). The two disagreeing is exactly what broke the dev-channel release:
`d912a48` added the global 90s bound while claiming no test needed anywhere
near that long, which was false for the Windows e2e test and cut the release
job's e2e step off ~10s before it would have finished.

`test_integration_e2e.py` now carries its own `@pytest.mark.timeout(...)`
derived from named budget constants instead of relying on the ini value. This
test is the guard that keeps that derivation honest: it fails if a future
change grows one of those constants (a slower collector, an added tool
assertion) without raising the mark to match, or if the mark is dropped, or if
the ini timeout is raised to swallow the mark's margin.
"""

from __future__ import annotations

import pytest

import test_integration_e2e as e2e


def _timeout_mark_value() -> float:
    for mark in e2e.test_real_agent_end_to_end.pytestmark:
        if mark.name == "timeout":
            return float(mark.args[0])
    raise AssertionError(
        "test_real_agent_end_to_end lost its @pytest.mark.timeout(...) — "
        "without it the test falls back to the suite-wide ini timeout, which "
        "is too tight for this test on Windows (see module docstring)."
    )


def test_e2e_mark_covers_its_own_declared_budget() -> None:
    """The mark must be at least as generous as the budgets it's built from."""
    declared_budget = (
        e2e._REGISTER_BUDGET_S + e2e._TELEMETRY_BUDGET_S + e2e._TOOLCALL_BUDGET_S
    )
    mark_value = _timeout_mark_value()
    assert mark_value >= declared_budget, (
        f"test_real_agent_end_to_end's timeout mark ({mark_value}s) is below its "
        f"own declared budget ({declared_budget}s) -- a budget constant grew "
        "without the mark growing with it"
    )


def test_e2e_mark_exceeds_the_ini_timeout(pytestconfig: pytest.Config) -> None:
    """The per-test mark must actually widen the bound past the ini default.

    A mark that happens to equal or fall under the ini timeout gives no real
    protection: the ini bound would fire first, silently making the mark dead
    weight (this is exactly the trap `d912a48` fell into).
    """
    ini_timeout = float(pytestconfig.getini("timeout"))
    mark_value = _timeout_mark_value()
    assert mark_value > ini_timeout, (
        f"test_real_agent_end_to_end's timeout mark ({mark_value}s) does not "
        f"exceed the suite-wide ini timeout ({ini_timeout}s) -- the ini bound "
        "would fire first and the mark would never take effect"
    )

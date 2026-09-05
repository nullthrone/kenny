"""Suite-wide guard: no test may reach GitHub for real.

``build_app``'s lifespan fetches the agent binary from GitHub on startup
(ADR-0015). That used to be gated on ``KENNY_GITHUB_TOKEN``, so the suite stayed
offline by accident — no token in the environment, no request. Since the read
became anonymous (ADR-0057) the gate is gone and every ``TestClient(app)`` would
hit the network, overwrite the fixtures under ``KENNY_AGENT_BINARY_CACHE`` with a
real ``kenny-agent.exe``, and make results depend on GitHub being up.

The guard replaces the *default* client with one whose transport refuses to
connect, so an unintended call fails loudly and instantly instead of succeeding
quietly. Tests that exercise the fetch on purpose inject their own
``client_factory`` (``httpx.MockTransport``) and are unaffected by this — that
injection point exists precisely so the network is never required.
"""

from __future__ import annotations

import httpx
import pytest

from kenny_server import agent_release


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(
        "blocked by tests/conftest.py: no real network in the test suite", request=request
    )


@pytest.fixture(autouse=True)
def _no_real_github(monkeypatch):
    monkeypatch.setattr(
        agent_release,
        "_default_client",
        lambda: httpx.Client(transport=httpx.MockTransport(_refuse)),
    )

"""MCP forwarder routing: explicit per-call ``agent_id`` (ADR-0038).

Remote MCP clients (Claude Desktop, claude.ai) carry no reliable
per-conversation identifier — the ``Mcp-Session-Id`` header the server hands
back at init is not echoed on follow-up requests. Two concurrent Claude
conversations authenticated with the *same* credential (PAT/OAuth token)
therefore resolve to the identical ``Principal.active_key``, so any routing
scheme keyed only by credential (the ADR-0033 ``registry._active_by_key`` slot)
can be shared — and silently clobbered — by two unrelated conversations. This
module proves that failure mode against the old sticky-only path, then proves
the new resolver (:func:`kenny_server.tools._resolve_target`) makes it
structurally impossible: routing is decided per call, from an explicit
``agent_id``, and fails closed rather than falling back to any shared slot.
"""

from __future__ import annotations

import pytest

from kenny_server.auth import Principal
from kenny_server.registry import AgentRegistry
from kenny_server.tools import _resolve_target
from kenny_server.tunnel import ToolError


async def _noop(_frame: object) -> None:
    return None


def _registry_with(*agent_ids: str) -> AgentRegistry:
    reg = AgentRegistry()
    for agent_id in agent_ids:
        reg.register_signed_async(agent_id, {}, _noop)
    return reg


def test_sticky_slot_collides_for_two_callers_sharing_one_credential() -> None:
    """Demonstrates the reported bug in the pre-ADR-0038 sticky-only model.

    Two principals authenticated with the same PAT (e.g. two concurrent
    claude.ai conversations for the same user) resolve to the identical
    ``active_key``. If routing still consulted the shared ``_active_by_key``
    slot, session B's ``select_agent`` would silently overwrite session A's.
    """

    registry = _registry_with("alpha", "beta")
    principal_a = Principal(user_id=1, username="a", role="operator", pat_id="shared")
    principal_b = Principal(user_id=2, username="b", role="operator", pat_id="shared")
    assert principal_a.active_key == principal_b.active_key == "p:shared"

    registry.select("alpha", key=principal_a.active_key)
    registry.select("beta", key=principal_b.active_key)

    # Session A's selection was clobbered by session B's — this is the race.
    assert registry.active_for(principal_a.active_key) == "beta"


def test_explicit_agent_id_isolates_shared_credential_callers() -> None:
    """The fix: each call names its own target, so the shared slot is never
    consulted and two conversations sharing a credential can never collide."""

    registry = _registry_with("alpha", "beta")
    principal_a = Principal(user_id=1, username="a", role="operator", pat_id="shared")
    principal_b = Principal(user_id=2, username="b", role="operator", pat_id="shared")

    # Poison the shared sticky slot the way the old race would have left it.
    registry.select("beta", key=principal_a.active_key)

    args_a = {"agent_id": "alpha", "script": "whoami"}
    args_b = {"agent_id": "beta"}

    assert _resolve_target(principal_a, args_a) == "alpha"
    assert _resolve_target(principal_b, args_b) == "beta"

    # agent_id is routing metadata: popped off before the wire frame is built.
    assert "agent_id" not in args_a
    assert "agent_id" not in args_b
    assert args_a == {"script": "whoami"}


def test_resolve_target_fails_closed_without_agent_id() -> None:
    principal = Principal(user_id=1, username="a", role="operator", pat_id="shared")

    with pytest.raises(ToolError) as excinfo:
        _resolve_target(principal, {})
    assert excinfo.value.code == "no_agent"


def test_resolve_target_ignores_blank_agent_id() -> None:
    principal = Principal(user_id=1, username="a", role="operator", pat_id="shared")

    with pytest.raises(ToolError) as excinfo:
        _resolve_target(principal, {"agent_id": "   "})
    assert excinfo.value.code == "no_agent"


def test_resolve_target_scope_checks_explicit_agent_id() -> None:
    """A scoped ``user`` principal cannot reach an explicit agent_id outside
    their host set — the explicit id is unvalidated client input and must
    still be scope-checked (defense in depth)."""

    principal = Principal(
        user_id=3, username="kid", role="user", hosts=frozenset({"alpha"})
    )

    with pytest.raises(ToolError) as excinfo:
        _resolve_target(principal, {"agent_id": "beta"})
    assert excinfo.value.code == "forbidden"

    # In-scope target still resolves normally.
    args = {"agent_id": "alpha"}
    assert _resolve_target(principal, args) == "alpha"


def test_resolve_target_works_without_a_principal() -> None:
    """Outside an HTTP request context (e.g. a direct/in-proc call in tests),
    ``_mcp_principal()`` returns ``None`` — the resolver must still require
    and return an explicit agent_id, just without a scope check."""

    assert _resolve_target(None, {"agent_id": "alpha"}) == "alpha"

    with pytest.raises(ToolError) as excinfo:
        _resolve_target(None, {})
    assert excinfo.value.code == "no_agent"

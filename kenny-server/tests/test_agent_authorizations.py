"""Standing authorizations: what a grant may say, and what a call may spend (ADR-0072).

The store is the whole of the match: these tests call it directly, with an
explicit clock, and pin every refusal at grant and every reason a call does
not match. The race for the last attempt runs two consumers on two connections
to one file at once, because "consume, then execute" is only true if two runs
cannot both be told yes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kenny_server.agents.authorizations import (
    MAX_EXPIRY_DAYS,
    NEVER_AUTHORIZED,
    SERVER_SCOPE,
    AuthorizationError,
    AuthorizationStore,
)
from kenny_server.agents.spec import AgentSpec, ArgConstraint, Trigger
from kenny_server.tool_classes import NORMAL_CHANGE, TOOL_CLASSES

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
HOST = "thomas-pc"
OTHER = "bob-pc"
SEVENZIP = "7zip.7zip"
HASH = "a" * 64
OLD_HASH = "b" * 64


def _spec(**overrides: Any) -> AgentSpec:
    fields: dict[str, Any] = {
        "id": "patcher",
        "title": "Patcher",
        "description": "Installs one package.",
        "prompt": "You keep this machine's packages in order.",
        "trigger": Trigger(kind="on_demand"),
        "tools": frozenset({"winget_list", "winget_update", "winget_install"}),
        "constraints": (
            ArgConstraint("winget_update", "id", frozenset({SEVENZIP})),
            ArgConstraint("winget_install", "id", frozenset({SEVENZIP})),
        ),
    }
    fields.update(overrides)
    return AgentSpec(**fields)


@pytest.fixture
async def store(tmp_path):
    s = AuthorizationStore(str(tmp_path / "auth.sqlite"))
    await s.connect()
    yield s
    await s.close()


async def _grant(store: AuthorizationStore, **overrides: Any):
    kwargs: dict[str, Any] = {
        "spec": _spec(),
        "effective_hash": HASH,
        "tool": "winget_install",
        "scope": [HOST],
        "max_attempts_per_day": 1,
        "expires_at": NOW + timedelta(days=30),
        "granted_by": "admin",
        "now": NOW,
    }
    kwargs.update(overrides)
    return await store.grant(**kwargs)


async def _consume(store: AuthorizationStore, **overrides: Any):
    kwargs: dict[str, Any] = {
        "agent_id": "patcher",
        "effective_hash": HASH,
        "tool": "winget_install",
        "host_id": HOST,
        "run_id": "run-1",
        "now": NOW,
    }
    kwargs.update(overrides)
    return await store.consume(**kwargs)


async def _insert_raw(store: AuthorizationStore, **row: Any) -> None:
    """A row written past ``grant`` — a restored backup, a hand edit."""

    values: dict[str, Any] = {
        "id": "raw",
        "agent_id": "patcher",
        "effective_hash": HASH,
        "tool": "winget_install",
        "scope": json.dumps([HOST]),
        "max_attempts_per_day": 5,
        "expires_at": (NOW + timedelta(days=1)).isoformat(timespec="microseconds"),
        "granted_by": "admin",
        "granted_at": NOW.isoformat(timespec="microseconds"),
        "note": "",
    }
    values.update(row)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    await store._conn.execute(
        f"INSERT INTO agent_authorizations ({cols}) VALUES ({marks})", tuple(values.values())
    )
    await store._conn.commit()


# -- the literal ---------------------------------------------------------------


def test_never_authorized_is_the_literal_adr_0072_names() -> None:
    assert NEVER_AUTHORIZED == frozenset({"shell_exec", "powershell_exec", "agent_update"})
    # Every one of them would otherwise be a candidate: each is a normal_change.
    assert {TOOL_CLASSES[t] for t in NEVER_AUTHORIZED} == {NORMAL_CHANGE}


# -- grant: every refusal ------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(NEVER_AUTHORIZED))
async def test_grant_refuses_a_never_authorized_tool(store, tool: str) -> None:
    spec = _spec(
        tools=frozenset({tool}),
        sensitive_ok=True,
        constraints=(ArgConstraint(tool, "x", frozenset({"v"})),),
    )
    with pytest.raises(AuthorizationError, match="never"):
        await _grant(store, spec=spec, tool=tool)


async def test_grant_refuses_a_tool_outside_the_spec(store) -> None:
    with pytest.raises(AuthorizationError, match="not one of"):
        await _grant(store, tool="service_restart")


async def test_grant_refuses_a_tool_that_is_not_a_normal_change(store) -> None:
    assert TOOL_CLASSES["winget_update"] != NORMAL_CHANGE
    with pytest.raises(AuthorizationError, match="not a normal_change"):
        await _grant(store, tool="winget_update")
    with pytest.raises(AuthorizationError, match="not a normal_change"):
        await _grant(store, tool="winget_list")


@pytest.mark.parametrize("scope", [[], (), [""], ["  "], [None], "all", None, {"hosts": [HOST]}])
async def test_grant_refuses_an_empty_or_malformed_scope(store, scope: Any) -> None:
    with pytest.raises(AuthorizationError):
        await _grant(store, scope=scope)


async def test_grant_refuses_the_server_sentinel_for_a_host_tool(store) -> None:
    with pytest.raises(AuthorizationError, match="runs on a host"):
        await _grant(store, scope=SERVER_SCOPE)


@pytest.mark.parametrize("attempts", [0, -1, True, "3", 1.5, None])
async def test_grant_refuses_a_bad_attempt_budget(store, attempts: Any) -> None:
    with pytest.raises(AuthorizationError, match="max_attempts_per_day"):
        await _grant(store, max_attempts_per_day=attempts)


@pytest.mark.parametrize(
    "expires_at",
    [
        NOW,
        NOW - timedelta(seconds=1),
        NOW + timedelta(days=MAX_EXPIRY_DAYS, seconds=1),
        "not a date",
        "",
        None,
    ],
)
async def test_grant_refuses_an_expiry_in_the_past_or_too_far_out(store, expires_at: Any) -> None:
    with pytest.raises(AuthorizationError, match="expires_at"):
        await _grant(store, expires_at=expires_at)


async def test_grant_accepts_the_longest_expiry_and_normalises_it(store) -> None:
    edge = NOW + timedelta(days=MAX_EXPIRY_DAYS)
    granted = await _grant(store, expires_at=edge.isoformat().replace("+00:00", "Z"))
    assert granted.expires_at == edge.isoformat(timespec="microseconds")


async def test_grant_refuses_a_missing_grantor_or_hash(store) -> None:
    with pytest.raises(AuthorizationError, match="granted_by"):
        await _grant(store, granted_by=" ")
    with pytest.raises(AuthorizationError, match="effective hash"):
        await _grant(store, effective_hash="")


async def test_a_grant_records_what_it_says(store) -> None:
    granted = await _grant(store, scope=[OTHER, HOST, HOST], max_attempts_per_day=3, note=" why ")
    assert granted.scope == [OTHER, HOST]
    assert (granted.agent_id, granted.effective_hash, granted.tool) == (
        "patcher",
        HASH,
        "winget_install",
    )
    assert (granted.max_attempts_per_day, granted.granted_by, granted.note) == (3, "admin", "why")
    assert granted.status(NOW) == "live"
    assert await store.get(granted.id) == granted
    assert await store.list("patcher") == [granted]


# -- consume: what matches -----------------------------------------------------


async def test_consume_spends_a_matching_authorization_and_records_the_use(store) -> None:
    granted = await _grant(store)
    used = await _consume(store)
    assert used is not None and used.id == granted.id
    assert await store.uses_since(granted.id, NOW - timedelta(hours=1)) == {HOST: 1}
    async with store._conn.execute("SELECT * FROM agent_authorization_uses") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert [(r["authorization_id"], r["host_id"], r["run_id"]) for r in rows] == [
        (granted.id, HOST, "run-1")
    ]


@pytest.mark.parametrize(
    "mismatch",
    [
        {"effective_hash": OLD_HASH},
        {"agent_id": "another_agent"},
        {"tool": "winget_update"},
        {"host_id": OTHER},
        {"host_id": None},
    ],
    ids=["hash", "agent", "tool", "host", "no-host"],
)
async def test_consume_matches_nothing_else(store, mismatch: dict[str, Any]) -> None:
    await _grant(store)
    assert await _consume(store, **mismatch) is None
    async with store._conn.execute("SELECT COUNT(*) AS n FROM agent_authorization_uses") as cur:
        assert (await cur.fetchone())["n"] == 0


async def test_the_server_sentinel_matches_only_a_call_without_a_host(store) -> None:
    spec = _spec(
        tools=frozenset({"ticket_rule_remove"}),
        constraints=(ArgConstraint("ticket_rule_remove", "rule_id", frozenset({"r1"})),),
    )
    granted = await _grant(store, spec=spec, tool="ticket_rule_remove", scope=SERVER_SCOPE)
    assert await _consume(store, tool="ticket_rule_remove", host_id=HOST) is None
    used = await _consume(store, tool="ticket_rule_remove", host_id=None)
    assert used is not None and used.id == granted.id
    assert await store.uses_since(granted.id, NOW - timedelta(hours=1)) == {SERVER_SCOPE: 1}


async def test_an_empty_scope_written_past_grant_matches_nothing(store) -> None:
    await _insert_raw(store, scope=json.dumps([]))
    assert await _consume(store) is None
    assert await _consume(store, host_id=None) is None


@pytest.mark.parametrize("tool", sorted(NEVER_AUTHORIZED))
async def test_a_never_authorized_row_written_past_grant_matches_nothing(store, tool) -> None:
    await _insert_raw(store, tool=tool)
    assert await _consume(store, tool=tool) is None


async def test_an_expired_authorization_matches_nothing(store) -> None:
    granted = await _grant(store, expires_at=NOW + timedelta(hours=1))
    assert await _consume(store, now=NOW + timedelta(hours=1)) is None
    assert granted.status(NOW + timedelta(hours=1)) == "expired"
    assert await _consume(store, now=NOW + timedelta(minutes=59)) is not None


async def test_a_revoked_authorization_matches_nothing(store) -> None:
    granted = await _grant(store, max_attempts_per_day=5)
    assert await _consume(store) is not None
    revoked = await store.revoke(granted.id, "admin", now=NOW)
    assert (revoked.revoked_by, revoked.status(NOW)) == ("admin", "revoked")
    assert await _consume(store) is None
    again = await store.revoke(granted.id, "someone-else", now=NOW + timedelta(hours=1))
    assert again.revoked_by == "admin"  # the first revocation stands


async def test_revoking_an_unknown_authorization_says_so(store) -> None:
    with pytest.raises(KeyError):
        await store.revoke("nope", "admin")


async def test_voiding_is_permanent_and_spares_the_current_hash(store) -> None:
    old = await _grant(store, effective_hash=OLD_HASH)
    current = await _grant(store)
    assert await store.void_other_hashes("patcher", HASH, now=NOW) == 1
    voided = await store.get(old.id)
    assert voided is not None and voided.status(NOW) == "voided" and voided.voided_by == "system"
    assert (await store.get(current.id)).status(NOW) == "live"  # type: ignore[union-attr]
    # Rolling back to the old hash revives nothing.
    assert await _consume(store, effective_hash=OLD_HASH) is None
    assert await store.void_other_hashes("patcher", OLD_HASH, actor="admin", now=NOW) == 1
    assert await _consume(store) is None
    # Voiding again changes nothing and keeps the first stamp.
    assert await store.void_other_hashes("patcher", "c" * 64, now=NOW) == 0
    assert (await store.get(old.id)).voided_by == "system"  # type: ignore[union-attr]


# -- consume: the budget -------------------------------------------------------


async def test_the_budget_counts_attempts_per_host_over_a_rolling_day(store) -> None:
    await _grant(store, scope=[HOST, OTHER], max_attempts_per_day=2)
    assert await _consume(store) is not None
    assert await _consume(store, now=NOW + timedelta(hours=1)) is not None
    assert await _consume(store, now=NOW + timedelta(hours=2)) is None
    # Another host has its own budget.
    assert await _consume(store, host_id=OTHER, now=NOW + timedelta(hours=2)) is not None
    # Just inside 24 hours of the first attempt it still counts; at 24 hours it has aged out.
    assert await _consume(store, now=NOW + timedelta(hours=24, microseconds=-1)) is None
    assert await _consume(store, now=NOW + timedelta(hours=24)) is not None
    assert await _consume(store, now=NOW + timedelta(hours=24, microseconds=1)) is None


async def test_an_exhausted_authorization_falls_through_to_another_live_one(store) -> None:
    first = await _grant(store)
    second = await _grant(store)
    spent = [await _consume(store), await _consume(store)]
    assert {a.id for a in spent if a is not None} == {first.id, second.id}
    assert await _consume(store) is None


async def test_two_runs_racing_for_the_last_attempt_get_it_once(tmp_path) -> None:
    db = str(tmp_path / "race.sqlite")
    a, b = AuthorizationStore(db), AuthorizationStore(db)
    await a.connect()
    await b.connect()
    try:
        await _grant(a, max_attempts_per_day=1)
        results = await asyncio.gather(_consume(a, run_id="run-a"), _consume(b, run_id="run-b"))
        assert sum(r is not None for r in results) == 1
        async with a._conn.execute("SELECT COUNT(*) AS n FROM agent_authorization_uses") as cur:
            assert (await cur.fetchone())["n"] == 1
    finally:
        await a.close()
        await b.close()


async def test_many_concurrent_consumers_never_overspend(store) -> None:
    await _grant(store, max_attempts_per_day=3)
    results = await asyncio.gather(*(_consume(store, run_id=f"r{i}") for i in range(10)))
    assert sum(r is not None for r in results) == 3


async def test_connect_is_idempotent_and_the_store_says_when_it_is_not_connected(tmp_path):
    s = AuthorizationStore(str(tmp_path / "x.sqlite"))
    with pytest.raises(RuntimeError, match="not connected"):
        await s.list("patcher")
    await s.connect()
    await s.connect()
    await s.close()
    await s.close()

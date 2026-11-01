"""``AuthorizationStore.remaining`` against ``consume``: one question, two answers that must agree.

``remaining`` is what a person is shown before starting a run; ``consume`` is
what the gate then spends. Each case builds one state on its own database, asks
``remaining``, then lets ``consume`` try on the same state: ``None`` exactly
when ``consume`` finds nothing, the same authorization when it finds one, and
one attempt fewer afterwards.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kenny_server.stewards.authorizations import SERVER_SCOPE, AuthorizationStore
from kenny_server.stewards.spec import ArgConstraint, StewardSpec, Trigger

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64
HOST = "pc-kid"
OTHER = "pc-den"


def _spec() -> StewardSpec:
    return StewardSpec(
        id="installer",
        title="Installer",
        description="Installs what the household allowed.",
        prompt="Install allowed packages.",
        trigger=Trigger(kind="event", event="alert:change"),
        tools=frozenset({"winget_install", "reliability_suppression_remove"}),
        constraints=(
            ArgConstraint("winget_install", "id", param="packages"),
            ArgConstraint("reliability_suppression_remove", "rule_id", param="rules"),
        ),
        params=("packages", "rules"),
    )


async def _grant(store: AuthorizationStore, **overrides: Any):
    kwargs: dict[str, Any] = {
        "spec": _spec(),
        "effective_hash": HASH,
        "tool": "winget_install",
        "scope": [HOST],
        "max_attempts_per_day": 2,
        "expires_at": NOW + timedelta(days=7),
        "granted_by": "admin",
        "now": NOW - timedelta(days=1),
    }
    kwargs.update(overrides)
    return await store.grant(**kwargs)


async def _spend(store: AuthorizationStore, times: int, *, at: datetime = NOW, host: str | None = HOST,
                 tool: str = "winget_install") -> None:
    for _ in range(times):
        assert await store.consume(
            steward_id="installer", effective_hash=HASH, tool=tool, host_id=host,
            run_id="earlier", now=at,
        ) is not None


Setup = Callable[[AuthorizationStore], Awaitable[None]]


async def _nothing(_s: AuthorizationStore) -> None:
    return None


async def _live(s: AuthorizationStore) -> None:
    await _grant(s)


async def _expired(s: AuthorizationStore) -> None:
    await _grant(s, expires_at=NOW - timedelta(minutes=1), now=NOW - timedelta(days=2))


async def _expires_now(s: AuthorizationStore) -> None:
    await _grant(s, expires_at=NOW, now=NOW - timedelta(days=2))


async def _revoked(s: AuthorizationStore) -> None:
    granted = await _grant(s)
    await s.revoke(granted.id, "admin", now=NOW - timedelta(hours=1))


async def _voided(s: AuthorizationStore) -> None:
    await _grant(s)
    await s.void_other_hashes("installer", "b" * 64, now=NOW - timedelta(hours=1))


async def _wrong_hash(s: AuthorizationStore) -> None:
    await _grant(s, effective_hash="c" * 64)


async def _wrong_host(s: AuthorizationStore) -> None:
    await _grant(s, scope=[OTHER])


async def _exhausted(s: AuthorizationStore) -> None:
    await _grant(s, max_attempts_per_day=2)
    await _spend(s, 2, at=NOW - timedelta(hours=1))


async def _partly_spent(s: AuthorizationStore) -> None:
    await _grant(s, max_attempts_per_day=3)
    await _spend(s, 1, at=NOW - timedelta(hours=1))


async def _spent_on_another_host(s: AuthorizationStore) -> None:
    await _grant(s, scope=[HOST, OTHER], max_attempts_per_day=1)
    await _spend(s, 1, at=NOW - timedelta(hours=1), host=OTHER)


async def _spent_yesterday(s: AuthorizationStore) -> None:
    await _grant(s, max_attempts_per_day=1, now=NOW - timedelta(days=3))
    await _spend(s, 1, at=NOW - timedelta(hours=25))


async def _first_exhausted_second_live(s: AuthorizationStore) -> None:
    await _grant(s, max_attempts_per_day=1, now=NOW - timedelta(days=2))
    await _spend(s, 1, at=NOW - timedelta(hours=1))
    await _grant(s, max_attempts_per_day=4, now=NOW - timedelta(days=1))


async def _server_scope(s: AuthorizationStore) -> None:
    await _grant(s, tool="reliability_suppression_remove", scope=SERVER_SCOPE)


async def _never_authorized_row(s: AuthorizationStore) -> None:
    # A row no grant could write (a restored backup, a hand edit).
    granted = await _grant(s)
    await s._conn.execute(
        "UPDATE steward_authorizations SET tool = 'shell_exec' WHERE id = ?", (granted.id,)
    )
    await s._conn.commit()


#: (case, setup, tool, host asked about, expected remaining_today or None)
CASES: list[tuple[str, Setup, str, str | None, int | None]] = [
    ("no grant", _nothing, "winget_install", HOST, None),
    ("live", _live, "winget_install", HOST, 2),
    ("expired", _expired, "winget_install", HOST, None),
    ("expires at this instant", _expires_now, "winget_install", HOST, None),
    ("revoked", _revoked, "winget_install", HOST, None),
    ("voided", _voided, "winget_install", HOST, None),
    ("wrong hash", _wrong_hash, "winget_install", HOST, None),
    ("wrong host", _wrong_host, "winget_install", HOST, None),
    ("host grant asked for the server", _live, "winget_install", None, None),
    ("other tool", _live, "reliability_suppression_remove", HOST, None),
    ("budget exhausted", _exhausted, "winget_install", HOST, None),
    ("budget partly spent", _partly_spent, "winget_install", HOST, 2),
    ("budget is per host", _spent_on_another_host, "winget_install", HOST, 1),
    ("budget window rolls", _spent_yesterday, "winget_install", HOST, 1),
    ("oldest grant with budget left", _first_exhausted_second_live, "winget_install", HOST, 4),
    ("server scope", _server_scope, "reliability_suppression_remove", None, 2),
    ("server scope asked for a host", _server_scope, "reliability_suppression_remove", HOST, None),
    ("never authorized", _never_authorized_row, "shell_exec", HOST, None),
]


@pytest.mark.parametrize(
    ("setup", "tool", "host", "expected"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_remaining_agrees_with_consume(tmp_path, setup: Setup, tool: str,
                                             host: str | None, expected: int | None) -> None:
    store = AuthorizationStore(str(tmp_path / "remaining.sqlite"))
    await store.connect()
    try:
        await setup(store)
        shown = await store.remaining("installer", HASH, tool, host, NOW)
        # Read-only: asking twice changes nothing.
        assert await store.remaining("installer", HASH, tool, host, NOW) == shown
        spent = await store.consume(
            steward_id="installer", effective_hash=HASH, tool=tool, host_id=host,
            run_id="r1", now=NOW,
        )
        assert (shown is None) == (spent is None)
        if shown is None:
            assert expected is None
            return
        assert spent is not None
        assert shown["id"] == spent.id
        assert shown["expires_at"] == spent.expires_at
        assert shown["remaining_today"] == expected
        after = await store.remaining("installer", HASH, tool, host, NOW)
        if expected == 1:
            # The last attempt of that grant is spent; another may still cover it.
            assert after is None or after["id"] != spent.id
        else:
            assert after is not None and after["id"] == spent.id
            assert after["remaining_today"] == expected - 1
    finally:
        await store.close()

"""Regression tests for the shared SQLite connection settings.

Every store opens its own connection to the *same* file. If any of them omits
``busy_timeout`` (SQLite default: 0), momentary write contention raises
``OperationalError: database is locked`` instead of waiting — which previously
propagated out of the agent tunnel and flapped every agent's WebSocket. These
tests pin WAL + a non-zero busy_timeout on every store, and prove a second
writer waits for a held lock instead of failing immediately.

``ALL_STORES`` must cover every store class in the package, not a hand-picked
subset — a store that quietly skips ``_configure_connection`` (as five of them
once did) is exactly the kind of gap this file exists to catch, so a docstring
promise of "every store" that is not actually true defeats the point. See
``test_all_store_classes_are_covered`` below.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil

import pytest

import kenny_server
from kenny_server.discord_identity import DiscordIdentityStore
from kenny_server.keystore import KeyStore
from kenny_server.oauthstore import OAuthStore
from kenny_server.store import (
    AlertStateStore,
    BackupTargetStore,
    ChatHistoryStore,
    EventStore,
    PolicyStore,
    ReliabilitySuppressionStore,
    SettingsStore,
    TelemetryStore,
    TicketRuleStore,
    UpdateStore,
    WebFilterStore,
)
from kenny_server.ticketstore import TicketStore
from kenny_server.tokenstore import AgentTokenStore
from kenny_server.userstore import UserStore

# Every entry is (store_cls, kwargs). ``db_path`` is filled in per-test from
# ``tmp_path``; anything else a constructor needs goes in kwargs here.
ALL_STORES: list[tuple[type, dict]] = [
    (TelemetryStore, {}),
    (EventStore, {}),
    (AlertStateStore, {}),
    (PolicyStore, {}),
    (WebFilterStore, {}),
    (ChatHistoryStore, {}),
    (BackupTargetStore, {}),
    (ReliabilitySuppressionStore, {}),
    (TicketRuleStore, {}),
    (UpdateStore, {}),
    (SettingsStore, {}),
    (AgentTokenStore, {}),
    (KeyStore, {}),
    (UserStore, {}),
    (OAuthStore, {}),
    (TicketStore, {}),
    (DiscordIdentityStore, {}),
]


def _discover_store_classes() -> dict[str, type]:
    """Every class in the package shaped like a store: async ``connect`` + a
    ``_conn`` property. Reflection, not a maintained list, so a new store
    that forgets to opt in here is a hard test failure, not a silent gap.
    """

    found: dict[str, type] = {}
    for _finder, name, ispkg in pkgutil.walk_packages(
        kenny_server.__path__, prefix="kenny_server."
    ):
        if ispkg:
            continue
        module = importlib.import_module(name)
        for attr_name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != module.__name__:
                continue
            connect = getattr(obj, "connect", None)
            conn_prop = inspect.getattr_static(obj, "_conn", None)
            if (
                connect is not None
                and inspect.iscoroutinefunction(connect)
                and isinstance(conn_prop, property)
            ):
                found[obj.__name__] = obj
    return found


def test_all_store_classes_are_covered() -> None:
    """Fails when a new store class defines connect()+_conn but isn't listed
    in ALL_STORES above — the seam that stops this suite's coverage from
    silently drifting behind the codebase again.
    """

    discovered = _discover_store_classes()
    covered = {cls.__name__ for cls, _ in ALL_STORES}
    missing = set(discovered) - covered
    assert not missing, (
        f"store class(es) {sorted(missing)} define connect()+_conn but are not "
        "in ALL_STORES in tests/test_store_pragmas.py — add them so this suite "
        "proves they set WAL + busy_timeout + synchronous"
    )


@pytest.mark.parametrize(
    "store_cls,kwargs", ALL_STORES, ids=[c.__name__ for c, _ in ALL_STORES]
)
async def test_store_sets_wal_and_busy_timeout(store_cls, kwargs, tmp_path) -> None:
    store = store_cls(db_path=str(tmp_path / "kenny.sqlite"), **kwargs)
    await store.connect()
    try:
        async with store._conn.execute("PRAGMA busy_timeout") as cur:
            busy_timeout = (await cur.fetchone())[0]
        async with store._conn.execute("PRAGMA journal_mode") as cur:
            journal_mode = (await cur.fetchone())[0]
        async with store._conn.execute("PRAGMA synchronous") as cur:
            synchronous = (await cur.fetchone())[0]
    finally:
        await store.close()

    assert busy_timeout > 0, f"{store_cls.__name__} left busy_timeout at 0 (locks fail instantly)"
    assert journal_mode.lower() == "wal", f"{store_cls.__name__} is not in WAL mode"
    # synchronous=NORMAL is SQLite's numeric level 1 (0=OFF, 1=NORMAL, 2=FULL).
    assert synchronous == 1, f"{store_cls.__name__} is not PRAGMA synchronous=NORMAL"


async def test_insert_waits_out_a_held_write_lock(tmp_path) -> None:
    """A telemetry insert must not raise while another connection holds the lock.

    This is the exact failure that tore down the agent tunnel: with busy_timeout=0
    the insert raised ``database is locked`` immediately. With the configured
    timeout it blocks until the holder commits, then succeeds.
    """

    db = str(tmp_path / "kenny.sqlite")
    writer = TelemetryStore(db_path=db)
    inserter = TelemetryStore(db_path=db)
    await writer.connect()
    await inserter.connect()
    try:
        # Hold an exclusive write transaction open on the first connection.
        await writer._conn.execute("BEGIN IMMEDIATE")
        await writer._conn.execute(
            "INSERT INTO snapshots (agent_id, collected_at, received_at, snapshot) "
            "VALUES ('holder', '2026-07-04T00:00:00Z', '2026-07-04T00:00:00Z', '{}')"
        )

        async def _release_after(delay: float) -> None:
            await asyncio.sleep(delay)
            await writer._conn.commit()

        # The second insert must block on the lock and then succeed once the
        # holder commits — never raise OperationalError. busy_timeout (5s default)
        # comfortably covers the 0.2s hold.
        release = asyncio.create_task(_release_after(0.2))
        # Must not raise OperationalError("database is locked"):
        await inserter.insert("example-pc", "2026-07-04T00:00:01Z", {"cpu": {"load": 1}})
        await release
        assert await inserter.latest("example-pc") is not None
    finally:
        await inserter.close()
        await writer.close()


# -- write_lock() itself (ADR-0051) -------------------------------------------
#
# Serializes this process's SQLite writers so a stuck or slow writer delays
# the next one instead of starving it out from under busy_timeout (see the
# module comment in store.py next to write_lock's definition). Two properties
# are easy to break by well-meaning refactor, so each gets its own test:
# re-entrancy is scoped to the *task* that holds the lock (not a ContextVar,
# which a child task would inherit), and the lock state is scoped to the
# *event loop* it was first acquired on (a plain module-level asyncio.Lock
# binds to whichever loop touches it first and raises when a later test's
# loop reuses it).


async def test_write_lock_is_exclusive_across_tasks() -> None:
    from kenny_server.store import write_lock

    order: list[str] = []

    async def worker(name: str, hold_s: float) -> None:
        async with write_lock():
            order.append(f"{name}-start")
            await asyncio.sleep(hold_s)
            order.append(f"{name}-end")

    await asyncio.wait_for(asyncio.gather(worker("a", 0.05), worker("b", 0.0)), timeout=5)
    # "a" acquired first and held the lock; "b" could not interleave into it.
    assert order == ["a-start", "a-end", "b-start", "b-end"]


async def test_write_lock_is_reentrant_within_one_task() -> None:
    from kenny_server.store import write_lock

    async def nested() -> str:
        async with write_lock():
            async with write_lock():  # same task: must not deadlock or re-block
                return "ok"

    assert await asyncio.wait_for(nested(), timeout=5) == "ok"


async def test_write_lock_does_not_let_a_child_task_in_early() -> None:
    """A task spawned *while* the lock is held must still block on it — the
    re-entrancy check is task identity, not something a child could inherit
    via context propagation.
    """

    from kenny_server.store import write_lock

    async def child(acquired: asyncio.Event) -> None:
        async with write_lock():
            acquired.set()

    acquired = asyncio.Event()
    async with write_lock():
        t = asyncio.create_task(child(acquired))
        await asyncio.sleep(0.02)
        assert not acquired.is_set(), "child task acquired the lock its parent still holds"
    await asyncio.wait_for(t, timeout=5)
    assert acquired.is_set()


def test_write_lock_works_across_separate_event_loops() -> None:
    """Regression guard for the classic asyncio.Lock footgun: a lock created
    on one loop raises ``RuntimeError`` if a later ``acquire()`` runs on a
    different loop. pytest-asyncio hands each test its own loop, so this can
    only be exercised with loops managed by hand, sequentially.
    """

    from kenny_server.store import write_lock

    async def use_lock() -> str:
        async with write_lock():
            return "ok"

    # Two independent asyncio.run() calls == two independent event loops.
    assert asyncio.run(use_lock()) == "ok"
    assert asyncio.run(use_lock()) == "ok"


async def test_concurrent_writers_across_stores_do_not_raise(tmp_path) -> None:
    """Reproduces the reported failure: many writers hitting the same DB file
    at once used to raise ``database is locked`` even with busy_timeout
    configured (a stuck unconfigured-store transaction could hold the WAL
    writer lock past every other connection's timeout). ``_BUSY_TIMEOUT_MS``
    is patched to a value too short for SQLite's own busy handler to save
    this on its own.

    Confirmed manually (not asserted here, since it would hang the suite):
    with ``write_lock`` neutralized to a no-op, this exact scenario does not
    just raise ``OperationalError`` — 24 connections all hammering a 1ms
    busy_timeout wedge into a hang, worse than the reported bug. The
    ``wait_for`` below turns a future regression of that shape into a fast,
    clear failure instead of a stuck CI run.
    """

    import kenny_server.store as store_mod

    monkeypatch_value = store_mod._BUSY_TIMEOUT_MS
    store_mod._BUSY_TIMEOUT_MS = 1
    try:
        db = str(tmp_path / "contend.sqlite")
        telemetry = [TelemetryStore(db_path=db) for _ in range(8)]
        webfilter = [WebFilterStore(db_path=db) for _ in range(8)]
        alerts = [AlertStateStore(db_path=db) for _ in range(8)]
        for s in (*telemetry, *webfilter, *alerts):
            await s.connect()
        try:

            async def _insert(i: int) -> None:
                await telemetry[i].insert(f"agent-{i}", f"2026-08-05T00:00:{i:02d}Z", {"n": i})

            async def _upsert(i: int) -> None:
                await webfilter[i].upsert_events(
                    f"agent-{i}", [{"domain": f"example{i}.test", "hits": 1}]
                )

            async def _alert(i: int) -> None:
                await alerts[i].upsert(
                    f"agent-{i}", "disk", status="ok", since="2026-08-05T00:00:00Z",
                    last_notified_at=None,
                )

            await asyncio.wait_for(
                asyncio.gather(
                    *(_insert(i) for i in range(8)),
                    *(_upsert(i) for i in range(8)),
                    *(_alert(i) for i in range(8)),
                ),
                timeout=10,
            )
        finally:
            for s in (*telemetry, *webfilter, *alerts):
                await s.close()

        # All 24 concurrent writers landed — nothing was silently dropped by a
        # swallowed OperationalError.
        verify = TelemetryStore(db_path=db)
        await verify.connect()
        try:
            for i in range(8):
                assert await verify.latest(f"agent-{i}") is not None
        finally:
            await verify.close()
    finally:
        store_mod._BUSY_TIMEOUT_MS = monkeypatch_value

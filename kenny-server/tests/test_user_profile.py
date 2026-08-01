"""Capability profile column: schema migration + UserStore API (ADR-0037 extension).

A capability profile is a third, optional authorization axis alongside role
and host scope: a named tool-allowlist that narrows what an account may do.
``NULL`` means "no profile set" and must behave exactly as before this column
existed — unrestricted, subject to role and host scope.
"""

from __future__ import annotations

import sqlite3

import pytest

from kenny_server.userstore import UserStore


def _seed_old_shape_db(db_path: str) -> None:
    """Create a ``users`` table as it looked before ``capability_profile`` existed."""

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            totp_secret   TEXT,
            email         TEXT,
            role          TEXT NOT NULL,
            avatar        TEXT,
            disabled      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO users "
        "(username, password_hash, role, disabled, created_at, updated_at) "
        "VALUES ('old', 'hash', 'user', 0, 't', 't')"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
async def store(tmp_path):
    us = UserStore(str(tmp_path / "users.sqlite"))
    await us.connect()
    yield us
    await us.close()


async def test_migration_adds_column_to_old_db_and_reads_back_none(tmp_path) -> None:
    db_path = str(tmp_path / "old.sqlite")
    _seed_old_shape_db(db_path)

    us = UserStore(db_path)
    await us.connect()
    try:
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        conn.close()
        assert "capability_profile" in cols

        row = await us.get_by_username("old")
        assert row is not None
        assert row["capability_profile"] is None
        assert (await us.get_capability_profile(row["id"])) is None
    finally:
        await us.close()


async def test_connect_twice_is_idempotent(tmp_path) -> None:
    db_path = str(tmp_path / "twice.sqlite")
    _seed_old_shape_db(db_path)

    us = UserStore(db_path)
    await us.connect()
    await us.connect()  # second connect() is a no-op (already connected)
    await us.close()

    # Reconnecting fresh (new instance) against the same file must not error
    # or duplicate the column.
    us2 = UserStore(db_path)
    await us2.connect()
    try:
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
        conn.close()
        assert cols.count("capability_profile") == 1
    finally:
        await us2.close()


async def test_set_and_get_capability_profile_round_trip(store, monkeypatch) -> None:
    monkeypatch.setattr(
        "kenny_server.tool_classes.PROFILES",
        {"self-service-basic": frozenset({"list_agents"})},
    )
    user = await store.create_user("kid", "pw-123456", "user")
    assert await store.get_capability_profile(user["id"]) is None

    await store.set_capability_profile(user["id"], "self-service-basic")
    assert await store.get_capability_profile(user["id"]) == "self-service-basic"

    # Setting back to None clears the restriction.
    await store.set_capability_profile(user["id"], None)
    assert await store.get_capability_profile(user["id"]) is None


async def test_public_user_includes_capability_profile(store, monkeypatch) -> None:
    monkeypatch.setattr(
        "kenny_server.tool_classes.PROFILES",
        {"self-service-basic": frozenset({"list_agents"})},
    )
    user = await store.create_user("kid", "pw-123456", "user")
    assert "capability_profile" in user
    assert user["capability_profile"] is None

    await store.set_capability_profile(user["id"], "self-service-basic")
    fetched = await store.get_user(user["id"])
    assert fetched is not None
    assert fetched["capability_profile"] == "self-service-basic"

    listed = await store.list_users()
    assert all("capability_profile" in u for u in listed)


async def test_unknown_profile_rejected_when_profiles_populated(store, monkeypatch) -> None:
    monkeypatch.setattr(
        "kenny_server.tool_classes.PROFILES",
        {"self-service-basic": frozenset({"list_agents"})},
    )
    user = await store.create_user("kid", "pw-123456", "user")
    with pytest.raises(ValueError):
        await store.set_capability_profile(user["id"], "not-a-real-profile")
    # The rejected write must not have taken effect.
    assert await store.get_capability_profile(user["id"]) is None


async def test_empty_profile_rejected_when_profiles_missing(store, monkeypatch) -> None:
    """With ``PROFILES`` empty (module not landed yet), only reject the empty string."""

    monkeypatch.setattr("kenny_server.tool_classes.PROFILES", {})
    user = await store.create_user("kid", "pw-123456", "user")
    with pytest.raises(ValueError):
        await store.set_capability_profile(user["id"], "")

    # Any non-empty name is accepted as a fallback until tool_classes lands.
    await store.set_capability_profile(user["id"], "whatever-name")
    assert await store.get_capability_profile(user["id"]) == "whatever-name"

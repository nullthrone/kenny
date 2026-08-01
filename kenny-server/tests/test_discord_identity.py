"""``DiscordIdentityStore`` schema, resolution, claims and retention.

This table is the only bridge from a Discord snowflake to a kenny principal, so
the tests here are about what must *not* resolve as much as what must: a
disabled row, a foreign guild, a second snowflake for the same account, a
replayed claim code, an expired one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kenny_server.discord_identity import (
    CLAIM_CODE_LEN,
    DiscordIdentityStore,
    IdentityConflict,
    to_iso,
)

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
GUILD = "111111111111111111"
OTHER_GUILD = "999999999999999999"
SNOWFLAKE = "222222222222222222"


async def _store(tmp_path, name: str = "identity.sqlite", **kwargs) -> DiscordIdentityStore:
    store = DiscordIdentityStore(str(tmp_path / name), **kwargs)
    await store.connect()
    return store


async def test_connect_is_idempotent(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        conn = store._conn
        await store.connect()
        assert store._conn is conn
    finally:
        await store.close()

    again = await _store(tmp_path)
    try:
        assert await again.list_identities() == []
    finally:
        await again.close()


async def test_identity_round_trips(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        identity = await store.link(
            discord_user_id=SNOWFLAKE,
            user_id=7,
            guild_id=GUILD,
            linked_via="member_list",
            linked_by=1,
            now=NOW,
        )
        assert identity.discord_user_id == SNOWFLAKE
        assert identity.user_id == 7
        assert identity.linked_via == "member_list"
        assert identity.linked_by == 1
        assert identity.linked_at == to_iso(NOW)
        assert identity.disabled is False

        resolved = await store.resolve(SNOWFLAKE, GUILD)
        assert resolved is not None
        assert resolved.user_id == 7
        assert resolved.as_dict()["guild_id"] == GUILD
    finally:
        await store.close()


async def test_resolve_is_guild_scoped(tmp_path) -> None:
    """A mapping made in one guild must not carry into another."""

    store = await _store(tmp_path)
    try:
        await store.link(
            discord_user_id=SNOWFLAKE, user_id=7, guild_id=GUILD, linked_via="claim"
        )
        assert await store.resolve(SNOWFLAKE, OTHER_GUILD) is None
        assert await store.resolve("333333333333333333", GUILD) is None
    finally:
        await store.close()


async def test_disabled_identity_resolves_to_none(tmp_path) -> None:
    """Disabling must behave exactly like never having linked."""

    store = await _store(tmp_path)
    try:
        await store.link(
            discord_user_id=SNOWFLAKE, user_id=7, guild_id=GUILD, linked_via="claim"
        )
        assert await store.set_disabled(SNOWFLAKE, disabled=True) is True

        assert await store.resolve(SNOWFLAKE, GUILD) is None
        # The row itself survives, with its trail intact — revocation is a flag.
        row = await store.get(SNOWFLAKE)
        assert row is not None and row.disabled is True
        assert row.linked_via == "claim"

        await store.set_disabled(SNOWFLAKE, disabled=False)
        assert await store.resolve(SNOWFLAKE, GUILD) is not None
    finally:
        await store.close()


async def test_one_identity_per_user_and_guild(tmp_path) -> None:
    """Two snowflakes must never both speak as the same kenny account."""

    store = await _store(tmp_path)
    try:
        await store.link(
            discord_user_id=SNOWFLAKE, user_id=7, guild_id=GUILD, linked_via="claim"
        )
        with pytest.raises(IdentityConflict):
            await store.link(
                discord_user_id="444444444444444444",
                user_id=7,
                guild_id=GUILD,
                linked_via="member_list",
            )
        # The failed insert must not have disturbed the existing binding.
        resolved = await store.resolve(SNOWFLAKE, GUILD)
        assert resolved is not None and resolved.user_id == 7
        assert len(await store.list_identities()) == 1

        # The same account in a *different* guild is fine.
        await store.link(
            discord_user_id="444444444444444444",
            user_id=7,
            guild_id=OTHER_GUILD,
            linked_via="member_list",
        )
        assert len(await store.list_identities(user_id=7)) == 2
    finally:
        await store.close()


async def test_rebinding_a_snowflake_replaces_the_row(tmp_path) -> None:
    """An operator fixing a misassignment moves the snowflake, not adds to it."""

    store = await _store(tmp_path)
    try:
        await store.link(
            discord_user_id=SNOWFLAKE, user_id=7, guild_id=GUILD, linked_via="claim"
        )
        await store.link(
            discord_user_id=SNOWFLAKE,
            user_id=8,
            guild_id=GUILD,
            linked_via="member_list",
            linked_by=1,
        )
        resolved = await store.resolve(SNOWFLAKE, GUILD)
        assert resolved is not None and resolved.user_id == 8
        assert resolved.linked_via == "member_list"
        assert len(await store.list_identities()) == 1
    finally:
        await store.close()


async def test_unknown_link_via_is_rejected(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        with pytest.raises(ValueError):
            await store.link(
                discord_user_id=SNOWFLAKE, user_id=7, guild_id=GUILD, linked_via="vibes"
            )
    finally:
        await store.close()


async def test_unlink_removes_the_binding(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        await store.link(
            discord_user_id=SNOWFLAKE, user_id=7, guild_id=GUILD, linked_via="claim"
        )
        assert await store.unlink(SNOWFLAKE) is True
        assert await store.resolve(SNOWFLAKE, GUILD) is None
        assert await store.unlink(SNOWFLAKE) is False
    finally:
        await store.close()


async def test_list_identities_filters(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        await store.link(
            discord_user_id=SNOWFLAKE,
            user_id=7,
            guild_id=GUILD,
            linked_via="claim",
            now=NOW,
        )
        await store.link(
            discord_user_id="555555555555555555",
            user_id=8,
            guild_id=GUILD,
            linked_via="claim",
            now=NOW + timedelta(minutes=1),
        )
        await store.set_disabled("555555555555555555", disabled=True)

        assert len(await store.list_identities(guild_id=GUILD)) == 2
        enabled = await store.list_identities(guild_id=GUILD, include_disabled=False)
        assert [i.discord_user_id for i in enabled] == [SNOWFLAKE]
        assert await store.list_identities(guild_id=OTHER_GUILD) == []
    finally:
        await store.close()


# -- claims ------------------------------------------------------------------


async def test_claim_is_single_use(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        claim = await store.open_claim(
            discord_user_id=SNOWFLAKE,
            display_hint="lena (not authoritative)",
            guild_id=GUILD,
            now=NOW,
        )
        assert len(claim.code) == CLAIM_CODE_LEN
        assert claim.consumed_at is None
        assert claim.is_open(NOW) is True

        identity = await store.consume_claim(claim.code, user_id=7, linked_by=1, now=NOW)
        assert identity is not None
        assert identity.user_id == 7
        assert identity.linked_via == "claim"
        assert identity.linked_by == 1
        assert await store.resolve(SNOWFLAKE, GUILD) is not None

        # Replaying the same code changes nothing and reports nothing.
        assert await store.consume_claim(claim.code, user_id=9, now=NOW) is None
        resolved = await store.resolve(SNOWFLAKE, GUILD)
        assert resolved is not None and resolved.user_id == 7
    finally:
        await store.close()


async def test_expired_claim_cannot_be_consumed(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        claim = await store.open_claim(
            discord_user_id=SNOWFLAKE,
            display_hint="lena",
            guild_id=GUILD,
            ttl_secs=60,
            now=NOW,
        )
        late = NOW + timedelta(seconds=61)
        assert claim.is_open(late) is False
        assert await store.consume_claim(claim.code, user_id=7, now=late) is None
        assert await store.resolve(SNOWFLAKE, GUILD) is None

        # And it is no longer offered for confirmation.
        assert await store.list_pending_claims(now=late) == []
        assert [c.code for c in await store.list_pending_claims(now=NOW)] == [claim.code]
    finally:
        await store.close()


async def test_unknown_claim_code_consumes_nothing(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        assert await store.consume_claim("nope", user_id=7, now=NOW) is None
        assert await store.list_identities() == []
    finally:
        await store.close()


async def test_claim_conflict_leaves_claim_unconsumed(tmp_path) -> None:
    """A conflicting confirmation must roll back the consume as well."""

    store = await _store(tmp_path)
    try:
        await store.link(
            discord_user_id=SNOWFLAKE, user_id=7, guild_id=GUILD, linked_via="member_list"
        )
        claim = await store.open_claim(
            discord_user_id="666666666666666666",
            display_hint="someone else",
            guild_id=GUILD,
            now=NOW,
        )
        with pytest.raises(IdentityConflict):
            await store.consume_claim(claim.code, user_id=7, now=NOW)

        still_open = await store.get_claim(claim.code)
        assert still_open is not None and still_open.consumed_at is None
        assert await store.resolve("666666666666666666", GUILD) is None
    finally:
        await store.close()


async def test_pending_claims_are_guild_filtered(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        await store.open_claim(
            discord_user_id=SNOWFLAKE, display_hint="a", guild_id=GUILD, now=NOW
        )
        await store.open_claim(
            discord_user_id="777777777777777777",
            display_hint="b",
            guild_id=OTHER_GUILD,
            now=NOW,
        )
        assert len(await store.list_pending_claims(now=NOW)) == 2
        only = await store.list_pending_claims(guild_id=OTHER_GUILD, now=NOW)
        assert [c.discord_user_id for c in only] == ["777777777777777777"]
    finally:
        await store.close()


async def test_prune_drops_dead_claims_only(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        expired = await store.open_claim(
            discord_user_id="888888888888888888",
            display_hint="stale",
            guild_id=GUILD,
            ttl_secs=60,
            now=NOW,
        )
        consumed = await store.open_claim(
            discord_user_id=SNOWFLAKE, display_hint="done", guild_id=GUILD, now=NOW
        )
        live = await store.open_claim(
            discord_user_id="999000111222333444",
            display_hint="fresh",
            guild_id=GUILD,
            ttl_secs=3600,
            now=NOW,
        )
        await store.consume_claim(consumed.code, user_id=7, now=NOW)

        later = NOW + timedelta(seconds=120)
        assert await store.prune(now=later) == 2
        assert await store.get_claim(expired.code) is None
        assert await store.get_claim(consumed.code) is None
        assert await store.get_claim(live.code) is not None

        # Pruning claims never touches the identities they produced.
        assert await store.resolve(SNOWFLAKE, GUILD) is not None
    finally:
        await store.close()

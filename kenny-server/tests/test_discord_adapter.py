"""Tests for the Discord transport seam: the frozen protocol, the chunking
helper, the guild allowlist, and the in-memory fake.

None of these tests require discord.py to be installed -- that is the point.
"""

from __future__ import annotations

import builtins
import dataclasses

import pytest

from kenny_server.discord_adapter import (
    CHUNK_TARGET_LIMIT,
    DISCORD_MESSAGE_HARD_LIMIT,
    ComponentEvent,
    DiscordGateway,
    DiscordPyGateway,
    GatewayUnavailable,
    GuildMember,
    MessageEvent,
    SlashCommandEvent,
    ThreadStateEvent,
    chunk_message,
)
from tests.support.fake_discord import FakeDiscordGateway

# ---------------------------------------------------------------------------
# start() lazy-import guard
# ---------------------------------------------------------------------------


async def test_start_raises_gateway_unavailable_when_discord_unimportable(monkeypatch):
    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "discord" or name.startswith("discord."):
            raise ImportError("No module named 'discord'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    with pytest.raises(GatewayUnavailable):
        await gateway.start()
    assert gateway.connected is False


async def test_start_raises_gateway_unavailable_via_sys_modules(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "discord", None)  # forces ImportError on `import discord`

    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    with pytest.raises(GatewayUnavailable):
        await gateway.start()


# ---------------------------------------------------------------------------
# chunk_message
# ---------------------------------------------------------------------------


def test_chunk_message_short_content_is_one_chunk():
    content = "hello world"
    assert chunk_message(content) == [content]


def test_chunk_message_splits_long_content_on_line_boundaries():
    line = "x" * 100 + "\n"
    content = line * 30  # 3030 chars, well above the 1900 target
    chunks = chunk_message(content)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= CHUNK_TARGET_LIMIT
        assert len(chunk) <= DISCORD_MESSAGE_HARD_LIMIT
    assert "".join(chunks) == content


def test_chunk_message_hard_splits_a_single_overlong_line():
    content = "y" * 5000  # one line, no newlines at all
    chunks = chunk_message(content)
    assert len(chunks) == 3  # 1900 + 1900 + 1200
    for chunk in chunks:
        assert len(chunk) <= CHUNK_TARGET_LIMIT
    assert "".join(chunks) == content


def test_chunk_message_mixed_short_and_overlong_lines_rejoin_exactly():
    content = "short line\n" + ("z" * 4200) + "\nanother short line\n" + ("a" * 50)
    chunks = chunk_message(content)
    for chunk in chunks:
        assert len(chunk) <= CHUNK_TARGET_LIMIT
    assert "".join(chunks) == content


def test_chunk_message_every_chunk_within_hard_limit_custom_limit():
    content = "line one\n" * 500
    chunks = chunk_message(content, limit=200)
    for chunk in chunks:
        assert len(chunk) <= 200
    assert "".join(chunks) == content


# ---------------------------------------------------------------------------
# Guild allowlist
# ---------------------------------------------------------------------------


def test_empty_guild_allowlist_denies_every_guild():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset())
    assert gateway._guild_allowed("any-guild") is False
    assert gateway._guild_allowed("") is False


def test_nonempty_allowlist_only_allows_listed_guilds():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1", "g2"}))
    assert gateway._guild_allowed("g1") is True
    assert gateway._guild_allowed("g2") is True
    assert gateway._guild_allowed("g3") is False


def test_intake_drops_events_from_guilds_not_on_the_allowlist():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    allowed = ThreadStateEvent(guild_id="g1", thread_id="t1", archived=True)
    dropped = ThreadStateEvent(guild_id="g2", thread_id="t2", archived=True)
    assert gateway._intake(allowed) is allowed
    assert gateway._intake(dropped) is None


def test_intake_with_empty_allowlist_drops_everything():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset())
    event = ThreadStateEvent(guild_id="g1", thread_id="t1", archived=False)
    assert gateway._intake(event) is None


# ---------------------------------------------------------------------------
# FakeDiscordGateway
# ---------------------------------------------------------------------------


def test_fake_discord_gateway_satisfies_the_protocol():
    fake = FakeDiscordGateway()
    assert isinstance(fake, DiscordGateway)


async def test_fake_discord_gateway_round_trips_a_fed_event():
    fake = FakeDiscordGateway()
    await fake.start()
    assert fake.connected is True

    event = MessageEvent(
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        message_id="m1",
        author_id="u1",
        author_is_bot=False,
        content="hi",
        mentions_bot=True,
        attachment_count=0,
    )
    fake.feed(event)
    await fake.close()

    seen = [e async for e in fake.events()]
    assert seen == [event]


async def test_fake_discord_gateway_records_outbound_calls():
    fake = FakeDiscordGateway()
    await fake.start()

    msg_id = await fake.post_message(channel_id="c1", content="hello")
    assert fake.posted == [("c1", "hello")]
    assert msg_id

    card_id = await fake.post_approval_card(
        channel_id="c1", approval_id="a1", summary="do the thing", detail_url="http://x"
    )
    assert fake.cards == [
        {
            "channel_id": "c1",
            "approval_id": "a1",
            "summary": "do the thing",
            "detail_url": "http://x",
        }
    ]
    assert card_id

    await fake.resolve_card(
        channel_id="c1", message_id=card_id, outcome="approved", decided_by="u1"
    )
    assert fake.resolved == [
        {
            "channel_id": "c1",
            "message_id": card_id,
            "outcome": "approved",
            "decided_by": "u1",
        }
    ]

    await fake.respond_ephemeral(interaction_id="i1", content="ok")
    assert fake.ephemerals == [("i1", "ok")]

    await fake.defer_interaction(interaction_id="i2")
    assert fake.deferred == ["i2"]

    thread = await fake.open_thread(
        channel_id="c1", name="support", private=True, invite_user_ids=["u1", "u2"]
    )
    assert fake.threads == [thread]
    assert fake.invited == [["u1", "u2"]]

    await fake.archive_thread(thread_id=thread.thread_id, locked=True)
    assert fake.archived == [thread.thread_id]


async def test_fake_discord_gateway_members_and_roles_are_configurable():
    fake = FakeDiscordGateway(
        members={"g1": [GuildMember(user_id="u1", display_hint="Alice")]},
        role_ids={("g1", "u1"): frozenset({"role-admin"})},
    )
    members = await fake.list_guild_members(guild_id="g1")
    assert members == [GuildMember(user_id="u1", display_hint="Alice")]

    roles = await fake.member_role_ids(guild_id="g1", user_id="u1")
    assert roles == frozenset({"role-admin"})

    roles_unknown = await fake.member_role_ids(guild_id="g1", user_id="unknown")
    assert roles_unknown == frozenset()


# ---------------------------------------------------------------------------
# Security-property guard: no `username` field anywhere in this protocol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_cls",
    [MessageEvent, SlashCommandEvent, ComponentEvent, ThreadStateEvent],
)
def test_no_event_dataclass_has_a_username_field(event_cls):
    field_names = {f.name for f in dataclasses.fields(event_cls)}
    assert "username" not in field_names
    for name in field_names:
        assert "username" not in name.lower()

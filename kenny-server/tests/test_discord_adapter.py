"""Tests for the Discord transport seam: the frozen protocol, the chunking
helper, the guild allowlist, and the in-memory fake.

None of these tests require discord.py to be installed -- that is the point --
except the one test that skips itself via ``pytest.importorskip`` when it is
not, because what it pins down only exists once discord.py is real.
"""

from __future__ import annotations

import asyncio
import builtins
import dataclasses
import logging
from types import SimpleNamespace

import pytest

from kenny_server.discord_adapter import (
    CHUNK_TARGET_LIMIT,
    DISCORD_MESSAGE_HARD_LIMIT,
    ApprovalAction,
    CommandOption,
    CommandSpec,
    ComponentEvent,
    DiscordGateway,
    DiscordPyGateway,
    GatewayUnavailable,
    GuildMember,
    MessageEvent,
    SlashCommandEvent,
    ThreadStateEvent,
    _command_spec_to_payload,
    _translate_component,
    _translate_guild_member,
    _translate_message,
    _translate_slash_command,
    _translate_thread_state,
    build_approval_custom_id,
    chunk_message,
    parse_approval_custom_id,
)
from support.fake_discord import FakeDiscordGateway

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
            # Built the way `DiscordPyGateway` builds them, so a test can click
            # the card the gateway produced instead of one it invented itself.
            "custom_ids": {
                "approve": build_approval_custom_id("approve", "a1"),
                "deny": build_approval_custom_id("deny", "a1"),
            },
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


# ---------------------------------------------------------------------------
# Approval-card custom_id scheme
# ---------------------------------------------------------------------------


def test_custom_id_round_trips_approve():
    custom_id = build_approval_custom_id("approve", "approval-123")
    assert parse_approval_custom_id(custom_id) == ApprovalAction(
        action="approve", approval_id="approval-123"
    )


def test_custom_id_round_trips_deny():
    custom_id = build_approval_custom_id("deny", "42")
    assert parse_approval_custom_id(custom_id) == ApprovalAction(action="deny", approval_id="42")


def test_custom_id_build_rejects_unknown_action():
    with pytest.raises(ValueError):
        build_approval_custom_id("maybe", "approval-123")


def test_custom_id_build_rejects_ids_over_the_discord_limit():
    with pytest.raises(ValueError):
        build_approval_custom_id("approve", "x" * 100)


@pytest.mark.parametrize(
    "malformed",
    [
        "not-an-approval-id",
        "kenny-approval:approve",  # missing approval_id
        "kenny-approval:frobnicate:123",  # unknown action
        "kenny-approval::123",  # empty action
        "kenny-approval:approve:",  # empty approval_id
        "",
        "other-prefix:approve:123",
    ],
)
def test_custom_id_parse_rejects_malformed_ids_rather_than_misparsing(malformed):
    assert parse_approval_custom_id(malformed) is None


def test_custom_id_parse_allows_colons_inside_the_approval_id():
    # approval_id itself is never expected to contain ':' in practice (it is
    # kenny's own id, not attacker input) but the parser must not silently
    # truncate it if it did -- maxsplit=2 keeps everything after the second
    # ':' together.
    custom_id = "kenny-approval:approve:abc:def"
    assert parse_approval_custom_id(custom_id) == ApprovalAction(
        action="approve", approval_id="abc:def"
    )


# ---------------------------------------------------------------------------
# discord.py object -> event dataclass translators
# ---------------------------------------------------------------------------


def _fake_message(
    *,
    guild_id="g1",
    channel_id="c1",
    parent_id=None,
    message_id="m1",
    author_id="u1",
    author_is_bot=False,
    content="hello",
    mention_ids=(),
    attachments=(),
):
    channel = SimpleNamespace(id=channel_id)
    if parent_id is not None:
        channel.parent_id = parent_id
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        channel=channel,
        author=SimpleNamespace(id=author_id, bot=author_is_bot),
        content=content,
        mentions=[SimpleNamespace(id=mid) for mid in mention_ids],
        attachments=list(attachments),
    )


def test_translate_message_top_level_channel_has_no_thread_id():
    message = _fake_message(channel_id="c1")
    event = _translate_message(message, bot_user_id="bot1")
    assert event == MessageEvent(
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        message_id="m1",
        author_id="u1",
        author_is_bot=False,
        content="hello",
        mentions_bot=False,
        attachment_count=0,
    )


def test_translate_message_thread_channel_splits_thread_and_parent_id():
    message = _fake_message(channel_id="thread1", parent_id="parent1")
    event = _translate_message(message, bot_user_id="bot1")
    assert event.channel_id == "parent1"
    assert event.thread_id == "thread1"


def test_translate_message_mentions_bot_true_when_bot_id_in_mentions():
    message = _fake_message(mention_ids=["other", "bot1"])
    event = _translate_message(message, bot_user_id="bot1")
    assert event.mentions_bot is True


def test_translate_message_mentions_bot_false_when_bot_id_absent():
    message = _fake_message(mention_ids=["other"])
    event = _translate_message(message, bot_user_id="bot1")
    assert event.mentions_bot is False


def test_translate_message_mentions_bot_false_when_no_bot_user_id_known():
    message = _fake_message(mention_ids=["bot1"])
    event = _translate_message(message, bot_user_id=None)
    assert event.mentions_bot is False


def test_translate_message_attachment_count_reflects_attachment_list():
    message = _fake_message(attachments=[SimpleNamespace(), SimpleNamespace()])
    event = _translate_message(message, bot_user_id=None)
    assert event.attachment_count == 2


def test_translate_message_never_populates_a_username_like_field():
    message = _fake_message(author_id="u1")
    event = _translate_message(message, bot_user_id=None)
    field_names = {f.name for f in dataclasses.fields(event)}
    assert not any("username" in name.lower() for name in field_names)


@pytest.mark.parametrize(
    "guild_id, allowed",
    [("g1", True), ("g2", False)],
)
def test_translated_message_respects_the_allowlist_via_intake(guild_id, allowed):
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    message = _fake_message(guild_id=guild_id)
    event = _translate_message(message, bot_user_id=None)
    result = gateway._intake(event)
    assert (result is not None) is allowed


def _fake_interaction_command(
    *,
    guild_id="g1",
    channel_id="c1",
    parent_id=None,
    interaction_id="i1",
    user_id="u1",
    command_name="status",
    options=None,
    include_channel=True,
):
    resolved_channel: SimpleNamespace | None = None
    if include_channel:
        resolved_channel = SimpleNamespace(id=channel_id)
        if parent_id is not None:
            resolved_channel.parent_id = parent_id
    namespace = SimpleNamespace(**(options or {}))
    return SimpleNamespace(
        id=interaction_id,
        guild_id=guild_id,
        channel_id=channel_id,
        channel=resolved_channel,
        user=SimpleNamespace(id=user_id),
        command=SimpleNamespace(name=command_name),
        namespace=namespace,
    )


def test_translate_slash_command_reads_stringified_options():
    interaction = _fake_interaction_command(options={"host": "living-room-pc", "minutes": 30})
    event = _translate_slash_command(interaction)
    assert event == SlashCommandEvent(
        guild_id="g1",
        channel_id="c1",
        thread_id=None,
        user_id="u1",
        interaction_id="i1",
        command="status",
        options={"host": "living-room-pc", "minutes": "30"},
    )


def test_translate_slash_command_thread_channel_splits_thread_and_parent_id():
    interaction = _fake_interaction_command(channel_id="thread1", parent_id="parent1")
    event = _translate_slash_command(interaction)
    assert event.channel_id == "parent1"
    assert event.thread_id == "thread1"


def test_translate_slash_command_falls_back_when_channel_object_absent():
    interaction = _fake_interaction_command(channel_id="c1", include_channel=False)
    event = _translate_slash_command(interaction)
    assert event.channel_id == "c1"
    assert event.thread_id is None


def test_translate_slash_command_never_populates_a_username_like_field():
    interaction = _fake_interaction_command()
    event = _translate_slash_command(interaction)
    field_names = {f.name for f in dataclasses.fields(event)}
    assert not any("username" in name.lower() for name in field_names)


@pytest.mark.parametrize(
    "guild_id, allowed",
    [("g1", True), ("g2", False)],
)
def test_translated_slash_command_respects_the_allowlist_via_intake(guild_id, allowed):
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    interaction = _fake_interaction_command(guild_id=guild_id)
    event = _translate_slash_command(interaction)
    result = gateway._intake(event)
    assert (result is not None) is allowed


def _fake_interaction_component(
    *,
    guild_id="g1",
    channel_id="c1",
    interaction_id="i1",
    user_id="u1",
    message_id="m1",
    custom_id="kenny-approval:approve:a1",
):
    return SimpleNamespace(
        id=interaction_id,
        guild_id=guild_id,
        channel_id=channel_id,
        user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(id=message_id),
        data={"custom_id": custom_id},
    )


def test_translate_component_reads_custom_id():
    interaction = _fake_interaction_component(custom_id="kenny-approval:deny:a9")
    event = _translate_component(interaction)
    assert event == ComponentEvent(
        guild_id="g1",
        channel_id="c1",
        message_id="m1",
        user_id="u1",
        interaction_id="i1",
        custom_id="kenny-approval:deny:a9",
    )


def test_translate_component_never_populates_a_username_like_field():
    interaction = _fake_interaction_component()
    event = _translate_component(interaction)
    field_names = {f.name for f in dataclasses.fields(event)}
    assert not any("username" in name.lower() for name in field_names)


@pytest.mark.parametrize(
    "guild_id, allowed",
    [("g1", True), ("g2", False)],
)
def test_translated_component_respects_the_allowlist_via_intake(guild_id, allowed):
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    interaction = _fake_interaction_component(guild_id=guild_id)
    event = _translate_component(interaction)
    result = gateway._intake(event)
    assert (result is not None) is allowed


def test_translate_thread_state():
    thread = SimpleNamespace(guild=SimpleNamespace(id="g1"), id="t1", archived=True)
    event = _translate_thread_state(thread)
    assert event == ThreadStateEvent(guild_id="g1", thread_id="t1", archived=True)


def test_translate_thread_state_never_populates_a_username_like_field():
    thread = SimpleNamespace(guild=SimpleNamespace(id="g1"), id="t1", archived=False)
    event = _translate_thread_state(thread)
    field_names = {f.name for f in dataclasses.fields(event)}
    assert not any("username" in name.lower() for name in field_names)


def test_translate_guild_member_uses_display_name_as_display_hint_only():
    member = SimpleNamespace(id="u1", display_name="Alice (nickname)")
    result = _translate_guild_member(member)
    assert result == GuildMember(user_id="u1", display_hint="Alice (nickname)")


# ---------------------------------------------------------------------------
# register_commands payload building
# ---------------------------------------------------------------------------


def test_command_spec_to_payload_shapes_options_as_discord_string_options():
    spec = CommandSpec(
        name="diagnose",
        description="Diagnose a host",
        options=(CommandOption(name="host", description="Which host", required=True),),
    )
    payload = _command_spec_to_payload(spec)
    assert payload == {
        "name": "diagnose",
        "description": "Diagnose a host",
        "type": 1,
        "options": [
            {"name": "host", "description": "Which host", "type": 3, "required": True}
        ],
    }


def test_command_spec_to_payload_with_no_options():
    spec = CommandSpec(name="status", description="Fleet status")
    payload = _command_spec_to_payload(spec)
    assert payload["options"] == []


async def test_register_commands_waits_for_the_client_to_finish_logging_in():
    """``start()`` returns once the connect task is *scheduled*, not once
    discord.py has actually logged in -- ``application_id`` stays unset until
    then. Pins the v2.0.2 regression: ``main.py``'s ``_discord_loop`` called
    ``register_commands`` immediately after ``start()`` returned, racing the
    connect task (which had not even had a turn to run yet) and silently
    skipping registration every single time, logging "gateway not started".
    """

    discord = pytest.importorskip("discord")

    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"123"}))
    client = discord.Client(intents=discord.Intents.none())
    await client._async_setup_hook()  # initialises `_ready`, as login() would
    gateway._client = client

    calls = []

    async def fake_bulk_upsert(app_id, guild_id, payload):
        calls.append((app_id, guild_id, payload))

    client.http.bulk_upsert_guild_commands = fake_bulk_upsert

    async def become_ready_shortly() -> None:
        await asyncio.sleep(0.05)
        client._connection.application_id = 999
        client._ready.set()

    asyncio.create_task(become_ready_shortly())
    spec = CommandSpec(name="whoami", description="Show what kenny knows about you")
    await gateway.register_commands(guild_id="123", commands=[spec])

    assert calls == [(999, 123, [_command_spec_to_payload(spec)])]


async def test_register_commands_gives_up_if_the_client_never_becomes_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    """A bad token or a dead network must not hang the caller forever."""

    discord = pytest.importorskip("discord")
    from kenny_server import discord_adapter

    monkeypatch.setattr(discord_adapter, "_READY_TIMEOUT_SECS", 0.05)
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"123"}))
    client = discord.Client(intents=discord.Intents.none())
    await client._async_setup_hook()  # `_ready` exists but is never set
    gateway._client = client

    await gateway.register_commands(
        guild_id="123",
        commands=[CommandSpec(name="whoami", description="Show what kenny knows about you")],
    )  # must return, not hang


# ---------------------------------------------------------------------------
# post_message: chunk-and-post ordering (fake the send, no discord package)
# ---------------------------------------------------------------------------


class _RecordingChannel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return SimpleNamespace(id=f"posted-{len(self.sent)}")


async def test_post_message_posts_every_chunk_in_order_and_returns_the_last_id():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    channel = _RecordingChannel()
    gateway._client = SimpleNamespace(get_channel=lambda cid: channel)

    line = "x" * 100 + "\n"
    content = line * 30  # forces multiple chunks, per chunk_message's own tests
    expected_chunks = chunk_message(content)
    assert len(expected_chunks) > 1

    last_id = await gateway.post_message(channel_id="111", content=content)

    assert channel.sent == expected_chunks
    assert last_id == f"posted-{len(expected_chunks)}"


async def test_post_message_short_content_is_a_single_post():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    channel = _RecordingChannel()
    gateway._client = SimpleNamespace(get_channel=lambda cid: channel)

    last_id = await gateway.post_message(channel_id="111", content="hi there")

    assert channel.sent == ["hi there"]
    assert last_id == "posted-1"


async def test_post_message_resolves_channel_via_fetch_when_not_cached():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    channel = _RecordingChannel()

    async def fetch_channel(cid):
        return channel

    gateway._client = SimpleNamespace(get_channel=lambda cid: None, fetch_channel=fetch_channel)

    last_id = await gateway.post_message(channel_id="111", content="hi")

    assert channel.sent == ["hi"]
    assert last_id == "posted-1"


# ---------------------------------------------------------------------------
# Empty-content-on-mention diagnostic (Message Content intent signature)
# ---------------------------------------------------------------------------


def test_empty_content_on_mention_warns_exactly_once(caplog):
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    gateway._bot_user_id = "bot1"

    mentioning_empty = _fake_message(guild_id="g1", content="", mention_ids=["bot1"])

    with caplog.at_level(logging.WARNING, logger="kenny.discord_adapter"):
        gateway._handle_message(mentioning_empty)
        gateway._handle_message(mentioning_empty)
        gateway._handle_message(mentioning_empty)

    warnings = [r for r in caplog.records if "Message Content" in r.getMessage()]
    assert len(warnings) == 1
    assert gateway._warned_empty_content is True


def test_no_diagnostic_when_mention_has_content():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    gateway._bot_user_id = "bot1"

    mentioning_with_content = _fake_message(
        guild_id="g1", content="hello bot", mention_ids=["bot1"]
    )
    gateway._handle_message(mentioning_with_content)

    assert gateway._warned_empty_content is False


def test_no_diagnostic_when_empty_content_without_a_mention():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    gateway._bot_user_id = "bot1"

    non_mentioning_empty = _fake_message(guild_id="g1", content="", mention_ids=[])
    gateway._handle_message(non_mentioning_empty)

    assert gateway._warned_empty_content is False


def test_handle_message_enqueues_translated_event_when_guild_allowed():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    message = _fake_message(guild_id="g1", message_id="m42")

    gateway._handle_message(message)

    assert gateway._queue.get_nowait().message_id == "m42"


def test_handle_message_drops_event_when_guild_not_allowed():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    message = _fake_message(guild_id="g2")

    gateway._handle_message(message)

    assert gateway._queue.empty()


def test_handle_message_ignores_dms():
    gateway = DiscordPyGateway(token="x", guild_allowlist=frozenset({"g1"}))
    dm = SimpleNamespace(guild=None)

    gateway._handle_message(dm)

    assert gateway._queue.empty()

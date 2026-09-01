"""Discord bot transport seam: the wire-shaped event/command protocol and its
concrete discord.py-backed implementation.

This is the transport seam only -- no bot behaviour or ticket logic lives
here. `DiscordPyGateway` implements the frozen `DiscordGateway` Protocol
against discord.py; bot behaviour and ticket logic are owned by later
workstreams that consume this contract.

**Security-critical.** Every user-identifying field on every event dataclass
below (`guild_id`, `channel_id`, `thread_id`, `message_id`, `author_id`,
`user_id`, `interaction_id`) is a Discord **snowflake ID string** -- a stable
numeric identifier, never a display name. There is deliberately **no
`username` field anywhere in this protocol**: kenny resolves identity only by
snowflake, so a mutable Discord display name can never structurally reach an
authorization decision. The single exception is `GuildMember.display_hint`,
which exists purely to render a picker in the dashboard -- see its docstring.

This module is the **only** place in the repo that ever does ``import
discord``, and it does so lazily inside `DiscordPyGateway.start()`, so a
server built without the optional `discord.py` dependency installed starts
and runs normally with the Discord surface simply unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

logger = logging.getLogger("kenny.discord_adapter")

# ---------------------------------------------------------------------------
# Inbound event dataclasses (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThreadRef:
    """A reference to a Discord thread, identified entirely by snowflakes."""

    guild_id: str
    channel_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class GuildMember:
    """A guild member, for rendering a member picker in the dashboard.

    ``display_hint`` (nickname or global display name) is the single
    exception to the snowflake-only identity rule in this module. It must
    never be used to resolve a user or feed into an authorization decision --
    it is a mutable, operator-facing label only. All identity resolution
    happens on ``user_id`` (a snowflake).
    """

    user_id: str
    display_hint: str


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """A message posted in a guild channel or thread."""

    guild_id: str
    channel_id: str
    thread_id: str | None
    message_id: str
    author_id: str
    author_is_bot: bool
    content: str
    mentions_bot: bool
    attachment_count: int


@dataclass(frozen=True, slots=True)
class SlashCommandEvent:
    """A slash command interaction."""

    guild_id: str
    channel_id: str
    thread_id: str | None
    user_id: str
    interaction_id: str
    command: str
    options: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComponentEvent:
    """A message-component interaction (button/select click)."""

    guild_id: str
    channel_id: str
    message_id: str
    user_id: str
    interaction_id: str
    custom_id: str


@dataclass(frozen=True, slots=True)
class ThreadStateEvent:
    """A thread archived/unarchived state change."""

    guild_id: str
    thread_id: str
    archived: bool


InboundEvent = MessageEvent | SlashCommandEvent | ComponentEvent | ThreadStateEvent


# ---------------------------------------------------------------------------
# Outbound command registration shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandOption:
    """One option of a slash command being registered."""

    name: str
    description: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A slash command to register with Discord via `register_commands`."""

    name: str
    description: str
    options: tuple[CommandOption, ...] = ()


# ---------------------------------------------------------------------------
# Message chunking (pure, testable without any gateway)
# ---------------------------------------------------------------------------

# Discord's hard per-message limit is 2000 characters; kenny targets a lower
# threshold so an off-by-a-few-characters miscount never risks the hard limit.
DISCORD_MESSAGE_HARD_LIMIT = 2000
CHUNK_TARGET_LIMIT = 1900

# How long register_commands waits for the client to finish logging in before
# giving up. start() returns as soon as the connect task is *scheduled*, not
# once discord.py has actually logged in -- application_id (and everything
# else on the client) is only populated once that finishes, which is what
# wait_until_ready() blocks on.
_READY_TIMEOUT_SECS = 30

# register_commands is called immediately after start() returns, which can
# race the connect task before it has run even once -- discord.py's
# wait_until_ready() raises RuntimeError outright in that exact window,
# rather than blocking (see its own docstring below). One retry, almost
# immediately, is enough in practice: the failure mode is "the other task
# has not had a single turn yet", not a real fault, and any scheduler tick
# clears it.
_REGISTER_ATTEMPTS = 3
_REGISTER_RETRY_DELAY_SECS = 1.0

# Cap on live Interaction objects held in DiscordPyGateway._interactions. Most
# entries are popped by a terminal respond_ephemeral, but any path that
# returns without ever answering (an unmapped user's click, an unrecognized
# custom_id) leaks its entry forever. Interactions are unusable ~15 minutes
# after creation anyway, so evicting the oldest once the dict grows past this
# many just bounds a long-lived bot's memory without changing behaviour for
# any interaction actually still in play.
_MAX_TRACKED_INTERACTIONS = 1000


def chunk_message(content: str, limit: int = CHUNK_TARGET_LIMIT) -> list[str]:
    """Split ``content`` into chunks of at most ``limit`` characters.

    Splits on line boundaries where possible (keeping line terminators, so
    ``"".join(chunk_message(content)) == content`` always holds); a single
    line longer than ``limit`` is hard-split mid-line. Every returned chunk
    has length <= ``limit`` <= `DISCORD_MESSAGE_HARD_LIMIT`.
    """

    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(line):
                chunks.append(line[start : start + limit])
                start += limit
            continue
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Gateway protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DiscordGateway(Protocol):
    """The transport boundary between kenny and Discord.

    Implementations: `DiscordPyGateway` (real, discord.py-backed) and
    `tests/support/fake_discord.FakeDiscordGateway` (in-memory, for tests).
    Bot behaviour, ticket logic, and gateway wiring are owned by later
    workstreams against this contract -- it does not change shape without a
    corresponding update here.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    @property
    def connected(self) -> bool: ...

    def events(self) -> AsyncIterator[InboundEvent]: ...

    async def open_thread(
        self, *, channel_id: str, name: str, private: bool, invite_user_ids: list[str]
    ) -> ThreadRef: ...

    async def archive_thread(self, *, thread_id: str, locked: bool) -> None: ...

    async def post_message(
        self, *, channel_id: str, content: str, reply_to: str | None = None
    ) -> str: ...

    async def post_approval_card(
        self, *, channel_id: str, approval_id: str, summary: str, detail_url: str
    ) -> str: ...

    async def post_host_picker(
        self,
        *,
        channel_id: str,
        request_id: str,
        hosts: list[str],
        prompt: str,
        reply_to: str | None = None,
    ) -> str: ...

    async def resolve_card(
        self, *, channel_id: str, message_id: str, outcome: str, decided_by: str
    ) -> None: ...

    async def respond_ephemeral(self, *, interaction_id: str, content: str) -> None: ...

    async def respond_ephemeral_picker(
        self, *, interaction_id: str, request_id: str, hosts: list[str], prompt: str
    ) -> None: ...

    async def defer_interaction(self, *, interaction_id: str) -> None: ...

    async def list_guild_members(self, *, guild_id: str) -> list[GuildMember]: ...

    # POSSIBLY DEAD: no caller in kenny_server invokes member_role_ids today
    # (discord_service.py never uses Discord roles for routing/visibility yet);
    # only exercised directly by tests against this protocol / its implementations.
    async def member_role_ids(self, *, guild_id: str, user_id: str) -> frozenset[str]:
        """Return the caller's Discord role IDs (snowflakes) in ``guild_id``.

        Discord roles are **advisory only** in kenny: they may drive routing
        and visibility (who gets pinged, who sees a channel), but must
        **never** enter an authorization decision. Authorization comes solely
        from kenny's own snowflake -> user mapping, not from Discord's role
        state (which the guild owner, not kenny, controls).
        """
        ...

    async def register_commands(
        self, *, guild_id: str, commands: list[CommandSpec]
    ) -> None: ...


class GatewayUnavailable(RuntimeError):
    """Raised by `DiscordPyGateway.start()` when discord.py is not installed."""


# ---------------------------------------------------------------------------
# Approval-card custom_id scheme (pure, testable without any gateway)
# ---------------------------------------------------------------------------

_APPROVAL_CUSTOM_ID_PREFIX = "kenny-approval"
_APPROVAL_ACTIONS = ("approve", "deny")

# Discord's hard limit on a component's custom_id.
_CUSTOM_ID_HARD_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ApprovalAction:
    """The decoded intent of an approval-card button click."""

    action: str  # "approve" | "deny"
    approval_id: str


def build_approval_custom_id(action: str, approval_id: str) -> str:
    """Build the `custom_id` for an approval-card button.

    Scheme: ``kenny-approval:<action>:<approval_id>``, where ``action`` is
    ``"approve"`` or ``"deny"``. The stable ``kenny-approval`` prefix lets a
    component-interaction handler recognise "this click belongs to an
    approval card" (and route accordingly) without guessing at every custom
    id it sees; pair with `parse_approval_custom_id` to decode.
    """

    if action not in _APPROVAL_ACTIONS:
        raise ValueError(f"unknown approval action: {action!r}")
    custom_id = f"{_APPROVAL_CUSTOM_ID_PREFIX}:{action}:{approval_id}"
    if len(custom_id) > _CUSTOM_ID_HARD_LIMIT:
        raise ValueError(f"custom_id exceeds Discord's 100-character limit: {custom_id!r}")
    return custom_id


def parse_approval_custom_id(custom_id: str) -> ApprovalAction | None:
    """Decode a `custom_id` built by `build_approval_custom_id`.

    Returns `None` for anything that doesn't match the expected
    ``kenny-approval:<action>:<approval_id>`` shape -- a malformed or
    unrelated custom_id is rejected, never mis-parsed into a bogus
    `ApprovalAction`.
    """

    parts = custom_id.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, action, approval_id = parts
    if prefix != _APPROVAL_CUSTOM_ID_PREFIX or action not in _APPROVAL_ACTIONS or not approval_id:
        return None
    return ApprovalAction(action=action, approval_id=approval_id)


# ---------------------------------------------------------------------------
# Host-picker custom_id scheme (pure, testable without any gateway)
# ---------------------------------------------------------------------------

_HOST_CUSTOM_ID_PREFIX = "kenny-host"


@dataclass(frozen=True, slots=True)
class HostChoice:
    """The decoded intent of a host-picker button click."""

    request_id: str
    agent_id: str


def build_host_custom_id(request_id: str, agent_id: str) -> str:
    """Build the `custom_id` for one button of a host picker.

    Scheme: ``kenny-host:<request_id>:<agent_id>``. The request id names the
    pending request the click answers; the agent id is carried literally rather
    than as an index into a list, so a click can be re-validated against the
    clicker's current host scope without depending on the order a list happened
    to have when the card was posted.

    A distinct prefix from `build_approval_custom_id` is what lets one
    component handler tell the two card kinds apart without guessing.
    """

    if not request_id or ":" in request_id:
        raise ValueError(f"request_id must be non-empty and colon-free: {request_id!r}")
    if not agent_id:
        raise ValueError("agent_id must not be empty")
    custom_id = f"{_HOST_CUSTOM_ID_PREFIX}:{request_id}:{agent_id}"
    if len(custom_id) > _CUSTOM_ID_HARD_LIMIT:
        raise ValueError(f"custom_id exceeds Discord's 100-character limit: {custom_id!r}")
    return custom_id


def parse_host_custom_id(custom_id: str) -> HostChoice | None:
    """Decode a `custom_id` built by `build_host_custom_id`, else `None`.

    The agent id is the *remainder* after the second separator, so an id
    containing a colon survives the round trip instead of being truncated into
    a different, possibly real, host name.
    """

    parts = custom_id.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, request_id, agent_id = parts
    if prefix != _HOST_CUSTOM_ID_PREFIX or not request_id or not agent_id:
        return None
    return HostChoice(request_id=request_id, agent_id=agent_id)


# ---------------------------------------------------------------------------
# discord.py object -> frozen event dataclass translators (pure functions)
#
# Each takes a "discord.py-shaped" object -- the real thing at runtime, a
# small namespace fake in tests -- and reads only the attributes documented
# below. None of them read or store anything display-name-shaped onto an
# event; identity crosses this seam by snowflake only (see module docstring).
# ---------------------------------------------------------------------------


def _thread_and_channel_id(channel: Any) -> tuple[str | None, str]:
    """Split a discord.py channel-shaped object into (thread_id, channel_id).

    Threads carry a non-None `parent_id` pointing at their parent channel;
    top-level channels don't have the attribute at all (hence `getattr`
    with a `None` default rather than direct attribute access).
    """

    parent_id = getattr(channel, "parent_id", None)
    if parent_id is not None:
        return str(channel.id), str(parent_id)
    return None, str(channel.id)


def _translate_message(message: Any, *, bot_user_id: str | None) -> MessageEvent:
    """Translate a discord.py `Message`-shaped object into a `MessageEvent`.

    Reads: `.id`, `.guild.id`, `.channel` (`.id`, `.parent_id` if a thread),
    `.author.id`, `.author.bot`, `.content`, `.attachments` (only `len()` is
    used), `.mentions` (a list of `.id`-bearing objects).
    """

    thread_id, channel_id = _thread_and_channel_id(message.channel)
    mentions = getattr(message, "mentions", [])
    mentions_bot = bot_user_id is not None and any(
        str(mentioned.id) == bot_user_id for mentioned in mentions
    )
    return MessageEvent(
        guild_id=str(message.guild.id),
        channel_id=channel_id,
        thread_id=thread_id,
        message_id=str(message.id),
        author_id=str(message.author.id),
        author_is_bot=bool(message.author.bot),
        content=message.content,
        mentions_bot=mentions_bot,
        attachment_count=len(message.attachments),
    )


def _translate_slash_command(interaction: Any) -> SlashCommandEvent:
    """Translate a discord.py `Interaction`-shaped object (application
    command type) into a `SlashCommandEvent`.

    Reads: `.id`, `.guild_id`, `.channel_id`, `.channel` (`.id`, `.parent_id`
    if a thread; may be absent/None, in which case `channel_id` falls back
    to `.channel_id` directly and `thread_id` is `None`), `.user.id`,
    `.data` -- the raw `ApplicationCommandInteractionData` payload Discord
    always sends, read directly rather than via `.command`/`.namespace`.
    Both of those are properties that require a `discord.app_commands.
    CommandTree` bound to the client's connection state -- this gateway
    deliberately never constructs one (see `register_commands`, which
    registers commands via the raw REST endpoint instead), so `.command`
    is always `None` and `.namespace` is always an empty `Namespace` on
    every real interaction here; reading `.data` sidesteps both.
    `.data["name"]` is the command name; `.data.get("options", [])` is a
    flat list of `{"name": ..., "value": ...}` dicts -- flat because every
    command this bot registers is a top-level command with only STRING
    options (see `_command_spec_to_payload`), never a subcommand or
    subcommand-group (Discord option type 1/2), so no nested walk is
    needed.
    """

    channel = getattr(interaction, "channel", None)
    if channel is not None:
        thread_id, channel_id = _thread_and_channel_id(channel)
    else:
        thread_id, channel_id = None, str(interaction.channel_id)
    data = interaction.data or {}
    options = {
        str(opt["name"]): str(opt["value"])
        for opt in data.get("options", [])
        if "value" in opt
    }
    return SlashCommandEvent(
        guild_id=str(interaction.guild_id),
        channel_id=channel_id,
        thread_id=thread_id,
        user_id=str(interaction.user.id),
        interaction_id=str(interaction.id),
        command=str(data["name"]),
        options=options,
    )


def _translate_component(interaction: Any) -> ComponentEvent:
    """Translate a discord.py `Interaction`-shaped object (message-component
    type) into a `ComponentEvent`.

    Reads: `.id`, `.guild_id`, `.channel_id`, `.user.id`, `.message.id`,
    `.data` (a mapping with a `"custom_id"` key).
    """

    data = interaction.data
    custom_id = data.get("custom_id", "") if isinstance(data, dict) else ""
    return ComponentEvent(
        guild_id=str(interaction.guild_id),
        channel_id=str(interaction.channel_id),
        message_id=str(interaction.message.id),
        user_id=str(interaction.user.id),
        interaction_id=str(interaction.id),
        custom_id=custom_id,
    )


def _translate_thread_state(thread: Any) -> ThreadStateEvent:
    """Translate a discord.py `Thread`-shaped object into a
    `ThreadStateEvent`. Reads: `.guild.id`, `.id`, `.archived`.
    """

    return ThreadStateEvent(
        guild_id=str(thread.guild.id),
        thread_id=str(thread.id),
        archived=bool(thread.archived),
    )


def _translate_guild_member(member: Any) -> GuildMember:
    """Translate a discord.py `Member`-shaped object into a `GuildMember`.

    Reads: `.id`, `.display_name` (discord.py's own nickname-or-global-name
    fallback -- display-only, see `GuildMember.display_hint`).
    """

    return GuildMember(user_id=str(member.id), display_hint=str(member.display_name))


def _command_spec_to_payload(spec: CommandSpec) -> dict[str, Any]:
    """Build the raw Discord application-command JSON body for `spec`.

    Every `CommandOption` becomes a STRING option (Discord application
    command option type ``3``) -- this bot's commands are simple by design;
    extend `CommandOption` with a type field first if that stops being true.
    """

    return {
        "name": spec.name,
        "description": spec.description,
        "type": 1,  # chat input (slash command)
        "options": [
            {
                "name": opt.name,
                "description": opt.description,
                "type": 3,  # STRING
                "required": opt.required,
            }
            for opt in spec.options
        ],
    }


@dataclass(frozen=True, slots=True)
class GatewayIntents:
    """Which discord.py gateway intents to request. Maps 1:1 onto
    ``discord.Intents`` flags; kept here so callers don't need discord.py
    importable just to construct a `DiscordPyGateway`.
    """

    guilds: bool = True
    guild_messages: bool = True
    message_content: bool = True
    guild_members: bool = True


class DiscordPyGateway:
    """`DiscordGateway` backed by discord.py.

    Connects a `discord.Client` in the background (see `start`); inbound
    messages, interactions (slash commands and message components), and
    thread archive/unarchive changes are translated into the frozen event
    dataclasses and pushed through `_intake` -- there is exactly one path
    an inbound event can take to reach `events()`, and the guild allowlist
    sits on that single path so nothing can bypass it.

    An **empty allowlist denies every guild** -- there is no allow-all mode.

    Outbound calls (`post_message`, `open_thread`, ...) let discord.py's own
    HTTP client handle rate-limit buckets and retries; kenny does not
    hand-roll backoff. Event-translation callbacks catch and log rather than
    propagate, so a bad or unexpected payload from Discord logs a warning
    instead of taking down the background connection task.
    """

    def __init__(
        self,
        *,
        token: str,
        guild_allowlist: frozenset[str],
        intents: GatewayIntents | None = None,
    ) -> None:
        self._token = token
        self._guild_allowlist = frozenset(guild_allowlist)
        self._intents = intents or GatewayIntents()
        self._connected = False

        self._discord: Any | None = None
        self._client: Any | None = None
        self._connect_task: asyncio.Task[None] | None = None
        self._bot_user_id: str | None = None
        self._warned_empty_content = False
        # Live Interaction objects, keyed by interaction_id, so
        # respond_ephemeral/defer_interaction can act on them within
        # Discord's response window. Entries are removed once a terminal
        # response (respond_ephemeral) has been sent for them.
        self._interactions: dict[str, Any] = {}
        self._queue: asyncio.Queue[InboundEvent | None] = asyncio.Queue()

    async def start(self) -> None:
        """Lazily import discord.py, connect in the background, and wire up
        the event translators that feed `events()`.

        Raises `GatewayUnavailable` with an actionable message if discord.py
        is not installed, so a server built without the optional dependency
        starts and runs normally with the Discord surface disabled.
        """

        try:
            import discord  # noqa: F401  -- the only import site in the repo, by design
        except ImportError as exc:
            raise GatewayUnavailable(
                "discord.py is not installed. Install the optional dependency "
                "(e.g. `pip install discord.py`) to enable the Discord bot surface."
            ) from exc

        self._discord = discord

        intents = discord.Intents.none()
        intents.guilds = self._intents.guilds
        intents.guild_messages = self._intents.guild_messages
        intents.message_content = self._intents.message_content
        intents.members = self._intents.guild_members

        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready() -> None:
            self._connected = True
            if client.user is not None:
                self._bot_user_id = str(client.user.id)

        @client.event
        async def on_resumed() -> None:
            self._connected = True

        @client.event
        async def on_disconnect() -> None:
            self._connected = False

        @client.event
        async def on_message(message: Any) -> None:
            self._handle_message(message)

        @client.event
        async def on_interaction(interaction: Any) -> None:
            self._handle_interaction(interaction)

        @client.event
        async def on_thread_update(before: Any, after: Any) -> None:
            self._handle_thread_update(after)

        self._connect_task = asyncio.create_task(self._run_client(client))

    async def _run_client(self, client: Any) -> None:
        """Run the discord.py connection loop until it exits or is cancelled.

        A failure here (bad token, network outage, ...) is logged and ends
        this task without propagating -- it must never take down the server
        process that owns this gateway.
        """

        try:
            await client.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Discord gateway connection loop exited unexpectedly")
        finally:
            self._connected = False

    def _handle_message(self, message: Any) -> None:
        if getattr(message, "guild", None) is None:
            return  # kenny operates on guilds only; DMs are out of scope
        try:
            event = _translate_message(message, bot_user_id=self._bot_user_id)
        except Exception:
            logger.exception("discord_adapter: failed to translate an inbound message")
            return
        if event.mentions_bot and event.content == "" and not self._warned_empty_content:
            self._warned_empty_content = True
            logger.warning(
                "Received a mention with empty message content. This is the signature "
                "of the privileged 'Message Content' intent not being enabled for this "
                "bot in the Discord Developer Portal (Bot > Privileged Gateway Intents) "
                "-- without it, kenny cannot read what it was asked."
            )
        self._enqueue(event)

    def _handle_interaction(self, interaction: Any) -> None:
        discord = self._discord
        self._interactions[str(interaction.id)] = interaction
        while len(self._interactions) > _MAX_TRACKED_INTERACTIONS:
            # dict preserves insertion order -- the first key is the oldest,
            # and by the time there are this many untouched entries it is
            # long past its ~15 minute token lifetime anyway.
            oldest = next(iter(self._interactions))
            del self._interactions[oldest]
        try:
            if interaction.type == discord.InteractionType.application_command:
                event: InboundEvent = _translate_slash_command(interaction)
            elif interaction.type == discord.InteractionType.component:
                event = _translate_component(interaction)
            else:
                return
        except Exception:
            logger.exception("discord_adapter: failed to translate an inbound interaction")
            return
        self._enqueue(event)

    def _handle_thread_update(self, thread: Any) -> None:
        try:
            event = _translate_thread_state(thread)
        except Exception:
            logger.exception("discord_adapter: failed to translate a thread-state event")
            return
        self._enqueue(event)

    def _enqueue(self, event: InboundEvent) -> None:
        filtered = self._intake(event)
        if filtered is not None:
            self._queue.put_nowait(filtered)

    async def close(self) -> None:
        self._connected = False
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        if self._connect_task is not None:
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._queue.put_nowait(None)

    @property
    def connected(self) -> bool:
        return self._connected

    def _guild_allowed(self, guild_id: str) -> bool:
        """True iff ``guild_id`` is present in the allowlist.

        An empty allowlist denies every guild -- never allow-all.
        """

        return guild_id in self._guild_allowlist

    def _intake(self, event: InboundEvent) -> InboundEvent | None:
        """Drop ``event`` if its guild is not on the allowlist, else pass it through.

        Called by every discord.py event handler before an event reaches
        `events()`; this is the single choke point the allowlist is
        enforced at, so there is no path that bypasses it.
        """

        if not self._guild_allowed(event.guild_id):
            return None
        return event

    async def events(self) -> AsyncIterator[InboundEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def _resolve_channel(self, channel_id: str) -> Any:
        assert self._client is not None
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(channel_id))
        return channel

    async def _resolve_guild(self, guild_id: str) -> Any | None:
        assert self._client is not None
        guild = self._client.get_guild(int(guild_id))
        if guild is not None:
            return guild
        try:
            return await self._client.fetch_guild(int(guild_id))
        except Exception as exc:
            logger.warning("guild %s not found or unreachable: %s", guild_id, exc)
            return None

    async def open_thread(
        self, *, channel_id: str, name: str, private: bool, invite_user_ids: list[str]
    ) -> ThreadRef:
        discord = self._discord
        channel = await self._resolve_channel(channel_id)

        if private:
            try:
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.private_thread
                )
            except discord.HTTPException as exc:
                logger.warning(
                    "open_thread: private threads unavailable in channel %s (%s); "
                    "falling back to a public thread",
                    channel_id,
                    exc,
                )
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.public_thread
                )
        else:
            thread = await channel.create_thread(name=name, type=discord.ChannelType.public_thread)

        for user_id in invite_user_ids:
            try:
                await thread.add_user(discord.Object(id=int(user_id)))
            except Exception as exc:
                logger.warning(
                    "open_thread: failed to invite user %s to thread %s: %s",
                    user_id,
                    thread.id,
                    exc,
                )

        return ThreadRef(
            guild_id=str(thread.guild.id), channel_id=str(channel_id), thread_id=str(thread.id)
        )

    async def archive_thread(self, *, thread_id: str, locked: bool) -> None:
        thread = await self._resolve_channel(thread_id)
        await thread.edit(archived=True, locked=locked)

    async def post_message(
        self, *, channel_id: str, content: str, reply_to: str | None = None
    ) -> str:
        """Chunk ``content`` with `chunk_message` and post each chunk, in
        order, to ``channel_id``, returning the id of the last message
        posted. ``reply_to``, if given, is only attached to the first chunk.
        """

        discord = self._discord
        channel = await self._resolve_channel(channel_id)
        reference = None
        if reply_to is not None:
            reference = discord.MessageReference(
                message_id=int(reply_to), channel_id=int(channel_id)
            )

        last_message_id = ""
        for chunk in chunk_message(content):
            sent = await channel.send(chunk, reference=reference)
            last_message_id = str(sent.id)
            reference = None  # only the first chunk carries the reply reference
        return last_message_id

    async def post_approval_card(
        self, *, channel_id: str, approval_id: str, summary: str, detail_url: str
    ) -> str:
        discord = self._discord
        embed = discord.Embed(title="Approval requested", description=summary)
        embed.add_field(name="Details", value=detail_url, inline=False)

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=build_approval_custom_id("approve", approval_id),
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=build_approval_custom_id("deny", approval_id),
            )
        )

        channel = await self._resolve_channel(channel_id)
        message = await channel.send(embed=embed, view=view)
        return str(message.id)

    def _host_picker_view(self, *, request_id: str, hosts: list[str]) -> Any:
        """Build the one-button-per-host view shared by the public and the
        ephemeral picker. `custom_id`s are read by `on_interaction` off the
        raw payload (see `parse_host_custom_id`), never through the `View`'s
        own callback machinery, so the view's lifetime does not matter --
        both kinds of picker stay answerable across a restart.
        """

        discord = self._discord
        view = discord.ui.View(timeout=None)
        for agent_id in hosts:
            view.add_item(
                discord.ui.Button(
                    label=agent_id[:80],  # Discord's label limit
                    style=discord.ButtonStyle.secondary,
                    custom_id=build_host_custom_id(request_id, agent_id),
                )
            )
        return view

    async def post_host_picker(
        self,
        *,
        channel_id: str,
        request_id: str,
        hosts: list[str],
        prompt: str,
        reply_to: str | None = None,
    ) -> str:
        """Post one button per host so the caller can pick a target by clicking.

        Discord allows 25 buttons on a message (5 rows of 5); callers are
        expected to fall back to plain text above that rather than have this
        raise from inside a reply path.
        """

        discord = self._discord
        view = self._host_picker_view(request_id=request_id, hosts=hosts)
        reference = None
        if reply_to is not None:
            reference = discord.MessageReference(
                message_id=int(reply_to), channel_id=int(channel_id)
            )
        channel = await self._resolve_channel(channel_id)
        message = await channel.send(prompt, view=view, reference=reference)
        return str(message.id)

    async def resolve_card(
        self, *, channel_id: str, message_id: str, outcome: str, decided_by: str
    ) -> None:
        """Edit a previously-posted approval card to remove its buttons and
        show the outcome, so a decided card cannot be clicked again.
        """

        discord = self._discord
        channel = await self._resolve_channel(channel_id)
        message = await channel.fetch_message(int(message_id))
        embed = message.embeds[0] if message.embeds else discord.Embed(title="Approval")
        embed.add_field(name="Outcome", value=outcome, inline=True)
        embed.add_field(name="Decided by", value=decided_by, inline=True)
        await message.edit(embed=embed, view=None)

    async def respond_ephemeral(self, *, interaction_id: str, content: str) -> None:
        """Send a reply to an interaction visible only to the user who
        triggered it.

        Discord requires the first acknowledgement of an interaction within
        ~3 seconds of receiving it, and the interaction token backing any
        response (deferred or not) expires ~15 minutes after creation --
        call this promptly, or call `defer_interaction` first and this
        later.
        """

        interaction = self._interactions.pop(interaction_id, None)
        if interaction is None:
            logger.warning(
                "respond_ephemeral: unknown or expired interaction_id=%s", interaction_id
            )
            return
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except Exception as exc:
            logger.warning("respond_ephemeral(%s): failed to send: %s", interaction_id, exc)

    async def respond_ephemeral_picker(
        self, *, interaction_id: str, request_id: str, hosts: list[str], prompt: str
    ) -> None:
        """Like `respond_ephemeral`, but with the host-picker buttons attached.

        This is what keeps a slash command's host picker in the same place
        the command was typed -- an interaction response (deferred or not)
        always renders there, whereas `post_host_picker` sends an ordinary
        channel message that has no such guarantee inside a thread.
        """

        interaction = self._interactions.pop(interaction_id, None)
        if interaction is None:
            logger.warning(
                "respond_ephemeral_picker: unknown or expired interaction_id=%s",
                interaction_id,
            )
            return
        view = self._host_picker_view(request_id=request_id, hosts=hosts)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(prompt, view=view, ephemeral=True)
            else:
                await interaction.response.send_message(prompt, view=view, ephemeral=True)
        except Exception as exc:
            logger.warning(
                "respond_ephemeral_picker(%s): failed to send: %s", interaction_id, exc
            )

    async def defer_interaction(self, *, interaction_id: str) -> None:
        """Acknowledge an interaction within Discord's ~3 second window
        without a visible reply yet.

        Use this when handling the interaction will take longer than 3
        seconds: it buys up to the interaction token's ~15 minute lifetime
        for a later `respond_ephemeral`, while the actual step-by-step work
        (e.g. a diagnosis) is posted into the thread via `post_message`
        rather than through the interaction itself.
        """

        interaction = self._interactions.get(interaction_id)
        if interaction is None:
            logger.warning(
                "defer_interaction: unknown or expired interaction_id=%s", interaction_id
            )
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception as exc:
            logger.warning("defer_interaction(%s): failed to defer: %s", interaction_id, exc)

    async def list_guild_members(self, *, guild_id: str) -> list[GuildMember]:
        if not self._intents.guild_members:
            logger.warning(
                "list_guild_members(%s): the privileged 'Server Members Intent' is not "
                "enabled on this gateway; returning an empty list. Enable it under "
                "Bot > Privileged Gateway Intents in the Discord Developer Portal.",
                guild_id,
            )
            return []

        guild = await self._resolve_guild(guild_id)
        if guild is None:
            return []
        try:
            return [_translate_guild_member(m) async for m in guild.fetch_members(limit=None)]
        except Exception as exc:
            logger.warning(
                "list_guild_members(%s): failed to fetch members (the privileged "
                "'Server Members Intent' may not be enabled in the Developer Portal "
                "even though it is requested here): %s",
                guild_id,
                exc,
            )
            return []

    async def member_role_ids(self, *, guild_id: str, user_id: str) -> frozenset[str]:
        """Return the caller's Discord role IDs (snowflakes) in ``guild_id``.

        Discord roles are **advisory only** in kenny: they may drive routing
        and visibility (who gets pinged, who sees a channel), but must
        **never** enter an authorization decision. Authorization comes solely
        from kenny's own snowflake -> user mapping, not from Discord's role
        state (which the guild owner, not kenny, controls).
        """

        guild = await self._resolve_guild(guild_id)
        if guild is None:
            return frozenset()
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except Exception as exc:
                logger.warning(
                    "member_role_ids(%s, %s): member not found: %s", guild_id, user_id, exc
                )
                return frozenset()
        return frozenset(str(role.id) for role in member.roles if not role.is_default())

    async def register_commands(self, *, guild_id: str, commands: list[CommandSpec]) -> None:
        """Register ``commands`` as guild-scoped application commands.

        Guild-scoped registration propagates immediately (global commands
        can take up to an hour to appear everywhere), which is what an
        operator reconfiguring a bot they are actively looking at expects.
        Registration only declares the commands to Discord; the resulting
        interactions arrive through the same `on_interaction` path as
        everything else and are translated by `_translate_slash_command`.

        Waits for the client to finish logging in first: a caller driving
        this right after ``start()`` returns would otherwise race the
        connect task, which has not had a chance to run yet, let alone
        finish -- ``application_id`` is unset until it does. That wait
        races the same connect task one level deeper still: discord.py's
        ``wait_until_ready()`` raises ``RuntimeError`` outright rather
        than blocking if called before the task has even reached its own
        internal setup -- observed live, not hypothetical: a caller
        driving this immediately after ``start()`` (as ``_discord_loop``
        does) hits it on the very first attempt more often than not, so a
        single try is not enough. Retried up to ``_REGISTER_ATTEMPTS``
        times with a short delay; every attempt failing is still caught,
        never raised, so the ticket surface below is never put at risk
        over slash commands.
        """

        if self._client is None:
            logger.warning("register_commands: gateway not started; skipping")
            return
        payload = [_command_spec_to_payload(spec) for spec in commands]
        last_exc: Exception | None = None
        for attempt in range(1, _REGISTER_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    self._client.wait_until_ready(), timeout=_READY_TIMEOUT_SECS
                )
                if self._client.application_id is None:
                    raise RuntimeError("application_id still unset after wait_until_ready")
                await self._client.http.bulk_upsert_guild_commands(
                    self._client.application_id, int(guild_id), payload
                )
                return
            except Exception as exc:  # noqa: BLE001 - retried, then swallowed below
                last_exc = exc
                if attempt < _REGISTER_ATTEMPTS:
                    await asyncio.sleep(_REGISTER_RETRY_DELAY_SECS)
        logger.warning(
            "register_commands(guild=%s): failed to register after %d attempts: %s",
            guild_id,
            _REGISTER_ATTEMPTS,
            last_exc,
        )

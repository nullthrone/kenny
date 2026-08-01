"""Discord bot transport seam: the wire-shaped event/command protocol and its
concrete discord.py-backed implementation.

This is the transport seam only -- no bot behaviour, ticket logic, or gateway
plumbing lives here. A later workstream implements `DiscordPyGateway` against
the `DiscordGateway` Protocol frozen in this module.

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

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

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

    async def resolve_card(
        self, *, channel_id: str, message_id: str, outcome: str, decided_by: str
    ) -> None: ...

    async def respond_ephemeral(self, *, interaction_id: str, content: str) -> None: ...

    async def defer_interaction(self, *, interaction_id: str) -> None: ...

    async def list_guild_members(self, *, guild_id: str) -> list[GuildMember]: ...

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

    This class implements the dependency guard (`start`/`close`/`connected`)
    fully. Everything else -- thread/message/interaction/command operations,
    and the actual discord.py event dispatch loop feeding `events()` -- is a
    **stub** here; a later workstream builds that out. `DiscordGateway` above
    is the frozen contract that workstream implements against; this class's
    method signatures already match it so that follow-up is a pure fill-in.

    The guild allowlist is enforced by `_intake`, the (not yet wired-up)
    event-intake helper: an event from a guild not on the allowlist is
    dropped. An **empty allowlist denies every guild** -- there is no
    allow-all mode.
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

    async def start(self) -> None:
        """Lazily import discord.py and mark the gateway session started.

        Raises `GatewayUnavailable` with an actionable message if discord.py
        is not installed, so a server built without the optional dependency
        starts and runs normally with the Discord surface disabled. Actual
        Discord login and the event dispatch loop are built out in a later
        workstream against `DiscordGateway`.
        """

        try:
            import discord  # noqa: F401  -- the only import site in the repo, by design
        except ImportError as exc:
            raise GatewayUnavailable(
                "discord.py is not installed. Install the optional dependency "
                "(e.g. `pip install discord.py`) to enable the Discord bot surface."
            ) from exc
        self._connected = True

    async def close(self) -> None:
        self._connected = False

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

        Called by the (future) discord.py event handlers before an event
        reaches `events()`.
        """

        if not self._guild_allowed(event.guild_id):
            return None
        return event

    def events(self) -> AsyncIterator[InboundEvent]:
        raise NotImplementedError("DiscordPyGateway.events: lands in a follow-up workstream")

    async def open_thread(
        self, *, channel_id: str, name: str, private: bool, invite_user_ids: list[str]
    ) -> ThreadRef:
        raise NotImplementedError(
            "DiscordPyGateway.open_thread: lands in a follow-up workstream"
        )

    async def archive_thread(self, *, thread_id: str, locked: bool) -> None:
        raise NotImplementedError(
            "DiscordPyGateway.archive_thread: lands in a follow-up workstream"
        )

    async def post_message(
        self, *, channel_id: str, content: str, reply_to: str | None = None
    ) -> str:
        """Chunk ``content`` with `chunk_message` and post each chunk, in
        order, to ``channel_id``, returning the id of the last message posted.

        Not implemented yet; see `chunk_message` for the (already frozen and
        tested) chunking behaviour this will use.
        """

        raise NotImplementedError(
            "DiscordPyGateway.post_message: lands in a follow-up workstream"
        )

    async def post_approval_card(
        self, *, channel_id: str, approval_id: str, summary: str, detail_url: str
    ) -> str:
        raise NotImplementedError(
            "DiscordPyGateway.post_approval_card: lands in a follow-up workstream"
        )

    async def resolve_card(
        self, *, channel_id: str, message_id: str, outcome: str, decided_by: str
    ) -> None:
        raise NotImplementedError(
            "DiscordPyGateway.resolve_card: lands in a follow-up workstream"
        )

    async def respond_ephemeral(self, *, interaction_id: str, content: str) -> None:
        raise NotImplementedError(
            "DiscordPyGateway.respond_ephemeral: lands in a follow-up workstream"
        )

    async def defer_interaction(self, *, interaction_id: str) -> None:
        raise NotImplementedError(
            "DiscordPyGateway.defer_interaction: lands in a follow-up workstream"
        )

    async def list_guild_members(self, *, guild_id: str) -> list[GuildMember]:
        raise NotImplementedError(
            "DiscordPyGateway.list_guild_members: lands in a follow-up workstream"
        )

    async def member_role_ids(self, *, guild_id: str, user_id: str) -> frozenset[str]:
        raise NotImplementedError(
            "DiscordPyGateway.member_role_ids: lands in a follow-up workstream"
        )

    async def register_commands(self, *, guild_id: str, commands: list[CommandSpec]) -> None:
        raise NotImplementedError(
            "DiscordPyGateway.register_commands: lands in a follow-up workstream"
        )

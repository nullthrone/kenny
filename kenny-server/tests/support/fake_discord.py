"""In-memory `DiscordGateway` for tests -- a real protocol boundary, not a
monkeypatched function. Mirrors the posture of the mock agent in
``tests/test_server_e2e.py``: tests feed inbound events and assert on
recorded outbound calls, never on discord.py internals (there are none here).

No sleeping, no real time, no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from kenny_server.discord_adapter import (
    CommandSpec,
    GuildMember,
    InboundEvent,
    ThreadRef,
    build_approval_custom_id,
    build_host_custom_id,
)


@dataclass
class FakeDiscordGateway:
    """Complete in-memory implementation of `DiscordGateway` for tests.

    Configure ``members`` (per guild_id) and ``role_ids`` (per (guild_id,
    user_id)) up front to drive `list_guild_members` / `member_role_ids`.
    Feed inbound events with `feed`; consume them via `events()`. Outbound
    calls are recorded on the list/dict attributes below for assertions.
    """

    members: dict[str, list[GuildMember]] = field(default_factory=dict)
    role_ids: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)

    posted: list[tuple[str, str]] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    pickers: list[dict] = field(default_factory=list)
    ephemerals: list[tuple[str, str]] = field(default_factory=list)
    threads: list[ThreadRef] = field(default_factory=list)
    invited: list[list[str]] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    resolved: list[dict] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    registered_commands: list[tuple[str, list[CommandSpec]]] = field(default_factory=list)

    _connected: bool = field(default=False, init=False, repr=False)
    _queue: asyncio.Queue[InboundEvent | None] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _closed: bool = field(default=False, init=False, repr=False)
    _next_thread_id: int = field(default=1, init=False, repr=False)

    async def start(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        self._closed = True
        await self._queue.put(None)

    @property
    def connected(self) -> bool:
        return self._connected

    def feed(self, event: InboundEvent) -> None:
        """Push an inbound event; consumed by `events()`."""

        self._queue.put_nowait(event)

    async def events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def open_thread(
        self, *, channel_id: str, name: str, private: bool, invite_user_ids: list[str]
    ) -> ThreadRef:
        thread_id = f"fake-thread-{self._next_thread_id}"
        self._next_thread_id += 1
        ref = ThreadRef(guild_id="fake-guild", channel_id=channel_id, thread_id=thread_id)
        self.threads.append(ref)
        self.invited.append(list(invite_user_ids))
        return ref

    async def archive_thread(self, *, thread_id: str, locked: bool) -> None:
        self.archived.append(thread_id)

    async def post_message(
        self, *, channel_id: str, content: str, reply_to: str | None = None
    ) -> str:
        self.posted.append((channel_id, content))
        return f"fake-message-{len(self.posted)}"

    async def post_approval_card(
        self, *, channel_id: str, approval_id: str, summary: str, detail_url: str
    ) -> str:
        self.cards.append(
            {
                "channel_id": channel_id,
                "approval_id": approval_id,
                "summary": summary,
                "detail_url": detail_url,
                # The custom_ids the buttons actually carry, built the same way
                # `DiscordPyGateway` builds them. Recording them is what makes a
                # test able to click the card the *gateway* produced rather than
                # one the test invented -- the two used to disagree, and every
                # click in production was silently dropped because of it.
                "custom_ids": {
                    action: build_approval_custom_id(action, approval_id)
                    for action in ("approve", "deny")
                },
            }
        )
        return f"fake-card-{len(self.cards)}"

    async def post_host_picker(
        self,
        *,
        channel_id: str,
        request_id: str,
        hosts: list[str],
        prompt: str,
        reply_to: str | None = None,
    ) -> str:
        self.pickers.append(
            {
                "channel_id": channel_id,
                "request_id": request_id,
                "hosts": list(hosts),
                "prompt": prompt,
                "reply_to": reply_to,
                # As for approval cards: the custom_ids the buttons really
                # carry, so a test clicks the gateway's own output.
                "custom_ids": {h: build_host_custom_id(request_id, h) for h in hosts},
            }
        )
        return f"fake-picker-{len(self.pickers)}"

    async def resolve_card(
        self, *, channel_id: str, message_id: str, outcome: str, decided_by: str
    ) -> None:
        self.resolved.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "outcome": outcome,
                "decided_by": decided_by,
            }
        )

    async def respond_ephemeral(self, *, interaction_id: str, content: str) -> None:
        self.ephemerals.append((interaction_id, content))

    async def defer_interaction(self, *, interaction_id: str) -> None:
        self.deferred.append(interaction_id)

    async def list_guild_members(self, *, guild_id: str) -> list[GuildMember]:
        return list(self.members.get(guild_id, []))

    async def member_role_ids(self, *, guild_id: str, user_id: str) -> frozenset[str]:
        return self.role_ids.get((guild_id, user_id), frozenset())

    async def register_commands(self, *, guild_id: str, commands: list[CommandSpec]) -> None:
        self.registered_commands.append((guild_id, list(commands)))

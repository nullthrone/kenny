"""The Discord surface: inbound events -> principal -> ticket assistant -> reply.

This module is where an outside chat platform is allowed to drive kenny's
capability tools, so it is written as a series of narrowing steps that a message
must survive. In order:

1. **Guild allowlist.** An event from a guild that is not listed is dropped
   before anything else happens. An empty allowlist denies every guild; there is
   no allow-all mode.
2. **Principal minting.** The author's Discord snowflake is resolved through
   :class:`~kenny_server.discord_identity.DiscordIdentityStore` and then through
   :class:`~kenny_server.userstore.UserStore` into a real
   :class:`~kenny_server.auth.Principal`. No other input participates: not a
   display name, not a mention, not a claim made in the message text, and not
   the author's Discord roles. An unmapped, disabled or unknown snowflake is
   **completely inert** — no ticket, no reply, and no model call.
3. **Frozen target.** The ticket's ``agent_id`` is chosen once, from the
   requester's own host scope, and nothing afterwards can move it.
   ``select_agent`` is absent from the tool schemas and an ``agent_id`` argument
   arriving from the model is discarded (never adopted) and logged.
4. **Capability profile.** The profile narrows the schemas the model is offered
   *and* is re-checked at dispatch.
5. **The gates.** Consent for privacy-touching tools, operator approval for
   ``normal_change`` — see :meth:`~kenny_server.ticket_assistant.TicketPolicy.gate`
   for the exact order and why consent comes first.
6. **Output redaction.** A result from a tool in ``REDACTED_OUTPUT`` never goes
   out over Discord; kenny summarises and links to the ticket in the
   authenticated dashboard.

The tool loop itself — session shape, gating policy, turn-driving, redaction —
lives in :mod:`kenny_server.ticket_assistant`. This module is one *surface* over
that shared assistant: it owns Discord's own concerns (gateway, threads,
identity mapping, slash commands) and implements
:class:`~kenny_server.ticket_assistant.TicketSurface` so the assistant can
deliver a reply, announce a gate, or report a lifecycle move back into a
thread. Transport-agnostic by construction otherwise: it talks to a
:class:`~kenny_server.discord_adapter.DiscordGateway` and never imports
``discord``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import urls
from .auth import Principal
from .discord_adapter import (
    CommandOption,
    CommandSpec,
    ComponentEvent,
    DiscordGateway,
    HostChoice,
    InboundEvent,
    MessageEvent,
    SlashCommandEvent,
    ThreadStateEvent,
    build_host_custom_id,
    chunk_message,
    parse_approval_custom_id,
    parse_host_custom_id,
)
from .discord_identity import DiscordIdentityStore
from .ticket_assistant import (
    EXCLUDED_TOOLS,
    FLEET_WIDE_TOOLS,
    TicketAssistant,
    TicketPolicy,  # noqa: F401 - re-exported: tests construct it directly
    TicketSession,
    _principal_from_row,
    _RateLimiter,
    _REDACTION_MARKER,  # noqa: F401 - re-exported for tests
    _scrub,
    _strip_spans,  # noqa: F401 - re-exported for tests
    _SYSTEM_PROMPT,  # noqa: F401 - re-exported for tests
    allowed_tools_for,
    envelope,
    redacted_payloads,
)
from .ticketstore import Ticket, TicketApproval, TicketChannel
from .tickets import TicketError, TicketService, redact_args
from .toolloop import ToolExecutor
from .userstore import UserStore

__all__ = [
    "DiscordService",
    "EXCLUDED_TOOLS",
    "FLEET_WIDE_TOOLS",
    "TicketPolicy",
    "TicketSession",
    "allowed_tools_for",
    "envelope",
]

logger = logging.getLogger("kenny.discord")

_MAX_TITLE_CHARS = 80


def _title_from(content: str) -> str:
    flat = " ".join((content or "").split())
    if not flat:
        return "Support request"
    return flat[:_MAX_TITLE_CHARS]


#: Discord's ceiling on message components: five action rows of five buttons.
_MAX_PICKER_BUTTONS = 25


@dataclass(frozen=True)
class HostPrompt:
    """A "which PC is this about" prompt, built but not yet sent.

    ``request_id`` is the parked row's id, and ``hosts`` are the candidates
    to render as buttons -- or ``request_id is None`` and ``prompt`` is a
    plain-text fallback when the fleet is too large to fit on one message's
    buttons (see `_picker_fits`). The caller decides *where* and *how*
    (public message with buttons vs. an ephemeral interaction reply).
    """

    prompt: str
    request_id: str | None
    hosts: list[str]


def _picker_fits(hosts: Sequence[str]) -> bool:
    """Whether every host can be offered as a button on one message.

    Two Discord ceilings, checked before anything is written: 25 components per
    message, and 100 characters per ``custom_id``. Asked up front so a fleet
    that does not fit degrades to the slash command instead of raising from
    inside a reply path — an unanswerable request is worse than a clumsy one.
    """

    if not 0 < len(hosts) <= _MAX_PICKER_BUTTONS:
        return False
    try:
        # A request id is always a 32-character uuid4 hex, so the budget can be
        # measured against a stand-in without minting a real one.
        for host in hosts:
            build_host_custom_id("0" * 32, host)
    except ValueError:
        return False
    return True


# The commands `handle_slash` below dispatches on. Registration (telling Discord
# these exist, so they show up in the `/` picker) is a separate step the caller
# drives explicitly via `DiscordGateway.register_commands` — this constant is
# the single place both sides read from, so the two cannot drift.
SLASH_COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="link",
        description="Link your Discord account to a kenny account",
        options=(
            CommandOption(
                name="name", description="A display hint for the operator", required=False
            ),
        ),
    ),
    CommandSpec(name="whoami", description="Show what kenny knows about you"),
    CommandSpec(name="status", description="List your open tickets"),
    CommandSpec(
        name="help-me",
        description="Open a support ticket",
        options=(
            CommandOption(
                name="host", description="Which PC, if you have more than one", required=False
            ),
            CommandOption(name="problem", description="What's going wrong", required=False),
        ),
    ),
    CommandSpec(
        name="close",
        description="Close one of your tickets",
        options=(
            CommandOption(
                name="ticket",
                description='Ticket number, e.g. KEN-000123, or "this" inside the ticket\'s thread',
            ),
        ),
    ),
    CommandSpec(
        name="cancel",
        description="Cancel one of your tickets",
        options=(CommandOption(name="ticket", description="Ticket number, e.g. KEN-000123"),),
    ),
)


# -- the service -------------------------------------------------------------


class DiscordService:
    """Turns gateway events into tickets, gated tool runs and Discord replies.

    Implements :class:`~kenny_server.ticket_assistant.TicketSurface`: the
    injected ``assistant`` drives every turn, this class only tells the
    assistant where Discord's own reply/gate/lifecycle messages go.
    """

    #: The ``fields.surface`` label a trail row carries when this surface
    #: produced (part of) a reply — see ``TicketAssistant._surface_label``.
    name = "discord"

    def __init__(
        self,
        *,
        gateway: DiscordGateway,
        identities: DiscordIdentityStore,
        tickets: TicketService,
        users: UserStore,
        executor: ToolExecutor,
        assistant: TicketAssistant,
        guild_ids: frozenset[str] | set[str] | Sequence[str] = (),
        support_channel_id: str | None = None,
        operator_channel_id: str | None = None,
        private_threads: bool = True,
        rate_limit_per_hour: int = 20,
        model_override: str | None = None,
        base_url: Callable[[], str] = urls.public_base_url,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gateway = gateway
        self.identities = identities
        self.tickets = tickets
        self.store = tickets.store
        self.users = users
        self.executor = executor
        self.assistant = assistant
        self.guild_ids = frozenset(guild_ids)
        self.support_channel_id = support_channel_id
        self.operator_channel_id = operator_channel_id
        self.private_threads = private_threads
        self.base_url = base_url
        # ``KENNY_DISCORD_MODEL`` overrides the assistant's shared model
        # (``KENNY_CHAT_MODEL``) for Discord-driven turns only — the assistant
        # itself is shared with the dashboard surface, so this stays a
        # per-call parameter threaded through ``run_turn``/``resume`` rather
        # than mutating ``assistant.model``.
        self._model_override = model_override
        self.model = model_override or assistant.model
        self._limiter = _RateLimiter(rate_limit_per_hour, clock=clock)
        # Registers this surface as a default notification target for
        # ``resume_expired`` (an expired gate has no per-call caller) and for
        # ``notify_transition`` (a lifecycle move triggered from anywhere,
        # including a dashboard action). Constructor-time on purpose: this
        # cannot be forgotten at a wiring site.
        assistant.register_surface(self)
        # Set once when a mention arrives with empty content — the symptom of a
        # missing Message Content intent, which otherwise looks like a dead bot.
        self.missing_message_content = False
        # Why the gateway is not up, when it failed to start. Refusing to start
        # is a designed outcome (a source install without the optional
        # dependency), but the operator who configured a bot token needs the
        # reason where they configured it, not only in a log line they will
        # never read.
        self.startup_error: str | None = None

    async def _drive_turn(
        self,
        session: TicketSession,
        ticket: Ticket | None,
        *,
        seed_events: Sequence[dict[str, Any]] = (),
        count_turn: bool = True,
    ) -> None:
        """Drive ``assistant.run_turn`` for this surface, discarding its events.

        Discord never streams a turn's events (there is no live-token UI to
        feed) — it only needs the side effects (trail rows, ``deliver_reply``,
        ``announce_gate``, persistence) ``run_turn`` performs as it goes.
        """

        async for _ in self.assistant.run_turn(
            session,
            ticket,
            seed_events=seed_events,
            count_turn=count_turn,
            surfaces=(self,),
            model_override=self._model_override,
        ):
            pass

    # -- intake ------------------------------------------------------------

    def guild_allowed(self, guild_id: str) -> bool:
        """Hard trust boundary. An empty allowlist denies every guild."""

        return bool(guild_id) and guild_id in self.guild_ids

    async def run(self) -> None:
        """Consume the gateway's event stream until it ends. Never dies on one event."""

        async for event in self.gateway.events():
            try:
                await self.handle_event(event)
            except Exception:  # noqa: BLE001 - one bad event must not stop the bot
                logger.exception("discord: handling %s failed", type(event).__name__)

    async def handle_event(self, event: InboundEvent) -> None:
        if not self.guild_allowed(event.guild_id):
            logger.debug("discord: dropping event from guild %r", event.guild_id)
            return
        if isinstance(event, MessageEvent):
            await self.handle_message(event)
        elif isinstance(event, ComponentEvent):
            await self.handle_component(event)
        elif isinstance(event, SlashCommandEvent):
            await self.handle_slash(event)
        elif isinstance(event, ThreadStateEvent):
            await self.handle_thread_state(event)

    # -- principal ---------------------------------------------------------

    async def _principal_for(self, discord_user_id: str, guild_id: str) -> Principal | None:
        """The kenny principal behind a snowflake, or None.

        The only inputs are the snowflake and the guild. The identity row gives a
        ``user_id``; the account row gives the role; ``user_hosts`` gives the
        scope. Nothing that travelled in a message participates, and Discord's
        own roles are never read here — they are advisory (routing and
        visibility) and a guild admin must not be able to grant kenny rights.
        """

        if not self.guild_allowed(guild_id):
            return None
        identity = await self.identities.resolve(discord_user_id, guild_id)
        if identity is None:
            return None
        row = await self.users.get_enabled_row(identity.user_id)
        if row is None:
            return None
        return await _principal_from_row(self.users, row)

    async def _hosts_for(self, principal: Principal) -> list[str]:
        """Which hosts ``principal`` may pick a ticket target from.

        The authorization question. A scoped (``user``-role) account is limited
        to whatever an operator explicitly assigned it via ``user_hosts``
        (ADR-0033). An operator or admin is *not* scoped by definition — it can
        already reach every host from the dashboard — so it may target the whole
        fleet. Matches the ``_known_agent_ids`` pattern in ``tools.py``.

        Not to be confused with :meth:`_own_hosts`, which answers a different
        question and must never widen this one.
        """

        if principal.scoped:
            return sorted(await self.users.get_user_hosts(principal.user_id or 0))
        ids = {a.agent_id for a in self.executor.registry.list()}
        ids.update(await self.executor.store.known_agents())
        return sorted(ids)

    async def _own_hosts(self, principal: Principal) -> list[str]:
        """Which hosts are *this account's own*, whatever its role.

        The ergonomic question, deliberately separate from :meth:`_hosts_for`.
        For a scoped account the two coincide. For an operator they do not: it
        may target the fleet, but "my PC is slow" is about one machine, and
        without this it was unanswerable — every bare mention from an unscoped
        account resolved to the whole fleet and could only ever be met with a
        question, so an operator could never open a ticket by mentioning kenny
        at all.

        An explicit ``user_hosts`` assignment is therefore read for every role.
        For an operator it grants nothing (``_hosts_for`` already returns the
        fleet) and narrows nothing (naming a host explicitly still works) — it
        only says which of the machines it may reach are the ones it lives with.
        """

        return sorted(await self.users.get_user_hosts(principal.user_id or 0))

    async def _target_candidates(self, principal: Principal) -> tuple[list[str], list[str]]:
        """``(may_target, ask_about)`` — the authorization set and the shortlist.

        The shortlist is what a bare request is offered a choice between; it
        falls back to the full set when nobody has said which machines are this
        account's own.
        """

        may_target = await self._hosts_for(principal)
        own = await self._own_hosts(principal)
        # Intersect rather than trust the assignment: a row naming a host this
        # principal may not target must not become a shortcut to it.
        shortlist = [h for h in own if h in set(may_target)]
        return may_target, shortlist or may_target

    def _actor(self, principal: Principal) -> str:
        return (
            f"operator:{principal.user_id}"
            if principal.at_least("operator")
            else f"user:{principal.user_id}"
        )

    # -- messages ----------------------------------------------------------

    async def handle_message(self, event: MessageEvent) -> None:
        if event.author_is_bot:
            return
        if event.thread_id:
            binding = await self.store.channel_by_thread(event.thread_id)
            if binding is not None:
                await self._handle_thread_message(event, binding)
                return
            return
        if not event.mentions_bot:
            return
        if self.support_channel_id and event.channel_id != self.support_channel_id:
            return
        await self._handle_mention(event)

    async def _handle_mention(self, event: MessageEvent) -> None:
        principal = await self._principal_for(event.author_id, event.guild_id)
        if principal is None:
            # Inert on purpose: no ticket, no reply, no model call. Anything else
            # would let an unknown guild member learn that kenny is listening.
            logger.info("discord: ignoring mention from unmapped user in %s", event.guild_id)
            return

        if not event.content.strip():
            await self._note_empty_mention(event)
            return

        if not self._limiter.allow(f"u:{principal.user_id}"):
            await self._reply(event, "You have opened a lot of requests recently — "
                              "please give kenny a moment before the next one.")
            return

        _, candidates = await self._target_candidates(principal)
        if not candidates:
            await self._reply(
                event,
                "No PC is assigned to your kenny account yet, so there is nothing I "
                "could look at. Ask an operator to assign one.",
            )
            return
        if len(candidates) > 1:
            choice = await self._prepare_host_choice(
                principal=principal,
                candidates=candidates,
                guild_id=event.guild_id,
                channel_id=event.channel_id,
                discord_user_id=event.author_id,
                content=event.content,
                message_id=event.message_id,
            )
            if choice.request_id is None:
                await self.gateway.post_message(
                    channel_id=event.channel_id, content=choice.prompt
                )
            else:
                await self.gateway.post_host_picker(
                    channel_id=event.channel_id,
                    request_id=choice.request_id,
                    hosts=choice.hosts,
                    prompt=choice.prompt,
                    reply_to=event.message_id,
                )
            return

        await self.open_ticket(
            principal=principal,
            agent_id=candidates[0],
            guild_id=event.guild_id,
            channel_id=event.channel_id,
            discord_user_id=event.author_id,
            content=event.content,
        )

    async def _prepare_host_choice(
        self,
        *,
        principal: Principal,
        candidates: list[str],
        guild_id: str,
        channel_id: str,
        discord_user_id: str,
        content: str,
        message_id: str | None = None,
    ) -> HostPrompt:
        """Park a request and build the "which PC" prompt. Sends nothing.

        A click is not a message. It carries no prose for the model to be steered
        by, it cannot be typed by a bystander into the channel, and it resolves
        through a row kenny wrote — so the target is still decided outside the
        model loop and frozen before the ticket exists (ADR-0044 control 1). The
        host is *still* never inferred from what anyone wrote; the only thing
        that changed is that saying which one no longer requires knowing a slash
        command's option syntax.

        Deliberately send-free: a bare mention answers this publicly in the
        channel it was sent to, while `/help-me` answers it as the
        interaction's own ephemeral reply — the two callers decide that, this
        just does the parking and wording shared by both. ``request_id is
        None`` means the fleet does not fit on one message's buttons (see
        `_picker_fits`) and ``prompt`` is a plain-text fallback instead.
        """

        listed = ", ".join(f"`{h}`" for h in candidates)
        if not _picker_fits(candidates):
            fallback = (
                f"You have several PCs ({listed}). Tell me which one with "
                "`/help-me` and pick the host there — I will not guess."
            )
            return HostPrompt(prompt=fallback, request_id=None, hosts=candidates)

        pending = await self.store.open_pending_request(
            discord_user_id=discord_user_id,
            user_id=principal.user_id or 0,
            guild_id=guild_id,
            channel_id=channel_id,
            content=content,
            candidates=candidates,
            message_id=message_id,
        )
        prompt = f"Which PC is this about? ({listed})"
        return HostPrompt(prompt=prompt, request_id=pending.id, hosts=candidates)

    async def _handle_thread_message(
        self, event: MessageEvent, binding: TicketChannel
    ) -> None:
        ticket = await self.store.get(binding.ticket_id)
        if ticket is None or ticket.state in ("closed", "cancelled"):
            return
        principal = await self._principal_for(event.author_id, event.guild_id)
        if principal is None:
            # An unmapped participant's words never enter the model context at
            # all — not even as context. There is no envelope that would make
            # them safe, because there is no identity to attribute them to.
            logger.info("discord: ignoring thread message from unmapped user")
            return

        session = await self.assistant.session_for(ticket, actor=principal)
        if session is None:
            return

        actionable = principal.user_id == ticket.requester_user_id
        self.assistant.append_inbound(
            session,
            author_id=event.author_id,
            kenny_user=principal.username,
            role=principal.role,
            actionable=actionable,
            content=event.content,
        )
        await self.assistant.append_message(
            ticket,
            actor=self._actor(principal),
            text=event.content,
            actionable=actionable,
            surface=self.name,
            verbatim=False,
            summary=("message from the requester" if actionable else "context message"),
        )

        if not actionable:
            # Context only: persisted into the transcript, but it does not get a
            # turn and it never changes whose principal is in force.
            await self.assistant._save_run(session)
            return

        if not self._limiter.allow(f"u:{principal.user_id}"):
            await self.assistant._save_run(session)
            await self._post(session, "One moment — kenny is catching up with your requests.")
            return

        open_gate = await self.store.get_open_approval(ticket.id)
        if open_gate is not None:
            # A ticket has exactly one open gate, and it is resolved by a
            # decision, never by talking past it. Running a turn here would also
            # hit the partial unique index the moment the model held again.
            await self.assistant._save_run(session)
            await self._post(
                session,
                "I still need your answer to the request above before I continue."
                if open_gate.kind == "user_consent"
                else "I still need an operator to approve the last step; I will "
                "continue as soon as that is decided.",
            )
            return

        if ticket.blocked_on == "user":
            # The common case: this message is the reply kenny was waiting for.
            await self.assistant._unblock(ticket.id, actor=self._actor(principal))
            ticket = await self.tickets.get(ticket.id)
        elif ticket.state != "in_progress":
            # A message on a resolved ticket reopens it.
            await self.assistant._transition(ticket.id, "in_progress", actor=self._actor(principal))
            ticket = await self.tickets.get(ticket.id)
        await self._drive_turn(session, ticket)

    async def open_ticket(
        self,
        *,
        principal: Principal,
        agent_id: str,
        guild_id: str,
        channel_id: str,
        discord_user_id: str,
        content: str,
        run: bool = True,
    ) -> Ticket:
        """Create a ticket with a frozen target, open its thread, work one turn."""

        profile = await self.users.get_capability_profile(principal.user_id)
        title = _title_from(content)
        ticket = await self.tickets.create(
            title=title,
            origin="discord",
            requester_user_id=principal.user_id,
            agent_id=agent_id,
            role_snapshot=principal.role,
            profile_snapshot=profile,
            actor=self._actor(principal),
            reason="opened from Discord",
        )
        thread = await self.gateway.open_thread(
            channel_id=channel_id,
            name=f"KEN-{ticket.number:06d} {title}"[:90],
            private=self.private_threads,
            invite_user_ids=[discord_user_id],
        )
        await self.store.bind_channel(
            ticket_id=ticket.id,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread.thread_id,
            private=self.private_threads,
        )
        await self.assistant._transition(
            ticket.id, "in_progress", actor="system", reason="opened from Discord"
        )
        ticket = await self.tickets.get(ticket.id)

        session = await self.assistant.session_for(ticket, actor=principal)
        if session is None:  # pragma: no cover - the account was just resolved
            return ticket
        self.assistant.append_inbound(
            session,
            author_id=discord_user_id,
            kenny_user=principal.username,
            role=principal.role,
            actionable=True,
            content=content,
        )
        await self.assistant.append_message(
            ticket,
            actor=self._actor(principal),
            text=content,
            actionable=True,
            surface=self.name,
            verbatim=False,
            summary="opening message",
        )
        if run:
            await self._drive_turn(session, ticket)
        else:
            await self.assistant._save_run(session)
        return ticket

    # -- TicketSurface -------------------------------------------------------

    async def deliver_reply(self, ticket: Ticket, session: TicketSession, text: str) -> None:
        """Post kenny's reply into the ticket's thread, scrubbed on the way out.

        Scrubbed on the way *out*, not on the way into the model's context: the
        model is supposed to read the file, that is what the tool is for. What
        it may not do is paste the body into a chat kenny does not own. The
        trail (``TicketAssistant.append_message``) stores ``text`` unscrubbed —
        this method's redaction is Discord's own outbound rule, not the
        server's record.
        """

        body = _scrub(text, session.turn_blobs, redacted_payloads(session)).strip()
        if session.turn_redacted_tools:
            names = ", ".join(sorted(set(session.turn_redacted_tools)))
            note = (
                f"I looked at {names} on `{ticket.agent_id}`. The detail stays on the "
                f"server — you can read it in the ticket: {self.assistant.ticket_url(ticket.id)}"
            )
            body = f"{body}\n\n{note}" if body else note
        if not body:
            return
        await self._post(session, body)

    async def announce_gate(self, ticket: Ticket, session: TicketSession) -> None:
        """Post the card for the gate the loop just opened."""

        approval = await self.store.get_open_approval(ticket.id)
        if approval is None:  # pragma: no cover - on_hold just created it
            return
        detail_url = self.assistant.ticket_url(ticket.id)
        shown = json.dumps(redact_args(approval.args), sort_keys=True, default=str)
        if approval.kind == "user_consent":
            channel = session.thread_id or session.channel_id
            summary = (
                f"kenny would like to run `{approval.tool}` on `{approval.agent_id}`. "
                f"This one needs your OK because it touches your privacy.\n`{shown}`"
            )
        else:
            channel = self.operator_channel_id or session.thread_id or session.channel_id
            summary = (
                f"Ticket KEN-{ticket.number:06d}: `{approval.tool}` on "
                f"`{approval.agent_id}` needs an operator's approval.\n`{shown}`"
            )
            await self._post(
                session,
                "That step needs an operator's approval. I have asked for it and "
                "will continue as soon as it is decided.",
            )
        if not channel:
            return
        try:
            message_id = await self.gateway.post_approval_card(
                channel_id=channel,
                approval_id=approval.id,
                summary=summary,
                detail_url=detail_url,
            )
            await self.store.set_approval_message(
                approval.id, channel_id=channel, message_id=message_id
            )
        except Exception:
            # The approval itself is already durably recorded (open_approval ran
            # before this); a failed Discord notification (e.g. missing channel
            # permissions) must not take the rest of the turn down with it.
            logger.exception(
                "ticket %s: failed to post the approval card to channel %s",
                ticket.id,
                channel,
            )

    async def on_transition(self, ticket: Ticket, to_state: str) -> None:
        """Tell the thread about a lifecycle move, and archive at a terminal state.

        Fires from ``TicketAssistant.notify_transition``, itself the registered
        ``TransitionNotifier`` called from inside ``TicketService.transition()``/
        ``auto_close_resolved()`` right after the state change already
        committed — so this is best-effort by construction: a failure here must
        never look like the transition itself failed.
        """

        if to_state not in ("resolved", "closed", "cancelled"):
            return
        try:
            channel = await self.store.get_channel(ticket.id)
            if channel is None:
                return
            target = channel.thread_id or channel.channel_id
            if not target:
                return
            message = {
                "resolved": f"KEN-{ticket.number:06d} was marked resolved.",
                "closed": f"KEN-{ticket.number:06d} is closed.",
                "cancelled": f"KEN-{ticket.number:06d} was cancelled.",
            }[to_state]
            for chunk in chunk_message(message):
                await self.gateway.post_message(channel_id=target, content=chunk)
            if to_state in ("closed", "cancelled"):
                await self._archive(ticket.id)
        except Exception:  # noqa: BLE001 - a notification must never break the transition
            logger.exception("ticket %s: on_transition(%s) failed", ticket.id, to_state)

    async def notify_stalled(self, ticket: Ticket, blocked_on: str) -> None:
        """Post a reminder for a ticket the stall sweep just nudged.

        Fires from ``TicketAssistant.notify_stalled``, itself the registered
        ``StallNotifier`` called from ``TicketService.nudge_stalled`` after the
        nudge is already durably recorded on the trail — best-effort by
        construction, same as :meth:`on_transition`. A ``user`` block reminds in
        the ticket's own thread (where the requester is); an ``operator`` block
        reminds in the operator channel, since nobody but an operator can act
        on it.
        """

        if blocked_on == "user":
            binding = await self.store.get_channel(ticket.id)
            channel_id = binding.thread_id if binding else None
            content = (
                "Still waiting to hear back on this one — reply here whenever "
                "you get a chance."
            )
        else:
            channel_id = self.operator_channel_id
            content = (
                f'Ticket KEN-{ticket.number:06d} ("{ticket.title}") has been '
                f"waiting on an operator for a while. {self.assistant.ticket_url(ticket.id)}"
            )
        if not channel_id:
            return
        try:
            for chunk in chunk_message(content):
                await self.gateway.post_message(channel_id=channel_id, content=chunk)
        except Exception:  # noqa: BLE001 - the nudge is already durably recorded
            logger.exception("ticket %s: failed to post the stall reminder", ticket.id)

    # -- decisions ---------------------------------------------------------

    async def _handle_host_choice(self, event: ComponentEvent, choice: HostChoice) -> None:
        """Open the parked request against the host that was clicked.

        Every check is redone here against state read now, not against what was
        true when the card was posted. The card is a Discord message: it outlives
        the assignment that produced it, anyone who can see the channel can click
        it, and Discord will happily deliver a click for a card from last week.
        """

        principal = await self._principal_for(event.user_id, event.guild_id)
        if principal is None:
            # Same inertness as an unmapped mention: a stranger clicking a button
            # must not learn that the button did anything.
            logger.info("discord: ignoring host-picker click from unmapped user")
            return

        pending = await self.store.get_pending_request(choice.request_id)
        if pending is None or pending.guild_id != event.guild_id:
            await self.gateway.respond_ephemeral(
                interaction_id=event.interaction_id,
                content="That request is no longer open.",
            )
            return
        if pending.user_id != principal.user_id:
            # Ownership, not host scope: picking a machine for somebody else's
            # request would open a ticket in their name.
            await self.gateway.respond_ephemeral(
                interaction_id=event.interaction_id,
                content="Only the person who asked can pick the PC.",
            )
            return
        if choice.agent_id not in await self._hosts_for(principal):
            # Re-checked at click time on purpose: a scope narrowed after the
            # card went out has to bite, and the button's own label is not
            # evidence of anything.
            await self.gateway.respond_ephemeral(
                interaction_id=event.interaction_id,
                content=f"`{choice.agent_id}` is not one of your PCs.",
            )
            return
        if not self._limiter.allow(f"u:{principal.user_id}"):
            await self.gateway.respond_ephemeral(
                interaction_id=event.interaction_id,
                content="You have opened a lot of requests recently — please wait a little.",
            )
            return

        claimed = await self.store.consume_pending_request(choice.request_id)
        if claimed is None:
            # Already answered, or expired between the checks above and here.
            await self.gateway.respond_ephemeral(
                interaction_id=event.interaction_id,
                content="That request is no longer open.",
            )
            return

        # Answer the interaction before the turn, not after: open_ticket's own
        # model turn can run for minutes (an approval gate), and the
        # interaction token backing this reply is only good for ~15 minutes.
        # Confirming first also means the click gets its "Opened KEN-..." the
        # moment the thread exists, instead of only once the model is done.
        ticket = await self.open_ticket(
            principal=principal,
            agent_id=choice.agent_id,
            guild_id=claimed.guild_id,
            channel_id=claimed.channel_id,
            discord_user_id=claimed.discord_user_id,
            content=claimed.content,
            run=False,
        )
        await self.gateway.respond_ephemeral(
            interaction_id=event.interaction_id,
            content=f"Opened KEN-{ticket.number:06d} for `{choice.agent_id}` — see the thread.",
        )
        session = await self.assistant.session_for(ticket, actor=principal)
        if session is not None:  # pragma: no cover - the account was just resolved
            await self._drive_turn(session, ticket)

    async def handle_component(self, event: ComponentEvent) -> None:
        choice = parse_host_custom_id(event.custom_id)
        if choice is not None:
            # Discord requires the first ack of an interaction within ~3s.
            # What follows (DB writes, thread creation, a full model turn) is
            # routinely slower than that, so defer immediately -- the same
            # fix handle_slash already has (see the docstring there). The two
            # custom_id parsers above are pure and synchronous, so deciding
            # this costs nothing and an interaction that is not kenny's own
            # (an unrecognized custom_id) is never touched.
            await self.gateway.defer_interaction(interaction_id=event.interaction_id)
            await self._handle_host_choice(event, choice)
            return
        parsed = parse_approval_custom_id(event.custom_id)
        if parsed is None:
            return
        await self.gateway.defer_interaction(interaction_id=event.interaction_id)
        approval_id, approve = parsed.approval_id, parsed.action == "approve"
        principal = await self._principal_for(event.user_id, event.guild_id)
        if principal is None:
            logger.info("discord: ignoring button click from unmapped user")
            return
        approval = await self.store.get_approval(approval_id)
        if approval is None or approval.status != "pending":
            await self.gateway.respond_ephemeral(
                interaction_id=event.interaction_id,
                content="That request has already been decided.",
            )
            return
        ticket = await self.store.get(approval.ticket_id)
        if ticket is None:  # pragma: no cover - approval implies a ticket
            return

        if approval.kind == "user_consent":
            if principal.user_id != ticket.requester_user_id:
                await self.gateway.respond_ephemeral(
                    interaction_id=event.interaction_id,
                    content=(
                        "Only the person this ticket belongs to can answer a consent "
                        "request."
                    ),
                )
                return
            await self._decide_consent(approval, ticket, approve=approve, principal=principal)
        else:
            if not principal.at_least("operator"):
                # Both directions, not just approval. ``decide_approval`` leaves
                # denial open to any actor so the sweeper can expire a gate, but
                # that is a service affordance: over Discord, denying someone
                # else's gate cancels their change and drives a model turn on a
                # ticket the clicker may not even read. Who can *see* the
                # operator channel is a Discord role, and Discord roles never
                # authorize.
                await self.gateway.respond_ephemeral(
                    interaction_id=event.interaction_id,
                    content="Only an operator can decide this step.",
                )
                return
            await self.tickets.decide_approval(
                approval.id,
                approve=approve,
                decided_by=principal.user_id,
                decided_via="discord",
                actor=self._actor(principal),
            )

        if approval.discord_channel_id and approval.discord_message_id:
            await self.gateway.resolve_card(
                channel_id=approval.discord_channel_id,
                message_id=approval.discord_message_id,
                outcome="approved" if approve else "denied",
                decided_by=str(principal.user_id),
            )
        await self.gateway.respond_ephemeral(
            interaction_id=event.interaction_id,
            content="Recorded — thank you." if approve else "Recorded: declined.",
        )
        await self.assistant.resume(
            ticket.id,
            decided_by=principal,
            surfaces=(self,),
            model_override=self._model_override,
        )

    async def _decide_consent(
        self,
        approval: TicketApproval,
        ticket: Ticket,
        *,
        approve: bool,
        principal: Principal,
    ) -> None:
        """Close a consent gate on behalf of the affected person.

        Goes through the service like every other decision: it knows that a
        consent gate is answered by the ticket's requester and refuses anyone
        else, including an operator.
        """

        await self.tickets.decide_approval(
            approval.id,
            approve=approve,
            decided_by=principal.user_id,
            decided_via="discord",
            actor=f"user:{principal.user_id}",
        )

    # -- threads -----------------------------------------------------------

    async def handle_thread_state(self, event: ThreadStateEvent) -> None:
        """Record a thread archiving. It is never the ticket's state."""

        if not event.archived:
            return
        binding = await self.store.channel_by_thread(event.thread_id)
        if binding is None:
            return
        await self.store.archive_channel(binding.ticket_id)

    # -- slash commands ----------------------------------------------------

    async def handle_slash(self, event: SlashCommandEvent) -> None:
        """Dispatch a slash command and answer it ephemerally.

        ``content`` ends up ``None`` exactly when the command has already
        answered the interaction itself (the host picker, see `help_me`) --
        the trailing `respond_ephemeral` is skipped for those so the picker
        is not immediately followed by a second, textual reply to the same
        interaction.
        """

        await self.gateway.defer_interaction(interaction_id=event.interaction_id)
        command = (event.command or "").strip().lower()
        options = event.options or {}
        content: str | None
        if command == "link":
            content = await self.link(
                discord_user_id=event.user_id,
                guild_id=event.guild_id,
                display_hint=options.get("name", ""),
            )
        elif command == "whoami":
            content = await self.whoami(
                discord_user_id=event.user_id, guild_id=event.guild_id
            )
        elif command == "status":
            content = await self.status(
                discord_user_id=event.user_id, guild_id=event.guild_id
            )
        elif command in ("help-me", "help_me"):
            # channel_id, not thread_id: this is what gets parked and what
            # open_ticket hands to open_thread -- a thread cannot itself host
            # a thread, so the parent channel is the only correct value here,
            # even when the command was typed inside an existing ticket
            # thread. The host picker itself stays with the caller regardless
            # (see help_me), because it is the interaction's own ephemeral
            # reply rather than a channel post.
            content = await self.help_me(
                discord_user_id=event.user_id,
                guild_id=event.guild_id,
                channel_id=event.channel_id,
                interaction_id=event.interaction_id,
                host=options.get("host"),
                content=options.get("problem", ""),
            )
        elif command == "close":
            content = await self.close_ticket(
                discord_user_id=event.user_id,
                guild_id=event.guild_id,
                ticket_ref=options.get("ticket", ""),
                thread_id=event.thread_id,
            )
        elif command == "cancel":
            content = await self.cancel_ticket(
                discord_user_id=event.user_id,
                guild_id=event.guild_id,
                ticket_ref=options.get("ticket", ""),
            )
        else:
            content = "Unknown command."
        if content is not None:
            await self.gateway.respond_ephemeral(
                interaction_id=event.interaction_id, content=content
            )

    async def link(
        self, *, discord_user_id: str, guild_id: str, display_hint: str = ""
    ) -> str:
        """Enrollment path A: open a claim an operator confirms in the dashboard."""

        if not self.guild_allowed(guild_id):
            return "kenny is not available in this server."
        existing = await self.identities.resolve(discord_user_id, guild_id)
        if existing is not None:
            return "You are already linked to a kenny account. Use `/whoami`."
        claim = await self.identities.open_claim(
            discord_user_id=discord_user_id,
            display_hint=display_hint or discord_user_id,
            guild_id=guild_id,
        )
        return (
            f"Ask an operator to confirm this code in the kenny dashboard: "
            f"**{claim.code}**\nIt expires at {claim.expires_at}. Until it is "
            "confirmed, kenny will not react to you."
        )

    async def whoami(self, *, discord_user_id: str, guild_id: str) -> str:
        """Show the caller exactly what kenny thinks they are — misbindings visible."""

        if not self.guild_allowed(guild_id):
            return "kenny is not available in this server."
        principal = await self._principal_for(discord_user_id, guild_id)
        if principal is None:
            return "You are not linked to a kenny account. Use `/link` to ask."
        hosts, candidates = await self._target_candidates(principal)
        profile = await self.users.get_capability_profile(principal.user_id or 0)
        allowed = allowed_tools_for(profile=profile, scoped=principal.scoped)
        lines = [
            f"kenny account: **{principal.username}** (role `{principal.role}`)",
            f"Capability profile: `{profile or 'role default'}` ({len(allowed)} tools)",
            f"PCs: {', '.join(f'`{h}`' for h in hosts) if hosts else 'none assigned'}",
        ]
        if candidates != hosts:
            # Only for an operator with an assignment, where the two differ and
            # the difference is exactly what decides where a bare mention goes.
            lines.append(f"Yours: {', '.join(f'`{h}`' for h in candidates)}")
        return "\n".join(lines)

    async def status(self, *, discord_user_id: str, guild_id: str) -> str:
        """List the caller's own open tickets."""

        principal = await self._principal_for(discord_user_id, guild_id)
        if principal is None:
            return "You are not linked to a kenny account."
        tickets = await self.store.list(
            requester_user_id=principal.user_id,
            states=("new", "in_progress", "resolved"),
            limit=10,
        )
        if not tickets:
            return "You have no open tickets."
        lines = [
            f"KEN-{t.number:06d} — {t.title}"
            f" ({t.state.replace('_', ' ')}"
            f"{', waiting on ' + t.blocked_on if t.blocked_on else ''})"
            for t in tickets
        ]
        return "Your open tickets:\n" + "\n".join(lines)

    async def help_me(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
        channel_id: str,
        interaction_id: str,
        host: str | None = None,
        content: str = "",
    ) -> str | None:
        """Open a ticket explicitly, naming the host when the caller has several.

        Returns ``None`` when the multi-host picker was sent as its own
        ephemeral reply to ``interaction_id`` -- the caller (`handle_slash`)
        must not answer the interaction a second time in that case.
        """

        principal = await self._principal_for(discord_user_id, guild_id)
        if principal is None:
            return "You are not linked to a kenny account."
        if not self._limiter.allow(f"u:{principal.user_id}"):
            return "You have opened a lot of requests recently — please wait a little."
        may_target, candidates = await self._target_candidates(principal)
        if not may_target:
            return "No PC is assigned to your kenny account yet."
        if host:
            # Validated against the full set, not the shortlist: naming a host
            # explicitly is how an operator reaches a machine that is not its
            # own. Widening nothing, since the set is the caller's own scope;
            # an unknown name is refused rather than guessed.
            if host not in may_target:
                return f"`{host}` is not one of your PCs ({', '.join(may_target)})."
            target = host
        elif len(candidates) == 1:
            target = candidates[0]
        else:
            # Same picker the mention path offers, rather than a second dead
            # end telling the caller to rerun the command they just ran --
            # but sent as this interaction's own ephemeral reply, not a
            # second channel post: an interaction response always renders
            # where the command was typed, so this is what keeps the picker
            # in a private ticket thread instead of leaking to its public
            # parent channel.
            choice = await self._prepare_host_choice(
                principal=principal,
                candidates=candidates,
                guild_id=guild_id,
                channel_id=channel_id,
                discord_user_id=discord_user_id,
                content=content or "(no description given)",
            )
            if choice.request_id is None:
                return choice.prompt
            await self.gateway.respond_ephemeral_picker(
                interaction_id=interaction_id,
                request_id=choice.request_id,
                hosts=choice.hosts,
                prompt=choice.prompt,
            )
            return None
        ticket = await self.open_ticket(
            principal=principal,
            agent_id=target,
            guild_id=guild_id,
            channel_id=channel_id,
            discord_user_id=discord_user_id,
            content=content or "(no description given)",
        )
        return f"Opened KEN-{ticket.number:06d} for `{target}` — see the thread."

    async def _own_ticket(
        self, ticket_ref: str, principal: Principal, *, thread_id: str | None = None
    ) -> Ticket | str:
        ref = (ticket_ref or "").strip().upper().removeprefix("KEN-")
        ticket: Ticket | None = None
        if ref == "THIS":
            # "this" resolves via the thread the command was typed in, not a
            # number -- it only ever means something inside a ticket's own
            # private thread, so a missing/unbound thread_id is a plain miss,
            # not a special-cased error.
            binding = await self.store.channel_by_thread(thread_id) if thread_id else None
            if binding is not None:
                ticket = await self.store.get(binding.ticket_id)
        elif ref.isdigit():
            ticket = await self.store.get_by_number(int(ref))
        if ticket is None:
            ticket = await self.store.get(ticket_ref.strip())
        if ticket is None:
            return "I could not find that ticket."
        if not principal.at_least("operator") and ticket.requester_user_id != principal.user_id:
            # Ownership, not host scope: a family member must never read or steer
            # somebody else's ticket.
            return "I could not find that ticket."
        return ticket

    async def close_ticket(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
        ticket_ref: str,
        thread_id: str | None = None,
    ) -> str:
        principal = await self._principal_for(discord_user_id, guild_id)
        if principal is None:
            return "You are not linked to a kenny account."
        found = await self._own_ticket(ticket_ref, principal, thread_id=thread_id)
        if isinstance(found, str):
            return found
        actor = self._actor(principal)
        try:
            if found.state != "resolved":
                # Resolving is a ``system``/``operator`` transition (a requester
                # may cancel, not resolve), so the service resolves *on behalf
                # of* the requester and the reason names who asked. Closing the
                # resolved ticket is then theirs to drive.
                await self.tickets.transition(
                    found.id, "resolved", actor="system", reason=f"resolved at {actor}'s request"
                )
            await self.tickets.transition(
                found.id, "closed", actor=actor, reason="closed from Discord"
            )
        except TicketError as exc:
            return f"I could not close that ticket: {exc}"
        await self._archive(found.id)
        return f"KEN-{found.number:06d} is closed. Thanks!"

    async def cancel_ticket(
        self, *, discord_user_id: str, guild_id: str, ticket_ref: str
    ) -> str:
        principal = await self._principal_for(discord_user_id, guild_id)
        if principal is None:
            return "You are not linked to a kenny account."
        found = await self._own_ticket(ticket_ref, principal)
        if isinstance(found, str):
            return found
        try:
            await self.tickets.transition(
                found.id,
                "cancelled",
                actor=self._actor(principal),
                reason="cancelled from Discord",
            )
        except TicketError as exc:
            return f"I could not cancel that ticket: {exc}"
        await self._archive(found.id)
        return f"KEN-{found.number:06d} is cancelled."

    async def _archive(self, ticket_id: str) -> None:
        binding = await self.store.get_channel(ticket_id)
        if binding is None or binding.archived_at is not None:
            return
        await self.gateway.archive_thread(thread_id=binding.thread_id, locked=False)
        await self.store.archive_channel(ticket_id)

    # -- posting helpers -----------------------------------------------------

    async def _post(self, session: TicketSession, content: str) -> None:
        """Post into the ticket's thread, chunked to Discord's message limit."""

        channel = session.thread_id or session.channel_id
        if not channel:
            return
        for chunk in chunk_message(content):
            await self.gateway.post_message(channel_id=channel, content=chunk)

    async def _reply(self, event: MessageEvent, content: str) -> None:
        for chunk in chunk_message(content):
            await self.gateway.post_message(
                channel_id=event.channel_id, content=chunk, reply_to=event.message_id
            )

    async def _note_empty_mention(self, event: MessageEvent) -> None:
        """A mention with no text means the Message Content intent is missing."""

        if self.missing_message_content:
            return
        self.missing_message_content = True
        logger.warning(
            "discord: mention arrived with empty content — the Message Content "
            "intent is probably not enabled for this application"
        )
        if self.operator_channel_id:
            await self.gateway.post_message(
                channel_id=self.operator_channel_id,
                content=(
                    "kenny received a mention with empty content. Enable the "
                    "**Message Content** privileged intent for the application, "
                    "otherwise kenny cannot read requests."
                ),
            )

    # -- diagnostics -------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """What ``/api/discord/status`` reports."""

        return {
            "connected": bool(self.gateway.connected),
            "guilds": sorted(self.guild_ids),
            "support_channel_id": self.support_channel_id,
            "operator_channel_id": self.operator_channel_id,
            "missing_message_content": self.missing_message_content,
            "startup_error": self.startup_error,
            "model": self.model,
        }

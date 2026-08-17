"""Outbound operator notifications: ntfy, a generic JSON webhook, and Discord.

Alert delivery is best-effort by design (ADR-0027): a dead or slow
notification target must never stall or kill the evaluation loop, so every
``send`` swallows and logs transport errors. With no channel configured, alert
evaluation still runs and records history, it just pushes nothing.

Which channels exist is *resolved per dispatch*, not captured at startup
(ADR-0054). :class:`NotifierProvider` reads the four channel keys
(``KENNY_NTFY_URL``, ``KENNY_NTFY_TOKEN``, ``KENNY_WEBHOOK_URL``,
``KENNY_DISCORD_WEBHOOK_URL``) through :class:`~kenny_server.config.Settings`
when one is wired — which resolves DB override > env var > default itself — and
falls back to reading the environment directly when it is not, so a server
constructed without a settings layer behaves exactly as it did before. The
built channels are memoised against the resolved values, so the common case
(nothing changed) costs one dict lookup per key and no object construction.

``client_factory`` is injected so tests can supply an ``httpx.MockTransport``
(same pattern as ``webfilter.ExternalListCache``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import httpx

logger = logging.getLogger("kenny.notify")

_SEND_TIMEOUT_S = 15.0

ClientFactory = Callable[[], httpx.AsyncClient]


@dataclass
class Notification:
    """One operator-facing message, channel-agnostic."""

    title: str
    body: str
    priority: str = "default"  # ntfy scale: "low" | "default" | "high" | "urgent"
    tags: list[str] = field(default_factory=list)
    agent_id: str | None = None
    kind: str = "alert"  # "alert" | "recovery" | "change" | "digest"
    # -- structured discriminator for auto-ticket rules (ticket_rules.py) ------
    # ``kind`` says whether this is a genuine alert vs. a recovery/change/digest;
    # ``event_type``/``sections`` say *which* alert, so an operator rule can name
    # it without parsing the free-text ``body``. Both default to empty so every
    # existing construction site (and every notifier that ignores them) keeps
    # working unchanged -- an empty ``event_type`` matches no rule and falls
    # through to the coded default in ``ticket_rules.decide``.
    event_type: str = ""  # "health" | "offline" | "disk_forecast" | "change" | "digest"
    # section name -> the severity this notification is about ("warn"/"crit"),
    # or "" for a producer with no severity axis (e.g. an inventory change).
    # Empty dict means "no per-section subject" (offline, disk_forecast, digest).
    sections: dict[str, str] = field(default_factory=dict)


class Notifier(Protocol):
    """A delivery channel for :class:`Notification`."""

    name: str

    async def send(self, notification: Notification) -> None: ...


class _HttpNotifier:
    """Shared httpx plumbing for the concrete channels."""

    name = "http"

    def __init__(self, url: str, *, client_factory: ClientFactory | None = None) -> None:
        self._url = url
        self._client_factory = client_factory

    def _make_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        return httpx.AsyncClient()

    async def _post(self, **kwargs: object) -> None:
        try:
            async with self._make_client() as client:
                resp = await client.post(self._url, timeout=_SEND_TIMEOUT_S, **kwargs)
            if resp.status_code >= 400:
                logger.warning("%s notify returned %s", self.name, resp.status_code)
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            logger.warning("%s notify failed: %s", self.name, exc)


class NtfyNotifier(_HttpNotifier):
    """POST to an ntfy topic URL (https://ntfy.sh/<topic> or self-hosted)."""

    name = "ntfy"

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        super().__init__(url, client_factory=client_factory)
        self._token = token

    async def send(self, notification: Notification) -> None:
        headers = {
            "Title": notification.title,
            "Priority": notification.priority,
        }
        if notification.tags:
            headers["Tags"] = ",".join(notification.tags)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        await self._post(content=notification.body.encode("utf-8"), headers=headers)


class WebhookNotifier(_HttpNotifier):
    """POST a JSON payload to a generic operator-configured webhook URL."""

    name = "webhook"

    async def send(self, notification: Notification) -> None:
        await self._post(
            json={
                "kind": notification.kind,
                "title": notification.title,
                "body": notification.body,
                "priority": notification.priority,
                "tags": notification.tags,
                "agent_id": notification.agent_id,
                "event_type": notification.event_type,
                "sections": notification.sections,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )


_DISCORD_TITLE_LIMIT = 256
_DISCORD_DESCRIPTION_LIMIT = 4096

# Discord embed colors (decimal), keyed by Notification.priority.
_DISCORD_COLORS = {
    "low": 0x95A5A6,  # grey
    "default": 0x3498DB,  # blue
    "high": 0xE67E22,  # orange
    "urgent": 0xE74C3C,  # red
}
_DISCORD_DEFAULT_COLOR = _DISCORD_COLORS["default"]


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` chars, replacing the tail with an ellipsis."""

    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class DiscordNotifier(_HttpNotifier):
    """POST a Discord webhook payload (embed) to a Discord channel webhook URL."""

    name = "discord"

    async def send(self, notification: Notification) -> None:
        fields = [{"name": "kind", "value": notification.kind, "inline": True}]
        if notification.agent_id:
            fields.append({"name": "agent_id", "value": notification.agent_id, "inline": True})
        embed = {
            "title": _truncate(notification.title, _DISCORD_TITLE_LIMIT),
            "description": _truncate(notification.body, _DISCORD_DESCRIPTION_LIMIT),
            "color": _DISCORD_COLORS.get(notification.priority, _DISCORD_DEFAULT_COLOR),
            "fields": fields,
        }
        await self._post(json={"embeds": [embed]})


# -- channel configuration (ADR-0054) -----------------------------------------

# The four keys that decide which channels exist, in catalog order. Each is a
# ``live`` setting in ``config.CATALOG`` and each is ``sensitive`` there: a
# webhook or ntfy topic URL is bearer-equivalent, so it is never serialised
# back out of the settings API.
CHANNEL_KEYS: tuple[str, ...] = (
    "KENNY_NTFY_URL",
    "KENNY_NTFY_TOKEN",
    "KENNY_WEBHOOK_URL",
    "KENNY_DISCORD_WEBHOOK_URL",
)


class SettingsReader(Protocol):
    """The one method this module needs from :class:`~kenny_server.config.Settings`."""

    def get(self, key: str) -> Any: ...


@dataclass(frozen=True)
class ChannelConfig:
    """The resolved channel values — also the memoisation key for the provider."""

    ntfy_url: str = ""
    ntfy_token: str = ""
    webhook_url: str = ""
    discord_webhook_url: str = ""


def _channel_value(key: str, settings: SettingsReader | None, env: Mapping[str, str]) -> str:
    """Resolve one channel key: settings when wired, else the environment.

    A wired ``Settings`` is authoritative and already layers DB override > env
    var > default, so its answer is used verbatim — including an empty one.
    That is what makes clearing a field in the dashboard actually turn a
    channel off instead of silently falling back to whatever the environment
    still holds. ``env`` is consulted only when there is no settings layer at
    all (direct construction, tests) or when the lookup fails.
    """

    if settings is not None:
        try:
            value = settings.get(key)
        except Exception:  # noqa: BLE001 - an unknown key must not kill delivery
            logger.warning("settings lookup for %s failed; reading the environment", key)
        else:
            return "" if value is None else str(value).strip()
    return (env.get(key) or "").strip()


def resolve_channels(
    *,
    settings: SettingsReader | None = None,
    env: Mapping[str, str] | None = None,
) -> ChannelConfig:
    """Read the four channel keys into an immutable, comparable snapshot."""

    env_map = env if env is not None else os.environ
    return ChannelConfig(*(_channel_value(key, settings, env_map) for key in CHANNEL_KEYS))


def build_notifiers(
    config: ChannelConfig, *, client_factory: ClientFactory | None = None
) -> list[Notifier]:
    """Construct the channels a :class:`ChannelConfig` describes (possibly none)."""

    notifiers: list[Notifier] = []
    if config.ntfy_url:
        notifiers.append(
            NtfyNotifier(
                config.ntfy_url, config.ntfy_token or None, client_factory=client_factory
            )
        )
    if config.webhook_url:
        notifiers.append(WebhookNotifier(config.webhook_url, client_factory=client_factory))
    if config.discord_webhook_url:
        notifiers.append(
            DiscordNotifier(config.discord_webhook_url, client_factory=client_factory)
        )
    return notifiers


def load_notifiers(
    *,
    settings: SettingsReader | None = None,
    env: Mapping[str, str] | None = None,
    client_factory: ClientFactory | None = None,
) -> list[Notifier]:
    """Build the configured channels once (possibly empty).

    One-shot convenience over :func:`resolve_channels` + :func:`build_notifiers`.
    Called with no arguments it reads the environment, exactly as it did before
    ADR-0054. The alert loop uses :class:`NotifierProvider` instead — a list
    built here is frozen at the moment of the call and would not see a later
    settings change.
    """

    return build_notifiers(
        resolve_channels(settings=settings, env=env), client_factory=client_factory
    )


class NotifierProvider:
    """Resolves the delivery channels on every call, memoised on their values.

    Handed to :class:`~kenny_server.alerting.AlertEngine` instead of a list, so
    a channel the operator adds, changes or clears from the dashboard applies
    to the very next dispatch — no restart, no re-composition. Construction is
    skipped whenever the resolved :class:`ChannelConfig` is unchanged, so the
    steady-state cost per alert is four settings lookups (dict reads) and a
    dataclass comparison.

    Not thread-safe and does not need to be: the server is a single process on
    one event loop, and ``current()`` never awaits.
    """

    def __init__(
        self,
        *,
        settings: SettingsReader | None = None,
        env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._env = env
        self._client_factory = client_factory
        self._config: ChannelConfig | None = None
        self._notifiers: list[Notifier] = []

    def current(self) -> list[Notifier]:
        """The channels configured right now (possibly none)."""

        try:
            config = resolve_channels(settings=self._settings, env=self._env)
        except Exception:  # noqa: BLE001 - delivery stays best-effort
            logger.exception("resolving the alert channels failed; keeping the last known set")
            return list(self._notifiers)
        if config != self._config:
            self._config = config
            self._notifiers = build_notifiers(config, client_factory=self._client_factory)
            logger.info(
                "alert delivery channels: %s",
                ", ".join(n.name for n in self._notifiers) or "none configured",
            )
        return list(self._notifiers)

    def __call__(self) -> list[Notifier]:
        return self.current()

"""Outbound operator notifications: ntfy, a generic JSON webhook, and Discord.

Alert delivery is best-effort by design (ADR-0029): a dead or slow
notification target must never stall or kill the evaluation loop, so every
``send`` swallows and logs transport errors. Channels are configured purely
via environment variables (``KENNY_NTFY_URL``, ``KENNY_NTFY_TOKEN``,
``KENNY_WEBHOOK_URL``); with none configured, alert evaluation still runs and
records history, it just pushes nothing. Discord adds ``KENNY_DISCORD_WEBHOOK_URL``.

``client_factory`` is injected so tests can supply an ``httpx.MockTransport``
(same pattern as ``webfilter.ExternalListCache``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

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
    # -- structured discriminator for auto-ticket rules (ADR-0053) ------------
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


def load_notifiers(*, client_factory: ClientFactory | None = None) -> list[Notifier]:
    """Build the configured channels from the environment (possibly empty)."""

    notifiers: list[Notifier] = []
    ntfy_url = os.environ.get("KENNY_NTFY_URL", "").strip()
    if ntfy_url:
        token = os.environ.get("KENNY_NTFY_TOKEN", "").strip() or None
        notifiers.append(NtfyNotifier(ntfy_url, token, client_factory=client_factory))
    webhook_url = os.environ.get("KENNY_WEBHOOK_URL", "").strip()
    if webhook_url:
        notifiers.append(WebhookNotifier(webhook_url, client_factory=client_factory))
    discord_url = os.environ.get("KENNY_DISCORD_WEBHOOK_URL", "").strip()
    if discord_url:
        notifiers.append(DiscordNotifier(discord_url, client_factory=client_factory))
    return notifiers

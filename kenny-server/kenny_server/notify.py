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

*Whether* a notification interrupts anyone is its ``route`` (ADR-0067):
``push`` goes to the human channels now, ``daily`` waits for the daily
summary, ``record`` stays in history and the ticket queue. The human channels
(ntfy, Discord) receive ``push`` only; the generic webhook is a machine
integration point and receives every route, labelled, to filter for itself.
Discord additionally returns the id of the message it posted, so a recovery can
edit that message in place instead of posting another one.

``client_factory`` is injected so tests can supply an ``httpx.MockTransport``
(same pattern as ``webfilter.ExternalListCache``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import quote

import httpx

logger = logging.getLogger("kenny.notify")

_SEND_TIMEOUT_S = 15.0

#: Every value ``Notification.route`` may take (ADR-0067).
ROUTES: tuple[str, ...] = ("push", "daily", "record")

ClientFactory = Callable[[], httpx.AsyncClient]


@dataclass
class Notification:
    """One operator-facing message, channel-agnostic."""

    title: str
    body: str
    priority: str = "default"  # ntfy scale: "low" | "default" | "high" | "urgent"
    tags: list[str] = field(default_factory=list)
    agent_id: str | None = None
    kind: str = "alert"  # "alert" | "recovery" | "change" | "digest" | "test"
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
    # Empty dict means "no per-section subject": the notification is about
    # the host rather than any section of it (offline, digest).
    sections: dict[str, str] = field(default_factory=dict)
    # Who this interrupts (ADR-0067): "push" reaches the human channels now,
    # "daily" is held for the daily summary, "record" only lands in history and
    # the ticket queue. Routing decides interruption; ticket_rules decides work.
    route: str = "push"
    # The body split per section -- {"section", "severity", "text"} -- for a
    # channel that renders and later edits each finding on its own (Discord).
    # Empty for producers whose body is not a list of section findings.
    items: list[dict[str, str]] = field(default_factory=list)


class Notifier(Protocol):
    """A delivery channel for :class:`Notification`."""

    name: str

    async def send(self, notification: Notification) -> str | None:
        """Deliver ``notification``; never raises.

        Returns ``None`` on delivery, or a short reason it failed — which only
        a caller that reports back to a person (the dashboard's test send)
        looks at; alert delivery stays best-effort either way.
        """
        ...


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

    async def _post(self, **kwargs: object) -> str | None:
        error, _resp = await self._request("POST", self._url, **kwargs)
        return error

    async def _request(
        self, method: str, url: str, **kwargs: object
    ) -> tuple[str | None, httpx.Response | None]:
        try:
            async with self._make_client() as client:
                resp = await client.request(method, url, timeout=_SEND_TIMEOUT_S, **kwargs)
            if resp.status_code >= 400:
                logger.warning("%s notify returned %s", self.name, resp.status_code)
                return f"HTTP {resp.status_code}", resp
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            logger.warning("%s notify failed: %s", self.name, exc)
            return str(exc) or type(exc).__name__, None
        return None, resp


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

    async def send(self, notification: Notification) -> str | None:
        headers = {
            "Title": notification.title,
            "Priority": notification.priority,
        }
        if notification.tags:
            headers["Tags"] = ",".join(notification.tags)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return await self._post(content=notification.body.encode("utf-8"), headers=headers)


class WebhookNotifier(_HttpNotifier):
    """POST a JSON payload to a generic operator-configured webhook URL."""

    name = "webhook"
    # A machine consumer: it gets every route, labelled, and filters itself.
    receives_all_routes = True

    async def send(self, notification: Notification) -> str | None:
        return await self._post(
            json={
                "kind": notification.kind,
                "route": notification.route,
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

# Discord embed colors (decimal), keyed by the severity a reader should take
# from the message -- not by ntfy priority, which conflated warn with recovery.
_DISCORD_COLORS = {
    "crit": 0xE74C3C,  # red
    "warn": 0xF1C40F,  # amber
    "resolved": 0x2ECC71,  # green
    "info": 0x95A5A6,  # grey
}
_DISCORD_MARKERS = {"crit": "\U0001F534", "warn": "\U0001F7E0", "resolved": "\u2705", "info": ""}


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` chars, replacing the tail with an ellipsis."""

    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def severity_of(notification: Notification) -> str:
    """What a reader should take from ``notification``: crit, warn, resolved or info."""

    if notification.kind == "recovery":
        return "resolved"
    if notification.priority in ("high", "urgent"):
        return "crit"
    if notification.priority == "low":
        return "info"
    return "warn"


def _public_base() -> str:
    """The dashboard origin a link in a chat message may point at, or ``""``.

    Only an explicitly configured ``KENNY_PUBLIC_URL`` counts: the localhost
    fallback ``urls.public_base_url`` uses for development is a dead link on
    the phone that reads the message.
    """

    return os.environ.get("KENNY_PUBLIC_URL", "").strip().rstrip("/")


def _duration(delta_s: float) -> str:
    minutes = max(0, int(delta_s // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def discord_doc(
    notification: Notification, *, at: datetime, base_url: str = ""
) -> dict[str, Any]:
    """The state of one Discord message, from which its embed is rendered.

    Persisted with the message id (``AlertStateStore.save_message``) so a later
    recovery re-renders the same message with some or all findings resolved,
    rather than composing a second one.
    """

    url = ""
    if base_url and notification.agent_id:
        url = f"{base_url}/#/fleet/{quote(notification.agent_id, safe='')}"
        worst = next(iter(notification.sections), "")
        if worst:
            url += f"?section={quote(worst, safe='')}"
    return {
        "title": notification.title,
        "severity": severity_of(notification),
        "items": [
            {
                "section": item.get("section", ""),
                "severity": item.get("severity", ""),
                "text": item.get("text", ""),
                "resolved_at": None,
            }
            for item in notification.items
        ],
        "body": notification.body,
        "url": url,
        "created_at": at.isoformat(),
        "resolved_at": None,
    }


def discord_embed(doc: dict[str, Any]) -> dict[str, Any]:
    """Render one embed from a :func:`discord_doc` state."""

    resolved_at = doc.get("resolved_at")
    severity = "resolved" if resolved_at else str(doc.get("severity") or "warn")
    marker = _DISCORD_MARKERS.get(severity, "")
    title = f"{marker} {doc.get('title', '')}".strip()
    items = doc.get("items") or []
    if items:
        lines = []
        for item in items:
            text = f"**{item['section']}**: {item['text']}" if item.get("text") else f"**{item['section']}**"
            if item.get("resolved_at"):
                lines.append(f"{_DISCORD_MARKERS['resolved']} ~~{text}~~")
            else:
                lines.append(f"{_DISCORD_MARKERS.get(item.get('severity', ''), '')} {text}".strip())
        description = "\n".join(lines)
    else:
        description = str(doc.get("body") or "")
    embed: dict[str, Any] = {
        "title": _truncate(title, _DISCORD_TITLE_LIMIT),
        "description": _truncate(description, _DISCORD_DESCRIPTION_LIMIT),
        "color": _DISCORD_COLORS.get(severity, _DISCORD_COLORS["warn"]),
    }
    if doc.get("created_at"):
        embed["timestamp"] = doc["created_at"]
    if doc.get("url"):
        embed["url"] = doc["url"]
    if resolved_at:
        end = datetime.fromisoformat(resolved_at)
        start = datetime.fromisoformat(doc["created_at"]) if doc.get("created_at") else end
        embed["fields"] = [
            {
                "name": "Resolved",
                "value": f"<t:{int(end.timestamp())}:R> · after "
                f"{_duration((end - start).total_seconds())}",
                "inline": False,
            }
        ]
    return embed


def resolve_doc(doc: dict[str, Any], sections: Iterable[str], *, at: datetime) -> bool:
    """Mark ``sections`` resolved in ``doc``; ``True`` if anything changed.

    The whole message counts as resolved once none of its findings is still
    open. A message without per-section items resolves as a whole.
    """

    wanted = set(sections)
    changed = False
    items = doc.get("items") or []
    for item in items:
        if item.get("resolved_at") is None and item.get("section") in wanted:
            item["resolved_at"] = at.isoformat()
            changed = True
    if doc.get("resolved_at") is None and (
        (items and all(i.get("resolved_at") for i in items)) or (not items and wanted)
    ):
        doc["resolved_at"] = at.isoformat()
        changed = True
    return changed


def doc_sections(doc: dict[str, Any]) -> set[str]:
    """The sections a message still reports as open."""

    return {i["section"] for i in doc.get("items") or [] if not i.get("resolved_at")}


class DiscordNotifier(_HttpNotifier):
    """POST a Discord webhook payload (embed) to a Discord channel webhook URL.

    Posts with ``?wait=true`` so Discord answers with the message it created;
    :meth:`send_tracked` hands its id back to the alert loop, and :meth:`edit`
    later rewrites that message (``PATCH .../messages/<id>``) when the
    condition resolves. A pushed alert is therefore one message that shows its
    current state, not an alert followed by a recovery.
    """

    name = "discord"

    def __init__(
        self,
        url: str,
        *,
        client_factory: ClientFactory | None = None,
        base_url: Callable[[], str] = _public_base,
    ) -> None:
        super().__init__(url, client_factory=client_factory)
        self._base_url = base_url

    def _endpoint(self, suffix: str = "") -> str:
        base, sep, query = self._url.partition("?")
        return f"{base.rstrip('/')}{suffix}{sep}{query}"

    async def send(self, notification: Notification) -> str | None:
        error, _message_id, _doc = await self.send_tracked(notification)
        return error

    async def send_tracked(
        self, notification: Notification, *, at: datetime | None = None
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """Post ``notification``; returns ``(error, message_id, doc)``."""

        doc = discord_doc(
            notification, at=at or datetime.now(timezone.utc), base_url=self._base_url()
        )
        url = self._endpoint()
        url += ("&" if "?" in url else "?") + "wait=true"
        error, resp = await self._request("POST", url, json={"embeds": [discord_embed(doc)]})
        message_id: str | None = None
        if error is None and resp is not None:
            try:
                message_id = str(resp.json().get("id") or "") or None
            except Exception:  # noqa: BLE001 - an unparseable answer only costs the edit
                message_id = None
        return error, message_id, doc

    async def edit(self, message_id: str, doc: dict[str, Any]) -> str | None:
        """Re-render an earlier message from ``doc``; ``None`` on success."""

        error, _resp = await self._request(
            "PATCH",
            self._endpoint(f"/messages/{quote(message_id, safe='')}"),
            json={"embeds": [discord_embed(doc)]},
        )
        return error


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


# POSSIBLY DEAD: no production caller remains — main.py wires NotifierProvider
# instead (ADR-0054). Only tests call this directly.
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

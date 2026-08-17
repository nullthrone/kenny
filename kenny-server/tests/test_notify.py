"""Tests for :mod:`kenny_server.notify` (ntfy + webhook channels).

Both channels are exercised against an ``httpx.MockTransport`` so no network
is touched; delivery failures must be swallowed (best-effort per ADR-0027).

The second half covers channel *configuration* (ADR-0054): a settings value
beating the environment, the environment still working alone, and a change
being visible to the very next resolution.
"""

from __future__ import annotations

import httpx
import pytest

from kenny_server.config import Settings
from kenny_server.notify import (
    CHANNEL_KEYS,
    ChannelConfig,
    Notification,
    NotifierProvider,
    NtfyNotifier,
    WebhookNotifier,
    load_notifiers,
    resolve_channels,
)


def _capture_factory(captured: list[httpx.Request], status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ntfy_posts_body_and_headers() -> None:
    captured: list[httpx.Request] = []
    notifier = NtfyNotifier(
        "https://ntfy.example/kenny", "tok", client_factory=_capture_factory(captured)
    )
    await notifier.send(
        Notification(
            title="pc1 health: crit",
            body="disk: warn -> crit (C: 96% full)",
            priority="high",
            tags=["rotating_light"],
            agent_id="pc1",
        )
    )
    assert len(captured) == 1
    req = captured[0]
    assert str(req.url) == "https://ntfy.example/kenny"
    assert req.headers["Title"] == "pc1 health: crit"
    assert req.headers["Priority"] == "high"
    assert req.headers["Tags"] == "rotating_light"
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.content == b"disk: warn -> crit (C: 96% full)"


async def test_ntfy_without_token_or_tags() -> None:
    captured: list[httpx.Request] = []
    notifier = NtfyNotifier("https://ntfy.example/kenny", client_factory=_capture_factory(captured))
    await notifier.send(Notification(title="t", body="b"))
    req = captured[0]
    assert "Authorization" not in req.headers
    assert "Tags" not in req.headers


async def test_webhook_posts_json_payload() -> None:
    captured: list[httpx.Request] = []
    notifier = WebhookNotifier("https://hook.example/x", client_factory=_capture_factory(captured))
    await notifier.send(
        Notification(
            title="pc1 is offline",
            body="No telemetry for 2.0h",
            priority="high",
            tags=["electric_plug"],
            agent_id="pc1",
            kind="alert",
            event_type="offline",
        )
    )
    import json

    payload = json.loads(captured[0].content)
    assert payload["kind"] == "alert"
    assert payload["title"] == "pc1 is offline"
    assert payload["body"] == "No telemetry for 2.0h"
    assert payload["priority"] == "high"
    assert payload["tags"] == ["electric_plug"]
    assert payload["agent_id"] == "pc1"
    assert payload["event_type"] == "offline"
    assert payload["sections"] == {}
    assert payload["at"]


async def test_webhook_payload_carries_the_event_discriminator() -> None:
    """event_type/sections reach the webhook payload, so an external
    consumer can filter without parsing the free-text body."""

    captured: list[httpx.Request] = []
    notifier = WebhookNotifier("https://hook.example/x", client_factory=_capture_factory(captured))
    await notifier.send(
        Notification(
            title="pc1 health: crit",
            body="disk: warn -> crit",
            agent_id="pc1",
            kind="alert",
            event_type="health",
            sections={"disk": "crit"},
        )
    )
    import json

    payload = json.loads(captured[0].content)
    assert payload["event_type"] == "health"
    assert payload["sections"] == {"disk": "crit"}


async def test_send_is_best_effort_on_http_error() -> None:
    captured: list[httpx.Request] = []
    notifier = NtfyNotifier(
        "https://ntfy.example/kenny", client_factory=_capture_factory(captured, status_code=500)
    )
    await notifier.send(Notification(title="t", body="b"))  # must not raise
    assert len(captured) == 1


async def test_send_is_best_effort_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    notifier = NtfyNotifier(
        "https://ntfy.example/kenny",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await notifier.send(Notification(title="t", body="b"))  # must not raise


def test_load_notifiers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KENNY_NTFY_URL", raising=False)
    monkeypatch.delenv("KENNY_NTFY_TOKEN", raising=False)
    monkeypatch.delenv("KENNY_WEBHOOK_URL", raising=False)
    assert load_notifiers() == []

    monkeypatch.setenv("KENNY_NTFY_URL", "https://ntfy.example/kenny")
    monkeypatch.setenv("KENNY_WEBHOOK_URL", "https://hook.example/x")
    notifiers = load_notifiers()
    assert [n.name for n in notifiers] == ["ntfy", "webhook"]


# -- channel configuration: settings over env, live (ADR-0054) ----------------
#
# The real ``Settings`` resolver is used rather than a stub, so these tests fail
# if either half of the seam moves: the precedence config.py implements, and the
# keys notify.py reads.


class _MemSettingsStore:
    """In-memory stand-in for SettingsStore."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def all(self) -> dict[str, str]:
        return dict(self.data)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> bool:
        existed = key in self.data
        self.data.pop(key, None)
        return existed


def _settings(env: dict[str, str] | None = None) -> Settings:
    # apply_hooks disabled so these never touch global logging state.
    return Settings(_MemSettingsStore(), env=env or {}, apply_hooks={})


async def _post_target(notifier, captured: list[httpx.Request]) -> str:
    """Deliver one notification and report where it actually went."""

    await notifier.send(Notification(title="t", body="b"))
    return str(captured[-1].url)


async def test_a_settings_value_overrides_the_environment() -> None:
    settings = _settings(env={"KENNY_NTFY_URL": "https://ntfy.example/from-env"})
    await settings.set("KENNY_NTFY_URL", "https://ntfy.example/from-dashboard")

    captured: list[httpx.Request] = []
    provider = NotifierProvider(settings=settings, client_factory=_capture_factory(captured))
    notifiers = provider.current()

    assert [n.name for n in notifiers] == ["ntfy"]
    # Not just "a channel exists" -- it posts to the dashboard's URL, not the
    # environment's, which is the only difference that matters in production.
    assert await _post_target(notifiers[0], captured) == "https://ntfy.example/from-dashboard"


async def test_the_environment_still_configures_a_channel_with_no_setting() -> None:
    """An existing env-configured deployment keeps working untouched."""

    settings = _settings(env={"KENNY_WEBHOOK_URL": "https://hook.example/from-env"})
    captured: list[httpx.Request] = []

    with_settings = NotifierProvider(
        settings=settings, client_factory=_capture_factory(captured)
    ).current()
    assert [n.name for n in with_settings] == ["webhook"]
    assert await _post_target(with_settings[0], captured) == "https://hook.example/from-env"

    # And with no settings layer wired at all (direct construction, tests),
    # the environment is read directly -- the pre-ADR-0054 behaviour.
    without_settings = NotifierProvider(
        env={"KENNY_WEBHOOK_URL": "https://hook.example/from-env"},
        client_factory=_capture_factory(captured),
    ).current()
    assert [n.name for n in without_settings] == ["webhook"]
    assert await _post_target(without_settings[0], captured) == "https://hook.example/from-env"


async def test_a_setting_change_is_visible_to_the_next_resolution() -> None:
    """No restart, no re-composition: the same provider object sees the change."""

    settings = _settings()
    captured: list[httpx.Request] = []
    provider = NotifierProvider(settings=settings, client_factory=_capture_factory(captured))
    assert provider.current() == []

    await settings.set("KENNY_WEBHOOK_URL", "https://hook.example/one")
    first = provider.current()
    assert [n.name for n in first] == ["webhook"]
    assert await _post_target(first[0], captured) == "https://hook.example/one"

    await settings.set("KENNY_WEBHOOK_URL", "https://hook.example/two")
    second = provider.current()
    assert await _post_target(second[0], captured) == "https://hook.example/two"

    await settings.set("KENNY_DISCORD_WEBHOOK_URL", "https://discord.example/hook")
    assert [n.name for n in provider.current()] == ["webhook", "discord"]

    # Resetting drops the override and falls back to env/default (here: off).
    await settings.reset("KENNY_WEBHOOK_URL")
    assert [n.name for n in provider.current()] == ["discord"]


async def test_clearing_a_channel_does_not_fall_back_to_the_environment() -> None:
    """An emptied field means "off", not "whatever the env still holds".

    Falling back here would be the silent failure this whole surface exists to
    avoid: the operator sees an empty field and still gets pushes.
    """

    settings = _settings(env={"KENNY_NTFY_URL": "https://ntfy.example/from-env"})
    provider = NotifierProvider(settings=settings)
    assert [n.name for n in provider.current()] == ["ntfy"]

    await settings.set("KENNY_NTFY_URL", "")
    assert provider.current() == []


async def test_all_four_channels_resolve_from_settings() -> None:
    settings = _settings()
    for key in CHANNEL_KEYS:
        await settings.set(key, "tok" if key.endswith("_TOKEN") else f"https://x.example/{key}")
    provider = NotifierProvider(settings=settings)
    assert [n.name for n in provider.current()] == ["ntfy", "webhook", "discord"]


async def test_the_ntfy_token_reaches_the_built_channel() -> None:
    settings = _settings()
    await settings.set("KENNY_NTFY_URL", "https://ntfy.example/topic")
    await settings.set("KENNY_NTFY_TOKEN", "tk_from_dashboard")
    captured: list[httpx.Request] = []
    notifier = NotifierProvider(
        settings=settings, client_factory=_capture_factory(captured)
    ).current()[0]

    await notifier.send(Notification(title="t", body="b"))
    assert captured[-1].headers["Authorization"] == "Bearer tk_from_dashboard"


async def test_channels_are_rebuilt_only_when_a_value_changes() -> None:
    """Memoised against the resolved values -- this runs on every alert."""

    settings = _settings()
    await settings.set("KENNY_WEBHOOK_URL", "https://hook.example/x")
    provider = NotifierProvider(settings=settings)

    first = provider.current()
    assert provider.current()[0] is first[0]  # same object, not rebuilt

    await settings.set("KENNY_NTFY_TOKEN", "irrelevant-to-webhook")
    rebuilt = provider.current()
    # A change anywhere in the config rebuilds the set; the point is only that
    # an unchanged config does not.
    assert rebuilt[0] is not first[0]
    assert provider.current()[0] is rebuilt[0]


def test_resolve_channels_reads_exactly_the_catalogued_keys() -> None:
    env = {key: f"value-{key}" for key in CHANNEL_KEYS}
    assert resolve_channels(env=env) == ChannelConfig(
        ntfy_url="value-KENNY_NTFY_URL",
        ntfy_token="value-KENNY_NTFY_TOKEN",
        webhook_url="value-KENNY_WEBHOOK_URL",
        discord_webhook_url="value-KENNY_DISCORD_WEBHOOK_URL",
    )
    assert resolve_channels(env={}) == ChannelConfig()
    # Whitespace-only configuration is not configuration.
    assert resolve_channels(env={"KENNY_NTFY_URL": "  "}) == ChannelConfig()


async def test_a_settings_lookup_that_raises_never_kills_the_provider() -> None:
    class _Exploding:
        def __init__(self) -> None:
            self.explode = False

        def get(self, key: str) -> str:
            if self.explode:
                raise RuntimeError("settings are on fire")
            return "https://hook.example/x" if key == "KENNY_WEBHOOK_URL" else ""

    settings = _Exploding()
    # env pinned to {} so the fallback is deterministic, not the test host's.
    provider = NotifierProvider(settings=settings, env={})
    assert [n.name for n in provider.current()] == ["webhook"]

    settings.explode = True
    # Falls back to the environment per key rather than raising into the caller.
    assert provider.current() == []

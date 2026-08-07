"""Tests for the Discord channel in :mod:`kenny_server.notify`.

Exercised against an ``httpx.MockTransport`` so no network is touched;
delivery failures must be swallowed (best-effort per ADR-0027).
"""

from __future__ import annotations

import json

import httpx
import pytest

from kenny_server.notify import (
    Notification,
    DiscordNotifier,
    load_notifiers,
)


def _capture_factory(captured: list[httpx.Request], status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_discord_posts_embed_payload() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook", client_factory=_capture_factory(captured)
    )
    await notifier.send(
        Notification(
            title="pc1 health: crit",
            body="disk: warn -> crit (C: 96% full)",
            priority="high",
            tags=["rotating_light"],
            agent_id="pc1",
            kind="alert",
        )
    )
    assert len(captured) == 1
    req = captured[0]
    assert str(req.url) == "https://discord.example/webhook"
    payload = json.loads(req.content)
    assert list(payload.keys()) == ["embeds"]
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == "pc1 health: crit"
    assert embed["description"] == "disk: warn -> crit (C: 96% full)"
    assert isinstance(embed["color"], int)
    assert {"name": "kind", "value": "alert", "inline": True} in embed["fields"]
    assert {"name": "agent_id", "value": "pc1", "inline": True} in embed["fields"]


async def test_discord_without_agent_id_omits_field() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook", client_factory=_capture_factory(captured)
    )
    await notifier.send(Notification(title="t", body="b"))
    embed = json.loads(captured[0].content)["embeds"][0]
    assert all(f["name"] != "agent_id" for f in embed["fields"])
    assert {"name": "kind", "value": "alert", "inline": True} in embed["fields"]


async def test_discord_priority_maps_to_distinct_colors() -> None:
    colors: dict[str, int] = {}
    for priority in ("low", "default", "high", "urgent"):
        captured: list[httpx.Request] = []
        notifier = DiscordNotifier(
            "https://discord.example/webhook", client_factory=_capture_factory(captured)
        )
        await notifier.send(Notification(title="t", body="b", priority=priority))
        embed = json.loads(captured[0].content)["embeds"][0]
        colors[priority] = embed["color"]
    assert len(set(colors.values())) == len(colors)


async def test_discord_truncates_overlong_title_and_description() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook", client_factory=_capture_factory(captured)
    )
    long_title = "T" * 500
    long_body = "B" * 5000
    await notifier.send(Notification(title=long_title, body=long_body))
    embed = json.loads(captured[0].content)["embeds"][0]
    assert len(embed["title"]) == 256
    assert embed["title"].endswith("…")
    assert len(embed["description"]) == 4096
    assert embed["description"].endswith("…")


async def test_discord_send_is_best_effort_on_http_error() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook",
        client_factory=_capture_factory(captured, status_code=500),
    )
    await notifier.send(Notification(title="t", body="b"))  # must not raise
    assert len(captured) == 1


async def test_discord_send_is_best_effort_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    notifier = DiscordNotifier(
        "https://discord.example/webhook",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await notifier.send(Notification(title="t", body="b"))  # must not raise


def test_load_notifiers_picks_up_discord_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KENNY_NTFY_URL", raising=False)
    monkeypatch.delenv("KENNY_NTFY_TOKEN", raising=False)
    monkeypatch.delenv("KENNY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("KENNY_DISCORD_WEBHOOK_URL", raising=False)
    assert load_notifiers() == []

    monkeypatch.setenv("KENNY_DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    notifiers = load_notifiers()
    assert [n.name for n in notifiers] == ["discord"]


def test_load_notifiers_omits_discord_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KENNY_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("KENNY_NTFY_URL", "https://ntfy.example/kenny")
    notifiers = load_notifiers()
    assert "discord" not in [n.name for n in notifiers]

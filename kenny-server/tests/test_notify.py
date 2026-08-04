"""Tests for :mod:`kenny_server.notify` (ntfy + webhook channels).

Both channels are exercised against an ``httpx.MockTransport`` so no network
is touched; delivery failures must be swallowed (best-effort per ADR-0029).
"""

from __future__ import annotations

import httpx
import pytest

from kenny_server.notify import (
    Notification,
    NtfyNotifier,
    WebhookNotifier,
    load_notifiers,
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
    """ADR-0053: event_type/sections reach the webhook payload, so an external
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

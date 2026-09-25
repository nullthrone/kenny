"""Tests for the Discord channel in :mod:`kenny_server.notify`.

Exercised against an ``httpx.MockTransport`` so no network is touched;
delivery failures must be swallowed (best-effort per ADR-0027).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kenny_server.notify import (
    DiscordNotifier,
    Notification,
    doc_sections,
    load_notifiers,
    resolve_doc,
    severity_of,
)


def _capture_factory(
    captured: list[httpx.Request], status_code: int = 200, message_id: str = "m-1"
):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, json={"id": message_id})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


AT = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _crit_with_warn() -> Notification:
    return Notification(
        title="pc1: disk: C: 96% full (>=95%)",
        body="[CRIT] disk: C: 96% full (>=95%)\n[WARN] memory: 88% used",
        priority="high",
        agent_id="pc1",
        kind="alert",
        event_type="health",
        sections={"disk": "crit", "memory": "warn"},
        items=[
            {"section": "disk", "severity": "crit", "text": "C: 96% full (>=95%)"},
            {"section": "memory", "severity": "warn", "text": "88% used"},
        ],
    )


async def test_discord_posts_embed_payload_and_waits_for_the_message() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook",
        client_factory=_capture_factory(captured),
        base_url=lambda: "",
    )
    error, message_id, doc = await notifier.send_tracked(_crit_with_warn(), at=AT)
    assert error is None
    assert message_id == "m-1"
    assert len(captured) == 1
    req = captured[0]
    # ``wait=true`` is what makes Discord answer with the message it created.
    assert str(req.url) == "https://discord.example/webhook?wait=true"
    payload = json.loads(req.content)
    assert list(payload.keys()) == ["embeds"]
    embed = payload["embeds"][0]
    assert embed["title"] == "\U0001F534 pc1: disk: C: 96% full (>=95%)"
    assert embed["description"].splitlines() == [
        "\U0001F534 **disk**: C: 96% full (>=95%)",
        "\U0001F7E0 **memory**: 88% used",
    ]
    assert embed["color"] == 0xE74C3C
    assert embed["timestamp"] == AT.isoformat()
    # Bookkeeping fields told the reader nothing; they are gone.
    assert "fields" not in embed
    assert "url" not in embed
    assert doc["items"][0]["resolved_at"] is None


async def test_discord_links_the_host_page_when_a_public_url_is_set() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook",
        client_factory=_capture_factory(captured),
        base_url=lambda: "https://kenny.example",
    )
    await notifier.send(_crit_with_warn())
    embed = json.loads(captured[0].content)["embeds"][0]
    assert embed["url"] == "https://kenny.example/#/fleet/pc1?section=disk"


async def test_discord_keeps_an_existing_query_on_the_webhook_url() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook?thread_id=42",
        client_factory=_capture_factory(captured),
        base_url=lambda: "",
    )
    _error, message_id, doc = await notifier.send_tracked(_crit_with_warn(), at=AT)
    assert str(captured[0].url) == "https://discord.example/webhook?thread_id=42&wait=true"
    await notifier.edit(message_id or "", doc)
    assert captured[1].method == "PATCH"
    assert str(captured[1].url) == "https://discord.example/webhook/messages/m-1?thread_id=42"


async def test_a_body_without_items_renders_as_is() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook", client_factory=_capture_factory(captured)
    )
    await notifier.send(Notification(title="t", body="b"))
    embed = json.loads(captured[0].content)["embeds"][0]
    assert embed["description"] == "b"


async def test_discord_colors_follow_severity_not_priority() -> None:
    colors: dict[str, int] = {}
    cases = {
        "crit": Notification(title="t", body="b", priority="high"),
        "warn": Notification(title="t", body="b", priority="default"),
        "resolved": Notification(title="t", body="b", kind="recovery", priority="low"),
        "info": Notification(title="t", body="b", kind="digest", priority="low"),
    }
    for name, note in cases.items():
        captured: list[httpx.Request] = []
        notifier = DiscordNotifier(
            "https://discord.example/webhook", client_factory=_capture_factory(captured)
        )
        await notifier.send(note)
        colors[name] = json.loads(captured[0].content)["embeds"][0]["color"]
    assert len(set(colors.values())) == len(colors)
    assert colors["crit"] == 0xE74C3C and colors["resolved"] == 0x2ECC71
    assert severity_of(cases["warn"]) == "warn"


async def test_a_partial_then_full_resolve_rewrites_one_message() -> None:
    captured: list[httpx.Request] = []
    notifier = DiscordNotifier(
        "https://discord.example/webhook",
        client_factory=_capture_factory(captured),
        base_url=lambda: "",
    )
    _error, message_id, doc = await notifier.send_tracked(_crit_with_warn(), at=AT)

    later = AT + timedelta(hours=3, minutes=20)
    assert resolve_doc(doc, {"disk"}, at=later)
    assert doc_sections(doc) == {"memory"}
    await notifier.edit(message_id or "", doc)
    partial = json.loads(captured[-1].content)["embeds"][0]
    assert partial["color"] == 0xE74C3C  # memory is still open
    assert "\u2705 ~~**disk**: C: 96% full (>=95%)~~" in partial["description"]
    assert "fields" not in partial

    assert resolve_doc(doc, {"memory"}, at=later)
    await notifier.edit(message_id or "", doc)
    final = json.loads(captured[-1].content)["embeds"][0]
    assert final["color"] == 0x2ECC71
    assert final["title"].startswith("\u2705 ")
    assert final["fields"][0]["name"] == "Resolved"
    assert final["fields"][0]["value"].endswith("after 3h 20m")
    # Nothing but edits after the first post.
    assert [r.method for r in captured] == ["POST", "PATCH", "PATCH"]
    # Resolving what is already resolved changes nothing.
    assert not resolve_doc(doc, {"disk"}, at=later)


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

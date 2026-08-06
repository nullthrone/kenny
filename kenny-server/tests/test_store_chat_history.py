"""``ChatHistoryStore`` round-trip tests (ADR-0025)."""

from __future__ import annotations

import pytest

from kenny_server.store import ChatHistoryStore


@pytest.mark.asyncio
async def test_chat_history_save_and_get_round_trips(tmp_path) -> None:
    store = ChatHistoryStore(str(tmp_path / "chat.sqlite"))
    await store.connect()
    try:
        messages = [
            {"role": "user", "content": "install git"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "On it."},
                    {"type": "tool_use", "id": "tu1", "name": "screen_capture", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "aGVsbG8=",
                                },
                            },
                            {"type": "text", "text": "screen_capture (png)"},
                        ],
                    }
                ],
            },
        ]
        await store.save(id="c1", title="Install git", agent_id="dev", messages=messages)
        row = await store.get("c1")
        assert row is not None
        assert row["id"] == "c1"
        assert row["title"] == "Install git"
        assert row["agent_id"] == "dev"
        # The base64 image blob round-trips losslessly through the JSON column.
        assert row["messages"] == messages
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_chat_history_get_missing_returns_none(tmp_path) -> None:
    store = ChatHistoryStore(str(tmp_path / "chat.sqlite"))
    await store.connect()
    try:
        assert await store.get("nope") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_chat_history_list_excludes_messages_and_orders_newest_first(tmp_path) -> None:
    store = ChatHistoryStore(str(tmp_path / "chat.sqlite"))
    await store.connect()
    try:
        await store.save(id="a", title="First", agent_id=None, messages=[{"role": "user", "content": "hi"}])
        await store.save(id="b", title="Second", agent_id="dev", messages=[{"role": "user", "content": "hi"}])
        # Re-save "a" so it becomes the most recently updated.
        await store.save(id="a", title="First (renamed attempt)", agent_id=None, messages=[])
        rows = await store.list()
        assert [r["id"] for r in rows] == ["a", "b"]
        for r in rows:
            assert "messages" not in r
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_chat_history_save_preserves_title_and_created_at_on_resave(tmp_path) -> None:
    store = ChatHistoryStore(str(tmp_path / "chat.sqlite"))
    await store.connect()
    try:
        await store.save(id="c1", title="Original title", agent_id=None, messages=[])
        first = await store.get("c1")
        assert first is not None
        await store.save(id="c1", title="Different title", agent_id="dev", messages=[{"role": "user", "content": "x"}])
        second = await store.get("c1")
        assert second is not None
        # Title and created_at are set once, at creation, never overwritten.
        assert second["title"] == "Original title"
        assert second["created_at"] == first["created_at"]
        # agent_id and messages DO refresh on every save.
        assert second["agent_id"] == "dev"
        assert second["messages"] == [{"role": "user", "content": "x"}]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_chat_history_delete(tmp_path) -> None:
    store = ChatHistoryStore(str(tmp_path / "chat.sqlite"))
    await store.connect()
    try:
        await store.save(id="c1", title="t", agent_id=None, messages=[])
        assert await store.delete("c1") is True
        assert await store.get("c1") is None
        assert await store.delete("c1") is False
    finally:
        await store.close()

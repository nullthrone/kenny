"""Tests for :mod:`kenny_server.logging_config`.

Covers ``configure_logging`` idempotency, ``StoreLogHandler`` enqueueing (and
its drop-on-full + drain-feedback guards), and ``drain_log_queue`` persisting a
record into :class:`~kenny_server.store.EventStore`.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kenny_server import logging_config
from kenny_server.logging_config import (
    StoreLogHandler,
    configure_logging,
    drain_log_queue,
)
from kenny_server.store import EventStore


def test_configure_logging_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(logging_config, "_configured", False)
    calls = {"n": 0}
    real = logging_config.logging.config.dictConfig

    def counting(cfg):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return real(cfg)

    monkeypatch.setattr(logging_config.logging.config, "dictConfig", counting)
    configure_logging()
    configure_logging()
    assert calls["n"] == 1


def test_store_log_handler_enqueues() -> None:
    handler = StoreLogHandler()
    record = logging.LogRecord(
        name="kenny.tunnel",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="agent %s connected",
        args=("dev",),
        exc_info=None,
    )
    handler.emit(record)
    payload = handler.queue.get_nowait()
    assert payload["level"] == "info"
    assert payload["target"] == "kenny.tunnel"
    assert payload["message"] == "agent dev connected"
    assert "at" in payload


def test_store_log_handler_skips_drain_logger() -> None:
    handler = StoreLogHandler()
    record = logging.LogRecord(
        name="kenny.events.drain",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    assert handler.queue.empty()


def test_store_log_handler_drops_when_full() -> None:
    handler = StoreLogHandler(maxsize=1)
    handler.queue.put_nowait({"filler": True})
    record = logging.LogRecord(
        name="kenny.tunnel",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="dropped",
        args=(),
        exc_info=None,
    )
    handler.emit(record)  # must not raise despite a full queue
    assert handler.queue.qsize() == 1


async def test_drain_log_queue_persists(tmp_path) -> None:
    es = EventStore(db_path=str(tmp_path / "drain.sqlite"))
    await es.connect()
    handler = StoreLogHandler()
    task = asyncio.create_task(drain_log_queue(handler.queue, es))
    try:
        handler.queue.put_nowait(
            {
                "at": "2026-06-04T18:00:01Z",
                "level": "info",
                "target": "kenny.tunnel",
                "message": "agent dev connected",
            }
        )
        await handler.queue.join()
        rows = await es.query(kind="log")
        assert len(rows) == 1
        assert rows[0]["source"] == "server"
        assert rows[0]["message"] == "agent dev connected"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await es.close()

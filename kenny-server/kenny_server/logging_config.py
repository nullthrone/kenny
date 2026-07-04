"""Server logging configuration and a queue-backed handler that persists logs.

:func:`configure_logging` installs a timestamped formatter across the root,
``uvicorn``, and ``kenny`` loggers (idempotent). :class:`StoreLogHandler`
captures server-side log records onto a bounded queue; :func:`drain_log_queue`
runs as a background task and writes them into :class:`~.store.EventStore`
(``source='server'``). Drops on a full queue rather than blocking the event loop.

See ADR 0007 for the SQLite persistence rationale.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
from datetime import datetime, timezone
from typing import Any

# Records emitted by the drain itself live under this logger name; skip them so a
# failing insert can't feed its own error back onto the queue.
_DRAIN_LOGGER_PREFIX = "kenny.events"

_configured = False

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """Install the kenny logging config. Idempotent across repeated calls."""

    global _configured
    if _configured:
        return

    level = os.environ.get("KENNY_LOG_LEVEL", "INFO").upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"kenny": {"format": _LOG_FORMAT}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "kenny",
                }
            },
            "root": {"level": level, "handlers": ["console"]},
            "loggers": {
                "uvicorn": {"level": level, "handlers": ["console"], "propagate": False},
                "uvicorn.access": {"level": level, "handlers": ["console"], "propagate": False},
                "uvicorn.error": {"level": level, "handlers": ["console"], "propagate": False},
                # `kenny` keeps no handler of its own and propagates to root: this
                # avoids double-logging via the root console handler and lets test
                # fixtures (pytest `caplog`, attached at root) capture kenny records.
                "kenny": {"level": level},
            },
        }
    )
    _configured = True


# Loggers whose level tracks KENNY_LOG_LEVEL (see configure_logging).
_LEVELLED_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "kenny")


def apply_log_level(level: str) -> None:
    """Set the root + uvicorn + kenny logger levels immediately.

    Used as the ``KENNY_LOG_LEVEL`` apply-hook so a level change from the
    settings UI takes effect without a restart. Unknown levels are ignored.
    """

    name = str(level).strip().upper()
    if name not in logging.getLevelNamesMapping():
        logging.getLogger("kenny.config").warning("ignoring unknown log level %r", level)
        return
    logging.getLogger().setLevel(name)
    for logger_name in _LEVELLED_LOGGERS:
        logging.getLogger(logger_name).setLevel(name)


class StoreLogHandler(logging.Handler):
    """A ``logging.Handler`` that enqueues server records for async persistence.

    Safe to construct without a running event loop. The queue may be passed in
    or created lazily on first use; :func:`drain_log_queue` consumes it.
    """

    def __init__(self, queue: asyncio.Queue[dict[str, Any]] | None = None, maxsize: int = 1000) -> None:
        super().__init__()
        self.queue: asyncio.Queue[dict[str, Any]] = queue or asyncio.Queue(maxsize=maxsize)

    def emit(self, record: logging.LogRecord) -> None:
        # Never persist the drain's own records (avoid feedback loops).
        if record.name.startswith(_DRAIN_LOGGER_PREFIX):
            return
        try:
            payload = {
                "at": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname.lower(),
                "target": record.name,
                "message": record.getMessage(),
            }
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # drop on backpressure rather than block the event loop
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


async def drain_log_queue(queue: asyncio.Queue[dict[str, Any]], event_store: Any) -> None:
    """Persist queued log records into ``event_store`` until cancelled."""

    drain_logger = logging.getLogger("kenny.events.drain")
    while True:
        record = await queue.get()
        try:
            await event_store.insert_log(source="server", **record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the drain must never die
            drain_logger.debug("failed to persist log record: %s", exc)
        finally:
            queue.task_done()

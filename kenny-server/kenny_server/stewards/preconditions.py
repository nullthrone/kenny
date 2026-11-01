"""Named server-side checks a steward spec can declare (``StewardSpec.preconditions``).

A precondition reads the server's own records and says whether a run is worth
starting on a host before any model is called
(:meth:`~kenny_server.stewards.runner.StewardRunner.precondition_refusal`). It
returns ``None`` to proceed, or a *stable* reason to skip: the reason is shown to
people and compared to tell one skip from another, so it never carries a
measurement.

A precondition fails open on missing evidence (no snapshot, an old one, nothing
readable in it) because it only ever saves a run; the steward's own gate is what
bounds what a run may do.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..health_rules import parse_ts

__all__ = [
    "DEFAULT_MIN_PERCENT_USED",
    "DISK_PRESSURE",
    "DISK_PRESSURE_SKIP",
    "STALE_AFTER",
    "disk_pressure",
    "register",
]

#: The name a spec declares (``preconditions=("disk_pressure",)``).
DISK_PRESSURE = "disk_pressure"

#: Why a run is skipped when no volume is as full as the threshold.
DISK_PRESSURE_SKIP = "no volume at or above the usage threshold"

#: The threshold when the steward's ``min_percent_used`` parameter is unset.
DEFAULT_MIN_PERCENT_USED = 80

#: A host's latest snapshot older than this says nothing about the disk now.
STALE_AFTER = timedelta(hours=12)


def _threshold(params: Mapping[str, Any]) -> int:
    value = params.get("min_percent_used")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 99:
        return DEFAULT_MIN_PERCENT_USED
    return value


def _fullest(snapshot: Any) -> float | None:
    """The highest ``disk.volumes[].percent_used`` in ``snapshot``, or ``None`` if none is readable."""

    disk = snapshot.get("disk") if isinstance(snapshot, Mapping) else None
    volumes = disk.get("volumes") if isinstance(disk, Mapping) else None
    if not isinstance(volumes, list):
        return None
    readings = [
        float(v["percent_used"])
        for v in volumes
        if isinstance(v, Mapping)
        and isinstance(v.get("percent_used"), (int, float))
        and not isinstance(v.get("percent_used"), bool)
        and math.isfinite(v["percent_used"])
    ]
    return max(readings) if readings else None


async def disk_pressure(
    store: Any,
    host_id: str | None,
    params: Mapping[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
) -> str | None:
    """:data:`DISK_PRESSURE_SKIP` when no volume of ``host_id`` is at the threshold, else ``None``.

    Reads the host's latest stored snapshot (``store.latest``). A host with no
    snapshot, one older than :data:`STALE_AFTER`, or one with no readable volume
    proceeds: the check saves a run, it never blocks one on a guess.
    """

    if not host_id:
        return None
    record = await store.latest(host_id)
    if not record:
        return None
    stamp = parse_ts(record.get("received_at")) or parse_ts(record.get("collected_at"))
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    current = now() if now is not None else datetime.now(timezone.utc)
    if current - stamp > STALE_AFTER:
        return None
    fullest = _fullest(record.get("snapshot"))
    if fullest is None:
        return None
    return DISK_PRESSURE_SKIP if fullest < _threshold(params) else None


def register(
    runner: Any, store: Any, *, now: Callable[[], datetime] | None = None
) -> None:
    """Register every precondition this module defines on ``runner``, reading ``store``."""

    async def _disk_pressure(host_id: str | None, params: Mapping[str, Any]) -> str | None:
        return await disk_pressure(store, host_id, params, now=now)

    runner.register_precondition(DISK_PRESSURE, _disk_pressure)

"""When a host was reachable: availability segments over a window.

:func:`compute` is pure. It turns the presence record (``store.PresenceStore``:
tunnel sessions, server runs, reboots) plus telemetry arrival times into
contiguous ``online``/``offline``/``unknown`` segments covering the window.

* From the ledger epoch on (the first recorded server run): inside a tunnel
  session is ``online``; inside a server run without a session is ``offline``;
  outside every server run is ``unknown`` -- the server was not there to see.
* Before the epoch, presence is reconstructed from snapshot arrival times and
  every segment is ``approx``: consecutive pushes at most ``offline_after_secs``
  apart are online between them, a longer gap is offline, and after the last
  push the host counts as online for ``offline_after_secs`` and offline after.
  ``offline_after_secs`` is the alert engine's offline threshold
  (``KENNY_ALERT_OFFLINE_AFTER_SECS``), so the chart and alerting agree on what
  offline means.
* Before a host's first known snapshot or session it is ``unknown``: it was not
  enrolled yet.

:func:`buckets` folds segments into fixed-width cells for a compact chart, and
:func:`summarize` shapes a result for chat (the ``agent_availability`` tool).
:func:`load_many` is the thin I/O shell that reads the stores in batch and calls
:func:`compute` once per host.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .alerting import DEFAULT_OFFLINE_AFTER_S

ONLINE = "online"
OFFLINE = "offline"
UNKNOWN = "unknown"

#: Days an availability window may span (the telemetry retention default).
MAX_DAYS = 30

#: Shortest offline/unknown span :func:`summarize` lists individually.
MIN_SPAN_SECS = 300
MAX_OUTAGES = 50
MAX_UNKNOWN_SPANS = 20
MAX_BOOTS = 50

TimeLike = str | datetime


def _parse(value: TimeLike) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dt(value: TimeLike) -> datetime:
    # Whole seconds: every boundary on one grid, so no segment is shorter than
    # the precision it is reported at.
    return _parse(value).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _union(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping/adjacent intervals; drop empty ones."""

    out: list[tuple[datetime, datetime]] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _inside(intervals: list[tuple[datetime, datetime]], starts: list[datetime], t: datetime) -> bool:
    i = bisect_right(starts, t) - 1
    return i >= 0 and t < intervals[i][1]


def offline_after_secs(settings: Any) -> int:
    """The offline threshold, read the way the alert engine reads it (live)."""

    value = settings.get("KENNY_ALERT_OFFLINE_AFTER_SECS") if settings is not None else None
    return int(value) if value is not None else DEFAULT_OFFLINE_AFTER_S


def compute(
    *,
    sessions: Iterable[Mapping[str, Any]],
    runs: Iterable[Mapping[str, Any]],
    received_times: Iterable[TimeLike],
    boots: Iterable[TimeLike],
    first_seen: TimeLike | None,
    window_start: TimeLike,
    window_end: TimeLike,
    ledger_epoch: TimeLike | None,
    offline_after_secs: int,
    now: TimeLike,
) -> dict[str, Any]:
    """Availability of one host over ``[window_start, min(window_end, now)]``.

    ``sessions`` carry ``connected_at``/``disconnected_at`` (None while open,
    which extends to ``now``). ``runs`` carry ``started_at``/``last_alive_at``
    and an optional ``current`` flag: the run of the reading process, extended to
    ``now`` because its ``last_alive_at`` trails by up to one touch interval.
    """

    now_dt = _dt(now)
    start = _dt(window_start)
    end = min(_dt(window_end), now_dt)
    if end < start:
        end = start
    epoch = _dt(ledger_epoch) if ledger_epoch is not None else None
    first = _dt(first_seen) if first_seen is not None else None
    threshold = timedelta(seconds=max(0, int(offline_after_secs)))

    online_iv = _union(
        (
            _dt(s["connected_at"]),
            _dt(s["disconnected_at"]) if s.get("disconnected_at") else now_dt,
        )
        for s in sessions
    )
    run_iv = _union(
        (
            _dt(r["started_at"]),
            max(_dt(r["last_alive_at"]), now_dt) if r.get("current") else _dt(r["last_alive_at"]),
        )
        for r in runs
    )
    online_starts = [s for s, _ in online_iv]
    run_starts = [s for s, _ in run_iv]
    pushes = sorted({_dt(t) for t in received_times})
    if epoch is not None:
        pushes = [p for p in pushes if p < epoch]

    def classify(t: datetime) -> tuple[str, bool]:
        if first is None or t < first:
            return UNKNOWN, False
        if epoch is not None and t >= epoch:
            if _inside(online_iv, online_starts, t):
                return ONLINE, False
            if _inside(run_iv, run_starts, t):
                return OFFLINE, False
            return UNKNOWN, False
        i = bisect_right(pushes, t) - 1
        if i < 0:
            # Known host, no push seen yet: it was not reporting.
            return OFFLINE, True
        if i + 1 < len(pushes):
            return (ONLINE if pushes[i + 1] - pushes[i] <= threshold else OFFLINE), True
        return (ONLINE if t - pushes[i] < threshold else OFFLINE), True

    points = {start, end}
    candidates: list[datetime] = [p for p in (epoch, first) if p is not None]
    for s, e in online_iv + run_iv:
        candidates += [s, e]
    for p in pushes:
        candidates += [p, p + threshold]
    points.update(c for c in candidates if start < c < end)
    ordered = sorted(points)

    segments: list[dict[str, Any]] = []
    totals = {ONLINE: 0.0, OFFLINE: 0.0, UNKNOWN: 0.0}
    merged: list[list[Any]] = []  # [start, end, state, approx]
    for a, b in zip(ordered, ordered[1:]):
        state, approx = classify(a)
        totals[state] += (b - a).total_seconds()
        if merged and merged[-1][2] == state and merged[-1][3] == approx:
            merged[-1][1] = b
        else:
            merged.append([a, b, state, approx])
    for a, b, state, approx in merged:
        segments.append({"start": _iso(a), "end": _iso(b), "state": state, "approx": approx})

    known = totals[ONLINE] + totals[OFFLINE]
    boot_list = sorted({_dt(b) for b in boots})
    return {
        "window": {"start": _iso(start), "end": _iso(end)},
        "segments": segments,
        "boots": [_iso(b) for b in boot_list if start <= b <= end],
        "online_pct": round(100.0 * totals[ONLINE] / known, 1) if known > 0 else None,
        "totals": {
            "online_secs": int(round(totals[ONLINE])),
            "offline_secs": int(round(totals[OFFLINE])),
            "unknown_secs": int(round(totals[UNKNOWN])),
        },
        "ledger_since": _iso(epoch) if epoch is not None else None,
    }


def buckets(
    result_or_segments: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    window_start: TimeLike,
    window_end: TimeLike,
    n: int,
) -> list[float | None]:
    """Split ``[window_start, window_end)`` into ``n`` equal cells.

    Each cell is the online share of its *known* (online + offline) time in
    [0, 1], rounded to 3 decimals, or None when nothing in it is known.
    """

    if n <= 0:
        return []
    segments = (
        result_or_segments["segments"]
        if isinstance(result_or_segments, Mapping)
        else result_or_segments
    )
    start = _parse(window_start)
    width = (_parse(window_end) - start).total_seconds() / n
    online = [0.0] * n
    known = [0.0] * n
    if width <= 0:
        return [None] * n
    for seg in segments:
        state = seg["state"]
        if state == UNKNOWN:
            continue
        s = (_parse(seg["start"]) - start).total_seconds()
        e = (_parse(seg["end"]) - start).total_seconds()
        if e <= 0 or s >= width * n:
            continue
        first = max(0, int(s // width))
        last = min(n - 1, int((e - 1e-9) // width))
        for i in range(first, last + 1):
            overlap = min(e, (i + 1) * width) - max(s, i * width)
            if overlap <= 0:
                continue
            known[i] += overlap
            if state == ONLINE:
                online[i] += overlap
    return [round(online[i] / known[i], 3) if known[i] > 0 else None for i in range(n)]


def _spans(
    segments: Sequence[Mapping[str, Any]], state: str, cap: int
) -> tuple[list[dict[str, Any]], bool]:
    spans = []
    for seg in segments:
        if seg["state"] != state:
            continue
        duration = int((_parse(seg["end"]) - _parse(seg["start"])).total_seconds())
        if duration < MIN_SPAN_SECS:
            continue
        spans.append(
            {
                "start": seg["start"],
                "end": seg["end"],
                "duration_secs": duration,
                "approx": bool(seg["approx"]),
            }
        )
    spans.reverse()  # newest first
    return spans[:cap], len(spans) > cap


def summarize(result: Mapping[str, Any], *, agent_id: str, online: bool) -> dict[str, Any]:
    """The chat-sized view of a :func:`compute` result.

    Segments become outages (offline spans of at least five minutes) and unknown
    spans, newest first and capped, so a flapping host cannot flood a
    conversation. Boots are newest first and capped too.
    """

    segments = result["segments"]
    outages, outages_truncated = _spans(segments, OFFLINE, MAX_OUTAGES)
    unknown, unknown_truncated = _spans(segments, UNKNOWN, MAX_UNKNOWN_SPANS)
    return {
        "agent_id": agent_id,
        "online": online,
        "window": result["window"],
        "online_pct": result["online_pct"],
        "totals": result["totals"],
        "ledger_since": result["ledger_since"],
        "outages": outages,
        "outages_truncated": outages_truncated,
        "unknown_spans": unknown,
        "unknown_spans_truncated": unknown_truncated,
        "boots": list(reversed(result["boots"]))[:MAX_BOOTS],
    }


def parse_days(raw: Any, default: int) -> int:
    """Validate a ``days`` argument: an integer in ``1..MAX_DAYS``.

    Raises ``ValueError`` otherwise; the caller turns that into its own error.
    """

    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError("days must be an integer")
    try:
        days = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("days must be an integer") from None
    if isinstance(raw, float) and raw != days:
        raise ValueError("days must be an integer")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    return days


async def load_many(
    *,
    presence: Any,
    store: Any,
    agent_ids: list[str],
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    offline_after_secs: int,
) -> dict[str, dict[str, Any]]:
    """Read everything :func:`compute` needs for many hosts in a fixed number of queries.

    ``presence`` may be None (no ledger wired): every host is then reconstructed
    from its snapshot arrival times alone.
    """

    if not agent_ids:
        return {}
    since, until = window_start.isoformat(), window_end.isoformat()
    epoch: str | None = None
    runs: list[dict[str, Any]] = []
    sessions: dict[str, list[dict[str, Any]]] = {}
    boots: dict[str, list[str]] = {}
    first_sessions: dict[str, str] = {}
    if presence is not None:
        epoch = await presence.ledger_epoch()
        runs = await presence.runs(since, until)
        sessions = await presence.sessions_many(agent_ids, since, until)
        boots = await presence.boots_many(agent_ids, since, until)
        first_sessions = await presence.first_session_many(agent_ids)
    pushes: dict[str, list[str]] = {}
    if epoch is None or _parse(epoch) > window_start:
        # Arrival times matter only before the ledger begins. The lookback lets
        # a push just before the window decide the window's first minutes.
        lookback = window_start - timedelta(seconds=offline_after_secs)
        push_until = epoch if epoch is not None else (now + timedelta(seconds=1)).isoformat()
        pushes = await store.received_times_many(agent_ids, lookback.isoformat(), push_until)
    first_collected = await store.first_collected_many(agent_ids)

    out: dict[str, dict[str, Any]] = {}
    for agent_id in agent_ids:
        agent_pushes = pushes.get(agent_id, [])
        firsts = [
            v
            for v in (
                first_collected.get(agent_id),
                first_sessions.get(agent_id),
                agent_pushes[0] if agent_pushes else None,
            )
            if v
        ]
        out[agent_id] = compute(
            sessions=sessions.get(agent_id, []),
            runs=runs,
            received_times=agent_pushes,
            boots=boots.get(agent_id, []),
            first_seen=min(firsts, key=_parse) if firsts else None,
            window_start=window_start,
            window_end=window_end,
            ledger_epoch=epoch,
            offline_after_secs=offline_after_secs,
            now=now,
        )
    return out


async def load_one(
    *,
    presence: Any,
    store: Any,
    agent_id: str,
    days: int,
    now: datetime | None = None,
    offline_after_secs: int,
) -> dict[str, Any]:
    """:func:`load_many` for one host over the last ``days`` days ending now."""

    now = now or datetime.now(timezone.utc)
    results = await load_many(
        presence=presence,
        store=store,
        agent_ids=[agent_id],
        window_start=now - timedelta(days=days),
        window_end=now,
        now=now,
        offline_after_secs=offline_after_secs,
    )
    return results[agent_id]

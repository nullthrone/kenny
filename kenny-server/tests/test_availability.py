"""Availability segments (``kenny_server/availability.py``): pure computation.

Every case builds the inputs by hand -- sessions, runs, arrival times -- and
checks the segments, totals and percentage ``compute`` derives from them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kenny_server import availability
from kenny_server.availability import buckets, compute, parse_days, summarize

T0 = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
THR = 2700


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def iso(minutes: float) -> str:
    return at(minutes).isoformat(timespec="seconds")


def session(start: float, end: float | None) -> dict:
    return {
        "connected_at": at(start).isoformat(),
        "disconnected_at": at(end).isoformat() if end is not None else None,
    }


def run(start: float, end: float, *, current: bool = False) -> dict:
    return {
        "started_at": at(start).isoformat(),
        "last_alive_at": at(end).isoformat(),
        "current": current,
    }


def ledger(
    sessions: list[dict],
    runs: list[dict],
    *,
    start: float = 0,
    end: float = 600,
    now: float | None = None,
    epoch: float | None = 0,
    first: float | None = 0,
    pushes: list[float] | None = None,
    boots: list[str] | None = None,
) -> dict:
    return compute(
        sessions=sessions,
        runs=runs,
        received_times=[at(p).isoformat() for p in (pushes or [])],
        boots=boots or [],
        first_seen=at(first) if first is not None else None,
        window_start=at(start),
        window_end=at(end),
        ledger_epoch=at(epoch) if epoch is not None else None,
        offline_after_secs=THR,
        now=at(now if now is not None else end),
    )


def states(result: dict) -> list[tuple[str, str, str, bool]]:
    return [(s["start"], s["end"], s["state"], s["approx"]) for s in result["segments"]]


# -- ledger --------------------------------------------------------------------


def test_session_inside_a_run_is_online_the_rest_offline() -> None:
    r = ledger([session(100, 200)], [run(0, 600)])
    assert states(r) == [
        (iso(0), iso(100), "offline", False),
        (iso(100), iso(200), "online", False),
        (iso(200), iso(600), "offline", False),
    ]
    assert r["totals"] == {"online_secs": 6000, "offline_secs": 30000, "unknown_secs": 0}
    assert r["online_pct"] == round(100 * 6000 / 36000, 1)
    assert r["window"] == {"start": iso(0), "end": iso(600)}
    assert r["ledger_since"] == iso(0)


def test_segments_are_contiguous_and_cover_the_window() -> None:
    r = ledger(
        [session(10, 50), session(40, 90), session(300, None)],
        [run(0, 200), run(250, 500, current=True)],
    )
    segs = r["segments"]
    assert segs[0]["start"] == iso(0)
    assert segs[-1]["end"] == iso(600)
    for a, b in zip(segs, segs[1:]):
        assert a["end"] == b["start"]
        assert (a["state"], a["approx"]) != (b["state"], b["approx"]), "adjacent equal not merged"
    total = sum(r["totals"].values())
    assert total == 600 * 60


def test_overlapping_and_duplicate_sessions_count_once() -> None:
    r = ledger(
        [session(100, 200), session(150, 250), session(100, 200)],
        [run(0, 600)],
    )
    assert r["totals"]["online_secs"] == 150 * 60
    assert [s["state"] for s in r["segments"]] == ["offline", "online", "offline"]


def test_open_session_extends_to_now_and_nothing_after_now() -> None:
    r = ledger([session(500, None)], [run(0, 520, current=True)], end=600, now=560)
    # The current run's stale last_alive_at is extended to now; the window ends at now.
    assert r["window"]["end"] == iso(560)
    assert states(r)[-1] == (iso(500), iso(560), "online", False)
    assert r["totals"]["unknown_secs"] == 0


def test_a_closed_run_is_not_extended() -> None:
    r = ledger([], [run(0, 300)], end=600)
    assert states(r) == [
        (iso(0), iso(300), "offline", False),
        (iso(300), iso(600), "unknown", False),
    ]


def test_server_downtime_is_unknown_and_excluded_from_the_percentage() -> None:
    r = ledger(
        [session(0, 100), session(400, 600)],
        [run(0, 100), run(400, 600)],
    )
    assert [s["state"] for s in r["segments"]] == ["online", "unknown", "online"]
    assert r["totals"] == {"online_secs": 18000, "offline_secs": 0, "unknown_secs": 18000}
    assert r["online_pct"] == 100.0


def test_before_first_seen_is_unknown_even_inside_a_run() -> None:
    r = ledger([session(300, 400)], [run(0, 600)], first=300)
    assert states(r)[0] == (iso(0), iso(300), "unknown", False)
    assert r["totals"]["unknown_secs"] == 300 * 60


def test_no_first_seen_means_all_unknown_and_no_percentage() -> None:
    r = ledger([], [run(0, 600)], first=None)
    assert [s["state"] for s in r["segments"]] == ["unknown"]
    assert r["online_pct"] is None


def test_boots_inside_the_window_oldest_first() -> None:
    boots = [iso(700), iso(50), iso(-10), iso(300)]
    r = ledger([], [run(0, 600)], boots=boots)
    assert r["boots"] == [iso(50), iso(300)]


# -- backfill (before the ledger epoch) ---------------------------------------


def test_backfill_pushes_within_threshold_are_online_and_approx() -> None:
    # Pushes every 15 min until minute 60, then silence until the epoch at 300.
    r = ledger([], [run(300, 600)], epoch=300, pushes=[0, 15, 30, 45, 60], first=0)
    assert states(r)[:2] == [
        (iso(0), iso(60 + THR / 60), "online", True),
        (iso(60 + THR / 60), iso(300), "offline", True),
    ]
    assert states(r)[-1] == (iso(300), iso(600), "offline", False)


def test_backfill_gap_larger_than_threshold_is_offline() -> None:
    r = ledger([], [], end=300, epoch=None, pushes=[0, 10, 200, 210], first=0, now=300)
    assert states(r) == [
        (iso(0), iso(10), "online", True),
        (iso(10), iso(200), "offline", True),
        (iso(200), iso(210 + THR / 60), "online", True),
        (iso(210 + THR / 60), iso(300), "offline", True),
    ]
    assert r["ledger_since"] is None


def test_backfill_gap_exactly_at_threshold_is_online() -> None:
    r = ledger([], [], end=100, epoch=None, pushes=[0, THR / 60], first=0, now=100)
    assert states(r)[0] == (iso(0), iso(THR / 60 + THR / 60), "online", True)


def test_boundary_at_the_ledger_epoch() -> None:
    # A push 10 min before the epoch would keep the host online for 45 min,
    # but from the epoch on only the ledger speaks.
    r = ledger([session(320, 600)], [run(300, 600, current=True)], epoch=300, pushes=[290], first=0)
    assert states(r) == [
        (iso(0), iso(290), "offline", True),
        (iso(290), iso(300), "online", True),
        (iso(300), iso(320), "offline", False),
        (iso(320), iso(600), "online", False),
    ]


def test_pushes_after_the_epoch_do_not_feed_the_backfill() -> None:
    r = ledger([], [run(300, 600, current=True)], epoch=300, pushes=[100, 400], first=0)
    assert states(r)[:2] == [
        (iso(0), iso(100), "offline", True),
        (iso(100), iso(100 + THR / 60), "online", True),
    ]


def test_a_push_before_the_window_decides_its_first_minutes() -> None:
    r = ledger([], [], start=100, end=300, epoch=None, pushes=[90], first=0, now=300)
    assert states(r)[0] == (iso(100), iso(90 + THR / 60), "online", True)


def test_percentage_excludes_unknown_time() -> None:
    r = ledger([session(0, 100)], [run(0, 200)], end=400)
    assert r["totals"] == {"online_secs": 6000, "offline_secs": 6000, "unknown_secs": 12000}
    assert r["online_pct"] == 50.0


# -- buckets -------------------------------------------------------------------


def test_buckets_report_the_online_share_of_known_time() -> None:
    r = ledger([session(0, 150)], [run(0, 400)], end=600)
    cells = buckets(r, at(0), at(600), 6)
    # 100-min cells: online, half online, offline, offline, unknown, unknown.
    assert cells == [1.0, 0.5, 0.0, 0.0, None, None]


def test_buckets_accept_a_bare_segment_list_and_round_to_three() -> None:
    r = ledger([session(0, 100)], [run(0, 300)], end=300)
    cells = buckets(r["segments"], at(0), at(300), 1)
    assert cells == [0.333]


def test_buckets_after_the_last_segment_are_none() -> None:
    r = ledger([session(0, 60)], [run(0, 60, current=True)], end=120, now=60)
    assert buckets(r, at(0), at(120), 2) == [1.0, None]


def test_buckets_with_no_cells() -> None:
    assert buckets([], at(0), at(10), 0) == []


# -- summarize / parse_days ----------------------------------------------------


def test_summarize_lists_long_outages_newest_first_and_caps_them() -> None:
    sessions = [session(i * 20 + 10, i * 20 + 20) for i in range(60)]
    r = ledger(sessions, [run(0, 1200)], end=1200)
    out = summarize(r, agent_id="pc1", online=False)
    assert out["agent_id"] == "pc1" and out["online"] is False
    assert len(out["outages"]) == availability.MAX_OUTAGES
    assert out["outages_truncated"] is True
    starts = [o["start"] for o in out["outages"]]
    assert starts == sorted(starts, reverse=True)
    assert all(o["duration_secs"] >= availability.MIN_SPAN_SECS for o in out["outages"])
    assert out["unknown_spans"] == [] and out["unknown_spans_truncated"] is False
    assert set(out) == {
        "agent_id", "online", "window", "online_pct", "totals", "ledger_since",
        "outages", "outages_truncated", "unknown_spans", "unknown_spans_truncated", "boots",
    }


def test_summarize_drops_short_outages_and_orders_boots_newest_first() -> None:
    r = ledger(
        [session(0, 100), session(102, 300), session(400, 600)],
        [run(0, 300), run(400, 600)],
        boots=[iso(10), iso(200)],
    )
    out = summarize(r, agent_id="pc1", online=True)
    assert out["outages"] == []  # the 2-minute blip is below the floor
    assert out["unknown_spans"] == [
        {"start": iso(300), "end": iso(400), "duration_secs": 6000, "approx": False}
    ]
    assert out["boots"] == [iso(200), iso(10)]


@pytest.mark.parametrize("raw,expected", [(None, 7), ("", 7), ("3", 3), (30, 30), (1.0, 1)])
def test_parse_days_accepts(raw, expected) -> None:
    assert parse_days(raw, 7) == expected


@pytest.mark.parametrize("raw", [0, 31, "-1", "abc", "2.5", True, 2.5])
def test_parse_days_rejects(raw) -> None:
    with pytest.raises(ValueError):
        parse_days(raw, 7)


def test_offline_threshold_is_the_alerting_one() -> None:
    from kenny_server.alerting import DEFAULT_OFFLINE_AFTER_S

    class _Settings:
        def __init__(self, value):
            self.value = value

        def get(self, key):
            assert key == "KENNY_ALERT_OFFLINE_AFTER_SECS"
            return self.value

    assert availability.offline_after_secs(None) == DEFAULT_OFFLINE_AFTER_S
    assert availability.offline_after_secs(_Settings(600)) == 600

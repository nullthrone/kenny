"""LLM event categorization (ADR-0028) with an injected fake client."""

from __future__ import annotations

import asyncio
import time

import pytest

from kenny_server import event_categories as ec


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, text: str, calls: list[int]) -> None:
        self._text = text
        self._calls = calls

    def create(self, **_kwargs):  # matches anthropic's client.messages.create
        self._calls[0] += 1
        return _Resp(self._text)


class _FakeClient:
    """Returns a fixed text body from ``messages.create`` and counts calls."""

    def __init__(self, text: str) -> None:
        self.calls = [0]
        self.messages = _Messages(text, self.calls)


class _SlowMessages:
    def __init__(self, text: str, calls: list[int], delay: float) -> None:
        self._text = text
        self._calls = calls
        self._delay = delay

    def create(self, **_kwargs):
        self._calls[0] += 1
        time.sleep(self._delay)  # runs on a worker thread via asyncio.to_thread
        return _Resp(self._text)


class _SlowFakeClient:
    """Like ``_FakeClient``, but ``messages.create`` blocks for ``delay``
    seconds — exercises the bounded-wait / background-completion path
    (``_classify`` runs it via ``asyncio.to_thread``, so this blocks a worker
    thread, never the event loop)."""

    def __init__(self, text: str, delay: float) -> None:
        self.calls = [0]
        self.messages = _SlowMessages(text, self.calls, delay)


@pytest.fixture(autouse=True)
def _clear_cache():
    ec._cache.clear()
    ec._inflight.clear()
    ec._failed_until.clear()
    yield
    ec._cache.clear()
    ec._inflight.clear()
    ec._failed_until.clear()


def _run(coro):
    return asyncio.run(coro)


def test_maps_in_order_and_validates_enums():
    groups = [
        {"source": "Application Error", "event_id": 1000, "sample": "chrome.exe crashed"},
        {"source": "disk", "event_id": 51, "sample": "paging error"},
        {"source": "Weird", "event_id": 9, "sample": "?"},
    ]
    # Third entry has bad category/severity -> coerced to the fallbacks.
    client = _FakeClient(
        '['
        '{"category": "App crash / hang", "severity": "notable", "cause": "chrome tab crash"},'
        '{"category": "Disk & storage", "severity": "serious", "cause": "failing sectors"},'
        '{"category": "Nonsense", "severity": "made up", "cause": "?"}'
        ']'
    )
    mapping = _run(ec.categorize_events(client, groups))
    assert mapping[("Application Error", 1000)] == {
        "category": "App crash / hang", "severity": "notable", "cause": "chrome tab crash",
    }
    assert mapping[("disk", 51)]["severity"] == "serious"
    assert mapping[("Weird", 9)] == {"category": "Other", "severity": "unknown", "cause": "?"}
    assert client.calls[0] == 1


def test_cache_avoids_second_call():
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    client = _FakeClient('[{"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}]')
    _run(ec.categorize_events(client, groups))
    # Second call for the same (source, event_id) is served from cache.
    mapping = _run(ec.categorize_events(client, groups))
    assert mapping[("disk", 51)]["category"] == "Disk & storage"
    assert client.calls[0] == 1


def test_no_client_returns_fallback():
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    mapping = _run(ec.categorize_events(None, groups))
    assert mapping[("disk", 51)] == {"category": "Other", "severity": "unknown", "cause": ""}


def test_bad_response_falls_back_and_is_not_cached():
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    bad = _FakeClient("not json at all")
    mapping = _run(ec.categorize_events(bad, groups))
    assert mapping[("disk", 51)] == {"category": "Other", "severity": "unknown", "cause": ""}
    # Fallback isn't cached, but a failed batch does back off for a while (so a
    # persistently broken API isn't re-hit on every read) — see
    # test_failed_batch_backs_off_then_retries for that in isolation. Once the
    # backoff has expired, a later good client still classifies it.
    assert ("disk", 51) in ec._failed_until
    ec._failed_until.clear()
    good = _FakeClient('[{"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}]')
    mapping2 = _run(ec.categorize_events(good, groups))
    assert mapping2[("disk", 51)]["category"] == "Disk & storage"
    assert mapping2[("disk", 51)]["severity"] == "serious"


def test_length_mismatch_falls_back():
    groups = [
        {"source": "a", "event_id": 1, "sample": "x"},
        {"source": "b", "event_id": 2, "sample": "y"},
    ]
    client = _FakeClient('[{"category": "Disk & storage", "severity": "serious", "cause": "x"}]')  # only one for two inputs
    mapping = _run(ec.categorize_events(client, groups))
    assert mapping[("a", 1)] == {"category": "Other", "severity": "unknown", "cause": ""}
    assert mapping[("b", 2)] == {"category": "Other", "severity": "unknown", "cause": ""}


def test_malformed_element_degrades_without_failing_whole_batch():
    groups = [
        {"source": "a", "event_id": 1, "sample": "x"},
        {"source": "b", "event_id": 2, "sample": "y"},
    ]
    # First element is a bare string, not an object -> degrades to defaults;
    # second is well-formed and still classifies normally.
    client = _FakeClient(
        '["not an object", {"category": "Network", "severity": "notable", "cause": "flaky wifi"}]'
    )
    mapping = _run(ec.categorize_events(client, groups))
    assert mapping[("a", 1)] == {"category": "Other", "severity": "unknown", "cause": ""}
    assert mapping[("b", 2)] == {"category": "Network", "severity": "notable", "cause": "flaky wifi"}


def test_annotate_events_stamps_category_severity_and_cause():
    events = [
        {"source": "disk", "event_id": 51, "count": 3},
        {"source": "x", "event_id": 9, "count": 1},
    ]
    mapping = {("disk", 51): {"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}}
    ec.annotate_events(events, mapping)
    assert events[0]["category"] == "Disk & storage"
    assert events[0]["severity"] == "serious"
    assert events[0]["suspected_cause"] == "bad sectors"
    # Missing from mapping -> safe defaults, never silently benign.
    assert events[1]["category"] == "Other"
    assert events[1]["severity"] == "unknown"
    assert events[1]["suspected_cause"] == ""


def test_annotate_events_leaves_suppression_markers_intact():
    # ADR-0045 / issue #166: suppression markers are stamped by a different
    # module (reliability_suppression.SuppressionList.mark) on the same event
    # dicts. annotate_events must only ever write its own three keys, so a
    # (re-)annotation pass never clobbers an existing suppression stamp.
    events = [
        {"source": "disk", "event_id": 51, "count": 3, "suppressed": True,
         "suppressed_by": {"id": "r", "scope": "fleet"}},
    ]
    mapping = {("disk", 51): {"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}}
    ec.annotate_events(events, mapping)
    assert events[0]["suppressed"] is True
    assert events[0]["suppressed_by"] == {"id": "r", "scope": "fleet"}
    assert events[0]["category"] == "Disk & storage"


def test_annotate_snapshots_no_events_is_noop():
    snap = {"reliability": {"status": "ok", "summary": "", "recent_crashes": 0, "events": []}}
    _run(ec.annotate_snapshots([snap], client_factory=lambda: _FakeClient("[]")))
    assert snap["reliability"]["events"] == []


def test_annotate_snapshots_stamps_across_multiple_snapshots(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    snap_a = {"reliability": {"status": "warn", "summary": "", "events": [
        {"source": "disk", "event_id": 51, "count": 10},
    ]}}
    snap_b = {"reliability": {"status": "warn", "summary": "", "events": [
        {"source": "disk", "event_id": 51, "count": 5},
        {"source": "Kernel-Power", "event_id": 41, "count": 1},
    ]}}
    client = _FakeClient(
        '['
        '{"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"},'
        '{"category": "Power & boot", "severity": "serious", "cause": "unexpected shutdown"}'
        ']'
    )
    _run(ec.annotate_snapshots([snap_a, snap_b], client_factory=lambda: client))
    assert snap_a["reliability"]["events"][0]["severity"] == "serious"
    assert snap_b["reliability"]["events"][0]["category"] == "Disk & storage"
    assert snap_b["reliability"]["events"][1]["suspected_cause"] == "unexpected shutdown"
    # One batched call for both snapshots' distinct (source, event_id) pairs.
    assert client.calls[0] == 1


# -- bounded wait / background completion (dashboard-latency fix) ----------


def test_annotate_snapshots_does_not_block_on_slow_client():
    # A cold cache + a slow/misbehaving Anthropic client used to leave the
    # whole /api/fleet/overview request hanging on the classify call; a read
    # must never block beyond `wait`.
    snap = {"reliability": {"status": "warn", "summary": "", "events": [
        {"source": "disk", "event_id": 51, "count": 3},
    ]}}
    client = _SlowFakeClient(
        '[{"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}]',
        delay=5.0,
    )

    async def scenario():
        t0 = time.monotonic()
        await ec.annotate_snapshots([snap], client_factory=lambda: client, wait=0.05)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        # Let the background batch finish inside this event loop, rather than
        # asyncio.run() cancelling a still-sleeping task at teardown.
        task = ec._inflight.get(("disk", 51))
        if task is not None:
            await task

    _run(scenario())
    event = snap["reliability"]["events"][0]
    # health_rules.py switches its whole reliability scoring rule based on
    # whether events carry a `severity` at all — the fast path must still
    # stamp the safe defaults rather than skip annotation outright.
    assert "severity" in event
    assert event["category"] == "Other"
    assert event["severity"] == "unknown"


def test_slow_classification_still_warms_the_cache():
    # A batch that times out is not abandoned (asyncio.shield) — it keeps
    # running and lands in the cache, so the next read gets the real category
    # instead of paying the same latency (or the same Other) forever.
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    client = _SlowFakeClient(
        '[{"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}]',
        delay=0.05,
    )

    async def scenario():
        first = await ec.categorize_events(client, groups, wait=0.0)
        assert first[("disk", 51)]["category"] == "Other"
        await asyncio.sleep(0.3)  # give the background batch time to land
        second = await ec.categorize_events(client, groups, wait=0.0)
        assert second[("disk", 51)]["category"] == "Disk & storage"
        assert client.calls[0] == 1

    _run(scenario())


def test_concurrent_annotations_share_one_classification():
    # api_fleet_overview and api_agent can race over the same event groups;
    # they must ride along on one in-flight batch, not each fire their own.
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    client = _SlowFakeClient(
        '[{"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}]',
        delay=0.05,
    )

    async def scenario():
        results = await asyncio.gather(
            ec.categorize_events(client, groups, wait=1.0),
            ec.categorize_events(client, groups, wait=1.0),
        )
        for mapping in results:
            assert mapping[("disk", 51)]["category"] == "Disk & storage"
        assert client.calls[0] == 1

    _run(scenario())


def test_failed_batch_backs_off_then_retries():
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    bad = _FakeClient("not json at all")
    mapping1 = _run(ec.categorize_events(bad, groups))
    assert mapping1[("disk", 51)]["category"] == "Other"
    assert bad.calls[0] == 1
    # Still cooling down: a second call does not re-hit the (still bad) API.
    mapping2 = _run(ec.categorize_events(bad, groups))
    assert mapping2[("disk", 51)]["category"] == "Other"
    assert bad.calls[0] == 1
    # Once the backoff has expired, classification retries and succeeds.
    ec._failed_until.clear()
    good = _FakeClient('[{"category": "Disk & storage", "severity": "serious", "cause": "bad sectors"}]')
    mapping3 = _run(ec.categorize_events(good, groups))
    assert mapping3[("disk", 51)]["category"] == "Disk & storage"
    assert good.calls[0] == 1

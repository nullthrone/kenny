"""LLM event categorization (ADR-0028) with an injected fake client."""

from __future__ import annotations

import asyncio

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


@pytest.fixture(autouse=True)
def _clear_cache():
    ec._cache.clear()
    yield
    ec._cache.clear()


def _run(coro):
    return asyncio.run(coro)


def test_maps_in_order_and_validates_enum():
    groups = [
        {"source": "Application Error", "event_id": 1000, "sample": "chrome.exe crashed"},
        {"source": "disk", "event_id": 51, "sample": "paging error"},
        {"source": "Weird", "event_id": 9, "sample": "?"},
    ]
    # Third value is not in the enum -> coerced to Other.
    client = _FakeClient('["App crash / hang", "Disk & storage", "Nonsense"]')
    mapping = _run(ec.categorize_events(client, groups))
    assert mapping[("Application Error", 1000)] == "App crash / hang"
    assert mapping[("disk", 51)] == "Disk & storage"
    assert mapping[("Weird", 9)] == "Other"
    assert client.calls[0] == 1


def test_cache_avoids_second_call():
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    client = _FakeClient('["Disk & storage"]')
    _run(ec.categorize_events(client, groups))
    # Second call for the same (source, event_id) is served from cache.
    mapping = _run(ec.categorize_events(client, groups))
    assert mapping[("disk", 51)] == "Disk & storage"
    assert client.calls[0] == 1


def test_no_client_returns_other():
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    mapping = _run(ec.categorize_events(None, groups))
    assert mapping[("disk", 51)] == "Other"


def test_bad_response_falls_back_and_is_not_cached():
    groups = [{"source": "disk", "event_id": 51, "sample": "x"}]
    bad = _FakeClient("not json at all")
    mapping = _run(ec.categorize_events(bad, groups))
    assert mapping[("disk", 51)] == "Other"
    # Fallback isn't cached, so a later good client still classifies it.
    good = _FakeClient('["Disk & storage"]')
    mapping2 = _run(ec.categorize_events(good, groups))
    assert mapping2[("disk", 51)] == "Disk & storage"


def test_length_mismatch_falls_back():
    groups = [
        {"source": "a", "event_id": 1, "sample": "x"},
        {"source": "b", "event_id": 2, "sample": "y"},
    ]
    client = _FakeClient('["Disk & storage"]')  # only one element for two inputs
    mapping = _run(ec.categorize_events(client, groups))
    assert mapping[("a", 1)] == "Other"
    assert mapping[("b", 2)] == "Other"


def test_annotate_events_stamps_category():
    events = [
        {"source": "disk", "event_id": 51, "count": 3},
        {"source": "x", "event_id": 9, "count": 1},
    ]
    mapping = {("disk", 51): "Disk & storage"}
    ec.annotate_events(events, mapping)
    assert events[0]["category"] == "Disk & storage"
    assert events[1]["category"] == "Other"  # missing -> FALLBACK

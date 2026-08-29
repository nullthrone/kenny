"""Live GitHub Releases changelog for the dashboard's About modal.

Uses ``httpx.MockTransport`` — no real network, matching ``test_agent_release.py``.

The bug these were extended for: a failed fetch used to return an empty list,
which the dashboard rendered as "this repo has published nothing yet" — a claim
about GitHub made from a request that never reached it.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from kenny_server import changelog

REPO = "nullthrone/kenny"


def _release(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "published_at": "2026-01-01T00:00:00Z",
        "body": "notes",
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "draft": draft,
        "prerelease": prerelease,
    }


def _releases_json():
    return [
        _release("v2.0.5-dev.17", prerelease=True),
        _release("v2.0.4"),
        _release("v2.0.3-draft", draft=True),
    ]


@pytest.fixture(autouse=True)
def _clear_cache():
    changelog._cache.clear()
    changelog._last_error.clear()
    yield
    changelog._cache.clear()
    changelog._last_error.clear()


_RealAsyncClient = httpx.AsyncClient


def _client_with(handler):
    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


async def _patched_client(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", _client_with(handler))


async def test_fetch_releases_excludes_prerelease_and_draft_by_default(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    result = await changelog.fetch_releases(REPO)
    tags = [r["tag"] for r in result.releases]
    assert tags == ["v2.0.4"]  # dev prerelease and the draft are both excluded
    assert result.ok and result.error is None and result.fetched_at


async def test_fetch_releases_include_prerelease_still_excludes_draft(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    result = await changelog.fetch_releases(REPO, include_prerelease=True)
    tags = [r["tag"] for r in result.releases]
    assert tags == ["v2.0.5-dev.17", "v2.0.4"]  # draft is always excluded


async def test_fetch_releases_cache_keys_do_not_clobber_each_other(monkeypatch):
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    stable = await changelog.fetch_releases(REPO, include_prerelease=False)
    dev = await changelog.fetch_releases(REPO, include_prerelease=True)
    assert [r["tag"] for r in stable.releases] == ["v2.0.4"]
    assert [r["tag"] for r in dev.releases] == ["v2.0.5-dev.17", "v2.0.4"]
    # each view hit the network once and is now independently cached
    assert calls["n"] == 2
    again = await changelog.fetch_releases(REPO, include_prerelease=False)
    assert [r["tag"] for r in again.releases] == ["v2.0.4"]
    assert calls["n"] == 2  # served from cache, no extra call
    assert again.ok and not again.stale


async def test_fetch_releases_degrades_to_empty_and_says_so(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    await _patched_client(monkeypatch, handle)
    result = await changelog.fetch_releases(REPO)
    assert result.releases == []
    assert result.ok is False
    assert "unreachable" in result.error
    assert result.stale is False  # nothing cached to be stale about


async def test_reports_401_as_a_failure_not_an_empty_repo(monkeypatch):
    """The reported bug: an expired token read as 'this repo has no releases'."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    await _patched_client(monkeypatch, handle)
    result = await changelog.fetch_releases(REPO)
    assert result.releases == []
    assert result.ok is False
    assert "401" in result.error
    # names the remedy, so the operator does not have to read the source
    assert "KENNY_GITHUB_TOKEN" in result.error


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, "rate limited"), (404, "not found"), (500, "HTTP 500")],
)
async def test_reports_each_github_status_distinctly(monkeypatch, status, expected):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "nope"})

    await _patched_client(monkeypatch, handle)
    result = await changelog.fetch_releases(REPO)
    assert result.ok is False
    assert expected in result.error


async def test_serves_stale_cache_and_marks_it(monkeypatch):
    """A failed refresh keeps the last good notes, but never calls them current."""

    state = {"fail": False}

    def handle(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    first = await changelog.fetch_releases(REPO)
    assert first.ok and [r["tag"] for r in first.releases] == ["v2.0.4"]

    # expire the entry, then break the network
    monkeypatch.setattr(changelog.time, "monotonic", lambda: 10_000.0)
    state["fail"] = True
    stale = await changelog.fetch_releases(REPO)
    assert [r["tag"] for r in stale.releases] == ["v2.0.4"]  # still useful
    assert stale.ok is False and stale.stale is True and stale.error
    # the timestamp describes the data, not the failed attempt
    assert stale.fetched_at == first.fetched_at


async def test_failure_does_not_poison_the_cache(monkeypatch):
    """No negative caching: a token fixed now takes effect now, not in 5 minutes."""

    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    assert (await changelog.fetch_releases(REPO)).ok is False
    recovered = await changelog.fetch_releases(REPO)
    assert calls["n"] == 2
    assert recovered.ok is True and [r["tag"] for r in recovered.releases] == ["v2.0.4"]


async def test_follows_a_repo_redirect(monkeypatch):
    """A transferred repo 301s; without following it the list silently came back empty."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/old-owner/kenny/releases"):
            return httpx.Response(
                301, headers={"Location": f"https://api.github.com/repos/{REPO}/releases"}
            )
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    result = await changelog.fetch_releases("old-owner/kenny")
    assert result.ok is True
    assert [r["tag"] for r in result.releases] == ["v2.0.4"]


async def test_logs_each_distinct_failure_once(monkeypatch, caplog):
    """The About dialog can be opened all day; the log must not scroll with it."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    await _patched_client(monkeypatch, handle)
    with caplog.at_level(logging.WARNING, logger="kenny.changelog"):
        await changelog.fetch_releases(REPO)
        await changelog.fetch_releases(REPO)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "401" in warnings[0].getMessage()

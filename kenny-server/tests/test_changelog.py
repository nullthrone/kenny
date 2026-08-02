"""Live GitHub Releases changelog for the dashboard's About modal.

Uses ``httpx.MockTransport`` — no real network, matching ``test_agent_release.py``.
"""

from __future__ import annotations

import httpx
import pytest

from kenny_server import changelog

REPO = "t11z/kenny"


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
    yield
    changelog._cache.clear()


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
    releases = await changelog.fetch_releases(REPO)
    tags = [r["tag"] for r in releases]
    assert tags == ["v2.0.4"]  # dev prerelease and the draft are both excluded


async def test_fetch_releases_include_prerelease_still_excludes_draft(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    releases = await changelog.fetch_releases(REPO, include_prerelease=True)
    tags = [r["tag"] for r in releases]
    assert tags == ["v2.0.5-dev.17", "v2.0.4"]  # draft is always excluded


async def test_fetch_releases_cache_keys_do_not_clobber_each_other(monkeypatch):
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_releases_json())

    await _patched_client(monkeypatch, handle)
    stable = await changelog.fetch_releases(REPO, include_prerelease=False)
    dev = await changelog.fetch_releases(REPO, include_prerelease=True)
    assert [r["tag"] for r in stable] == ["v2.0.4"]
    assert [r["tag"] for r in dev] == ["v2.0.5-dev.17", "v2.0.4"]
    # each view hit the network once and is now independently cached
    assert calls["n"] == 2
    stable_again = await changelog.fetch_releases(REPO, include_prerelease=False)
    assert [r["tag"] for r in stable_again] == ["v2.0.4"]
    assert calls["n"] == 2  # served from cache, no extra call


async def test_fetch_releases_degrades_to_empty_on_failure(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    await _patched_client(monkeypatch, handle)
    releases = await changelog.fetch_releases(REPO)
    assert releases == []

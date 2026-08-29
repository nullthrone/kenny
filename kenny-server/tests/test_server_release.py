"""Read-only GHCR polling for the server image (ADR-0040).

All tests use ``httpx.MockTransport`` — no real network, matching
``test_agent_release.py``.
"""

from __future__ import annotations

import httpx

from kenny_server import server_release


def _factory(handler):
    def make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return make


def test_parse_semver():
    assert server_release._parse_semver("1.4.2") == (1, 4, 2)
    assert server_release._parse_semver("v1.4.2") is None  # leading "v" is not a bare semver tag
    assert server_release._parse_semver("latest") is None
    assert server_release._parse_semver("1.4") is None
    assert server_release._parse_semver("1.4.2-rc1") is None


def test_is_newer():
    assert server_release.is_newer("1.4.2", "1.4.1")
    assert not server_release.is_newer("1.4.1", "1.4.2")
    assert not server_release.is_newer("1.4.2", "1.4.2")
    # an unparseable running version (e.g. the dev fallback) never claims "newer"
    assert not server_release.is_newer("1.4.2", "0.0.0-dev")
    assert not server_release.is_newer("latest", "1.0.0")


async def test_fetch_latest_server_tag_picks_highest_semver_and_digest():
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        if url.endswith("/tags/list"):
            return httpx.Response(200, json={"tags": ["latest", "1.4", "1.3.9", "1.4.2", "1.4.10"]})
        if "/manifests/1.4.10" in url and request.method == "HEAD":
            return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:" + "a" * 64})
        return httpx.Response(404)

    res = await server_release.fetch_latest_server_tag(
        "ghcr.io/nullthrone/kenny-server", client_factory=_factory(handle)
    )
    assert res.ok
    assert res.tag == "1.4.10"  # highest well-formed semver, not lexicographic ("1.4.2" > "1.4.10")
    assert res.digest == "sha256:" + "a" * 64


async def test_fetch_latest_server_tag_no_semver_tags():
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        if url.endswith("/tags/list"):
            return httpx.Response(200, json={"tags": ["latest", "dev"]})
        return httpx.Response(404)

    res = await server_release.fetch_latest_server_tag(
        "ghcr.io/nullthrone/kenny-server", client_factory=_factory(handle)
    )
    assert not res.ok
    assert res.tag is None


async def test_fetch_latest_server_tag_unreachable_is_non_fatal():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    res = await server_release.fetch_latest_server_tag(
        "ghcr.io/nullthrone/kenny-server", client_factory=_factory(handle)
    )
    assert not res.ok
    assert "GHCR check failed" in res.message


async def test_fetch_latest_server_tag_invalid_ref():
    res = await server_release.fetch_latest_server_tag("not-a-valid-ref")
    assert not res.ok
    assert "invalid image ref" in res.message


async def test_fetch_latest_server_tag_no_such_package():
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        if url.endswith("/tags/list"):
            return httpx.Response(404)
        return httpx.Response(404)

    res = await server_release.fetch_latest_server_tag(
        "ghcr.io/nullthrone/kenny-server", client_factory=_factory(handle)
    )
    assert not res.ok
    assert "no such package" in res.message


# -- dev channel (ADR-0048) ---------------------------------------------------


def test_parse_semver_prerelease():
    assert server_release._parse_semver_prerelease("2.0.5-dev.17") == (2, 0, 5, 17)
    assert server_release._parse_semver_prerelease("2.0.5-dev.0") == (2, 0, 5, 0)
    # a plain release tag is not itself a prerelease tag
    assert server_release._parse_semver_prerelease("2.0.5") is None
    assert server_release._parse_semver_prerelease("2.0.5-rc1") is None
    assert server_release._parse_semver_prerelease("latest") is None


def test_is_newer_dev_channel_orders_by_dev_n():
    assert server_release.is_newer("2.0.5-dev.18", "2.0.5-dev.17", channel="dev")
    assert not server_release.is_newer("2.0.5-dev.17", "2.0.5-dev.18", channel="dev")
    assert not server_release.is_newer("2.0.5-dev.17", "2.0.5-dev.17", channel="dev")
    # a higher patch triple wins regardless of dev_n
    assert server_release.is_newer("2.0.6-dev.1", "2.0.5-dev.99", channel="dev")


def test_is_newer_dev_channel_plain_release_compares_as_older_baseline():
    # A same-triple plain release is treated as dev_n = -1 for comparison
    # purposes only — a dev prerelease of that triple is "newer".
    assert server_release.is_newer("2.0.5-dev.1", "2.0.5", channel="dev")
    assert not server_release.is_newer("2.0.5", "2.0.5-dev.1", channel="dev")


def test_is_newer_dev_channel_unparseable_never_claims_newer():
    assert not server_release.is_newer("latest", "2.0.5-dev.1", channel="dev")
    assert not server_release.is_newer("2.0.5-dev.1", "latest", channel="dev")


def test_is_newer_stable_channel_unaffected_by_dev_tags():
    # the stable comparison must never consider a "-dev." suffixed tag "newer"
    assert not server_release.is_newer("2.0.5-dev.99", "2.0.4", channel="stable")


async def test_fetch_latest_server_tag_dev_picks_highest_dev_tag():
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        if url.endswith("/tags/list"):
            return httpx.Response(
                200,
                json={
                    "tags": [
                        "latest",
                        "edge",
                        "2.0.4",
                        "2.0.5-dev.3",
                        "2.0.5-dev.17",
                        "2.0.5-dev.9",
                    ]
                },
            )
        if "/manifests/2.0.5-dev.17" in url and request.method == "HEAD":
            return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:" + "e" * 64})
        return httpx.Response(404)

    res = await server_release.fetch_latest_server_tag(
        "ghcr.io/nullthrone/kenny-server", client_factory=_factory(handle), channel="dev"
    )
    assert res.ok
    assert res.tag == "2.0.5-dev.17"  # highest dev_n, never the floating "edge" alias
    assert res.digest == "sha256:" + "e" * 64


async def test_fetch_latest_server_tag_dev_no_prerelease_tags():
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        if url.endswith("/tags/list"):
            return httpx.Response(200, json={"tags": ["latest", "2.0.4", "edge"]})
        return httpx.Response(404)

    res = await server_release.fetch_latest_server_tag(
        "ghcr.io/nullthrone/kenny-server", client_factory=_factory(handle), channel="dev"
    )
    assert not res.ok
    assert res.tag is None


async def test_fetch_latest_server_tag_uses_pat_for_private_package(monkeypatch):
    seen_auth = {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/token":
            seen_auth["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, json={"token": "private-token"})
        if url.endswith("/tags/list"):
            seen_auth["bearer"] = request.headers.get("authorization")
            return httpx.Response(200, json={"tags": ["2.0.0"]})
        if "/manifests/2.0.0" in url:
            return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:" + "b" * 64})
        return httpx.Response(404)

    res = await server_release.fetch_latest_server_tag(
        "ghcr.io/nullthrone/kenny-server", github_token="ghp_secret", client_factory=_factory(handle)
    )
    assert res.ok
    assert res.tag == "2.0.0"
    # PAT is sent as HTTP Basic to the token endpoint, and the resulting bearer
    # token (not the raw PAT) is what's sent to the actual API calls.
    assert seen_auth["authorization"].startswith("Basic ")
    assert seen_auth["bearer"] == "Bearer private-token"

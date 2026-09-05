"""GitHub auto-fetch of the prebuilt agent binary (ADR-0015).

All tests use ``httpx.MockTransport`` — no real network.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from kenny_server import agent_release

EXE_BYTES = b"MZ fake kenny-agent.exe \x00\x01\x02payload"
ASSET_NAME = "kenny-agent-v0.2.4-x86_64-pc-windows-msvc.exe"
EXE_URL = "https://cdn.example.com/exe"
SHA_URL = "https://cdn.example.com/sha"


def _release_json(*, with_sha: bool = True, sha_text: str | None = None) -> dict:
    assets = [{"name": ASSET_NAME, "browser_download_url": EXE_URL}]
    if with_sha:
        assets.append({"name": ASSET_NAME + ".sha256", "browser_download_url": SHA_URL})
    return {"tag_name": "v0.2.4", "assets": assets}


def _handler(release: dict, *, sha_text: str | None = None):
    if sha_text is None:
        sha_text = f"{hashlib.sha256(EXE_BYTES).hexdigest()}  {ASSET_NAME}"

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/releases/latest"):
            return httpx.Response(200, json=release)
        if url == EXE_URL:
            return httpx.Response(200, content=EXE_BYTES)
        if url == SHA_URL:
            return httpx.Response(200, text=sha_text)
        return httpx.Response(404)

    return handle


def _factory(handler):
    def make() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    return make


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("KENNY_GITHUB_TOKEN", "ghp_test")


def test_fetch_success_verifies_and_caches(tmp_path, token):
    dest = str(tmp_path / "kenny-agent.exe")
    res = agent_release.fetch_latest_agent_binary(
        client_factory=_factory(_handler(_release_json())), dest=dest
    )
    assert res.ok
    assert res.source == "github"
    assert res.version == "0.2.4"  # leading "v" stripped from the tag
    assert res.asset_name == ASSET_NAME
    assert (tmp_path / "kenny-agent.exe").read_bytes() == EXE_BYTES
    assert res.sha256 == hashlib.sha256(EXE_BYTES).hexdigest()
    # the release tag is persisted next to the binary and leads the agent version
    assert (tmp_path / "kenny-agent.exe.version").read_text() == "0.2.4"
    assert agent_release.resolve_agent_version(dest) == "0.2.4"


def test_fetch_sha256_mismatch_fails(tmp_path, token):
    dest = str(tmp_path / "kenny-agent.exe")
    bad = f"{'0' * 64}  {ASSET_NAME}"
    res = agent_release.fetch_latest_agent_binary(
        client_factory=_factory(_handler(_release_json(), sha_text=bad)), dest=dest
    )
    assert not res.ok
    assert "verification failed" in res.message
    assert not (tmp_path / "kenny-agent.exe").exists()
    # no leftover temp files
    assert list(tmp_path.glob("*.part")) == []


# ``test_fetch_no_token_returns_none`` stood here and asserted the opposite of what
# kenny now does: that without KENNY_GITHUB_TOKEN the network must not be touched at
# all. ADR-0057 reversed that decision, and its replacement is
# ``test_fetches_without_any_token_configured`` further down.


def test_fetch_network_error_is_non_fatal(tmp_path, token):
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    res = agent_release.fetch_latest_agent_binary(
        client_factory=_factory(handle), dest=str(tmp_path / "x.exe")
    )
    assert not res.ok
    assert "fetch failed" in res.message


def test_fetch_rate_limited_403(tmp_path, token):
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limit"})

    res = agent_release.fetch_latest_agent_binary(
        client_factory=_factory(handle), dest=str(tmp_path / "x.exe")
    )
    assert not res.ok
    assert "403" in res.message


def test_fetch_no_matching_asset(tmp_path, token):
    rel = {"tag_name": "v1", "assets": [{"name": "notes.txt", "browser_download_url": EXE_URL}]}
    res = agent_release.fetch_latest_agent_binary(
        client_factory=_factory(_handler(rel)), dest=str(tmp_path / "x.exe")
    )
    assert not res.ok
    assert "fetch failed" in res.message


def test_fetch_missing_sha_proceeds_with_warning(tmp_path, token):
    dest = str(tmp_path / "kenny-agent.exe")
    res = agent_release.fetch_latest_agent_binary(
        client_factory=_factory(_handler(_release_json(with_sha=False))), dest=dest
    )
    assert res.ok
    assert "no .sha256" in res.message
    assert (tmp_path / "kenny-agent.exe").read_bytes() == EXE_BYTES


def test_resolve_version_tag_leads_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNY_AGENT_VERSION", "9.9.9")
    binary = tmp_path / "kenny-agent.exe"
    binary.write_bytes(EXE_BYTES)
    # no sidecar -> falls back to the env override (normalized)
    assert agent_release.resolve_agent_version(str(binary)) == "9.9.9"
    # a sidecar (the release tag) leads over the env override
    (tmp_path / "kenny-agent.exe.version").write_text("v0.3.0\n")
    assert agent_release.resolve_agent_version(str(binary)) == "0.3.0"


def test_resolve_version_env_fallback_and_default(monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_VERSION", raising=False)
    assert agent_release.resolve_agent_version(None) == agent_release.DEFAULT_VERSION
    monkeypatch.setenv("KENNY_AGENT_VERSION", "v1.2.3")
    assert agent_release.resolve_agent_version(None) == "1.2.3"


def test_normalize_version():
    assert agent_release._normalize_version("v0.3.0") == "0.3.0"
    assert agent_release._normalize_version("0.3.0") == "0.3.0"
    assert agent_release._normalize_version("  V2.0 ") == "2.0"
    assert agent_release._normalize_version("") == ""


def test_parse_sha256_format():
    digest = "a" * 64
    assert agent_release._parse_sha256(f"{digest}  some-name.exe\n") == digest
    with pytest.raises(ValueError):
        agent_release._parse_sha256("not-a-hash file")


def test_cache_path_derives_from_db(monkeypatch, tmp_path):
    monkeypatch.delenv("KENNY_AGENT_BINARY_CACHE", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "sub" / "kenny.sqlite"))
    assert agent_release.cache_path() == str(tmp_path / "sub" / "kenny-agent.exe")
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "override.exe"))
    assert agent_release.cache_path() == str(tmp_path / "override.exe")


def test_binary_status_unavailable(monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    st = agent_release.binary_status(manual_path=None)
    assert not st.ok
    assert st.source == "none"
    assert "releases/latest" in st.message


def test_binary_status_manual(tmp_path, monkeypatch):
    p = tmp_path / "kenny-agent.exe"
    p.write_bytes(EXE_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY", str(p))
    st = agent_release.binary_status(manual_path=str(p))
    assert st.ok
    assert st.source == "manual"
    assert st.sha256 == hashlib.sha256(EXE_BYTES).hexdigest()


# -- Linux per-(os, arch) support (ADR-0031 Phase 4 / ADR-0034) --------------

LINUX_X64_NAME = "kenny-agent-v0.2.4-x86_64-unknown-linux-musl"
LINUX_ARM_NAME = "kenny-agent-v0.2.4-aarch64-unknown-linux-musl"
LINUX_X64_URL = "https://cdn.example.com/linux-x64"
LINUX_ARM_URL = "https://cdn.example.com/linux-arm"
LINUX_X64_BYTES = b"\x7fELF fake linux x86_64 agent"
LINUX_ARM_BYTES = b"\x7fELF fake linux aarch64 agent"


def test_linux_asset_re_matches_musl_arches():
    assert agent_release.LINUX_ASSET_RE.match(LINUX_X64_NAME)
    assert agent_release.LINUX_ASSET_RE.match(LINUX_ARM_NAME)
    # no extension, and windows exe must not match the linux regex
    assert not agent_release.LINUX_ASSET_RE.match(ASSET_NAME)
    assert not agent_release.LINUX_ASSET_RE.match(LINUX_X64_NAME + ".exe")
    # per-(os, arch) regex is arch-specific
    assert agent_release._asset_re("linux", "aarch64").match(LINUX_ARM_NAME)
    assert not agent_release._asset_re("linux", "aarch64").match(LINUX_X64_NAME)


def test_cache_path_per_os_arch(monkeypatch, tmp_path):
    monkeypatch.delenv("KENNY_AGENT_BINARY_CACHE", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    assert agent_release.cache_path("linux", "x86_64") == str(tmp_path / "kenny-agent-linux-x86_64")
    assert agent_release.cache_path("linux", "aarch64") == str(
        tmp_path / "kenny-agent-linux-aarch64"
    )
    # the windows cache override does not leak into the linux paths
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "override.exe"))
    assert agent_release.cache_path() == str(tmp_path / "override.exe")
    assert agent_release.cache_path("linux", "x86_64") == str(tmp_path / "kenny-agent-linux-x86_64")


def _full_release_json() -> dict:
    def sha(name, url):
        return {"name": name + ".sha256", "browser_download_url": url + "-sha"}

    return {
        "tag_name": "v0.2.4",
        "assets": [
            {"name": ASSET_NAME, "browser_download_url": EXE_URL},
            sha(ASSET_NAME, EXE_URL),
            {"name": LINUX_X64_NAME, "browser_download_url": LINUX_X64_URL},
            sha(LINUX_X64_NAME, LINUX_X64_URL),
            {"name": LINUX_ARM_NAME, "browser_download_url": LINUX_ARM_URL},
            sha(LINUX_ARM_NAME, LINUX_ARM_URL),
        ],
    }


def _full_handler():
    bodies = {
        EXE_URL: EXE_BYTES,
        LINUX_X64_URL: LINUX_X64_BYTES,
        LINUX_ARM_URL: LINUX_ARM_BYTES,
    }
    names = {EXE_URL: ASSET_NAME, LINUX_X64_URL: LINUX_X64_NAME, LINUX_ARM_URL: LINUX_ARM_NAME}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/releases/latest"):
            return httpx.Response(200, json=_full_release_json())
        if url in bodies:
            return httpx.Response(200, content=bodies[url])
        if url.endswith("-sha"):
            base = url[: -len("-sha")]
            return httpx.Response(
                200, text=f"{hashlib.sha256(bodies[base]).hexdigest()}  {names[base]}"
            )
        return httpx.Response(404)

    return handle


def test_fetch_caches_windows_and_all_linux_arches(tmp_path, token, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY_CACHE", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    res = agent_release.fetch_latest_agent_binary(client_factory=_factory(_full_handler()))
    assert res.ok
    # windows result leads the return value...
    assert res.asset_name == ASSET_NAME
    assert (tmp_path / "kenny-agent.exe").read_bytes() == EXE_BYTES
    # ...and every linux musl arch is cached with its own .version sidecar
    assert (tmp_path / "kenny-agent-linux-x86_64").read_bytes() == LINUX_X64_BYTES
    assert (tmp_path / "kenny-agent-linux-aarch64").read_bytes() == LINUX_ARM_BYTES
    assert (tmp_path / "kenny-agent-linux-x86_64.version").read_text() == "0.2.4"
    assert (tmp_path / "kenny-agent-linux-aarch64.version").read_text() == "0.2.4"
    # the aggregate message mentions the linux assets that were also fetched
    assert "linux" in res.message


# -- dev channel (ADR-0048) ---------------------------------------------------

DEV_ASSET_NAME = "kenny-agent-v2.0.5-dev.17-x86_64-pc-windows-msvc.exe"
DEV_EXE_URL = "https://cdn.example.com/dev-exe"
DEV_EXE_BYTES = b"MZ fake kenny-agent.exe dev build \x00\x01\x02"


def _releases_list_json():
    """A ``/releases`` list: an older stable release, a newer dev prerelease."""

    return [
        {
            "tag_name": "v2.0.5-dev.17",
            "prerelease": True,
            "draft": False,
            "assets": [{"name": DEV_ASSET_NAME, "browser_download_url": DEV_EXE_URL}],
        },
        {
            "tag_name": "v2.0.4",
            "prerelease": False,
            "draft": False,
            "assets": [{"name": ASSET_NAME, "browser_download_url": EXE_URL}],
        },
    ]


def _channel_handler():
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/releases/latest"):
            # The stable path never sees the prerelease — GitHub's own
            # `/releases/latest` semantics exclude it by construction.
            return httpx.Response(200, json=_release_json())
        if url.endswith("/releases") or "/releases?" in url:
            return httpx.Response(200, json=_releases_list_json())
        if url == EXE_URL:
            return httpx.Response(200, content=EXE_BYTES)
        if url == DEV_EXE_URL:
            return httpx.Response(200, content=DEV_EXE_BYTES)
        return httpx.Response(404)

    return handle


def test_select_release_stable_ignores_prerelease(token):
    with _factory(_channel_handler())() as client:
        release = agent_release._select_release(client, "nullthrone/kenny", "stable")
    assert release["tag_name"] == "v0.2.4"  # the /releases/latest response, never the dev one


def test_select_release_dev_picks_newest_prerelease(token):
    with _factory(_channel_handler())() as client:
        release = agent_release._select_release(client, "nullthrone/kenny", "dev")
    assert release["tag_name"] == "v2.0.5-dev.17"


def test_fetch_dev_channel_downloads_prerelease_asset(tmp_path, token, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY_CACHE", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    res = agent_release.fetch_latest_agent_binary(
        client_factory=_factory(_channel_handler()), channel="dev"
    )
    assert res.ok
    assert res.asset_name == DEV_ASSET_NAME
    assert res.version == "2.0.5-dev.17"
    # cached to the dev cache path, not the stable one
    assert (tmp_path / "kenny-agent-dev.exe").read_bytes() == DEV_EXE_BYTES
    assert not (tmp_path / "kenny-agent.exe").exists()


def test_fetch_dev_channel_no_prerelease_returns_none(tmp_path, token, monkeypatch):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/releases") or "/releases?" in url:
            return httpx.Response(200, json=[{"tag_name": "v1.0.0", "prerelease": False, "draft": False}])
        return httpx.Response(404)

    res = agent_release.fetch_latest_agent_binary(client_factory=_factory(handle), channel="dev")
    assert not res.ok
    assert "no dev releases found" in res.message


def test_cache_path_dev_differs_from_stable(monkeypatch, tmp_path):
    monkeypatch.delenv("KENNY_AGENT_BINARY_CACHE", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    stable = agent_release.cache_path("windows", "x86_64", "stable")
    dev = agent_release.cache_path("windows", "x86_64", "dev")
    assert stable == str(tmp_path / "kenny-agent.exe")
    assert dev == str(tmp_path / "kenny-agent-dev.exe")
    assert stable != dev
    # windows/stable is byte-identical to the pre-channel default call
    assert agent_release.cache_path() == stable
    # linux dev gets a -dev suffix, distinct from the stable linux cache
    assert agent_release.cache_path("linux", "x86_64", "dev") == str(
        tmp_path / "kenny-agent-linux-x86_64-dev"
    )
    assert agent_release.cache_path("linux", "x86_64", "dev") != agent_release.cache_path(
        "linux", "x86_64", "stable"
    )


def test_dev_tagged_asset_still_matches_asset_re():
    assert agent_release.ASSET_RE.match(DEV_ASSET_NAME)
    assert agent_release._asset_re("windows", "x86_64").match(DEV_ASSET_NAME)


def test_binary_status_dev_never_reports_manual_source(tmp_path, monkeypatch):
    """Dev has no manual-override env in this iteration; status is always cache."""

    p = tmp_path / "kenny-agent-dev.exe"
    p.write_bytes(EXE_BYTES)
    # Even if KENNY_AGENT_BINARY happens to be set (stable's override), the
    # dev channel must not honor it.
    monkeypatch.setenv("KENNY_AGENT_BINARY", str(p))
    st = agent_release.binary_status(manual_path=str(p), channel="dev")
    assert st.ok
    assert st.source == "cache"


def test_fetch_linux_only_release_returns_success(tmp_path, token, monkeypatch):
    """A release with only linux assets still caches them and returns ok."""

    monkeypatch.delenv("KENNY_AGENT_BINARY_CACHE", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    rel = {
        "tag_name": "v0.2.4",
        "assets": [{"name": LINUX_X64_NAME, "browser_download_url": LINUX_X64_URL}],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/releases/latest"):
            return httpx.Response(200, json=rel)
        if url == LINUX_X64_URL:
            return httpx.Response(200, content=LINUX_X64_BYTES)
        return httpx.Response(404)

    res = agent_release.fetch_latest_agent_binary(client_factory=_factory(handle))
    assert res.ok
    assert res.asset_name == LINUX_X64_NAME
    assert (tmp_path / "kenny-agent-linux-x86_64").read_bytes() == LINUX_X64_BYTES
    # no windows binary was produced
    assert not (tmp_path / "kenny-agent.exe").exists()


# -- ADR-0057: releases are read anonymously ----------------------------------


def _captured_requests(handler_response):
    """A client factory that records every request it is asked to make."""

    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler_response(request)

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)

    return seen, factory


def test_never_sends_a_credential_even_when_one_is_configured(tmp_path, monkeypatch):
    """The decision itself, pinned.

    Everything read here is public, so no ``Authorization`` header goes out under
    any configuration (ADR-0057). Without this test the header slips back in on
    the next refactor and the failure mode it caused returns with it.
    """

    monkeypatch.setenv("KENNY_GITHUB_TOKEN", "ghp_should_never_be_sent")
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "k.sqlite"))

    seen, factory = _captured_requests(lambda _r: httpx.Response(404, json={"message": "nope"}))
    agent_release.fetch_latest_agent_binary(client_factory=factory)

    assert seen, "no request was attempted at all"
    for request in seen:
        assert "authorization" not in {k.lower() for k in request.headers}
    # and the token never leaks into the outgoing bytes some other way
    assert "ghp_should_never_be_sent" not in str(seen[0].headers)


def test_fetches_without_any_token_configured(tmp_path, monkeypatch):
    """No credential is a precondition any more — the attempt is simply made."""

    monkeypatch.delenv("KENNY_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "k.sqlite"))

    seen, factory = _captured_requests(lambda _r: httpx.Response(404, json={"message": "nope"}))
    result = agent_release.fetch_latest_agent_binary(client_factory=factory)

    assert seen, "the fetch refused to try without a token"
    assert result.ok is False  # a 404 from the stub, not a configuration refusal
    assert "not configured" not in result.message


# -- 403: rate limit and refusal are different problems -----------------------


def _status_error(status: int, *, headers=None, body=None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/repos/x/y/releases")
    response = httpx.Response(status, headers=headers or {}, json=body or {}, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_403_rate_limited_names_the_reset_time():
    exc = _status_error(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1788000000"},
        body={"message": "API rate limit exceeded"},
    )
    text = agent_release.describe_http_error(exc, "nullthrone/kenny")
    assert "rate limit" in text.lower()
    assert "2026-" in text  # the reset instant, not just "some time later"
    assert "per IP" in text


def test_403_without_the_rate_limit_header_reads_as_a_refusal():
    """Remaining quota means the limiter is not what refused this."""

    exc = _status_error(
        403,
        headers={"x-ratelimit-remaining": "4999"},
        body={"message": "Resource protected by organization SAML enforcement"},
    )
    text = agent_release.describe_http_error(exc, "nullthrone/kenny")
    assert "rate limit" not in text.lower()
    assert "refused" in text.lower()
    assert "SAML enforcement" in text  # GitHub's own words carry the real cause


def test_403_with_no_rate_limit_headers_at_all_falls_back_to_refusal():
    text = agent_release.describe_http_error(_status_error(403), "nullthrone/kenny")
    assert "refused" in text.lower()


def test_429_is_classified_like_403():
    exc = _status_error(429, headers={"x-ratelimit-remaining": "0"})
    assert "rate limit" in agent_release.describe_http_error(exc, "r/k").lower()


def test_404_points_at_the_manual_path_for_a_private_repo():
    """Anonymously, a private repo is indistinguishable from a missing one."""

    text = agent_release.describe_http_error(_status_error(404), "nullthrone/kenny")
    assert "not public" in text
    assert "KENNY_AGENT_BINARY" in text


def test_a_non_json_error_body_does_not_break_the_description():
    request = httpx.Request("GET", "https://api.github.com/repos/x/y/releases")
    response = httpx.Response(403, text="<html>gateway</html>", request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    assert "refused" in agent_release.describe_http_error(exc, "r/k").lower()

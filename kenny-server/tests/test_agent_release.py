"""GitHub auto-fetch of the prebuilt agent binary (ADR-0014).

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


def test_fetch_no_token_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_GITHUB_TOKEN", raising=False)

    def boom() -> httpx.Client:  # must not be called
        raise AssertionError("network must not be touched without a token")

    res = agent_release.fetch_latest_agent_binary(
        client_factory=boom, dest=str(tmp_path / "x.exe")
    )
    assert not res.ok
    assert res.source == "none"
    assert "KENNY_GITHUB_TOKEN" in res.message


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

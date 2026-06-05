"""Agent distribution: installer download, shareable link, update trigger, public binary."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from starlette.testclient import TestClient

from kenny_server.distribution import _sha256_file
from kenny_server.main import build_app

BINARY_BYTES = b"MZ fake kenny-agent.exe payload \x00\x01\x02"


@pytest.fixture
def binary(tmp_path, monkeypatch):
    p = tmp_path / "kenny-agent.exe"
    p.write_bytes(BINARY_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY", str(p))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    return p


def _app(tmp_path):
    return build_app(db_path=str(tmp_path / "dist.sqlite"))


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def test_installer_requires_operator_auth(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/agents/papa-pc/installer").status_code == 401


def test_installer_returns_zip_with_token(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/papa-pc/installer", headers=_bearer(app))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(zf.namelist())
        assert {"kenny-agent.exe", "install.bat", "README.txt"} <= names
        bat = zf.read("install.bat").decode()
        assert "--agent-id papa-pc" in bat
        assert "install --server wss://kenny.example.com/agent/ws" in bat
        # the minted token in the bat authenticates the agent
        token = bat.split("--token ", 1)[1].split(" ", 1)[0]
        assert token


def test_installer_503_without_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/agents/papa-pc/installer", headers=_bearer(app)).status_code == 503


def test_share_link_then_public_download_once(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/oma-laptop/share-link", headers=_bearer(app))
        assert r.status_code == 200
        url = r.json()["url"]
        assert "/d/installer/" in url
        path = url.split("kenny.example.com", 1)[1]
        # public (no auth) and one-time
        first = c.get(path)
        assert first.status_code == 200
        assert first.content[:2] == b"PK"  # zip magic
        assert c.get(path).status_code == 404  # consumed


def test_public_binary_serves_and_validates_nonce(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        nonce = app.state.share_links.create("papa-pc", "binary", 600)
        r = c.get(f"/d/binary/{nonce}")  # public, no auth
        assert r.status_code == 200
        assert r.content == BINARY_BYTES
        assert c.get("/d/binary/does-not-exist").status_code == 404


def test_update_requires_auth_and_502_without_agent(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/agents/papa-pc/update").status_code == 401
        # operator-authed but no online agent -> 502
        r = c.post("/api/agents/papa-pc/update", headers=_bearer(app))
        assert r.status_code == 502


def test_sha256_helper(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(BINARY_BYTES)
    assert _sha256_file(str(p)) == hashlib.sha256(BINARY_BYTES).hexdigest()


# -- agent-binary status / fetch / precedence (ADR-0014) --------------------


def test_agent_binary_status_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agent-binary", headers=_bearer(app))
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["source"] == "none"
        assert body["github_configured"] is False
        assert "releases/latest" in body["message"]


def test_agent_binary_status_manual(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/agent-binary", headers=_bearer(app)).json()
        assert body["available"] is True
        assert body["source"] == "manual"


def test_agent_binary_status_requires_auth(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/agent-binary").status_code == 401


def test_agent_binary_fetch_without_token_400(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_GITHUB_TOKEN", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agent-binary/fetch", headers=_bearer(app))
        assert r.status_code == 400


def test_cache_served_when_no_explicit_binary(tmp_path, monkeypatch):
    """With no KENNY_AGENT_BINARY, the GitHub cache file is served."""

    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    cache = tmp_path / "kenny-agent.exe"
    cache.write_bytes(BINARY_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(cache))
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/papa-pc/installer", headers=_bearer(app))
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.read("kenny-agent.exe") == BINARY_BYTES


def test_explicit_binary_wins_over_cache(tmp_path, monkeypatch):
    """KENNY_AGENT_BINARY takes precedence over the GitHub cache."""

    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    cache = tmp_path / "kenny-agent.exe"
    cache.write_bytes(b"CACHED BYTES")
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(cache))
    explicit = tmp_path / "manual.exe"
    explicit.write_bytes(BINARY_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY", str(explicit))
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/papa-pc/installer", headers=_bearer(app))
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.read("kenny-agent.exe") == BINARY_BYTES

"""Agent distribution: installer download, shareable link, update trigger, public binary."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from starlette.testclient import TestClient

from kenny_server.distribution import _install_sh, _sha256_file, agent_binary_path
from kenny_server.main import build_app

BINARY_BYTES = b"MZ fake kenny-agent.exe payload \x00\x01\x02"
LINUX_BYTES = b"\x7fELF fake kenny-agent linux payload"


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
        assert c.get("/api/agents/example-pc/installer").status_code == 401


def test_installer_returns_zip_with_token(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/example-pc/installer", headers=_bearer(app))
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(zf.namelist())
        assert {
            "kenny-agent.exe",
            "setup.bat",
            "kenny-agent.setup.json",
            "README.txt",
        } <= names
        # the connection settings (including the secret enroll token) live in the sidecar
        cfg = json.loads(zf.read("kenny-agent.setup.json").decode())
        assert cfg["server"] == "wss://kenny.example.com/agent/ws"
        assert cfg["agent_id"] == "example-pc"
        assert cfg["enroll_token"]  # minted one-time enrollment token provisions the agent
        assert cfg["server_pubkey"]  # pinned server public key travels for anti-spoofing
        assert isinstance(cfg["telemetry_interval_secs"], int)
        # the launcher just runs the self-elevating setup subcommand; no secret in it
        bat = zf.read("setup.bat").decode()
        assert 'kenny-agent.exe" setup' in bat
        assert cfg["enroll_token"] not in bat
        assert "Server public key" in zf.read("README.txt").decode()


def test_installer_503_without_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/agents/example-pc/installer", headers=_bearer(app)).status_code == 503


def test_share_link_then_public_download_once(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/example-laptop/share-link", headers=_bearer(app))
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
        nonce = app.state.share_links.create("example-pc", "binary", 600)
        r = c.get(f"/d/binary/{nonce}")  # public, no auth
        assert r.status_code == 200
        assert r.content == BINARY_BYTES
        assert c.get("/d/binary/does-not-exist").status_code == 404


def test_update_requires_auth_and_502_without_agent(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/agents/example-pc/update").status_code == 401
        # operator-authed but no online agent -> 502
        r = c.post("/api/agents/example-pc/update", headers=_bearer(app))
        assert r.status_code == 502


def test_sha256_helper(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(BINARY_BYTES)
    assert _sha256_file(str(p)) == hashlib.sha256(BINARY_BYTES).hexdigest()


# -- enrollment endpoint (ADR-0023) -----------------------------------------


def _agent_pubkey() -> str:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pub = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return base64.b64encode(pub).decode()


def test_enroll_with_bearer_token(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        # Mint the one-time enrollment token (operator path).
        token = c.post("/api/agents/enroll-pc/token", headers=_bearer(app)).json()["token"]
        # The agent enrolls its public key using that token (no operator auth).
        pub = _agent_pubkey()
        r = c.post(
            "/api/agents/enroll-pc/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json={"public_key": pub},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        # Re-enrolling is refused (bind-once).
        r2 = c.post(
            "/api/agents/enroll-pc/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json={"public_key": _agent_pubkey()},
        )
        assert r2.status_code == 409


def test_enroll_with_json_token_field(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        token = c.post("/api/agents/json-pc/token", headers=_bearer(app)).json()["token"]
        r = c.post(
            "/api/agents/json-pc/enroll",
            json={"public_key": _agent_pubkey(), "token": token},
        )
        assert r.status_code == 200, r.text


def test_enroll_bad_token_401(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/agents/example-pc/enroll",
            headers={"Authorization": "Bearer wrong-token"},
            json={"public_key": _agent_pubkey()},
        )
        assert r.status_code == 401


def test_enroll_missing_public_key_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        token = c.post("/api/agents/bad-pc/token", headers=_bearer(app)).json()["token"]
        r = c.post(
            "/api/agents/bad-pc/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert r.status_code == 400


# -- agent-binary status / fetch / precedence (ADR-0015) --------------------


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


# -- Linux agent distribution (ADR-0035 Phase 4 / ADR-0038) -----------------


@pytest.fixture
def linux_binary(tmp_path, monkeypatch):
    p = tmp_path / "kenny-agent-linux"
    p.write_bytes(LINUX_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX", str(p))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    return p


def test_install_sh_content_contract():
    script = _install_sh(
        agent_id="study-pc",
        token="tok-abc123",
        wss="wss://kenny.example.com/agent/ws",
        server_pubkey="PUBKEYb64==",
        interval=900,
        binary_url="https://kenny.example.com/d/binary/NONCE?os=linux",
    )
    # POSIX sh, LF only, strict mode, must run as root
    assert script.startswith("#!/bin/sh\n")
    assert "\r" not in script
    assert "set -eu" in script
    assert '[ "$(id -u)" -eq 0 ]' in script
    assert "sudo sh" in script
    # arch detection maps arm64/aarch64 -> aarch64, else x86_64
    assert "uname -m" in script
    assert "aarch64|arm64) arch=aarch64" in script
    assert "*) arch=x86_64" in script
    # tempdir + cleanup trap, and the arch-qualified download of the baked url
    assert "mktemp -d" in script
    assert "trap 'rm -rf" in script
    assert 'curl -fsSL "https://kenny.example.com/d/binary/NONCE?os=linux&arch=$arch"' in script
    assert 'chmod +x "$BIN"' in script
    # the exact setup invocation contract, with values shell-quoted
    assert '"$BIN" setup \\' in script
    assert "--server 'wss://kenny.example.com/agent/ws'" in script
    assert "--agent-id 'study-pc'" in script
    assert "--server-pubkey 'PUBKEYb64=='" in script
    assert "--enroll-token 'tok-abc123'" in script
    assert "--telemetry-interval-secs 900" in script
    # success line points the operator at the service
    assert "systemctl status kenny-agent" in script


def test_install_sh_omits_absent_pubkey_and_token():
    script = _install_sh(
        agent_id="study-pc",
        token="",
        wss="wss://k/agent/ws",
        server_pubkey="",
        interval=60,
        binary_url="https://k/d/binary/N?os=linux",
    )
    assert "--server-pubkey" not in script
    assert "--enroll-token" not in script
    assert "--agent-id 'study-pc'" in script
    assert "--telemetry-interval-secs 60" in script


def test_agent_binary_path_per_os_arch(tmp_path, monkeypatch):
    # windows default is bit-identical to today
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX_AARCH64", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    assert agent_binary_path() is None
    assert agent_binary_path("linux", "x86_64") is None

    lx = tmp_path / "linux-x64"
    lx.write_bytes(LINUX_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX", str(lx))
    assert agent_binary_path("linux", "x86_64") == str(lx)
    # aarch64 uses a distinct env var and is still unset here
    assert agent_binary_path("linux", "aarch64") is None

    arm = tmp_path / "linux-arm"
    arm.write_bytes(LINUX_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX_AARCH64", str(arm))
    assert agent_binary_path("linux", "aarch64") == str(arm)
    # windows still unaffected
    assert agent_binary_path() is None


def test_installer_linux_returns_script(tmp_path, linux_binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/agents/study-pc/installer?os=linux", headers=_bearer(app))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/x-shellscript")
        body = r.text
        assert body.startswith("#!/bin/sh\n")
        assert "--agent-id 'study-pc'" in body


def test_share_link_linux_oneliner_and_install_flow(tmp_path, linux_binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agents/study-pc/share-link?os=linux", headers=_bearer(app))
        assert r.status_code == 200
        j = r.json()
        assert j["url"].startswith("https://kenny.example.com/d/install/")
        assert j["oneliner"] == f"curl -fsSL {j['url']} | sudo sh"
        assert j["expires_in"] == 3600
        path = j["url"].split("kenny.example.com", 1)[1]

        # fetching the install link yields the script (correct content-type)
        first = c.get(path)
        assert first.status_code == 200
        assert first.headers["content-type"].startswith("text/x-shellscript")
        script = first.text
        assert "--agent-id 'study-pc'" in script
        # the install nonce is one-time (consumed on fetch)
        assert c.get(path).status_code == 404

        # the paired binary nonce baked into the script is still resolvable,
        # even though the install nonce was consumed.
        marker = "/d/binary/"
        start = script.index(marker) + len(marker)
        end = script.index("?os=linux", start)
        binary_nonce = script[start:end]
        b = c.get(f"/d/binary/{binary_nonce}?os=linux&arch=x86_64")
        assert b.status_code == 200
        assert b.content == LINUX_BYTES


def test_update_picks_linux_binary_for_linux_agent(tmp_path, monkeypatch):
    # Only a linux binary is configured (no windows binary at all).
    lx = tmp_path / "linux-x64"
    lx.write_bytes(LINUX_BYTES)
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX", str(lx))
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    app = _app(tmp_path)
    # Register a linux agent, then mark it offline so the update fails fast at the
    # send (502) rather than 503 — proving it resolved the linux binary by os.
    reg = app.state.registry

    async def _noop(_frame):
        return None

    reg.register_signed_async("linux-pc", {"os": "linux"}, _noop)
    reg.mark_offline("linux-pc")
    with TestClient(app) as c:
        r = c.post("/api/agents/linux-pc/update", headers=_bearer(app))
        # 502 (agent offline) not 503 (binary missing) => linux binary was selected
        assert r.status_code == 502


def test_update_503_for_linux_agent_without_linux_binary(tmp_path, binary):
    # Only a windows binary exists; a linux agent must not fall back to it.
    app = _app(tmp_path)
    reg = app.state.registry

    async def _noop(_frame):
        return None

    reg.register_signed_async("linux-pc", {"os": "linux"}, _noop)
    reg.mark_offline("linux-pc")
    with TestClient(app) as c:
        r = c.post("/api/agents/linux-pc/update", headers=_bearer(app))
        assert r.status_code == 503


def test_agent_binary_status_by_os(tmp_path, linux_binary, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/agent-binary", headers=_bearer(app)).json()
        # windows absent, linux present: the Linux path must not be blocked
        assert body["available"] is False
        assert body["by_os"]["windows"] is False
        assert body["by_os"]["linux"] is True


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
        r = c.get("/api/agents/example-pc/installer", headers=_bearer(app))
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
        r = c.get("/api/agents/example-pc/installer", headers=_bearer(app))
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.read("kenny-agent.exe") == BINARY_BYTES

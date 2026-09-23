"""Agent distribution: installer download, shareable link, update trigger, public binary."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from functools import partial

import pytest
from starlette.testclient import TestClient

from kenny_server import agent_release
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


# -- enrollment endpoint (ADR-0022) -----------------------------------------


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
        assert "github_configured" not in body  # the gate it reported is gone (ADR-0057)
        assert "releases/latest" in body["message"]


def test_agent_binary_status_manual(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/agent-binary", headers=_bearer(app)).json()
        assert body["available"] is True
        assert body["source"] == "manual"


# -- Linux agent distribution (ADR-0031 Phase 4 / ADR-0034) -----------------


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


def test_install_sh_pinned_arch_skips_uname_detection():
    """An operator-pinned arch (ADR-0036) is emitted literally; the script never
    shells out to `uname -m` to decide it."""

    script = _install_sh(
        agent_id="study-pc",
        token="tok-abc123",
        wss="wss://kenny.example.com/agent/ws",
        server_pubkey="PUBKEYb64==",
        interval=900,
        binary_url="https://kenny.example.com/d/binary/NONCE?os=linux",
        arch="aarch64",
    )
    assert "uname -m" not in script
    assert "case " not in script
    assert "arch=aarch64\n" in script
    assert 'curl -fsSL "https://kenny.example.com/d/binary/NONCE?os=linux&arch=$arch"' in script


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


def test_share_link_pinned_arch_survives_mint_to_fetch_gap(tmp_path, monkeypatch):
    """An operator-pinned ``?arch=`` (ADR-0036) rides on both the install and the
    binary nonce, so `public_install` recovers it at fetch time and the script it
    renders skips `uname -m` detection — proving the pin, not `uname`, decided it."""

    arm = tmp_path / "linux-arm"
    arm.write_bytes(LINUX_BYTES)
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX_AARCH64", str(arm))
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post(
            "/api/agents/study-pc/share-link?os=linux&arch=aarch64", headers=_bearer(app)
        )
        assert r.status_code == 200
        path = r.json()["url"].split("kenny.example.com", 1)[1]

        script = c.get(path).text
        assert "uname -m" not in script
        assert "arch=aarch64\n" in script

        marker = "/d/binary/"
        start = script.index(marker) + len(marker)
        end = script.index("?os=linux", start)
        binary_nonce = script[start:end]
        # No arch on this request either — only the aarch64 binary is configured,
        # so a wrong (x86_64-default) fallback would 503 instead of serving it.
        b = c.get(f"/d/binary/{binary_nonce}?os=linux")
        assert b.status_code == 200
        assert b.content == LINUX_BYTES


def test_installer_pinned_arch_query_param_reaches_the_script(tmp_path, monkeypatch):
    lx = tmp_path / "linux-x64"
    lx.write_bytes(LINUX_BYTES)
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX", str(lx))
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get(
            "/api/agents/study-pc/installer?os=linux&arch=x86_64", headers=_bearer(app)
        )
        assert r.status_code == 200
        assert "uname -m" not in r.text
        assert "arch=x86_64\n" in r.text


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


def test_update_picks_aarch64_binary_for_aarch64_agent(tmp_path, monkeypatch):
    """Coverage gap named in #139: no test asserted that an agent reporting
    ``arch: aarch64`` in ``register.meta`` actually gets the aarch64 binary.

    Only an aarch64 binary is configured (no x86_64 binary at all), so a wrong
    selection surfaces as 503 (binary missing) instead of 502 (agent offline).
    Note this test alone does not reproduce the historical bug: `trigger_update`
    already read `meta.get("arch")` correctly before this fix (see
    `Agent.arch` in registry.py) — the actual bug was that no real agent ever
    put `arch` on the wire in the first place (kenny-agent's `RegisterMeta` had
    no such field). That part is covered by the Rust `fixtures_round_trip` test
    and `tunnel.rs` sending `util::arch()`. This test guards the server-side
    selection logic against a future regression once arch *is* reported.
    """

    arm = tmp_path / "linux-arm"
    arm.write_bytes(LINUX_BYTES)
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX_AARCH64", str(arm))
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    app = _app(tmp_path)
    reg = app.state.registry

    async def _noop(_frame):
        return None

    reg.register_signed_async("arm-pc", {"os": "linux", "arch": "aarch64"}, _noop)
    reg.mark_offline("arm-pc")
    with TestClient(app) as c:
        r = c.post("/api/agents/arm-pc/update", headers=_bearer(app))
        # 502 (agent offline) not 503 (binary missing) => the aarch64 binary was
        # selected.
        assert r.status_code == 502


def test_update_defaults_to_x86_64_for_legacy_agent_without_arch(tmp_path, monkeypatch):
    """A legacy agent that predates #139 and never reports `arch` must still
    resolve to x86_64 (the documented backward-compat default), not fail to
    resolve at all. Only an aarch64 binary is configured, so this proves arch
    actually gates selection rather than matching anything."""

    arm = tmp_path / "linux-arm"
    arm.write_bytes(LINUX_BYTES)
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX_AARCH64", str(arm))
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    app = _app(tmp_path)
    reg = app.state.registry

    async def _noop(_frame):
        return None

    reg.register_signed_async("legacy-pc", {"os": "linux"}, _noop)
    reg.mark_offline("legacy-pc")
    with TestClient(app) as c:
        r = c.post("/api/agents/legacy-pc/update", headers=_bearer(app))
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


def test_agent_binary_status_targets_reflect_per_arch_availability(tmp_path, monkeypatch):
    """The dashboard's arch dropdown (ADR-0036) is driven by ``targets``: every
    combination we could ever ship, each flagged by whether a binary is actually
    configured for it right now."""

    lx = tmp_path / "linux-x64"
    lx.write_bytes(LINUX_BYTES)
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX_AARCH64", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_LINUX", str(lx))
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    app = _app(tmp_path)
    with TestClient(app) as c:
        targets = c.get("/api/agent-binary", headers=_bearer(app)).json()["targets"]
        by_combo = {(t["os"], t["arch"]): t["available"] for t in targets}
        assert by_combo == {
            ("windows", "x86_64"): False,
            ("linux", "x86_64"): True,
            ("linux", "aarch64"): False,
        }


# -- dev channel (ADR-0048) ---------------------------------------------------


def test_agent_binary_path_dev_resolves_channel_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_CACHE", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    # nothing cached yet
    assert agent_binary_path(channel="dev") is None

    dev_cache = tmp_path / "kenny-agent-dev.exe"
    dev_cache.write_bytes(LINUX_BYTES)
    assert agent_binary_path(channel="dev") == str(dev_cache)
    # stable path is unaffected by the dev cache existing
    assert agent_binary_path() is None


def test_agent_binary_path_dev_ignores_stable_manual_override(tmp_path, monkeypatch):
    """Dev has no manual-placement env in this iteration; KENNY_AGENT_BINARY
    (stable's override) must never leak into the dev resolution."""

    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    manual = tmp_path / "manual.exe"
    manual.write_bytes(BINARY_BYTES)
    monkeypatch.setenv("KENNY_AGENT_BINARY", str(manual))
    # stable resolves the manual override...
    assert agent_binary_path() == str(manual)
    # ...but dev does not, even though nothing is cached at the dev path
    assert agent_binary_path(channel="dev") is None

    dev_cache = tmp_path / "kenny-agent-dev.exe"
    dev_cache.write_bytes(LINUX_BYTES)
    assert agent_binary_path(channel="dev") == str(dev_cache)


def test_agent_binary_path_dev_linux_per_arch(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX", raising=False)
    monkeypatch.delenv("KENNY_AGENT_BINARY_LINUX_AARCH64", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "kenny.sqlite"))
    assert agent_binary_path("linux", "x86_64", "dev") is None
    dev_lx = tmp_path / "kenny-agent-linux-x86_64-dev"
    dev_lx.write_bytes(LINUX_BYTES)
    assert agent_binary_path("linux", "x86_64", "dev") == str(dev_lx)
    # the stable linux cache is a distinct path, unaffected
    assert agent_binary_path("linux", "x86_64") is None


def test_agent_binary_status_dev_sibling_in_status_route(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "dist.sqlite"))
    dev_cache = tmp_path / "kenny-agent-dev.exe"
    dev_cache.write_bytes(BINARY_BYTES)
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/agent-binary", headers=_bearer(app)).json()
        # the existing stable response shape is untouched...
        assert body["available"] is False
        # ...and the dev sibling is additive
        assert body["dev"]["available"] is True
        assert body["dev"]["by_os"]["windows"] is True


def test_agent_binary_status_dev_absent_when_no_dev_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "dist2.sqlite"))
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/agent-binary", headers=_bearer(app)).json()
        assert body["dev"]["available"] is False


def test_trigger_update_uses_agent_desired_channel(tmp_path, monkeypatch):
    """A manual 'update now' for a dev-desired agent pulls the dev binary."""

    monkeypatch.setenv("KENNY_PUBLIC_URL", "https://kenny.example.com")
    monkeypatch.setenv("KENNY_AGENT_BINARY_CACHE", str(tmp_path / "nope.exe"))
    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "dist3.sqlite"))
    # Only a dev binary is configured (no stable one at all).
    dev_cache = tmp_path / "kenny-agent-dev.exe"
    dev_cache.write_bytes(BINARY_BYTES)
    app = _app(tmp_path)
    reg = app.state.registry

    async def _noop(_frame):
        return None

    reg.register_signed_async("dev-pc", {"os": "windows"}, _noop)
    reg.mark_offline("dev-pc")
    with TestClient(app) as c:
        h = _bearer(app)
        # flip this agent's desired channel to dev via the dashboard route
        r = c.put("/api/agent/dev-pc/channel", headers=h, json={"channel": "dev"})
        assert r.status_code == 200, r.text
        assert r.json()["desired_channel"] == "dev"

        # 502 (agent offline) not 503 (binary missing) => the dev binary was
        # selected, proving the desired channel drove resolution.
        r2 = c.post("/api/agents/dev-pc/update", headers=h)
        assert r2.status_code == 502


def test_agent_binary_status_requires_auth(tmp_path, binary):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/agent-binary").status_code == 401


def test_agent_binary_fetch_attempts_without_a_token(tmp_path, monkeypatch):
    """No credential is a precondition any more (ADR-0057), so nothing refuses up front.

    This route used to 400 without KENNY_GITHUB_TOKEN. Reads are anonymous now, so
    the attempt is always worth making; whether it succeeds is the network's answer,
    not a configuration check's.
    """

    monkeypatch.delenv("KENNY_GITHUB_TOKEN", raising=False)

    def fake_fetch(**_kwargs):
        return agent_release.FetchResult(ok=True, source="github", message="fetched", version="9.9.9")

    monkeypatch.setattr(agent_release, "fetch_latest_agent_binary", fake_fetch)
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agent-binary/fetch", headers=_bearer(app))
        assert r.status_code == 200
        assert r.json()["ok"] is True


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


# -- the durable record of the last agent-binary refresh -----------------------
#
# ``last_fetch`` lives on ``app.state`` and dies with the process. That was the
# only channel carrying *why* a refresh failed, so a restarted server showed a
# months-old staged version with no reason attached — and the Fleet banner said
# "no fetch has been attempted yet" while a real, repeated failure stood.
# ``last_check`` is the same outcome read back off ADR-0040's availability row.


def test_agent_binary_status_carries_the_durable_last_check(tmp_path, monkeypatch):
    """A failure recorded before this process started is still reported."""

    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_GITHUB_TOKEN", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        c.portal.call(
            partial(
                app.state.update_store.set_availability,
                "agent",
                version="2.1.0",
                ok=False,
                message="GitHub API 401 (token expired)",
            )
        )
        body = c.get("/api/agent-binary", headers=_bearer(app)).json()
        assert body["last_check"]["ok"] is False
        assert body["last_check"]["message"] == "GitHub API 401 (token expired)"
        assert body["last_check"]["version"] == "2.1.0"
        assert body["last_check"]["checked_at"]


def test_agent_binary_status_records_the_startup_attempt(tmp_path, monkeypatch):
    """Startup fetches without a credential now, and records how it went.

    There is no "skipped, not configured" branch left to assert (ADR-0057): the
    attempt always happens. Here it is the suite's network guard that fails it,
    which is exactly the shape a real unreachable GitHub would take.
    """

    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.delenv("KENNY_GITHUB_TOKEN", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/agent-binary", headers=_bearer(app)).json()
        assert body["last_check"] is not None
        assert body["last_check"]["ok"] is False
        assert body["last_check"]["message"]  # the reason, whatever it was
        assert body["last_check"]["checked_at"]


def test_agent_binary_status_survives_a_missing_update_store(tmp_path, monkeypatch):
    """Fleet reads this route on every render; a store hiccup is not a 500."""

    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as c:
        app.state.update_store = None
        r = c.get("/api/agent-binary", headers=_bearer(app))
        assert r.status_code == 200
        assert r.json()["last_check"] is None


def test_manual_fetch_persists_its_outcome_durably(tmp_path, monkeypatch):
    """An operator's failed retry outlives the process that ran it."""

    monkeypatch.delenv("KENNY_AGENT_BINARY", raising=False)
    monkeypatch.setenv("KENNY_GITHUB_TOKEN", "ghp_whatever")

    def fake_fetch(**_kwargs):
        return agent_release.FetchResult(
            ok=False, source="none", message="GitHub API 403 (rate limited)"
        )

    monkeypatch.setattr(agent_release, "fetch_latest_agent_binary", fake_fetch)
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/agent-binary/fetch", headers=_bearer(app))
        assert r.status_code == 502
        row = c.portal.call(partial(app.state.update_store.get_availability, "agent"))
        assert row["ok"] == 0
        assert row["message"] == "GitHub API 403 (rate limited)"

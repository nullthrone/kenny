"""Agent distribution: download an installer from the GUI, share an expiring link,
and trigger a server-side self-update (ADR-0012, ADR-0013).

The server serves a **prebuilt** agent binary (``KENNY_AGENT_BINARY``) and injects
per-install config — it does not build per download. Endpoints:

* ``GET  /api/agents/{id}/installer``    (operator) -> Windows: a ZIP (exe + setup.bat +
  kenny-agent.setup.json + README); Linux (``?os=linux``): the install ``sh`` script
  directly. Mints a fresh per-agent token via the token store.
* ``POST /api/agents/{id}/share-link``   (operator) -> Windows: an expiring one-time
  ``/d/installer/{nonce}`` link; Linux (``?os=linux``): a ``curl … | sudo sh`` one-liner
  pointing at ``/d/install/{nonce}``.
* ``GET  /d/installer/{nonce}``          (public, nonce-gated) -> the Windows installer ZIP, once.
* ``GET  /d/install/{nonce}``            (public, nonce-gated) -> the Linux install script, once
  (mints the token + a paired non-consumed ``/d/binary`` nonce baked into the script).
* ``POST /api/agents/{id}/update``       (operator) -> compute the (OS-matched) binary sha256,
  mint a short-lived ``/d/binary/{nonce}`` URL, and send ``agent_update`` to the online agent.
* ``GET  /d/binary/{nonce}``             (public, nonce-gated) -> the raw binary (self-update /
  Linux install download), served per the nonce's os/arch.

``/d/*`` is exempt from operator auth (the nonce is the credential); see ``auth.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import time
import zipfile
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import agent_release
from .agent_release import _sha256_file as _sha256_file  # re-export (used by tests)
from .keystore import KeyStore
from .registry import AgentRegistry
from .tokenstore import AgentTokenStore
from .tunnel import AgentTunnel, ToolError

INSTALLER_TTL_S = 3600  # one hour for an operator-shared installer link
BINARY_TTL_S = 600  # ten minutes for a self-update binary fetch


def agent_binary_path(os_name: str = "windows", arch: str = "x86_64") -> str | None:
    """Path to the prebuilt agent binary for ``(os_name, arch)``, or None.

    Operator-placed env override wins, otherwise the GitHub-fetched cache
    (``agent_release.cache_path``) is used if present (ADR-0015). Overrides:

    * windows/x86_64 -> ``KENNY_AGENT_BINARY`` (the default, pre-Linux behavior).
    * linux/x86_64   -> ``KENNY_AGENT_BINARY_LINUX``.
    * linux/aarch64  -> ``KENNY_AGENT_BINARY_LINUX_AARCH64``.
    """

    if os_name == "linux":
        env = (
            "KENNY_AGENT_BINARY_LINUX_AARCH64" if arch == "aarch64" else "KENNY_AGENT_BINARY_LINUX"
        )
        override = os.environ.get(env, "").strip()
        if override and os.path.exists(override):
            return override
        cache = agent_release.cache_path("linux", arch)
        return cache if os.path.exists(cache) else None

    path = os.environ.get("KENNY_AGENT_BINARY", "").strip()
    if path and os.path.exists(path):
        return path
    cache = agent_release.cache_path()
    return cache if os.path.exists(cache) else None


def _public_url() -> str:
    """Externally reachable base URL of this server (for links the agent/user open)."""

    base = os.environ.get("KENNY_PUBLIC_URL", "").strip()
    if base:
        return base.rstrip("/")
    port = os.environ.get("KENNY_PORT", "8000")
    return f"http://localhost:{port}"


def _wss_url() -> str:
    """Agent --server URL derived from the public URL (https->wss, http->ws)."""

    base = _public_url()
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base.rstrip("/") + "/agent/ws"


@dataclass
class _Nonce:
    agent_id: str
    kind: str  # "installer" | "install" | "binary"
    expires_at: float
    used: bool = False
    os: str = "windows"
    arch: str = "x86_64"
    # For a Linux "install" nonce, the paired "binary" nonce it hands the target
    # box (baked into the install script's download URL). It lives longer than
    # the install nonce (which is consumed on fetch) so the fetch->run gap is OK.
    binary_nonce: str | None = None


@dataclass
class ShareLinks:
    """In-memory nonce store for shareable download links (dev-grade, like CallLog)."""

    _nonces: dict[str, _Nonce] = field(default_factory=dict)

    def create(
        self,
        agent_id: str,
        kind: str,
        ttl_s: int,
        *,
        os_name: str = "windows",
        arch: str = "x86_64",
        binary_nonce: str | None = None,
    ) -> str:
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = _Nonce(
            agent_id,
            kind,
            time.time() + ttl_s,
            os=os_name,
            arch=arch,
            binary_nonce=binary_nonce,
        )
        return nonce

    def resolve_entry(self, nonce: str, kind: str, *, consume: bool) -> _Nonce | None:
        entry = self._nonces.get(nonce)
        if entry is None or entry.kind != kind or entry.used:
            return None
        if time.time() > entry.expires_at:
            self._nonces.pop(nonce, None)
            return None
        if consume:
            entry.used = True
        return entry

    def resolve(self, nonce: str, kind: str, *, consume: bool) -> str | None:
        entry = self.resolve_entry(nonce, kind, consume=consume)
        return entry.agent_id if entry is not None else None


def _setup_bat() -> str:
    return (
        "@echo off\r\n"
        "rem kenny-agent installer. Double-click this file and approve the Windows security prompt.\r\n"
        "rem The agent reads its connection settings from kenny-agent.setup.json (next to this file),\r\n"
        "rem elevates via UAC, installs itself into %ProgramFiles%\\kenny as an auto-start service,\r\n"
        "rem generates its Ed25519 keypair, and enrolls its public key with the server.\r\n"
        '"%~dp0kenny-agent.exe" setup\r\n'
        "pause\r\n"
    )


def _setup_json(agent_id: str, token: str, wss: str, interval: int, server_pubkey: str) -> str:
    return json.dumps(
        {
            "server": wss,
            "agent_id": agent_id,
            "enroll_token": token,
            "server_pubkey": server_pubkey,
            "telemetry_interval_secs": interval,
        },
        indent=2,
    )


def _readme(agent_id: str, wss: str, server_pubkey: str) -> str:
    return (
        "kenny-agent\r\n"
        "===========\r\n\r\n"
        f"Agent id          : {agent_id}\r\n"
        f"Server            : {wss}\r\n"
        f"Server public key : {server_pubkey}\r\n\r\n"
        "To install: double-click setup.bat and approve the Windows security prompt.\r\n"
        "Setup installs kenny-agent.exe as an auto-starting Windows service into\r\n"
        "%ProgramFiles%\\kenny, reading its connection settings from the bundled\r\n"
        "kenny-agent.setup.json. On first run the agent generates its Ed25519 keypair\r\n"
        "and enrolls its public key with the server using the one-time enrollment token.\r\n"
        "Thereafter only signatures authenticate. The pinned server public key above lets\r\n"
        "the agent verify the server's challenge (anti-spoofing).\r\n"
        "To remove (as Administrator): kenny-agent.exe uninstall\r\n"
    )


def _norm_arch(arch: str | None) -> str:
    """Normalize a reported/queried arch onto our release naming (x86_64|aarch64)."""

    a = (arch or "").strip().lower()
    return "aarch64" if a in ("aarch64", "arm64") else "x86_64"


def _sh_squote(value: str) -> str:
    """POSIX single-quote a value (server-controlled, but quoted defensively)."""

    return "'" + value.replace("'", "'\\''") + "'"


def _install_sh(
    agent_id: str,
    token: str,
    wss: str,
    server_pubkey: str,
    interval: int,
    binary_url: str,
) -> str:
    """The Linux install script (POSIX ``sh``, LF line endings).

    Downloads the arch-matched agent binary from ``binary_url`` (which already
    carries ``?os=linux``; the script appends ``&arch=$arch``) and runs the exact
    agent CLI contract:

        <binary> setup --server <wss> --agent-id <id> \
            [--server-pubkey <b64>] [--enroll-token <tok>] \
            --telemetry-interval-secs <n>

    The enrollment token lives only here (in argv) — never on disk.
    """

    setup = [
        '"$BIN" setup \\',
        f"  --server {_sh_squote(wss)} \\",
        f"  --agent-id {_sh_squote(agent_id)} \\",
    ]
    if server_pubkey:
        setup.append(f"  --server-pubkey {_sh_squote(server_pubkey)} \\")
    if token:
        setup.append(f"  --enroll-token {_sh_squote(token)} \\")
    setup.append(f"  --telemetry-interval-secs {int(interval)}")
    setup_block = "\n".join(setup)

    return (
        "#!/bin/sh\n"
        "# kenny-agent Linux installer (generated by kenny-server).\n"
        "# Run as root, e.g.:  curl -fsSL <this-url> | sudo sh\n"
        "set -eu\n"
        "\n"
        'if [ "$(id -u)" -eq 0 ]; then\n'
        "  :\n"
        "else\n"
        '  echo "kenny-agent install must run as root. Re-run with:" >&2\n'
        '  echo "  curl -fsSL <install-url> | sudo sh" >&2\n'
        "  exit 1\n"
        "fi\n"
        "\n"
        "# Map the machine arch onto our release naming.\n"
        'case "$(uname -m)" in\n'
        "  aarch64|arm64) arch=aarch64 ;;\n"
        "  *) arch=x86_64 ;;\n"
        "esac\n"
        "\n"
        "tmp=$(mktemp -d)\n"
        "trap 'rm -rf \"$tmp\"' EXIT\n"
        'BIN="$tmp/kenny-agent"\n'
        "\n"
        'echo "Downloading kenny-agent ($arch)..."\n'
        f'curl -fsSL "{binary_url}&arch=$arch" -o "$BIN"\n'
        'chmod +x "$BIN"\n'
        "\n"
        f"{setup_block}\n"
        "\n"
        'echo "kenny-agent installed. Check status with: systemctl status kenny-agent"\n'
    )


def _build_installer_zip(binary: str, agent_id: str, token: str, server_pubkey: str) -> bytes:
    wss = _wss_url()
    interval = int(os.environ.get("KENNY_TELEMETRY_INTERVAL_SECS", "900") or 900)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with open(binary, "rb") as fh:
            zf.writestr("kenny-agent.exe", fh.read())
        zf.writestr("setup.bat", _setup_bat())
        zf.writestr(
            "kenny-agent.setup.json",
            _setup_json(agent_id, token, wss, interval, server_pubkey),
        )
        zf.writestr("README.txt", _readme(agent_id, wss, server_pubkey))
    return buf.getvalue()


def build_download_routes(
    *,
    registry: AgentRegistry,
    token_store: AgentTokenStore,
    tunnel: AgentTunnel,
    share_links: ShareLinks,
    key_store: KeyStore | None = None,
) -> list[Route]:
    """Build the agent-distribution routes (installer download, share link, update)."""

    def _server_pubkey() -> str:
        return key_store.server_public_key_b64() if key_store is not None else ""

    def _interval() -> int:
        return int(os.environ.get("KENNY_TELEMETRY_INTERVAL_SECS", "900") or 900)

    def _req_os(request: Request) -> str:
        return (request.query_params.get("os") or "windows").strip().lower() or "windows"

    async def _linux_install_script(agent_id: str) -> str:
        """Mint a fresh token + a non-consumed Linux binary nonce, render the script.

        The binary nonce lives as long as the install link (INSTALLER_TTL_S) so it
        survives the fetch->run gap. The token rides only in the script argv.
        """

        token = await token_store.create_or_rotate(agent_id)
        binary_nonce = share_links.create(agent_id, "binary", INSTALLER_TTL_S, os_name="linux")
        binary_url = f"{_public_url()}/d/binary/{binary_nonce}?os=linux"
        return _install_sh(agent_id, token, _wss_url(), _server_pubkey(), _interval(), binary_url)

    async def installer(request: Request) -> Response:
        agent_id = request.path_params["id"]
        if _req_os(request) == "linux":
            # The operator brings this script to the Linux box and runs it as root.
            script = await _linux_install_script(agent_id)
            return Response(
                script,
                media_type="text/x-shellscript",
                headers={"Content-Disposition": f'attachment; filename="install-{agent_id}.sh"'},
            )
        binary = agent_binary_path()
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        token = await token_store.create_or_rotate(agent_id)
        data = _build_installer_zip(binary, agent_id, token, _server_pubkey())
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="kenny-agent-{agent_id}.zip"'},
        )

    async def share_link(request: Request) -> Response:
        agent_id = request.path_params["id"]
        if _req_os(request) == "linux":
            # A one-time install link + its paired (non-consumed) binary nonce. We
            # store the binary nonce on the install nonce so /d/install can reach it.
            binary_nonce = share_links.create(agent_id, "binary", INSTALLER_TTL_S, os_name="linux")
            install_nonce = share_links.create(
                agent_id,
                "install",
                INSTALLER_TTL_S,
                os_name="linux",
                binary_nonce=binary_nonce,
            )
            url = f"{_public_url()}/d/install/{install_nonce}"
            return JSONResponse(
                {
                    "url": url,
                    "oneliner": f"curl -fsSL {url} | sudo sh",
                    "expires_in": INSTALLER_TTL_S,
                }
            )
        nonce = share_links.create(agent_id, "installer", INSTALLER_TTL_S)
        url = f"{_public_url()}/d/installer/{nonce}"
        return JSONResponse({"url": url, "expires_in": INSTALLER_TTL_S})

    async def public_install(request: Request) -> Response:
        """Serve the Linux install script once, minting the token at fetch time."""

        nonce = request.path_params["nonce"]
        entry = share_links.resolve_entry(nonce, "install", consume=True)
        if entry is None:
            return JSONResponse({"error": "link invalid or expired"}, status_code=404)
        agent_id = entry.agent_id
        token = await token_store.create_or_rotate(agent_id)
        binary_nonce = entry.binary_nonce or share_links.create(
            agent_id, "binary", INSTALLER_TTL_S, os_name="linux"
        )
        binary_url = f"{_public_url()}/d/binary/{binary_nonce}?os=linux"
        script = _install_sh(agent_id, token, _wss_url(), _server_pubkey(), _interval(), binary_url)
        return Response(script, media_type="text/x-shellscript")

    async def public_installer(request: Request) -> Response:
        binary = agent_binary_path()
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        nonce = request.path_params["nonce"]
        agent_id = share_links.resolve(nonce, "installer", consume=True)
        if agent_id is None:
            return JSONResponse({"error": "link invalid or expired"}, status_code=404)
        token = await token_store.create_or_rotate(agent_id)
        data = _build_installer_zip(binary, agent_id, token, _server_pubkey())
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="kenny-agent-{agent_id}.zip"'},
        )

    async def enroll(request: Request) -> Response:
        """Bind an agent's Ed25519 public key on first contact (ADR-0023).

        Auth: the per-agent enrollment token (the minted installer token) acts as
        the one-time enrollment secret. It is read from the ``Authorization:
        Bearer <token>`` header, or a JSON ``token`` field as a fallback, and
        verified against the token store. Body: ``{"public_key": "<base64>"}``.
        Returns 200 on success, 409 if already enrolled, 401 on a bad token.
        """

        if key_store is None:
            return JSONResponse({"error": "key store not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        auth_header = request.headers.get("authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[len("bearer ") :].strip()
        if not token:
            token = str(body.get("token", "")).strip()
        if not token or not await token_store.verify(agent_id, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        public_key = body.get("public_key")
        if not isinstance(public_key, str) or not public_key:
            return JSONResponse({"error": "public_key is required"}, status_code=400)
        try:
            await key_store.enroll(agent_id, public_key)
        except ValueError as exc:
            msg = str(exc)
            if "already enrolled" in msg:
                return JSONResponse({"error": msg}, status_code=409)
            return JSONResponse({"error": msg}, status_code=400)
        return JSONResponse({"ok": True, "agent_id": agent_id})

    async def trigger_update(request: Request) -> Response:
        agent_id = request.path_params["id"]
        # Resolve the agent's OS/arch so we push (and serve) the matching binary.
        agent = registry.get(agent_id)
        os_name = agent.os if agent is not None else "windows"
        arch = agent.arch if agent is not None else "x86_64"
        binary = agent_binary_path(os_name=os_name, arch=arch)
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        version = agent_release.resolve_agent_version(binary)
        sha256 = _sha256_file(binary)
        nonce = share_links.create(agent_id, "binary", BINARY_TTL_S, os_name=os_name, arch=arch)
        url = f"{_public_url()}/d/binary/{nonce}"
        try:
            result = await tunnel.send_request(
                agent_id, "agent_update", {"version": version, "url": url, "sha256": sha256}, 120
            )
        except ToolError as exc:
            return JSONResponse({"ok": False, "error": exc.message}, status_code=502)
        except Exception as exc:  # noqa: BLE001 - agent offline etc., surface to UI
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, "version": version, "sha256": sha256, "result": result})

    async def public_binary(request: Request) -> Response:
        nonce = request.path_params["nonce"]
        # Not consumed: the agent's updater / installer may retry within the TTL.
        entry = share_links.resolve_entry(nonce, "binary", consume=False)
        if entry is None:
            return JSONResponse({"error": "link invalid or expired"}, status_code=404)
        os_name = entry.os
        # The install script appends the box's real arch as a query param.
        arch = _norm_arch(request.query_params.get("arch") or entry.arch)
        binary = agent_binary_path(os_name=os_name, arch=arch)
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        filename = "kenny-agent" if os_name == "linux" else "kenny-agent.exe"
        return FileResponse(binary, filename=filename, media_type="application/octet-stream")

    async def agent_binary_status(request: Request) -> Response:
        """Report binary availability + GitHub-fetch config for the dashboard (no network)."""

        win = agent_binary_path()
        status = agent_release.binary_status(manual_path=win)
        body = status.to_public()
        # ``available`` keeps its historical (Windows) meaning; ``by_os`` lets the
        # dashboard offer the Linux path even when the Windows binary is absent.
        body["available"] = win is not None
        body["by_os"] = {
            "windows": win is not None,
            "linux": (
                agent_binary_path("linux", "x86_64") is not None
                or agent_binary_path("linux", "aarch64") is not None
            ),
        }
        body["github_configured"] = agent_release.github_configured()
        body["repo"] = agent_release.github_repo()
        last = getattr(request.app.state, "last_fetch", None)
        body["last_fetch"] = last.to_public() if last is not None else None
        return JSONResponse(body)

    async def agent_binary_fetch(request: Request) -> Response:
        """Manually (re)trigger the GitHub fetch so no restart is needed."""

        if not agent_release.github_configured():
            return JSONResponse(
                {"ok": False, "error": "GitHub fetch not configured (set KENNY_GITHUB_TOKEN)"},
                status_code=400,
            )
        result = await asyncio.to_thread(agent_release.fetch_latest_agent_binary)
        request.app.state.last_fetch = result
        return JSONResponse(result.to_public(), status_code=200 if result.ok else 502)

    return [
        Route("/api/agents/{id}/installer", installer),
        Route("/api/agents/{id}/enroll", enroll, methods=["POST"]),
        Route("/api/agents/{id}/share-link", share_link, methods=["POST"]),
        Route("/api/agents/{id}/update", trigger_update, methods=["POST"]),
        Route("/api/agent-binary", agent_binary_status),
        Route("/api/agent-binary/fetch", agent_binary_fetch, methods=["POST"]),
        Route("/d/installer/{nonce}", public_installer),
        Route("/d/install/{nonce}", public_install),
        Route("/d/binary/{nonce}", public_binary),
    ]

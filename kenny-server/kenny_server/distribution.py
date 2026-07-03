"""Agent distribution: download an installer from the GUI, share an expiring link,
and trigger a server-side self-update (ADR-0012, ADR-0013).

The server serves a **prebuilt** agent binary (``KENNY_AGENT_BINARY``) and injects
per-install config — it does not build per download. Endpoints:

* ``GET  /api/agents/{id}/installer``    (operator) -> a ZIP (exe + setup.bat +
  kenny-agent.setup.json + README), minting a fresh per-agent token via the token store.
* ``POST /api/agents/{id}/share-link``   (operator) -> an expiring one-time ``/d/installer/{nonce}``
  link the target user can open without an operator login.
* ``GET  /d/installer/{nonce}``          (public, nonce-gated) -> the installer ZIP, once.
* ``POST /api/agents/{id}/update``       (operator) -> compute the binary sha256, mint a
  short-lived ``/d/binary/{nonce}`` URL, and send ``agent_update`` to the online agent.
* ``GET  /d/binary/{nonce}``             (public, nonce-gated) -> the raw exe (for self-update).

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
BINARY_TTL_S = 600      # ten minutes for a self-update binary fetch


def agent_binary_path() -> str | None:
    """Path to the prebuilt agent binary, or None if unavailable.

    Operator-placed ``KENNY_AGENT_BINARY`` wins; otherwise the GitHub-fetched
    cache (``agent_release.cache_path()``) is used if present (ADR-0015).
    """

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
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base.rstrip("/") + "/agent/ws"


@dataclass
class _Nonce:
    agent_id: str
    kind: str  # "installer" | "binary"
    expires_at: float
    used: bool = False


@dataclass
class ShareLinks:
    """In-memory nonce store for shareable download links (dev-grade, like CallLog)."""

    _nonces: dict[str, _Nonce] = field(default_factory=dict)

    def create(self, agent_id: str, kind: str, ttl_s: int) -> str:
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = _Nonce(agent_id, kind, time.time() + ttl_s)
        return nonce

    def resolve(self, nonce: str, kind: str, *, consume: bool) -> str | None:
        entry = self._nonces.get(nonce)
        if entry is None or entry.kind != kind or entry.used:
            return None
        if time.time() > entry.expires_at:
            self._nonces.pop(nonce, None)
            return None
        if consume:
            entry.used = True
        return entry.agent_id


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


def _setup_json(
    agent_id: str, token: str, wss: str, interval: int, server_pubkey: str
) -> str:
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


def _build_installer_zip(
    binary: str, agent_id: str, token: str, server_pubkey: str
) -> bytes:
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

    async def installer(request: Request) -> Response:
        binary = agent_binary_path()
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        token = await token_store.create_or_rotate(agent_id)
        data = _build_installer_zip(binary, agent_id, token, _server_pubkey())
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="kenny-agent-{agent_id}.zip"'},
        )

    async def share_link(request: Request) -> Response:
        agent_id = request.path_params["id"]
        nonce = share_links.create(agent_id, "installer", INSTALLER_TTL_S)
        url = f"{_public_url()}/d/installer/{nonce}"
        return JSONResponse({"url": url, "expires_in": INSTALLER_TTL_S})

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
        binary = agent_binary_path()
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        agent_id = request.path_params["id"]
        version = agent_release.resolve_agent_version(binary)
        sha256 = _sha256_file(binary)
        nonce = share_links.create(agent_id, "binary", BINARY_TTL_S)
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
        binary = agent_binary_path()
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        nonce = request.path_params["nonce"]
        # Not consumed: the agent's updater may retry within the TTL.
        agent_id = share_links.resolve(nonce, "binary", consume=False)
        if agent_id is None:
            return JSONResponse({"error": "link invalid or expired"}, status_code=404)
        return FileResponse(binary, filename="kenny-agent.exe", media_type="application/octet-stream")

    async def agent_binary_status(request: Request) -> Response:
        """Report binary availability + GitHub-fetch config for the dashboard (no network)."""

        status = agent_release.binary_status(manual_path=agent_binary_path())
        body = status.to_public()
        body["available"] = agent_binary_path() is not None
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
        Route("/d/binary/{nonce}", public_binary),
    ]

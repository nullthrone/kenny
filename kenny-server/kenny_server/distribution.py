"""Agent distribution: download an installer from the GUI, share an expiring link,
and trigger a server-side self-update (ADR-0012, ADR-0013).

The server serves a **prebuilt** agent binary (``KENNY_AGENT_BINARY``) and injects
per-install config — it does not build per download. Endpoints:

* ``GET  /api/agents/{id}/installer``    (operator) -> Windows: a ZIP (exe + setup.bat +
  kenny-agent.setup.json + README); Linux (``?os=linux``): the install ``sh`` script
  directly. Mints a fresh per-agent token via the token store.
* ``POST /api/agents/share-link``        (operator) -> body ``{name, os[, arch]}``; a 24h,
  single-use link for a host that need not exist yet. Returns ``{url, expires_at, os,
  name}``.
* ``POST /api/agents/{id}/share-link``   (operator) -> the same thing keyed by path param,
  at the shorter :data:`INSTALLER_TTL_S`; kept for the bundled dashboard.
* ``GET  /d/installer/{nonce}``          (public, nonce-gated) -> the Windows installer ZIP, once.
* ``GET  /d/install/{nonce}``            (public, nonce-gated) -> the Linux install script, once
  (mints the token + a paired non-consumed ``/d/binary`` nonce baked into the script).
* ``POST /api/agents/{id}/update``       (operator) -> compute the (OS-matched) binary sha256,
  mint a short-lived ``/d/binary/{nonce}`` URL, and send ``agent_update`` to the online agent.
* ``GET  /d/binary/{nonce}``             (public, nonce-gated) -> the raw binary (self-update /
  Linux install download), served per the nonce's os/arch.

**Two credential models meet here, and which route uses which is the whole
security story of this module (ADR-0053).**

*Operator-authenticated, and therefore wrapped in* ``webui.authz.guard()``: every
``/api/agents/*`` route that *mints* something — an installer, a share link, an
update push. Minting is provisioning: it creates the path by which a machine
enrolls into the fleet. These carry ``min_role="operator"``, and the path-param
ones additionally carry ``host_param="id"`` so a scoped ``user`` cannot mint for
a host outside their scope.

*Deliberately unauthenticated, and therefore NOT guarded*: the ``/d/*``
redemptions and ``/api/agents/{id}/enroll``. Both are fetched by someone who
holds no operator credential and could not obtain one — the relative clicking a
share link in a message, and the agent binary enrolling on first boot. Their
credential is the one they carry: an unguessable single-use nonce with a TTL
(ADR-0012/ADR-0030), and the one-time enrollment token (ADR-0022), each verified
in the handler. ``auth.py::_is_public`` exempts exactly these paths from the
operator middleware, so wrapping them in ``guard()`` would 401 every legitimate
caller and break onboarding outright. That they are open is the design; that the
minting side is not is what makes it safe.
"""

from __future__ import annotations

import asyncio
import io
import logging
import json
import os
import secrets
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import agent_release
from .agent_release import _sha256_file as _sha256_file  # re-export (used by tests)
from .keystore import KeyStore
from .registry import AgentRegistry
from .tokenstore import AgentTokenStore
from .tunnel import AgentTunnel, ToolError
from .urls import public_base_url
from .webui.authz import guard

logger = logging.getLogger("kenny.distribution")

INSTALLER_TTL_S = 3600  # one hour for an operator-shared installer link
BINARY_TTL_S = 600  # ten minutes for a self-update binary fetch
# ``POST /api/agents/share-link`` hands its URL to a person who is not at the
# operator's keyboard — it travels by message and gets opened whenever they next
# sit down at the machine. An hour does not survive that; a day does. The window
# is affordable because the nonce is still single-use and still mints nothing
# until it is redeemed: an unopened link expires having created no credential.
SHARE_LINK_TTL_S = 24 * 3600

SUPPORTED_OS: frozenset[str] = frozenset({"windows", "linux"})


def agent_binary_path(
    os_name: str = "windows", arch: str = "x86_64", channel: str = "stable"
) -> str | None:
    """Path to the prebuilt agent binary for ``(os_name, arch, channel)``, or None.

    Operator-placed env override wins for ``channel="stable"``, otherwise the
    GitHub-fetched cache (``agent_release.cache_path``) is used if present
    (ADR-0015). Overrides (stable only):

    * windows/x86_64 -> ``KENNY_AGENT_BINARY`` (the default, pre-Linux behavior).
    * linux/x86_64   -> ``KENNY_AGENT_BINARY_LINUX``.
    * linux/aarch64  -> ``KENNY_AGENT_BINARY_LINUX_AARCH64``.

    ``channel="dev"`` has no manual-placement env in this iteration (ADR-0048)
    — it goes straight to the channel-aware cache path.
    """

    if channel != "stable":
        cache = agent_release.cache_path(os_name, arch, channel)
        return cache if os.path.exists(cache) else None

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


def _expires_at(ttl_s: int) -> str:
    """Wall-clock expiry of a nonce minted now, as ISO-8601 UTC.

    The nonce itself keeps a monotonic-ish ``time.time()`` deadline; this is the
    same instant rendered for a human and for the console's countdown, so the
    two cannot describe different moments.
    """

    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat()


def _public_url() -> str:
    """Externally reachable base URL of this server (for links the agent/user open)."""

    return public_base_url()


def _wss_url() -> str:
    """Agent --server URL derived from the public URL (https->wss, http->ws)."""

    base = _public_url()
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base.rstrip("/") + "/agent/ws"


async def perform_agent_update(
    tunnel: AgentTunnel,
    share_links: "ShareLinks",
    agent_id: str,
    *,
    os_name: str,
    arch: str,
    version: str,
    binary_path: str,
    sha256: str,
    timeout_s: float = 120,
) -> dict:
    """Mint a fresh nonce for ``binary_path`` and send ``agent_update`` to ``agent_id``.

    The one place that actually calls the wire tool, shared by the manual
    per-agent "update now" route (below, resolves the live agent-binary cache)
    and the update-campaign machinery (``update_manager.py``, resolves a
    durable, pinned per-campaign artifact — ADR-0040) so both paths mint
    nonces and call ``agent_update`` identically. Raises :class:`ToolError` on
    an agent-side error or timeout, same as ``tunnel.send_request``.
    """

    nonce = share_links.create(
        agent_id, "binary", BINARY_TTL_S, os_name=os_name, arch=arch, path=binary_path
    )
    url = f"{_public_url()}/d/binary/{nonce}"
    return await tunnel.send_request(
        agent_id, "agent_update", {"version": version, "url": url, "sha256": sha256}, timeout_s
    )


@dataclass
class _Nonce:
    agent_id: str
    kind: str  # "installer" | "install" | "binary"
    expires_at: float
    used: bool = False
    os: str = "windows"
    # None means "not pinned" — the arch is unresolved until something else decides
    # it (the Linux script's own `uname -m` detection). ``public_binary``'s fallback
    # (``_norm_arch(query_param or entry.arch)``) treats None identically to the old
    # literal "x86_64" default, so this is behavior-preserving.
    arch: str | None = None
    # For a Linux "install" nonce, the paired "binary" nonce it hands the target
    # box (baked into the install script's download URL). It lives longer than
    # the install nonce (which is consumed on fetch) so the fetch->run gap is OK.
    binary_nonce: str | None = None
    # An explicit file path this nonce must serve, overriding the live
    # agent_binary_path(os, arch) lookup. Used by a pinned update campaign
    # (ADR-0040) so a "binary" nonce always serves the exact artifact snapshot
    # the operator approved, even if the shared agent-release cache has since
    # been overwritten by a later detection pass. None (the default) preserves
    # the original "serve whatever is currently cached" behavior.
    path: str | None = None


@dataclass
class ShareLinks:
    """In-memory nonce store for shareable download links (dev-grade, like CallLog).

    **This does not survive a restart.** The dict is process-local, so a deploy,
    a crash or a container restart invalidates every outstanding link. Within the
    old one-hour window that was nearly invisible; against
    :data:`SHARE_LINK_TTL_S`'s 24 hours it is a real failure mode — a link mailed
    in the evening can be dead by morning, and the person opening it sees only
    "link invalid or expired".

    It fails in the safe direction, which is why it is tolerated for now: a lost
    nonce is a link that cannot be redeemed, and since the agent token is minted
    only on redemption, nothing was created that now dangles. The operator's
    recovery is to mint another link. Making it durable means a SQLite table and
    an async ``create``/``resolve``, which ripples through ``perform_agent_update``
    and ``update_manager`` — worth doing, tracked in ADR-0053, deliberately not
    bundled into the change that guarded these routes.

    Entries are reaped lazily, when an expired nonce is next looked up.
    """

    _nonces: dict[str, _Nonce] = field(default_factory=dict)

    def create(
        self,
        agent_id: str,
        kind: str,
        ttl_s: int,
        *,
        os_name: str = "windows",
        arch: str | None = None,
        binary_nonce: str | None = None,
        path: str | None = None,
    ) -> str:
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = _Nonce(
            agent_id,
            kind,
            time.time() + ttl_s,
            os=os_name,
            arch=arch,
            binary_nonce=binary_nonce,
            path=path,
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


def _norm_arch_or_none(raw: object) -> str | None:
    """An operator-pinned arch, or None for "not pinned".

    Unlike :func:`_norm_arch`, an absent or unrecognized value returns None
    rather than guessing — None lets the Linux install script fall back to its
    own ``uname -m`` auto-detection at install time (ADR-0036).
    """

    value = str(raw or "").strip().lower()
    if value in ("aarch64", "arm64"):
        return "aarch64"
    if value in ("x86_64", "amd64"):
        return "x86_64"
    return None


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
    arch: str | None = None,
) -> str:
    """The Linux install script (POSIX ``sh``, LF line endings).

    Downloads the arch-matched agent binary from ``binary_url`` (which already
    carries ``?os=linux``; the script appends ``&arch=$arch``) and runs the exact
    agent CLI contract:

        <binary> setup --server <wss> --agent-id <id> \
            [--server-pubkey <b64>] [--enroll-token <tok>] \
            --telemetry-interval-secs <n>

    The enrollment token lives only here (in argv) — never on disk.

    ``arch``, when given, is an operator-pinned target (ADR-0036): the script
    skips ``uname -m`` detection and uses the pinned value literally — the
    operator is preparing a package for a specific, already-known host. When
    None (the default), the script auto-detects at curl-time exactly as before.
    """

    arch_detect = (
        "# Arch pinned by the operator when this install link was prepared.\n"
        f"arch={arch}\n"
        if arch is not None
        else "# Map the machine arch onto our release naming.\n"
        'case "$(uname -m)" in\n'
        "  aarch64|arm64) arch=aarch64 ;;\n"
        "  *) arch=x86_64 ;;\n"
        "esac\n"
    )

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
        f"{arch_detect}"
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

    def _req_arch(request: Request) -> str | None:
        """An operator-pinned ``?arch=`` for a not-yet-existing host, or None."""

        return _norm_arch_or_none(request.query_params.get("arch"))

    async def _linux_install_script(agent_id: str, arch: str | None = None) -> str:
        """Mint a fresh token + a non-consumed Linux binary nonce, render the script.

        The binary nonce lives as long as the install link (INSTALLER_TTL_S) so it
        survives the fetch->run gap. The token rides only in the script argv. ``arch``
        (ADR-0036) pins the target CPU arch when the operator already knows it;
        None preserves the existing ``uname -m`` auto-detection.
        """

        token = await token_store.create_or_rotate(agent_id)
        binary_nonce = share_links.create(
            agent_id, "binary", INSTALLER_TTL_S, os_name="linux", arch=arch
        )
        binary_url = f"{_public_url()}/d/binary/{binary_nonce}?os=linux"
        return _install_sh(
            agent_id, token, _wss_url(), _server_pubkey(), _interval(), binary_url, arch=arch
        )

    async def installer(request: Request) -> Response:
        agent_id = request.path_params["id"]
        if _req_os(request) == "linux":
            # The operator brings this script to the Linux box and runs it as root.
            script = await _linux_install_script(agent_id, arch=_req_arch(request))
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

    def _mint_share_link(agent_id: str, os_name: str, arch: str | None, ttl_s: int) -> dict:
        """Mint the nonce(s) for one share link and render its public payload.

        No token is minted here, and that is the point: the credential is
        created only when the link is *redeemed* (``public_install`` /
        ``public_installer`` call ``token_store.create_or_rotate``). A link that
        is never opened therefore expires having created nothing to revoke.

        Windows needs one ``installer`` nonce. Linux needs two: a one-time
        ``install`` nonce plus the paired, non-consumed ``binary`` nonce it hands
        the target box (the script is fetched once, the binary download it starts
        may retry). ``arch`` (ADR-0036), when pinned, rides on BOTH — the install
        nonce so ``public_install`` can recover it across the mint->fetch gap,
        the binary nonce as ``public_binary``'s own fallback.
        """

        if os_name == "linux":
            binary_nonce = share_links.create(
                agent_id, "binary", ttl_s, os_name="linux", arch=arch
            )
            install_nonce = share_links.create(
                agent_id,
                "install",
                ttl_s,
                os_name="linux",
                arch=arch,
                binary_nonce=binary_nonce,
            )
            url = f"{_public_url()}/d/install/{install_nonce}"
            return {
                "url": url,
                "oneliner": f"curl -fsSL {url} | sudo sh",
                "expires_in": ttl_s,
                "expires_at": _expires_at(ttl_s),
                "os": "linux",
                "name": agent_id,
            }
        nonce = share_links.create(agent_id, "installer", ttl_s)
        return {
            "url": f"{_public_url()}/d/installer/{nonce}",
            "expires_in": ttl_s,
            "expires_at": _expires_at(ttl_s),
            "os": "windows",
            "name": agent_id,
        }

    async def share_link(request: Request) -> Response:
        """Path-param form, at :data:`INSTALLER_TTL_S`. The bundled dashboard's."""

        return JSONResponse(
            _mint_share_link(
                request.path_params["id"],
                _req_os(request),
                _req_arch(request),
                INSTALLER_TTL_S,
            )
        )

    async def share_link_by_name(request: Request) -> Response:
        """Body form ``{name, os[, arch]}`` at :data:`SHARE_LINK_TTL_S` (24h).

        ``name`` is the agent id the host will enroll under. It intentionally
        need not exist yet: there is no separate "create the agent" step in
        kenny, and share-linking an id that has never enrolled *is* the
        first-time onboarding flow. Nothing is written for it here either — only
        a nonce, which expires on its own if the link is never opened.

        Guarded at ``min_role="operator"`` like every other minting route in this
        module. There is no ``host_param`` to hand :func:`guard` because the id
        arrives in the body, but none is needed: host scope only ever narrows a
        ``user``, and a ``user`` cannot reach an operator route at all.
        """

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed/empty body
            body = {}
        if not isinstance(body, dict):
            body = {}
        name = str(body.get("name", "")).strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        os_name = str(body.get("os", "windows")).strip().lower() or "windows"
        if os_name not in SUPPORTED_OS:
            return JSONResponse(
                {"error": f"os must be one of {', '.join(sorted(SUPPORTED_OS))}"},
                status_code=400,
            )
        arch = _norm_arch_or_none(body.get("arch"))
        return JSONResponse(_mint_share_link(name, os_name, arch, SHARE_LINK_TTL_S))

    async def public_install(request: Request) -> Response:
        """Serve the Linux install script once, minting the token at fetch time."""

        nonce = request.path_params["nonce"]
        entry = share_links.resolve_entry(nonce, "install", consume=True)
        if entry is None:
            return JSONResponse({"error": "link invalid or expired"}, status_code=404)
        agent_id = entry.agent_id
        token = await token_store.create_or_rotate(agent_id)
        binary_nonce = entry.binary_nonce or share_links.create(
            agent_id, "binary", INSTALLER_TTL_S, os_name="linux", arch=entry.arch
        )
        binary_url = f"{_public_url()}/d/binary/{binary_nonce}?os=linux"
        script = _install_sh(
            agent_id,
            token,
            _wss_url(),
            _server_pubkey(),
            _interval(),
            binary_url,
            arch=entry.arch,
        )
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
        """Bind an agent's Ed25519 public key on first contact (ADR-0022).

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
        # Manual "update this one agent now" pulls the agent's *desired*
        # channel (ADR-0048, soll not ist) via the update store already on
        # app.state, so a dev-pinned agent gets the dev binary, not stable.
        update_store = getattr(request.app.state, "update_store", None)
        channel = await update_store.get_desired_channel(agent_id) if update_store is not None else "stable"
        binary = agent_binary_path(os_name=os_name, arch=arch, channel=channel)
        if binary is None:
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        version = agent_release.resolve_agent_version(binary)
        sha256 = _sha256_file(binary)
        try:
            result = await perform_agent_update(
                tunnel,
                share_links,
                agent_id,
                os_name=os_name,
                arch=arch,
                version=version,
                binary_path=binary,
                sha256=sha256,
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
        # A pinned update campaign (ADR-0040) carries its own artifact path;
        # otherwise resolve the live agent-binary cache as before.
        binary = entry.path or agent_binary_path(os_name=os_name, arch=arch)
        if binary is None or not os.path.exists(binary):
            return JSONResponse({"error": "agent binary not configured"}, status_code=503)
        filename = "kenny-agent" if os_name == "linux" else "kenny-agent.exe"
        return FileResponse(binary, filename=filename, media_type="application/octet-stream")

    async def _durable_last_check(request: Request) -> dict[str, Any] | None:
        """The last recorded agent-fetch outcome, or None if nothing is recorded.

        Best-effort: this route is read on every Fleet render and by test apps
        built without an update store, so it degrades to None rather than 500.
        """

        store = getattr(request.app.state, "update_store", None)
        if store is None:
            return None
        try:
            row = await store.get_availability("agent")
        except Exception as exc:  # noqa: BLE001 - a status read must not 500 on a store hiccup
            logger.warning("could not read the agent availability row: %s", exc)
            return None
        if row is None:
            return None
        return {
            "ok": bool(row["ok"]),
            "message": row["message"],
            "checked_at": row["checked_at"],
            "version": row["version"],
        }

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
        # Per-(os,arch) availability (ADR-0036): the dashboard's "Add a PC" arch
        # dropdown offers only combinations we actually have a binary for.
        body["targets"] = [
            {"os": os_name, "arch": arch, "available": agent_binary_path(os_name, arch) is not None}
            for os_name, arch in agent_release.SUPPORTED_TARGETS
        ]
        body["repo"] = agent_release.github_repo()
        last = getattr(request.app.state, "last_fetch", None)
        body["last_fetch"] = last.to_public() if last is not None else None
        # The durable counterpart (ADR-0040's ``update_availability`` row), which
        # ``last_fetch`` is not: that one is per-process, so a restart erases the
        # reason a refresh has been failing and the dashboard falls back to
        # "no fetch has been attempted yet" while a real failure stands.
        body["last_check"] = await _durable_last_check(request)
        # Dev-channel cache status, additive (ADR-0048): lets the dashboard show
        # "dev binary available: yes/no" without a second round trip.
        win_dev = agent_binary_path(channel="dev")
        body["dev"] = {
            "available": win_dev is not None,
            "by_os": {
                "windows": win_dev is not None,
                "linux": (
                    agent_binary_path("linux", "x86_64", "dev") is not None
                    or agent_binary_path("linux", "aarch64", "dev") is not None
                ),
            },
            "targets": [
                {
                    "os": os_name,
                    "arch": arch,
                    "available": agent_binary_path(os_name, arch, "dev") is not None,
                }
                for os_name, arch in agent_release.SUPPORTED_TARGETS
            ],
        }
        return JSONResponse(body)

    async def agent_binary_fetch(request: Request) -> Response:
        """Manually (re)trigger the GitHub fetch so no restart is needed.

        Always attempts: the read is anonymous (ADR-0057), so there is no
        configuration left that could make the attempt pointless in advance.
        """

        result = await asyncio.to_thread(agent_release.fetch_latest_agent_binary)
        request.app.state.last_fetch = result
        # Same outcome, durably: an operator's retry that fails is exactly the
        # thing the next person opening the dashboard needs to see.
        store = getattr(request.app.state, "update_store", None)
        if store is not None:
            from .update_manager import record_agent_fetch

            await record_agent_fetch(store, result)
        return JSONResponse(result.to_public(), status_code=200 if result.ok else 502)

    # See the module docstring for the rule these two blocks encode. Provisioning
    # goes through ``guard()``; the two surfaces whose caller holds a different
    # credential entirely (a nonce, an enrollment token) must not, or the
    # operator middleware would 401 the very people they exist for.
    op = {"min_role": "operator"}
    op_scoped = {"min_role": "operator", "host_param": "id"}
    return [
        # -- operator-authenticated: these mint --------------------------------
        Route("/api/agents/share-link", guard(share_link_by_name, **op), methods=["POST"]),
        Route("/api/agents/{id}/installer", guard(installer, **op_scoped)),
        Route("/api/agents/{id}/share-link", guard(share_link, **op_scoped), methods=["POST"]),
        Route("/api/agents/{id}/update", guard(trigger_update, **op_scoped), methods=["POST"]),
        # Read-only availability report. Kept at the authenticated floor rather
        # than raised to operator: the fleet view fetches it on every render for
        # every role, and it discloses only whether a binary is on disk.
        Route("/api/agent-binary", guard(agent_binary_status)),
        # Reaches out to GitHub and writes the cache — provisioning, so operator.
        Route("/api/agent-binary/fetch", guard(agent_binary_fetch, **op), methods=["POST"]),
        # -- deliberately unauthenticated: these redeem ------------------------
        # The agent's one-time enrollment token is verified inside ``enroll``
        # (ADR-0022); the caller is a freshly installed binary with no operator
        # credential and no way to get one.
        Route("/api/agents/{id}/enroll", enroll, methods=["POST"]),
        # The nonce is the credential (ADR-0012/ADR-0030): single-use, TTL-bound,
        # unguessable, and verified in each handler. The person opening these is
        # not signed in, which is the entire purpose of a share link.
        Route("/d/installer/{nonce}", public_installer),
        Route("/d/install/{nonce}", public_install),
        Route("/d/binary/{nonce}", public_binary),
    ]

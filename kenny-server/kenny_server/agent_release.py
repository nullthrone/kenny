"""Best-effort fetch of the prebuilt agent binary from GitHub Releases (ADR-0015).

The server serves a **prebuilt** ``kenny-agent.exe`` (ADR-0012). To avoid the
first-agent chicken-and-egg (operator must hand-place the binary into the data
volume before any installer can be downloaded), the server can fetch the latest
release asset itself **when it can** — i.e. a GitHub token is configured and the
repo is reachable. The fetch is gated on the token (avoids anonymous rate limits
and unlocks private repos), best-effort, non-fatal, and sha256-verified; the
result is cached on the data volume. An operator-placed ``KENNY_AGENT_BINARY``
always wins over the cache (see ``distribution.agent_binary_path``).

This module imports nothing from ``distribution`` to keep the dependency one-way.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

import httpx

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "t11z/kenny"
ASSET_RE = re.compile(r"^kenny-agent-.*-x86_64-pc-windows-msvc\.exe$")
FETCH_TIMEOUT_S = 15.0


def github_repo() -> str:
    """``owner/name`` of the repo to fetch the agent binary from."""

    return os.environ.get("KENNY_GITHUB_REPO", "").strip() or DEFAULT_REPO


def github_token() -> str | None:
    """The GitHub token enabling auto-fetch, or None if unset."""

    tok = os.environ.get("KENNY_GITHUB_TOKEN", "").strip()
    return tok or None


def github_configured() -> bool:
    """Whether auto-fetch is enabled (a token is present)."""

    return github_token() is not None


def cache_path() -> str:
    """Where an auto-fetched binary is cached.

    Explicit ``KENNY_AGENT_BINARY_CACHE`` wins; otherwise it sits next to the
    SQLite store (``<dir of KENNY_DB_PATH>/kenny-agent.exe``), which is the
    persisted ``/data`` volume in the container.
    """

    override = os.environ.get("KENNY_AGENT_BINARY_CACHE", "").strip()
    if override:
        return override
    db = os.environ.get("KENNY_DB_PATH", "kenny.sqlite")
    return os.path.join(os.path.dirname(os.path.abspath(db)) or ".", "kenny-agent.exe")


DEFAULT_VERSION = "0.2.0"


def _normalize_version(v: str) -> str:
    """Strip a leading ``v`` so a git tag (``v0.3.0``) and a plain version align."""

    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    return v


def version_sidecar(binary_path: str) -> str:
    """Path of the version marker written next to a binary (holds the release tag)."""

    return binary_path + ".version"


def _read_sidecar(binary_path: str) -> str | None:
    try:
        with open(version_sidecar(binary_path), "r", encoding="utf-8") as fh:
            return _normalize_version(fh.read()) or None
    except OSError:
        return None


def resolve_agent_version(manual_path: str | None = None) -> str:
    """Agent version, **led by the GitHub release tag** (ADR-0015).

    The tag of the served binary wins: it is written to a ``.version`` sidecar on
    fetch (and may be dropped next to a manually-placed binary). ``KENNY_AGENT_VERSION``
    is only a fallback when no tag is known, then a built-in default.
    """

    if manual_path:
        tag = _read_sidecar(manual_path)
        if tag:
            return tag
    return _normalize_version(os.environ.get("KENNY_AGENT_VERSION", "")) or DEFAULT_VERSION


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manual_hint() -> str:
    repo = github_repo()
    return (
        "No agent binary available. Download "
        "`kenny-agent-<tag>-x86_64-pc-windows-msvc.exe` from "
        f"https://github.com/{repo}/releases/latest and place it on the server "
        "(set `KENNY_AGENT_BINARY` to its path, or mount it at "
        "`/data/kenny-agent.exe`). Or set `KENNY_GITHUB_TOKEN` so the server "
        "fetches it automatically."
    )


@dataclass
class FetchResult:
    """Outcome of a fetch attempt or a status probe."""

    ok: bool
    source: str  # "manual" | "github" | "cache" | "none"
    message: str
    asset_name: str | None = None
    sha256: str | None = None
    version: str | None = None  # release tag_name

    def to_public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "message": self.message,
            "asset_name": self.asset_name,
            "sha256": self.sha256,
            "version": self.version,
        }


def _default_client() -> httpx.Client:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return httpx.Client(timeout=FETCH_TIMEOUT_S, headers=headers, follow_redirects=True)


def _pick_assets(release: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return ``(exe_asset, sha256_asset|None)``; raise if no exe asset matches."""

    assets = release.get("assets") or []
    exe_assets = sorted(
        (a for a in assets if ASSET_RE.match(str(a.get("name", "")))),
        key=lambda a: str(a.get("name", "")),
    )
    if not exe_assets:
        raise ValueError("no kenny-agent .exe asset in the latest release")
    exe = exe_assets[0]
    sha_name = str(exe["name"]) + ".sha256"
    sha = next((a for a in assets if str(a.get("name", "")) == sha_name), None)
    return exe, sha


def _parse_sha256(text: str) -> str:
    """Parse a ``<hash>  <name>`` sha256 file; return the lowercase 64-hex digest."""

    token = text.strip().split()[0] if text.strip() else ""
    token = token.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("malformed sha256 file")
    return token


def fetch_latest_agent_binary(
    *,
    client_factory: Callable[[], httpx.Client] = _default_client,
    dest: str | None = None,
) -> FetchResult:
    """Resolve the latest release, download + verify + atomically cache the exe.

    Best-effort: **never raises** — any failure returns ``FetchResult(ok=False)``.
    ``client_factory`` is injected so tests use ``httpx.MockTransport`` (no network).
    """

    if not github_configured():
        return FetchResult(
            ok=False,
            source="none",
            message="GitHub fetch not configured (set KENNY_GITHUB_TOKEN)",
        )

    dest = dest or cache_path()
    repo = github_repo()
    try:
        with client_factory() as client:
            rel = client.get(f"{GITHUB_API}/repos/{repo}/releases/latest")
            if rel.status_code == 404:
                return FetchResult(
                    ok=False, source="none", message=f"no releases found for {repo}"
                )
            if rel.status_code == 403:
                return FetchResult(
                    ok=False,
                    source="none",
                    message="GitHub API 403 (rate limited or token lacks access)",
                )
            rel.raise_for_status()
            release = rel.json()
            tag = release.get("tag_name")

            exe_asset, sha_asset = _pick_assets(release)
            asset_name = str(exe_asset["name"])

            os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(os.path.abspath(dest)) or ".", suffix=".part"
            )
            try:
                with os.fdopen(fd, "wb") as out:
                    with client.stream("GET", exe_asset["browser_download_url"]) as resp:
                        resp.raise_for_status()
                        for chunk in resp.iter_bytes(1 << 20):
                            out.write(chunk)

                digest = _sha256_file(tmp)
                warning = ""
                if sha_asset is not None:
                    sha_resp = client.get(sha_asset["browser_download_url"])
                    sha_resp.raise_for_status()
                    expected = _parse_sha256(sha_resp.text)
                    if digest != expected:
                        return FetchResult(
                            ok=False,
                            source="none",
                            message=f"sha256 verification failed for {asset_name}",
                        )
                else:
                    warning = " (no .sha256 asset to verify against)"

                os.replace(tmp, dest)
                tmp = None  # consumed by os.replace
            finally:
                if tmp is not None and os.path.exists(tmp):
                    os.unlink(tmp)

        # Persist the release tag next to the binary: it is the leading source
        # of the agent version (read back by resolve_agent_version).
        norm = _normalize_version(tag) if tag else None
        try:
            with open(version_sidecar(dest), "w", encoding="utf-8") as fh:
                fh.write(norm or "")
        except OSError:
            pass

        return FetchResult(
            ok=True,
            source="github",
            message=f"fetched {asset_name}{warning}",
            asset_name=asset_name,
            sha256=digest,
            version=norm,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, surface as a result
        return FetchResult(ok=False, source="none", message=f"fetch failed: {exc}")


def binary_status(*, manual_path: str | None) -> FetchResult:
    """Describe current availability **without** contacting GitHub.

    ``manual_path`` is the resolved binary path (``distribution.agent_binary_path``)
    so precedence stays in one place. ``source`` distinguishes a manually-placed
    binary from the GitHub cache.
    """

    version = resolve_agent_version(manual_path)
    explicit = os.environ.get("KENNY_AGENT_BINARY", "").strip()
    if manual_path:
        source = "manual" if explicit and os.path.exists(explicit) else "cache"
        return FetchResult(
            ok=True,
            source=source,
            message="agent binary available",
            sha256=_sha256_file(manual_path),
            version=version,
        )
    return FetchResult(ok=False, source="none", message=_manual_hint(), version=version)

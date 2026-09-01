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
DEFAULT_REPO = "nullthrone/kenny"
# Release asset naming (shared contract with the agent's release workflow):
#   windows: kenny-agent-<tag>-x86_64-pc-windows-msvc.exe
#   linux:   kenny-agent-<tag>-<arch>-unknown-linux-musl  (arch: x86_64 | aarch64)
ASSET_RE = re.compile(r"^kenny-agent-.*-x86_64-pc-windows-msvc\.exe$")
# POSSIBLY DEAD: _asset_re() below compiles its own per-arch pattern instead of
# using this; only referenced directly by tests.
LINUX_ASSET_RE = re.compile(r"^kenny-agent-.*-(x86_64|aarch64)-unknown-linux-musl$")
LINUX_ARCHES = ("x86_64", "aarch64")
# The (os, arch) combinations we actually ship a binary for — the authoritative list
# behind the dashboard's "Add a PC" arch dropdown (ADR-0036) and its availability
# check. Windows has only ever shipped one target; `agent_binary_path` doesn't
# consult `arch` for windows at all.
SUPPORTED_TARGETS: tuple[tuple[str, str], ...] = (("windows", "x86_64"),) + tuple(
    ("linux", arch) for arch in LINUX_ARCHES
)
FETCH_TIMEOUT_S = 15.0


def _asset_re(os_name: str, arch: str) -> re.Pattern[str]:
    """The release-asset name regex for a given (os, arch)."""

    if os_name == "linux":
        return re.compile(rf"^kenny-agent-.*-{re.escape(arch)}-unknown-linux-musl$")
    return ASSET_RE


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


def cache_path(os_name: str = "windows", arch: str = "x86_64", channel: str = "stable") -> str:
    """Where an auto-fetched binary is cached, per (os, arch, channel).

    Binaries sit next to the SQLite store (``<dir of KENNY_DB_PATH>/...``), the
    persisted ``/data`` volume in the container:

    * windows/stable -> ``kenny-agent.exe`` (explicit ``KENNY_AGENT_BINARY_CACHE``
      wins, preserving the pre-Linux, pre-channel behavior byte-identically).
    * windows/dev    -> ``kenny-agent-dev.exe``, next to the stable cache. No
      ``KENNY_AGENT_BINARY_CACHE``-style manual-placement override in this
      iteration (ADR-0048) — dev has no operator-placed-binary path.
    * linux          -> ``kenny-agent-linux-<arch>`` (``x86_64`` | ``aarch64``),
      with a ``-dev`` suffix for ``channel="dev"``.
    """

    db = os.environ.get("KENNY_DB_PATH", "kenny.sqlite")
    base_dir = os.path.dirname(os.path.abspath(db)) or "."
    dev_suffix = "-dev" if channel == "dev" else ""
    if os_name == "linux":
        return os.path.join(base_dir, f"kenny-agent-linux-{arch}{dev_suffix}")
    if channel == "dev":
        return os.path.join(base_dir, "kenny-agent-dev.exe")
    override = os.environ.get("KENNY_AGENT_BINARY_CACHE", "").strip()
    if override:
        return override
    return os.path.join(base_dir, "kenny-agent.exe")


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


def _match_asset(
    release: dict[str, Any], asset_re: re.Pattern[str]
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Return ``(asset, sha256_asset|None)`` for ``asset_re``, or None if absent."""

    assets = release.get("assets") or []
    matched = sorted(
        (a for a in assets if asset_re.match(str(a.get("name", "")))),
        key=lambda a: str(a.get("name", "")),
    )
    if not matched:
        return None
    asset = matched[0]
    sha_name = str(asset["name"]) + ".sha256"
    sha = next((a for a in assets if str(a.get("name", "")) == sha_name), None)
    return asset, sha


def _parse_sha256(text: str) -> str:
    """Parse a ``<hash>  <name>`` sha256 file; return the lowercase 64-hex digest."""

    token = text.strip().split()[0] if text.strip() else ""
    token = token.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("malformed sha256 file")
    return token


def _fetch_asset(
    client: httpx.Client,
    release: dict[str, Any],
    asset_re: re.Pattern[str],
    dest: str,
    tag: str | None,
) -> FetchResult | None:
    """Download+verify+atomically cache the single asset matching ``asset_re``.

    Returns ``None`` when no such asset exists in the release (nothing to do), a
    failing :class:`FetchResult` when the download/verify fails (best-effort:
    **never raises**), and a succeeding one when cached. Writes the release tag
    to the binary's ``.version`` sidecar on success.
    """

    picked = _match_asset(release, asset_re)
    if picked is None:
        return None
    asset, sha_asset = picked
    asset_name = str(asset["name"])
    norm = _normalize_version(tag) if tag else None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(dest)) or ".", suffix=".part"
        )
        try:
            with os.fdopen(fd, "wb") as out:
                with client.stream("GET", asset["browser_download_url"]) as resp:
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


def _select_release(client: httpx.Client, repo: str, channel: str) -> dict[str, Any] | None:
    """Resolve the release JSON to fetch assets from, per channel (ADR-0048).

    ``stable`` -> ``GET /releases/latest`` (unchanged, excludes prereleases by
    GitHub's own construction). ``dev`` -> ``GET /releases`` (newest first),
    the first non-draft entry with ``prerelease: true``. Returns ``None`` when
    there is no matching release (a 404 on the stable path, or no matching
    entry / a 404 on the dev path) — the caller turns that into a
    ``FetchResult(ok=False)``.
    """

    if channel == "dev":
        resp = client.get(f"{GITHUB_API}/repos/{repo}/releases", params={"per_page": 30})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        for release in resp.json():
            if release.get("prerelease") is True and release.get("draft") is False:
                return release
        return None

    resp = client.get(f"{GITHUB_API}/repos/{repo}/releases/latest")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_latest_agent_binary(
    *,
    client_factory: Callable[[], httpx.Client] = _default_client,
    dest: str | None = None,
    channel: str = "stable",
) -> FetchResult:
    """Resolve the latest release, then download+verify+cache **every** known
    asset it can match: the Windows exe plus each Linux musl arch present.

    Each asset is best-effort/non-fatal, cached to its own ``cache_path`` with a
    ``.version`` sidecar. The Windows result (to ``dest`` if given) leads the
    return value for back-compat; when there is no Windows asset but a Linux one
    cached, the first successful Linux result is returned instead. Best-effort:
    **never raises** — any failure surfaces as ``FetchResult(ok=False)``.
    ``client_factory`` is injected so tests use ``httpx.MockTransport`` (no network).
    """

    if not github_configured():
        return FetchResult(
            ok=False,
            source="none",
            message="GitHub fetch not configured (set KENNY_GITHUB_TOKEN)",
        )

    repo = github_repo()
    try:
        with client_factory() as client:
            release = _select_release(client, repo, channel)
            if release is None:
                return FetchResult(
                    ok=False, source="none", message=f"no {channel} releases found for {repo}"
                )
            tag = release.get("tag_name")

            win_dest = dest or cache_path("windows", "x86_64", channel)
            win_res = _fetch_asset(client, release, ASSET_RE, win_dest, tag)

            linux_ok: list[FetchResult] = []
            for arch in LINUX_ARCHES:
                lres = _fetch_asset(
                    client,
                    release,
                    _asset_re("linux", arch),
                    cache_path("linux", arch, channel),
                    tag,
                )
                if lres is not None and lres.ok:
                    linux_ok.append(lres)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return FetchResult(
                ok=False,
                source="none",
                message="GitHub API 403 (rate limited or token lacks access)",
            )
        return FetchResult(ok=False, source="none", message=f"fetch failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - best-effort, surface as a result
        return FetchResult(ok=False, source="none", message=f"fetch failed: {exc}")

    if win_res is not None:
        if win_res.ok and linux_ok:
            extra = ", ".join(str(r.asset_name) for r in linux_ok)
            win_res.message = f"{win_res.message} (+linux: {extra})"
        return win_res
    if linux_ok:
        return linux_ok[0]
    return FetchResult(
        ok=False,
        source="none",
        message="fetch failed: no kenny-agent asset in the latest release",
    )


_EXPLICIT_ENV = {
    ("windows", "x86_64"): "KENNY_AGENT_BINARY",
    ("linux", "x86_64"): "KENNY_AGENT_BINARY_LINUX",
    ("linux", "aarch64"): "KENNY_AGENT_BINARY_LINUX_AARCH64",
}


def binary_status(
    *,
    manual_path: str | None,
    os_name: str = "windows",
    arch: str = "x86_64",
    channel: str = "stable",
) -> FetchResult:
    """Describe current availability **without** contacting GitHub.

    ``manual_path`` is the resolved binary path (``distribution.agent_binary_path``)
    so precedence stays in one place. ``source`` distinguishes an operator-placed
    binary (via the per-(os, arch) env var) from the GitHub cache. Dev has no
    manual-override env in this iteration (ADR-0048), so for ``channel="dev"``
    ``source`` is always ``"cache"`` when a file exists at ``manual_path``.
    """

    version = resolve_agent_version(manual_path)
    if channel == "stable":
        env_name = _EXPLICIT_ENV.get((os_name, arch), "KENNY_AGENT_BINARY")
        explicit = os.environ.get(env_name, "").strip()
    else:
        explicit = ""
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

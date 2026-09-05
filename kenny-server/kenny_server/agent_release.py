"""Best-effort fetch of the prebuilt agent binary from GitHub Releases (ADR-0015).

The server serves a **prebuilt** ``kenny-agent.exe`` (ADR-0012). To avoid the
first-agent chicken-and-egg (operator must hand-place the binary into the data
volume before any installer can be downloaded), the server fetches the latest
release asset itself. Best-effort, non-fatal, sha256-verified; the result is
cached on the data volume, and an operator-placed ``KENNY_AGENT_BINARY`` always
wins over that cache (see ``distribution.agent_binary_path``).

The fetch is **unauthenticated** (ADR-0057, amending ADR-0015). Everything read
here — the release list and its assets — is public, so kenny reads it the way
the public does. Sending a credential for public data bought private-repo
support the canonical deployment never used, and charged a single point of
failure for it: an authorization problem that had nothing to do with the data
being read once held a whole fleet on a month-old agent. A private release repo
answers 404 anonymously and needs a hand-placed ``KENNY_AGENT_BINARY`` instead.

This module imports nothing from ``distribution`` to keep the dependency one-way.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "nullthrone/kenny"
# Release asset naming (shared contract with the agent's release workflow):
#   windows: kenny-agent-<tag>-x86_64-pc-windows-msvc.exe
#   linux:   kenny-agent-<tag>-<arch>-unknown-linux-musl  (arch: x86_64 | aarch64)
ASSET_RE = re.compile(r"^kenny-agent-.*-x86_64-pc-windows-msvc\.exe$")
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
    """The GitHub token, or None if unset.

    **Not** used for anything in this module: the release reads here are
    anonymous (ADR-0057). Its one remaining consumer is the GHCR poll in
    ``server_release`` (ADR-0040), where a private container package does still
    need a credential. It lives here because that is where the environment
    variable has always been read.
    """

    tok = os.environ.get("KENNY_GITHUB_TOKEN", "").strip()
    return tok or None


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


#: No ``Authorization`` header, deliberately and unconditionally (ADR-0057).
#: ``tests/test_agent_release.py`` fails if one reappears.
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _default_client() -> httpx.Client:
    return httpx.Client(
        timeout=FETCH_TIMEOUT_S, headers=dict(GITHUB_HEADERS), follow_redirects=True
    )


def is_rate_limited(response: httpx.Response) -> bool:
    """Whether a 403/429 is GitHub's rate limiter rather than an authorization refusal.

    The two arrive under the same status code and need opposite remedies — wait
    versus fix access — so the header is the only thing that tells them apart.
    """

    return response.headers.get("x-ratelimit-remaining") == "0"


def _rate_limit_reset(response: httpx.Response) -> str:
    """``x-ratelimit-reset`` (a Unix timestamp) as a readable UTC time, or ""."""

    raw = response.headers.get("x-ratelimit-reset", "")
    try:
        when = datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    return when.strftime("%Y-%m-%d %H:%M UTC")


def _github_message(response: httpx.Response) -> str:
    """GitHub's own explanation from the response body, bounded and flattened.

    Worth carrying because it usually names the exact cause (an IP allow list, a
    secondary rate limit). It is third-party text: treat it as data, keep it
    short, and never let it reach a markup renderer.
    """

    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON error body is not itself an error
        return ""
    if not isinstance(body, dict):
        return ""
    message = str(body.get("message") or "").strip()
    message = " ".join(message.split())
    return message[:200]


def describe_http_error(exc: httpx.HTTPStatusError, repo: str) -> str:
    """One operator-readable reason for a failed GitHub read, for both callers.

    Shared with ``changelog`` so the same response never gets two differently
    worded explanations — it used to, and the two drifted. Names no credential:
    these reads are anonymous (ADR-0057), so there is none to blame.
    """

    response = exc.response
    code = response.status_code
    if code in (403, 429):
        if is_rate_limited(response):
            reset = _rate_limit_reset(response)
            when = f", resets at {reset}" if reset else ""
            return (
                f"GitHub rate limit exhausted{when}. Reads are anonymous and the "
                "limit is per IP, so anything else sharing this address counts "
                "against it too."
            )
        detail = _github_message(response)
        return f"GitHub refused the request ({code})" + (f": {detail}" if detail else "")
    if code == 404:
        return (
            f"{repo} not found, or not public — releases are read anonymously, so a "
            "private repo needs a hand-placed KENNY_AGENT_BINARY instead."
        )
    return f"GitHub returned HTTP {code} for {repo}"


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
    client_factory: Callable[[], httpx.Client] | None = None,
    dest: str | None = None,
    channel: str = "stable",
) -> FetchResult:
    """Resolve the latest release, then download+verify+cache **every** known
    asset it can match: the Windows exe plus each Linux musl arch present.

    Reads GitHub anonymously (ADR-0057); no credential is required or sent.
    Each asset is best-effort/non-fatal, cached to its own ``cache_path`` with a
    ``.version`` sidecar. The Windows result (to ``dest`` if given) leads the
    return value for back-compat; when there is no Windows asset but a Linux one
    cached, the first successful Linux result is returned instead. Best-effort:
    **never raises** — any failure surfaces as ``FetchResult(ok=False)``.
    ``client_factory`` is injected so tests use ``httpx.MockTransport`` (no network);
    it is resolved on call, not bound at definition, so the default is patchable too.
    """

    factory = client_factory or _default_client
    repo = github_repo()
    try:
        with factory() as client:
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
        return FetchResult(ok=False, source="none", message=describe_http_error(exc, repo))
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

"""Live GitHub Releases changelog for the dashboard's About modal.

Proxies ``GET /repos/{repo}/releases`` server-side (rather than having the
browser call GitHub directly) so the fetch can be shared across operators and
degrade gracefully instead of hitting per-client CORS/rate-limit issues. This
module imports nothing from ``webui`` to keep the dependency one-way.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import agent_release

CACHE_TTL_S = 300.0
FETCH_TIMEOUT_S = 10.0

# repo -> (fetched_at, releases)
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = agent_release.github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _to_public(release: dict[str, Any]) -> dict[str, Any]:
    tag = str(release.get("tag_name") or "")
    return {
        "version": tag[1:] if tag[:1] in ("v", "V") else tag,
        "tag": tag,
        "name": release.get("name") or tag,
        "published_at": release.get("published_at"),
        "body": release.get("body") or "",
        "html_url": release.get("html_url"),
        "prerelease": bool(release.get("prerelease")),
    }


async def fetch_releases(repo: str) -> list[dict[str, Any]]:
    """Non-draft releases for ``repo``, newest first, cached for ``CACHE_TTL_S``.

    Best-effort: never raises. On failure, serves the last good cache entry
    (if any) rather than erroring the whole About popup; falls back to ``[]``.
    """

    now = time.monotonic()
    cached = _cache.get(repo)
    if cached is not None and now - cached[0] < CACHE_TTL_S:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, headers=_headers()) as client:
            resp = await client.get(
                f"{agent_release.GITHUB_API}/repos/{repo}/releases",
                params={"per_page": 30},
            )
            resp.raise_for_status()
            releases = [_to_public(r) for r in resp.json() if not r.get("draft")]
    except Exception:  # noqa: BLE001 - best-effort, degrade instead of erroring the modal
        return cached[1] if cached is not None else []
    _cache[repo] = (now, releases)
    return releases

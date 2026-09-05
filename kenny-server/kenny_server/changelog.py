"""Live GitHub Releases changelog for the dashboard's About modal.

Proxies ``GET /repos/{repo}/releases`` server-side (rather than having the
browser call GitHub directly) so the fetch can be shared across operators and
degrade gracefully instead of hitting per-client CORS/rate-limit issues. This
module imports nothing from ``webui`` to keep the dependency one-way.

Degrading is not the same as claiming success. A failed fetch is reported as
one — ``ChangelogResult.ok`` is False and ``error`` names the remedy — because
the alternative, an empty release list, is a statement the operator cannot tell
apart from "this repo has published nothing", and one they cannot check without
reading this file.

Like the agent-binary fetch beside it, this read is anonymous (ADR-0057): the
release list of a public repo needs no credential, and demanding one only adds a
way for it to fail.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from . import agent_release

logger = logging.getLogger("kenny.changelog")

CACHE_TTL_S = 300.0
FETCH_TIMEOUT_S = 10.0

# (repo, include_prerelease) -> (fetched_at_monotonic, fetched_at_iso, releases)
_cache: dict[tuple[str, bool], tuple[float, str, list[dict[str, Any]]]] = {}
# (repo, include_prerelease) -> the error text last logged for that view, so a
# dialog opened repeatedly against a broken token warns once, not once per open.
_last_error: dict[tuple[str, bool], str | None] = {}


@dataclass(frozen=True)
class ChangelogResult:
    """Releases plus what happened while getting them.

    Mirrors :class:`agent_release.FetchResult`'s shape (an outcome flag, an
    operator-readable message, a ``to_public``) so the two GitHub-facing modules
    report failure the same way.

    ``fetched_at`` describes the **data**, never the attempt: when a refresh
    fails and the last good list is served anyway, it stays at the timestamp of
    the fetch that produced that list.
    """

    releases: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    stale: bool = False
    fetched_at: str | None = None

    def to_public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "stale": self.stale,
            "fetched_at": self.fetched_at,
        }


def _describe(exc: Exception, repo: str) -> str:
    """An operator-readable reason for a failed read.

    HTTP statuses are classified by ``agent_release.describe_http_error`` rather
    than here: the agent-binary fetch hits the same API and used to word the same
    response differently, which left an operator comparing two texts for one
    fault. This module keeps only the transport-level cases, which are its own.
    """

    if isinstance(exc, httpx.HTTPStatusError):
        return agent_release.describe_http_error(exc, repo)
    if isinstance(exc, httpx.TimeoutException):
        return f"GitHub timed out after {FETCH_TIMEOUT_S:g}s"
    if isinstance(exc, httpx.RequestError):
        return f"GitHub unreachable: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _log(cache_key: tuple[str, bool], repo: str, error: str | None) -> None:
    """Warn on a new or changed failure, once; note the recovery."""

    previous = _last_error.get(cache_key)
    if error is None:
        if previous is not None:
            logger.info("changelog fetch recovered for %s", repo)
        _last_error[cache_key] = None
        return
    if error != previous:
        logger.warning("changelog fetch failed for %s: %s", repo, error)
    else:
        logger.debug("changelog fetch still failing for %s: %s", repo, error)
    _last_error[cache_key] = error


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


async def fetch_releases(repo: str, *, include_prerelease: bool = False) -> ChangelogResult:
    """Non-draft releases for ``repo``, newest first, cached for ``CACHE_TTL_S``.

    ``include_prerelease=False`` (the default, matching the About modal's
    existing stable-only view) additionally excludes ``prerelease`` entries —
    a dev-channel (ADR-0048) release published via `main`-push never shows up
    in the default changelog. The cache key includes the flag so the two views
    never clobber each other.

    Best-effort: never raises. A failed refresh serves the last good list with
    ``stale=True`` when there is one, and an empty list otherwise — with ``ok``
    False and ``error`` set either way. Failures are **not** cached: the next
    dialog open retries, so a token the operator has just fixed takes effect
    immediately instead of after the TTL.
    """

    now = time.monotonic()
    cache_key = (repo, include_prerelease)
    cached = _cache.get(cache_key)
    if cached is not None and now - cached[0] < CACHE_TTL_S:
        return ChangelogResult(releases=cached[2], fetched_at=cached[1])
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            headers=dict(agent_release.GITHUB_HEADERS),
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                f"{agent_release.GITHUB_API}/repos/{repo}/releases",
                params={"per_page": 30},
            )
            resp.raise_for_status()
            releases = [
                _to_public(r)
                for r in resp.json()
                if not r.get("draft") and (include_prerelease or not r.get("prerelease"))
            ]
    except Exception as exc:  # noqa: BLE001 - best-effort, degrade instead of erroring the modal
        error = _describe(exc, repo)
        _log(cache_key, repo, error)
        if cached is not None:
            return ChangelogResult(
                releases=cached[2], ok=False, error=error, stale=True, fetched_at=cached[1]
            )
        return ChangelogResult(ok=False, error=error)
    fetched_at = datetime.now(timezone.utc).isoformat()
    _cache[cache_key] = (now, fetched_at, releases)
    _log(cache_key, repo, None)
    return ChangelogResult(releases=releases, fetched_at=fetched_at)

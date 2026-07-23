"""Read-only GHCR polling for the kenny-server container image (ADR-0044).

The server ships as ``ghcr.io/t11z/kenny-server``, semver-tagged on every git
tag (ADR-0010). This module answers one question — "is there a newer tag than
the one currently running?" — via the anonymous OCI Distribution v2 API, and
nothing else: it never pulls an image and never touches Docker. Detection is
metadata-only (tag list + the winning tag's manifest digest, fetched with one
extra request); the operator-facing apply command pins that digest, since tags
are mutable and a digest is not.

Best-effort like ``agent_release``/``changelog``: any failure (unreachable,
rate-limited, malformed) is a skipped pass, never raised, and never a
downgrade prompt — only a strictly newer, well-formed semver tag is reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import httpx

DEFAULT_IMAGE_REF = "ghcr.io/t11z/kenny-server"
FETCH_TIMEOUT_S = 10.0

# The exact-version tag docker/metadata-action publishes (`{{version}}`), e.g.
# "1.4.2" — deliberately excludes "latest", "{{major}}.{{minor}}", and any
# prerelease/build-metadata suffix, so only a fully-qualified release tag is
# ever considered.
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.match(tag.strip())
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_image_ref(image_ref: str) -> tuple[str, str] | None:
    """Split ``ghcr.io/OWNER/NAME`` into ``(registry, "OWNER/NAME")``, or None."""

    parts = image_ref.strip().split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


@dataclass
class ServerReleaseInfo:
    """Outcome of a GHCR poll."""

    ok: bool
    message: str
    tag: str | None = None
    digest: str | None = None

    def to_public(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "tag": self.tag, "digest": self.digest}


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=FETCH_TIMEOUT_S)


async def _bearer_token(
    client: httpx.AsyncClient, registry: str, repo: str, github_token: str | None
) -> str | None:
    """Anonymous (or, for a private package, GitHub-PAT-authenticated) pull token."""

    params = {"service": registry, "scope": f"repository:{repo}:pull"}
    auth = ("token", github_token) if github_token else None
    resp = await client.get(f"https://{registry}/token", params=params, auth=auth)
    resp.raise_for_status()
    token = resp.json().get("token")
    return str(token) if token else None


async def fetch_latest_server_tag(
    image_ref: str = DEFAULT_IMAGE_REF,
    *,
    github_token: str | None = None,
    client_factory: Callable[[], httpx.AsyncClient] = _default_client_factory,
) -> ServerReleaseInfo:
    """Newest well-formed semver tag for ``image_ref``, with its manifest digest.

    Two GHCR requests when a candidate exists: the tag list, then one manifest
    HEAD for the winning tag's digest. ``client_factory`` is injected so tests
    use ``httpx.MockTransport`` (no network), matching ``agent_release``.
    """

    parsed = _parse_image_ref(image_ref)
    if parsed is None:
        return ServerReleaseInfo(ok=False, message=f"invalid image ref: {image_ref!r}")
    registry, repo = parsed

    try:
        async with client_factory() as client:
            token = await _bearer_token(client, registry, repo, github_token)
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            tags_resp = await client.get(f"https://{registry}/v2/{repo}/tags/list", headers=headers)
            if tags_resp.status_code == 404:
                return ServerReleaseInfo(ok=False, message=f"no such package: {repo}")
            tags_resp.raise_for_status()
            tags = tags_resp.json().get("tags") or []

            candidates = sorted(
                (t for t in ((tag, _parse_semver(tag)) for tag in tags) if t[1] is not None),
                key=lambda t: t[1],
            )
            if not candidates:
                return ServerReleaseInfo(ok=False, message="no semver-tagged release found")
            best_tag, _ = candidates[-1]

            manifest_headers = dict(headers)
            manifest_headers["Accept"] = (
                "application/vnd.oci.image.index.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.docker.distribution.manifest.v2+json"
            )
            manifest_resp = await client.head(
                f"https://{registry}/v2/{repo}/manifests/{best_tag}", headers=manifest_headers
            )
            manifest_resp.raise_for_status()
            digest = manifest_resp.headers.get("Docker-Content-Digest")
    except Exception as exc:  # noqa: BLE001 - best-effort, never raise, never a downgrade prompt
        return ServerReleaseInfo(ok=False, message=f"GHCR check failed: {exc}")

    return ServerReleaseInfo(ok=True, message=f"latest tag {best_tag}", tag=best_tag, digest=digest)


def is_newer(candidate_tag: str, current_version: str) -> bool:
    """Whether ``candidate_tag`` is a strictly newer semver than ``current_version``.

    ``current_version`` failing to parse as a clean ``X.Y.Z`` (e.g. the dev
    fallback ``"0.0.0-dev"``) means "unknown" — never claim an update is
    available when the running version can't be confidently compared, so a
    dev/unreleased build never shows update noise.
    """

    candidate = _parse_semver(candidate_tag)
    current = _parse_semver(current_version)
    if candidate is None or current is None:
        return False
    return candidate > current

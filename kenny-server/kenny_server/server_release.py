"""Read-only GHCR polling for the kenny-server container image (ADR-0040).

The server ships as ``ghcr.io/nullthrone/kenny-server``, semver-tagged on every git
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

DEFAULT_IMAGE_REF = "ghcr.io/nullthrone/kenny-server"
FETCH_TIMEOUT_S = 10.0

# The exact-version tag docker/metadata-action publishes (`{{version}}`), e.g.
# "1.4.2" — deliberately excludes "latest", "{{major}}.{{minor}}", and any
# prerelease/build-metadata suffix, so only a fully-qualified release tag is
# ever considered.
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# The exact-version tag the dev-channel workflow publishes for a `main`-push
# build (ADR-0048): `X.Y.Z-dev.N`, mirroring the git tag `vX.Y.Z-dev.N` minus
# the `v`. `N` is the CI run number, monotonically increasing per triple.
_SEMVER_PRERELEASE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-dev\.(\d+)$")


def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.match(tag.strip())
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_semver_prerelease(tag: str) -> tuple[int, int, int, int] | None:
    """Parse a dev-channel tag/version, e.g. ``2.0.5-dev.17`` -> ``(2, 0, 5, 17)``."""

    m = _SEMVER_PRERELEASE_RE.match(tag.strip())
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def _parse_dev_comparable(tag: str) -> tuple[int, int, int, int] | None:
    """Parse ``tag`` for dev-channel ordering purposes.

    A proper dev-prerelease (``X.Y.Z-dev.N``) parses to ``(X, Y, Z, N)``. A
    plain release tag (``X.Y.Z``) also parses, as ``(X, Y, Z, -1)``, so a
    same-triple plain release compares as strictly older than any dev
    prerelease of that triple — this comparison direction only matters within
    the dev channel's own history (e.g. comparing a stable-built server's
    ``__version__`` baseline against a dev candidate), never against the
    stable channel's own semver ordering.
    """

    prerelease = _parse_semver_prerelease(tag)
    if prerelease is not None:
        return prerelease
    plain = _parse_semver(tag)
    if plain is not None:
        return (*plain, -1)
    return None


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
    channel: str = "stable",
) -> ServerReleaseInfo:
    """Newest well-formed semver tag for ``image_ref``, with its manifest digest.

    Two GHCR requests when a candidate exists: the tag list, then one manifest
    HEAD for the winning tag's digest. ``client_factory`` is injected so tests
    use ``httpx.MockTransport`` (no network), matching ``agent_release``.

    ``channel="stable"`` is byte-identical to the original behavior: only bare
    ``X.Y.Z`` tags are considered (excludes ``edge`` and any ``-dev.`` tag).
    ``channel="dev"`` instead considers only ``X.Y.Z-dev.N`` tags, picking the
    highest ``(major, minor, patch, dev_n)`` tuple — the exact versioned tag,
    never the floating ``:edge`` alias, so a later pin is always an immutable
    tag+digest (ADR-0040's pinning discipline).
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

            parse = _parse_semver_prerelease if channel == "dev" else _parse_semver
            candidates = sorted(
                (t for t in ((tag, parse(tag)) for tag in tags) if t[1] is not None),
                key=lambda t: t[1],
            )
            if not candidates:
                kind = "dev-prerelease-tagged" if channel == "dev" else "semver-tagged"
                return ServerReleaseInfo(ok=False, message=f"no {kind} release found")
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


def is_newer(candidate_tag: str, current_version: str, *, channel: str = "stable") -> bool:
    """Whether ``candidate_tag`` is a strictly newer version than ``current_version``.

    ``channel="stable"`` is byte-identical to the original behavior: both sides
    must parse as a clean ``X.Y.Z``; a failure to parse (e.g. the dev fallback
    ``"0.0.0-dev"``) means "unknown" — never claim an update is available when
    the running version can't be confidently compared.

    ``channel="dev"`` compares ``(major, minor, patch, dev_n)`` tuples instead,
    parsing either an ``X.Y.Z-dev.N`` prerelease tag or (falling back, for the
    comparison only) a plain ``X.Y.Z`` release as ``dev_n = -1`` — so a same-
    triple plain release compares as older than any dev prerelease of that
    triple. This fallback direction only matters within the dev channel's own
    history, never against the stable channel's own semver ordering. If either
    side fails to parse under the channel's own rule, this returns ``False``
    (never claim newer on ambiguity), matching the stable path's behavior.
    """

    if channel == "dev":
        candidate = _parse_dev_comparable(candidate_tag)
        current = _parse_dev_comparable(current_version)
        if candidate is None or current is None:
            return False
        return candidate > current

    candidate = _parse_semver(candidate_tag)
    current = _parse_semver(current_version)
    if candidate is None or current is None:
        return False
    return candidate > current

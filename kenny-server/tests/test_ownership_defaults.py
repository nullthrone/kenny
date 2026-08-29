"""Where kenny publishes and where the server looks must be the same place.

The owner of the canonical repository appears as a literal in six shipped
surfaces — two Python defaults, the settings catalog, the legacy dashboard, the
React dialog, and the compose/env files an operator copies. Nothing bound them
together, so when the project moved orgs the release workflow followed
``github.repository_owner`` while ``server_release.DEFAULT_IMAGE_REF`` kept
polling a GHCR package that no longer receives pushes. That failure is invisible
by design (``update_manager`` demotes an unreachable image to one info line), so
the drift survived two releases.

This module makes the halves agree, and fails closed: a file carrying the owner
counts as needing the canonical one unless it is listed here as maintainer
identity.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from kenny_server import agent_release, config, server_release

_SERVER = Path(__file__).resolve().parents[1]
_ROOT = _SERVER.parent
_PYPROJECT = _SERVER / "pyproject.toml"
_RELEASE_ARTIFACTS = _ROOT / ".github" / "workflows" / "_release-artifacts.yml"

def _canonical_owner() -> str:
    """The one declared source of the owner.

    Everything else is checked against this rather than against another
    literal, so there is exactly one place to edit if the project moves again.
    """

    data = tomllib.loads(_PYPROJECT.read_text())
    repo_url = data["project"]["urls"]["Repository"]
    match = re.fullmatch(r"https://github\.com/([^/]+)/kenny", repo_url)
    assert match, f"[project.urls] Repository is not a github.com/<owner>/kenny URL: {repo_url!r}"
    return match.group(1)


#: Files whose ``t11z`` is the **maintainer** — a person, a copyright holder, or
#: a GitHub login a workflow condition compares against — and must NOT track the
#: repository's location. Changing these would misattribute the work, or (for
#: ``triage.yml``) silently switch off the triage automation by comparing against
#: a login that does not exist.
_MAINTAINER_IDENTITY = frozenset(
    {
        "SECURITY.md",  # the private-report contact, @t11z (the advisories URL on L11 is a location and does track)
        "CODE_OF_CONDUCT.md",  # enforcement contact
        "mkdocs.yml",  # copyright line
        "kenny-agent/Cargo.toml",  # authors
        "kenny-server/pyproject.toml",  # authors
        ".github/workflows/triage.yml",  # `issue.user.login != 't11z'` gates
        "docs/adr/0041-reliability-alarm-suppression.md",  # a record's citation is historical
    }
)

#: The surfaces that ship the owner as behavior or as a link an operator follows.
#: Each is scanned for a stale owner; the canonical one is asserted present.
_LOCATION_BEARING = (
    "kenny-server/kenny_server/agent_release.py",
    "kenny-server/kenny_server/server_release.py",
    "kenny-server/kenny_server/config.py",
    "kenny-server/kenny_server/webui/index.html",
    "kenny-web/src/components/AboutModal/AboutModal.tsx",
    "compose.yaml",
    ".env.example",
)


def test_python_defaults_agree_on_the_canonical_repo() -> None:
    owner = _canonical_owner()
    assert agent_release.DEFAULT_REPO == f"{owner}/kenny"
    assert config.CATALOG["KENNY_GITHUB_REPO"].default_raw == agent_release.DEFAULT_REPO


def test_server_image_ref_defaults_agree() -> None:
    assert (
        config.CATALOG["KENNY_SERVER_IMAGE_REF"].default_raw == server_release.DEFAULT_IMAGE_REF
    )


def test_ci_publishes_to_the_image_ref_the_server_polls() -> None:
    """The joined half: the workflow's target and the poller's default are one place.

    Checking only that both mention *an* owner would pass while they disagree,
    which is exactly the state this test was written for.
    """

    text = _RELEASE_ARTIFACTS.read_text()
    assert "images: ghcr.io/${{ github.repository_owner }}/kenny-server" in text, (
        "the release workflow no longer publishes to "
        "ghcr.io/<repository_owner>/kenny-server; DEFAULT_IMAGE_REF cannot be "
        "derived from the owner any more."
    )
    owner = _canonical_owner()
    assert server_release.DEFAULT_IMAGE_REF == f"ghcr.io/{owner}/kenny-server", (
        "the server polls a GHCR package the release workflow does not push to; "
        "every update check would be skipped with one info-level log line."
    )


def test_compose_and_env_example_track_the_same_owner() -> None:
    owner = _canonical_owner()
    compose = (_ROOT / "compose.yaml").read_text()
    assert f"ghcr.io/${{KENNY_OWNER:-{owner}}}/kenny-server" in compose
    assert f"KENNY_GITHUB_REPO:-{owner}/kenny" in compose
    env_example = (_ROOT / ".env.example").read_text()
    assert f"KENNY_GITHUB_REPO={owner}/kenny" in env_example
    assert f"KENNY_OWNER={owner}" in env_example


def test_the_dashboards_fall_back_to_the_same_repo_as_the_server() -> None:
    """Three copies of one constant: the API's default and both dashboards'."""

    canonical = agent_release.DEFAULT_REPO
    modal = (_ROOT / "kenny-web/src/components/AboutModal/AboutModal.tsx").read_text()
    assert f"const DEFAULT_REPO = '{canonical}'" in modal
    legacy = (_ROOT / "kenny-server/kenny_server/webui/index.html").read_text()
    assert f'"{canonical}"' in legacy


def test_shipped_surfaces_carry_no_stale_owner() -> None:
    owner = _canonical_owner()
    stale: list[str] = []
    for rel in _LOCATION_BEARING:
        assert rel not in _MAINTAINER_IDENTITY, f"{rel} cannot be both a location and an identity"
        for i, line in enumerate((_ROOT / rel).read_text().splitlines(), 1):
            if re.search(r"\bt11z\b", line) and owner != "t11z":
                stale.append(f"{rel}:{i}")
    assert not stale, (
        f"stale repository owner still shipped at {stale}. These are locations, "
        "not attribution — the maintainer's name belongs only in the files "
        "listed in _MAINTAINER_IDENTITY."
    )

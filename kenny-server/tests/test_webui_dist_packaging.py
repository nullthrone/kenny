"""The seam between two claims that must never drift apart (ADR-0052):
packaging says the built SPA ships inside the wheel, and the server says it
serves that build. Nothing enforced either claim against the other before
this test existed -- dropping the ``dist/**/*`` glob from ``pyproject.toml``
would silently ship a wheel with an unreachable dashboard, and the failure
would only surface as an end user's blank page long after CI was green.

This module writes a real ``dist/index.html`` next to the actual package (not
a stand-in for it, and not somewhere setuptools would need special-casing to
find), then checks it from both sides of the seam against that one concrete
file: setuptools' own ``package_data`` glob resolution -- loaded from the
real ``pyproject.toml``, not copied here -- must find it, and the real app,
built through ``build_app`` and driven with ``TestClient`` exactly like every
other integration test in this suite, must serve it. Breaking either side
fails the test; so does a change that leaves the legacy page as the only
working entry point, because the serving assertion is pinned to a sentinel
only the freshly written ``dist/index.html`` contains.

A real ``kenny-web`` build may already sit at ``dist/`` in this checkout
(it's a gitignored artifact -- ADR-0052 -- so its presence is just whatever
the last local build left behind). The fixture below never deletes one: it
parks a pre-existing ``dist/`` aside for the duration of the test and puts it
back afterward, so this test's own synthetic build is what gets exercised
either way.
"""

from __future__ import annotations

import shutil
import tomllib
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from setuptools.command.build_py import build_py
from setuptools.dist import Distribution
from starlette.testclient import TestClient

from kenny_server.main import build_app

_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"
_WEBUI_DIR = _ROOT / "kenny_server" / "webui"
_DIST_DIR = _WEBUI_DIR / "dist"
_DIST_INDEX = _DIST_DIR / "index.html"
_PARKED_DIST_DIR = _WEBUI_DIR / "dist.seam-test-parked"


def _package_data() -> dict[str, list[str]]:
    """The real ``[tool.setuptools.package-data]`` table, read from the repo's
    actual ``pyproject.toml`` -- not a copy of it, so this test drifts
    together with the config it exists to guard."""

    data = tomllib.loads(_PYPROJECT.read_text())
    return data["tool"]["setuptools"]["package-data"]


def _files_setuptools_would_ship(package: str, src_dir: Path) -> set[Path]:
    """What setuptools' own package-data glob resolution finds on disk right
    now for ``package`` -- the same mechanism ``python -m build`` uses to
    decide what a wheel carries, driven directly instead of through a full
    wheel build."""

    package_data = _package_data()
    dist = Distribution({"package_data": package_data})
    cmd = build_py(dist)
    cmd.package_data = package_data
    cmd.manifest_files = {}
    cmd.exclude_package_data = {}
    return {Path(f).resolve() for f in cmd.find_data_files(package, str(src_dir))}


@pytest.fixture
def no_dist() -> Iterator[None]:
    """Guarantees ``webui/dist/`` does not exist for the duration of the
    test. If a real build is already sitting there (e.g. a local
    ``npm run build`` left it behind -- it's gitignored, ADR-0052), it is
    parked aside and restored afterward rather than deleted."""

    assert not _PARKED_DIST_DIR.exists(), (
        f"{_PARKED_DIST_DIR} already exists from a previous interrupted run -- "
        "remove it manually before re-running this test"
    )
    parked = False
    if _DIST_DIR.exists():
        _DIST_DIR.rename(_PARKED_DIST_DIR)
        parked = True
    try:
        yield
    finally:
        shutil.rmtree(_DIST_DIR, ignore_errors=True)
        if parked:
            _PARKED_DIST_DIR.rename(_DIST_DIR)


@pytest.fixture
def built_dist(no_dist: None) -> Iterator[str]:
    """This test's own real, temporary build at the exact path the server
    resolves -- ``kenny_server/webui/dist/index.html`` -- so both halves of
    the seam are checked against one concrete file instead of a stand-in for
    either.

    Content is a random sentinel, so a passing serving assertion can only be
    explained by this file being served: not by the legacy page (which
    cannot contain it) and not by coincidence.
    """

    sentinel = f"SEAM-TEST-{uuid.uuid4().hex}"
    (_DIST_DIR / "assets").mkdir(parents=True)
    _DIST_INDEX.write_text(f"<!doctype html><html><body>{sentinel}</body></html>")
    (_DIST_DIR / "assets" / "app.js").write_text(f"// {sentinel}")
    yield sentinel


def test_dist_glob_in_pyproject_matches_what_the_server_serves(
    built_dist: str, tmp_path: Path
) -> None:
    """The packaging seam, joined: the same ``dist/index.html`` that
    ``pyproject.toml``'s package-data glob would ship into the wheel is the
    one the running server actually returns for ``GET /``."""

    # Side 1: packaging. Dropping "dist/**/*" from pyproject.toml's
    # package-data makes this assertion fail -- the wheel would no longer
    # carry the built SPA, even though it exists right here on disk.
    shipped = _files_setuptools_would_ship("kenny_server.webui", _WEBUI_DIR)
    assert _DIST_INDEX.resolve() in shipped, (
        "kenny_server/webui/dist/index.html would NOT be included in the wheel -- "
        "check [tool.setuptools.package-data] in pyproject.toml still globs dist/**/*"
    )

    # Side 2: serving. The sentinel exists only in the dist/index.html this
    # fixture just wrote, so this can only pass if the server is actually
    # resolving and returning *that* file for "/" -- not the legacy page
    # (which cannot contain a sentinel it was never given) and not the
    # "frontend not built" diagnostic.
    app = build_app(db_path=str(tmp_path / "seam.sqlite"))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.operator_token}"}
        response = client.get("/", headers=headers)
    assert response.status_code == 200
    assert built_dist in response.text, (
        "GET / did not return the built SPA's dist/index.html -- the server's "
        "static-serving path is not resolving the built entry point"
    )


def test_dist_glob_does_not_trivially_match_the_legacy_page(no_dist: None) -> None:
    """Guards against a vacuous version of the test above: with no ``dist/``
    built at all, setuptools' glob resolution must not report the compiled
    entry point as shipped just because the legacy ``index.html`` happens to
    exist -- they are different files, matched by different package-data
    patterns ("dist/**/*" vs "*.html")."""

    assert not _DIST_DIR.exists(), "dist/ must not exist for this assertion to mean anything"
    shipped = _files_setuptools_would_ship("kenny_server.webui", _WEBUI_DIR)
    assert _DIST_INDEX.resolve() not in shipped
    # The legacy page is still covered, by its own glob, independent of dist/.
    assert (_WEBUI_DIR / "index.html").resolve() in shipped

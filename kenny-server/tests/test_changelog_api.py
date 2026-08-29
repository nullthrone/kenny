"""``GET /api/changelog`` — the route, not just the module behind it.

There was no route-level test here, which is part of how the defect survived:
``fetch_releases`` swallowed every failure and the handler always 200'd with an
empty list, so the dashboard reported "this repo has published nothing yet" for
a request that never reached GitHub. These pin the distinction on the wire.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from kenny_server import changelog
from kenny_server.main import build_app


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _app(tmp_path):
    return build_app(db_path=str(tmp_path / "changelog-api.sqlite"))


def test_changelog_endpoint_carries_the_failure(tmp_path, monkeypatch):
    async def failing(repo, *, include_prerelease=False):
        return changelog.ChangelogResult(
            ok=False,
            error="GitHub rejected the credentials (401) — KENNY_GITHUB_TOKEN is invalid",
        )

    monkeypatch.setattr(changelog, "fetch_releases", failing)
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/changelog", headers=_bearer(app))
        # 200: *this* API worked. The upstream outcome rides in the payload, so
        # a client can still render whatever cached releases came with it.
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert "KENNY_GITHUB_TOKEN" in body["error"]
        assert body["releases"] == []


def test_changelog_endpoint_marks_cached_notes_as_stale(tmp_path, monkeypatch):
    entry = {
        "version": "2.2.1", "tag": "v2.2.1", "name": "v2.2.1",
        "published_at": "2026-08-29T14:21:16Z", "body": "notes",
        "html_url": None, "prerelease": False,
    }

    async def stale(repo, *, include_prerelease=False):
        return changelog.ChangelogResult(
            releases=[entry], ok=False, error="GitHub unreachable: boom",
            stale=True, fetched_at="2026-08-29T10:00:00+00:00",
        )

    monkeypatch.setattr(changelog, "fetch_releases", stale)
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/changelog", headers=_bearer(app)).json()
        assert body["releases"] == [entry]  # still useful
        assert body["stale"] is True and body["ok"] is False
        assert body["fetched_at"] == "2026-08-29T10:00:00+00:00"


def test_changelog_endpoint_keeps_its_shape_on_success(tmp_path, monkeypatch):
    """`repo` and `releases` are unchanged, so an older bundled UI still works."""

    async def ok(repo, *, include_prerelease=False):
        return changelog.ChangelogResult(releases=[], fetched_at="2026-08-29T10:00:00+00:00")

    monkeypatch.setattr(changelog, "fetch_releases", ok)
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = c.get("/api/changelog", headers=_bearer(app)).json()
        assert set(body) == {"repo", "releases", "ok", "error", "stale", "fetched_at"}
        assert body["repo"] and body["releases"] == []
        assert body["ok"] is True and body["error"] is None and body["stale"] is False


def test_changelog_requires_auth(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/changelog").status_code == 401

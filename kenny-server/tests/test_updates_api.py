"""Dashboard API for scheduled updates + campaign rollout (ADR-0040).

Route-level smoke tests, following the ``build_app`` + ``TestClient`` +
bearer-token pattern from ``test_backup_api.py``. GHCR is never touched over
the real network: ``server_release.fetch_latest_server_tag`` is monkeypatched
per test that exercises the check route.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from kenny_server import agent_release, server_release
from kenny_server.main import build_app
from kenny_server.server_release import ServerReleaseInfo

BINARY_BYTES = b"MZ fake kenny-agent.exe payload \x00\x01\x02"


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _app(tmp_path, name="updates_api.sqlite"):
    return build_app(db_path=str(tmp_path / name))


def _write_cached_binary(tmp_path, monkeypatch, db_name, version):
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / db_name))
    path = agent_release.cache_path("windows", "x86_64")
    with open(path, "wb") as fh:
        fh.write(BINARY_BYTES)
    with open(path + ".version", "w", encoding="utf-8") as fh:
        fh.write(version)


def test_updates_requires_auth(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/updates").status_code == 401


def test_updates_shape_empty(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/updates", headers=_bearer(app))
        assert r.status_code == 200
        body = r.json()
        # Not empty: startup records how the agent-binary fetch went, and the
        # reason belongs on a durable row rather than in a log line that scrolls
        # away. Here the suite's network guard fails it (tests/conftest.py); on a
        # real server this row is how a stale staged version explains itself.
        # Nothing has been detected as *available*, though.
        assert set(body["available"]) == {"agent"}
        assert body["available"]["agent"]["ok"] is False
        assert body["available"]["agent"]["message"]
        assert body["active_campaign"] is None
        assert body["campaigns"] == []
        assert body["agents"] == []
        assert set(body["config"]) == {
            "check_interval_secs", "rollout_on_connect", "server_image_ref"
        }
        assert body["server_apply"] is None


def test_updates_check_now_records_availability_without_network(tmp_path, monkeypatch) -> None:
    _write_cached_binary(tmp_path, monkeypatch, "updates_api.sqlite", "1.0.0")

    async def fake_fetch(image_ref, *, github_token=None, client_factory=None):
        return ServerReleaseInfo(ok=True, message="latest tag 9.9.9", tag="9.9.9", digest="sha256:" + "c" * 64)

    monkeypatch.setattr(server_release, "fetch_latest_server_tag", fake_fetch)
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        r = c.post("/api/updates/check", headers=h)
        assert r.status_code == 200
        assert r.json()["ok"] is True

        status = c.get("/api/updates", headers=h).json()
        assert status["available"]["agent"]["version"] == "1.0.0"
        # The dev-build fallback version ("0.0.0-dev") never parses as a clean
        # semver, so `is_newer` refuses to call anything "newer" against it —
        # a dev build must never show update noise. This exercises the "up to
        # date" branch; the "genuinely newer" branch is unit-tested directly
        # against `UpdateManager.check_now` in test_update_manager.py, where
        # `__version__` can be patched to a real semver.
        assert status["available"]["server"]["version"] == "0.0.0-dev"
        assert status["server_apply"] is None


def test_updates_campaign_approve_apply_revoke(tmp_path, monkeypatch) -> None:
    _write_cached_binary(tmp_path, monkeypatch, "updates_api.sqlite", "1.0.0")
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        created = c.post("/api/updates/campaigns", headers=h, json={"version": "1.0.0"})
        assert created.status_code == 201
        campaign = created.json()["campaign"]
        assert campaign["version"] == "1.0.0"
        assert campaign["status"] == "active"

        status = c.get("/api/updates", headers=h).json()
        assert status["active_campaign"]["id"] == campaign["id"]

        # no agent is online -> nothing to attempt, but the call itself succeeds
        applied = c.post(f"/api/updates/campaigns/{campaign['id']}/apply-now", headers=h)
        assert applied.status_code == 200
        assert applied.json()["attempted"] == []

        revoked = c.post(f"/api/updates/campaigns/{campaign['id']}/revoke", headers=h)
        assert revoked.status_code == 200
        assert revoked.json()["ok"] is True

        after = c.get("/api/updates", headers=h).json()
        assert after["active_campaign"] is None


def test_updates_campaign_approve_400_without_cached_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KENNY_DB_PATH", str(tmp_path / "updates_api.sqlite"))
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/updates/campaigns", headers=_bearer(app), json={"version": "9.9.9"})
        assert r.status_code == 400


def test_updates_apply_now_404_without_active_campaign(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/updates/campaigns/does-not-exist/apply-now", headers=_bearer(app))
        assert r.status_code == 400


def test_updates_revoke_404_when_not_active(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/updates/campaigns/does-not-exist/revoke", headers=_bearer(app))
        assert r.status_code == 404


def test_updates_campaign_suspend_and_resume(tmp_path, monkeypatch) -> None:
    _write_cached_binary(tmp_path, monkeypatch, "updates_api.sqlite", "1.0.0")
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        created = c.post("/api/updates/campaigns", headers=h, json={"version": "1.0.0"})
        campaign_id = created.json()["campaign"]["id"]

        suspended = c.post(f"/api/updates/campaigns/{campaign_id}/suspend", headers=h)
        assert suspended.status_code == 200
        assert suspended.json()["ok"] is True

        # no longer the active campaign, and apply-now refuses it
        after_suspend = c.get("/api/updates", headers=h).json()
        assert after_suspend["active_campaign"] is None
        applied = c.post(f"/api/updates/campaigns/{campaign_id}/apply-now", headers=h)
        assert applied.status_code == 400

        # suspending again is refused (already suspended, not active)
        again = c.post(f"/api/updates/campaigns/{campaign_id}/suspend", headers=h)
        assert again.status_code == 404

        resumed = c.post(f"/api/updates/campaigns/{campaign_id}/resume", headers=h)
        assert resumed.status_code == 200
        assert resumed.json()["ok"] is True

        after_resume = c.get("/api/updates", headers=h).json()
        assert after_resume["active_campaign"]["id"] == campaign_id


def test_updates_suspend_404_when_not_active(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/updates/campaigns/does-not-exist/suspend", headers=_bearer(app))
        assert r.status_code == 404


def test_updates_resume_404_when_not_suspended(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/updates/campaigns/does-not-exist/resume", headers=_bearer(app))
        assert r.status_code == 404

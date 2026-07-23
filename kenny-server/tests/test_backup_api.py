"""Dashboard API for DB backup/restore + remote backup targets (Phase B).

Route-level smoke tests: shape of the list/create/verify/delete/restore
endpoints and the backup-target CRUD, plus role enforcement (superuser only).
Follows the ``build_app`` + ``TestClient`` + bearer-token pattern used by
``test_dashboard_api.py`` / ``test_rbac.py``.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from kenny_server.main import build_app


def _bearer(app):
    return {"Authorization": f"Bearer {app.state.operator_token}"}


def _app(tmp_path, name="backup_api.sqlite"):
    return build_app(db_path=str(tmp_path / name))


def _setup_admin(c) -> None:
    r = c.post(
        "/setup", data={"username": "admin", "password": "pw-123456"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _pat_for(c, username: str) -> str:
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    uid = users[username]["id"]
    return c.post(f"/api/users/{uid}/pats", json={"label": "t"}).json()["token"]


def test_backups_list_shape(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        r = c.get("/api/backups", headers=_bearer(app))
        assert r.status_code == 200
        body = r.json()
        assert body["backups"] == []
        assert body["targets"] == []
        assert set(body["config"]) == {"interval_secs", "retention", "backup_dir"}


def test_backups_create_list_verify_download_delete_roundtrip(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        created = c.post("/api/backups", headers=h).json()
        assert created["ok"] is True
        name = created["name"]
        assert created["integrity"] == "ok"

        listed = c.get("/api/backups", headers=h).json()["backups"]
        assert any(b["name"] == name for b in listed)

        verified = c.post(f"/api/backups/{name}/verify", headers=h).json()
        assert verified["integrity"] == "ok"

        dl = c.get(f"/api/backups/{name}/download?source=local", headers=h)
        assert dl.status_code == 200
        assert dl.headers["content-type"] == "application/octet-stream"
        assert len(dl.content) > 0

        deleted = c.delete(f"/api/backups/{name}", headers=h).json()
        assert deleted["ok"] is True
        assert deleted["results"]["local"] is True

        listed_after = c.get("/api/backups", headers=h).json()["backups"]
        assert not any(b["name"] == name for b in listed_after)


def test_backups_verify_unknown_name_is_400(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        r = c.post("/api/backups/not-a-real-name.sqlite/verify", headers=h)
        assert r.status_code == 400


def test_backups_restore_stages_and_schedules_shutdown(tmp_path, monkeypatch) -> None:
    """Restore stages the file and returns ok; the self-shutdown kill is stubbed
    so the test process is never actually signalled (the real call is scheduled
    ~1s out via call_later, which may or may not fire before the TestClient's
    background loop shuts down — either way this must never touch the real
    process signal)."""

    import kenny_server.webui as webui_module

    monkeypatch.setattr(webui_module.os, "kill", lambda pid, sig: None)

    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        created = c.post("/api/backups", headers=h).json()
        name = created["name"]

        r = c.post(f"/api/backups/{name}/restore", headers=h)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "restarting": True}

        pending_path = str(tmp_path / "backup_api.sqlite.restore-pending")
        marker_path = str(tmp_path / "backup_api.sqlite.restore-marker")
        import os

        assert os.path.exists(pending_path)
        assert os.path.exists(marker_path)


def test_backup_targets_crud_masks_secrets(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        created = c.post(
            "/api/backup-targets",
            headers=h,
            json={
                "kind": "ftp",
                "label": "Offsite",
                "config": {"host": "ftp.example.test", "password": "s3cr3t"},
            },
        )
        assert created.status_code == 201
        row = created.json()
        assert row["config"]["password"] is None
        assert row["config"]["password_set"] is True
        target_id = row["id"]

        listed = c.get("/api/backup-targets", headers=h).json()["targets"]
        assert len(listed) == 1
        assert listed[0]["config"]["password"] is None

        # Update label only: the password (omitted) must remain set.
        updated = c.put(
            f"/api/backup-targets/{target_id}",
            headers=h,
            json={"label": "Offsite (renamed)"},
        ).json()
        assert updated["label"] == "Offsite (renamed)"
        assert updated["config"]["password_set"] is True

        test_result = c.post(f"/api/backup-targets/{target_id}/test", headers=h).json()
        assert "ok" in test_result

        deleted = c.delete(f"/api/backup-targets/{target_id}", headers=h).json()
        assert deleted["ok"] is True
        assert c.get("/api/backup-targets", headers=h).json()["targets"] == []


def test_backup_targets_reject_local_kind(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = _bearer(app)
        r = c.post(
            "/api/backup-targets",
            headers=h,
            json={"kind": "local", "label": "nope", "config": {}},
        )
        assert r.status_code == 400


def test_backup_routes_require_superuser(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        _setup_admin(c)
        c.post(
            "/api/users",
            json={"username": "op", "password": "pw-123456", "role": "operator"},
        )
        op_pat = _pat_for(c, "op")
        h = {"Authorization": f"Bearer {op_pat}"}
        assert c.get("/api/backups", headers=h).status_code == 403
        assert c.get("/api/backup-targets", headers=h).status_code == 403
        assert c.post("/api/backups", headers=h).status_code == 403

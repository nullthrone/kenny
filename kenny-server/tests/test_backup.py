"""Tests for the backup engine (``backup.py``) and destinations (``backup_targets.py``).

Follows the ``tmp_path`` + connect/close pattern from ``test_store_chat_history.py``.
Network-touching destinations (HTTP/SCP/FTP) are exercised either against a fake
in-memory transport (HTTP) or via negative-path connectivity checks that must
never raise (SCP/FTP), per the plan's guidance to avoid real network in CI.
"""

from __future__ import annotations

import email
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from kenny_server.backup import BackupManager, apply_pending_restore
from kenny_server.backup_targets import (
    BackupDestination,
    FtpDestination,
    HttpDestination,
    LocalDestination,
    ScpDestination,
    build_destination,
)
from kenny_server.store import BackupTargetStore, TelemetryStore


# --- helpers ----------------------------------------------------------------


def _parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
    """Parse an httpx-generated multipart/form-data body (MIME-compatible)."""

    header = f"Content-Type: {content_type}\r\n\r\n".encode()
    msg = email.message_from_bytes(header + body)
    parts: dict[str, bytes] = {}
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = part.get_param("name", header="Content-Disposition")
        if name is None:
            continue
        payload = part.get_payload(decode=True)
        parts[name] = payload if payload is not None else b""
    return parts


def _make_http_handler(storage: dict[str, dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/" and request.method == "POST":
            content_type = request.headers.get("content-type", "")
            parts = _parse_multipart(content_type, request.content)
            meta = json.loads(parts["meta"].decode("utf-8"))
            storage[meta["name"]] = {"file": parts["file"], "meta": meta}
            return httpx.Response(200, json={"ok": True})
        if path == "/" and request.method == "GET":
            return httpx.Response(200, json=[v["meta"] for v in storage.values()])
        name = path.lstrip("/")
        if request.method == "GET":
            entry = storage.get(name)
            if entry is None:
                return httpx.Response(404)
            return httpx.Response(200, content=entry["file"])
        if request.method == "DELETE":
            if name in storage:
                del storage[name]
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404)
        return httpx.Response(404)

    return handler


class FailingDestination(BackupDestination):
    """A fake remote target whose ``store()`` always raises."""

    async def store(self, local_path: str, name: str, meta: dict[str, Any]) -> None:
        raise RuntimeError("simulated remote failure")

    async def list(self) -> list[dict[str, Any]]:
        return []

    async def retrieve(self, name: str, dest_local_path: str) -> None:
        raise RuntimeError("simulated remote failure")

    async def delete(self, name: str) -> bool:
        return False

    async def test(self) -> dict[str, Any]:
        return {"ok": False, "message": "simulated"}


# --- LocalDestination ---------------------------------------------------------


async def test_local_destination_roundtrip(tmp_path) -> None:
    src = tmp_path / "src.sqlite"
    src.write_bytes(b"hello world" * 100)
    dest_dir = tmp_path / "backups"
    dest = LocalDestination(str(dest_dir))

    meta = {"name": "kenny-backup-20260101T000000Z.sqlite", "created_at": "x", "size": 1}
    await dest.store(str(src), meta["name"], meta)

    listed = await dest.list()
    assert len(listed) == 1
    assert listed[0]["name"] == meta["name"]

    out = tmp_path / "out.sqlite"
    await dest.retrieve(meta["name"], str(out))
    assert out.read_bytes() == src.read_bytes()

    result = await dest.test()
    assert result["ok"] is True

    assert await dest.delete(meta["name"]) is True
    assert await dest.list() == []
    assert await dest.delete(meta["name"]) is False


# --- BackupManager.create -----------------------------------------------------


async def test_backup_manager_create_produces_consistent_snapshot(tmp_path) -> None:
    db_path = str(tmp_path / "kenny.sqlite")
    telemetry = TelemetryStore(db_path=db_path)
    await telemetry.connect()
    await telemetry.insert("pc-a", "2026-01-01T00:00:00Z", {"cpu": {"load": 1}})
    await telemetry.close()

    target_store = BackupTargetStore(db_path)
    await target_store.connect()
    try:
        mgr = BackupManager(db_path, target_store)
        result = await mgr.create("manual")

        assert result["integrity"] == "ok"
        assert result["trigger"] == "manual"
        assert result["size"] > 0
        assert result["push_status"] == [{"target": "local", "ok": True}]

        backup_path = os.path.join(mgr.backup_dir, result["name"])
        assert os.path.exists(backup_path)

        # Reopening the backup file directly must show the same row.
        reopened = TelemetryStore(db_path=backup_path)
        await reopened.connect()
        try:
            latest = await reopened.latest("pc-a")
            assert latest is not None
            assert latest["snapshot"] == {"cpu": {"load": 1}}
        finally:
            await reopened.close()
    finally:
        await target_store.close()


# --- BackupManager.list / prune -----------------------------------------------


async def test_backup_manager_list_newest_first_and_prune_retention(tmp_path) -> None:
    db_path = str(tmp_path / "kenny.sqlite")
    telemetry = TelemetryStore(db_path=db_path)
    await telemetry.connect()
    await telemetry.close()

    target_store = BackupTargetStore(db_path)
    await target_store.connect()
    try:
        mgr = BackupManager(db_path, target_store)
        names = []
        for _ in range(3):
            result = await mgr.create("manual")
            names.append(result["name"])
            # Ensure distinct created_at ordering even if the clock resolution
            # collapses two runs into the same second.
            backups = await mgr.list()
            assert backups[0]["name"] == result["name"]

        # prune() with a low retention keeps exactly N.
        await mgr.prune(1)
        backups = await mgr.list()
        assert len(backups) == 1
    finally:
        await target_store.close()


# --- verify --------------------------------------------------------------------


async def test_verify_detects_corruption(tmp_path) -> None:
    db_path = str(tmp_path / "kenny.sqlite")
    telemetry = TelemetryStore(db_path=db_path)
    await telemetry.connect()
    await telemetry.close()

    target_store = BackupTargetStore(db_path)
    await target_store.connect()
    try:
        mgr = BackupManager(db_path, target_store)
        result = await mgr.create("manual")

        ok = await mgr.verify(result["name"])
        assert ok["integrity"] == "ok"

        backup_path = os.path.join(mgr.backup_dir, result["name"])
        with open(backup_path, "r+b") as fh:
            fh.truncate(64)  # truncate mid-header: guaranteed corruption

        corrupted = await mgr.verify(result["name"])
        assert corrupted["integrity"] != "ok"
    finally:
        await target_store.close()


# --- restore roundtrip -----------------------------------------------------


async def test_restore_roundtrip(tmp_path) -> None:
    db_path = str(tmp_path / "kenny.sqlite")

    telemetry = TelemetryStore(db_path=db_path)
    await telemetry.connect()
    await telemetry.insert("row-a", "2026-01-01T00:00:00Z", {"marker": "A"})
    await telemetry.close()

    target_store = BackupTargetStore(db_path)
    await target_store.connect()
    mgr = BackupManager(db_path, target_store)
    result = await mgr.create("manual")
    await target_store.close()

    # Mutate the live DB after the backup: add row B, remove row A.
    telemetry2 = TelemetryStore(db_path=db_path)
    await telemetry2.connect()
    await telemetry2.insert("row-b", "2026-01-02T00:00:00Z", {"marker": "B"})
    async with telemetry2._conn.execute(
        "DELETE FROM snapshots WHERE agent_id = ?", ("row-a",)
    ):
        pass
    await telemetry2._conn.commit()
    assert await telemetry2.latest("row-a") is None
    assert await telemetry2.latest("row-b") is not None
    await telemetry2.close()

    # Stage the restore (stores must all be closed beforehand, as in production).
    target_store2 = BackupTargetStore(db_path)
    await target_store2.connect()
    mgr2 = BackupManager(db_path, target_store2)
    await mgr2.stage_restore(result["name"])
    await target_store2.close()

    pending_path = f"{db_path}.restore-pending"
    marker_path = f"{db_path}.restore-marker"
    assert os.path.exists(pending_path)
    assert os.path.exists(marker_path)

    applied = apply_pending_restore(db_path)
    assert applied is not None

    assert not os.path.exists(pending_path)
    assert not os.path.exists(marker_path)

    restored = TelemetryStore(db_path=db_path)
    await restored.connect()
    try:
        assert await restored.latest("row-a") is not None
        assert await restored.latest("row-b") is None
    finally:
        await restored.close()


async def test_apply_pending_restore_noop_when_no_marker(tmp_path) -> None:
    db_path = str(tmp_path / "kenny.sqlite")
    assert apply_pending_restore(db_path) is None


async def test_apply_pending_restore_handles_stale_marker(tmp_path) -> None:
    db_path = str(tmp_path / "kenny.sqlite")
    Path(db_path).write_text("original", encoding="utf-8")
    marker_path = f"{db_path}.restore-marker"
    Path(marker_path).write_text(f"{db_path}.restore-pending\nsome-name\n", encoding="utf-8")

    applied = apply_pending_restore(db_path)
    assert applied is None
    assert not os.path.exists(marker_path)
    # The stale marker is removed but the live DB is left untouched.
    assert Path(db_path).read_text(encoding="utf-8") == "original"


# --- BackupTargetStore ---------------------------------------------------------


async def test_backup_target_store_roundtrip(tmp_path) -> None:
    store = BackupTargetStore(str(tmp_path / "kenny.sqlite"))
    await store.connect()
    try:
        target_id = await store.add(
            kind="ftp", label="Offsite FTP", config={"host": "example.test", "password": "s3cr3t"}
        )
        assert await store.get(target_id) is not None

        rows = await store.list()
        assert len(rows) == 1
        assert rows[0]["kind"] == "ftp"
        assert rows[0]["label"] == "Offsite FTP"
        assert rows[0]["config"]["host"] == "example.test"
        assert rows[0]["enabled"] is True

        assert await store.update(target_id, label="Renamed") is True
        row = await store.get(target_id)
        assert row["label"] == "Renamed"
        assert row["config"]["host"] == "example.test"  # unchanged

        assert await store.update(target_id, config={"host": "new.test"}) is True
        row = await store.get(target_id)
        assert row["config"] == {"host": "new.test"}

        assert await store.set_enabled(target_id, False) is True
        row = await store.get(target_id)
        assert row["enabled"] is False

        assert await store.delete(target_id) is True
        assert await store.get(target_id) is None
        assert await store.delete(target_id) is False
        assert await store.update("nope", label="x") is False
        assert await store.set_enabled("nope", True) is False
    finally:
        await store.close()


# --- fan-out semantics -----------------------------------------------------


async def test_fanout_semantics_local_succeeds_remote_fails(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "kenny.sqlite")
    telemetry = TelemetryStore(db_path=db_path)
    await telemetry.connect()
    await telemetry.close()

    target_store = BackupTargetStore(db_path)
    await target_store.connect()
    try:
        await target_store.add(kind="ftp", label="broken", config={"host": "127.0.0.1"})
        mgr = BackupManager(db_path, target_store)

        # Patch build_destination (used by BackupManager._active_targets) so
        # the ftp row resolves to a fake that always fails on store().
        import kenny_server.backup as backup_module

        monkeypatch.setattr(
            backup_module, "build_destination", lambda kind, config: FailingDestination()
        )

        result = await mgr.create("manual")
        statuses = {s["target"]: s for s in result["push_status"]}
        assert statuses["local"]["ok"] is True
        remote_status = [s for s in result["push_status"] if s["target"] != "local"][0]
        assert remote_status["ok"] is False
        assert "simulated remote failure" in remote_status["error"]

        # local backup still exists despite the remote failure.
        backups = await mgr.list()
        assert any(b["name"] == result["name"] for b in backups)
    finally:
        await target_store.close()


# --- HttpDestination (mock transport) ------------------------------------------


async def test_http_destination_roundtrip_via_mock_transport(tmp_path) -> None:
    storage: dict[str, dict[str, Any]] = {}
    transport = httpx.MockTransport(_make_http_handler(storage))

    dest = HttpDestination(
        {"url": "https://backup.example.test", "token": "tkn", "transport": transport}
    )

    src = tmp_path / "src.sqlite"
    src.write_bytes(b"payload-bytes")
    name = "kenny-backup-20260101T000000Z.sqlite"
    meta = {"name": name, "created_at": "2026-01-01T00:00:00Z", "size": 13}

    await dest.store(str(src), name, meta)

    listed = await dest.list()
    assert listed == [meta]

    out = tmp_path / "out.sqlite"
    await dest.retrieve(name, str(out))
    assert out.read_bytes() == src.read_bytes()

    result = await dest.test()
    assert result["ok"] is True

    assert await dest.delete(name) is True
    assert await dest.list() == []


async def test_http_destination_wraps_transport_errors() -> None:
    def raising_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(raising_handler)
    dest = HttpDestination({"url": "https://unreachable.example.test", "transport": transport})

    with pytest.raises(RuntimeError):
        await dest.list()

    result = await dest.test()
    assert result["ok"] is False


# --- build_destination factory + negative-path connectivity ----------------


def test_build_destination_dispatches_by_kind(tmp_path) -> None:
    assert isinstance(build_destination("local", {"backup_dir": str(tmp_path)}), LocalDestination)
    assert isinstance(build_destination("http", {"url": "https://x.test"}), HttpDestination)
    assert isinstance(
        build_destination("scp", {"host": "127.0.0.1", "username": "u"}), ScpDestination
    )
    assert isinstance(build_destination("ftp", {"host": "127.0.0.1"}), FtpDestination)
    with pytest.raises(ValueError):
        build_destination("carrier-pigeon", {})


async def test_scp_destination_test_reports_failure_without_raising() -> None:
    dest = ScpDestination({"host": "127.0.0.1", "port": 1, "username": "nobody"})
    result = await dest.test()
    assert result == {"ok": False, "message": result["message"]}
    assert result["ok"] is False


async def test_ftp_destination_test_reports_failure_without_raising() -> None:
    dest = FtpDestination({"host": "127.0.0.1", "port": 1, "use_tls": False})
    result = await dest.test()
    assert result["ok"] is False

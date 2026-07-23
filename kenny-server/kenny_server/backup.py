"""Server-DB backup/restore engine: consistent snapshots, fan-out, restore staging.

Context (see the backup/restore ADR): kenny persists everything in one WAL-mode
SQLite file. Syncing that *live* file (e.g. with Syncthing) causes lock
contention and can replicate an inconsistent copy. :class:`BackupManager`
produces a consistent snapshot via ``VACUUM INTO`` into a local "backups"
directory (which *is* safe to sync — it only ever contains finished, static
files) and optionally fans it out to operator-configured remote destinations
(see :mod:`kenny_server.backup_targets`).

Restore is "apply on next boot": the live file has ~11 open aiosqlite
connections (one per store) so it cannot be swapped safely while the process
is running. :meth:`BackupManager.stage_restore` verifies and stages a chosen
backup next to the DB and writes a marker; :func:`apply_pending_restore` is a
free, synchronous function called at the very start of the app lifespan —
*before* any store connects — that swaps the staged file into place.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .backup_targets import BackupDestination, LocalDestination, build_destination
from .store import BackupTargetStore, _configure_connection

logger = logging.getLogger("kenny.backup")

DEFAULT_RETENTION = 7

# Backup file names are generated exclusively by ``create()`` in this exact
# shape; every public method taking a ``name`` validates against this pattern
# as a defense against path traversal once this is wired to HTTP routes.
_NAME_RE = re.compile(r"^kenny-backup-\d{8}T\d{6}Z\.sqlite$")


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid backup name: {name!r}")


def _utc_basic_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: str) -> str:
    """Stream-hash a (potentially large) file in fixed-size chunks."""

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _quick_check(path: str) -> str:
    """Run ``PRAGMA quick_check`` on a standalone sqlite file (read-only).

    ``quick_check`` (not ``integrity_check``) is the fast structural check —
    the right choice here given the DB can be hundreds of MB.
    """

    uri = f"file:{Path(path).as_posix()}?mode=ro"
    try:
        conn = await aiosqlite.connect(uri, uri=True)
    except aiosqlite.Error as exc:
        return f"error: {exc}"
    try:
        async with conn.execute("PRAGMA quick_check") as cur:
            rows = await cur.fetchall()
        results = [r[0] for r in rows]
        return "ok" if results == ["ok"] else "; ".join(str(r) for r in results)
    except aiosqlite.Error as exc:
        # A severely corrupted/truncated file can raise instead of returning a
        # "quick_check" row (e.g. "database disk image is malformed") — treat
        # that as a definitive non-"ok" integrity result, not a hard failure.
        return f"error: {exc}"
    finally:
        await conn.close()


class BackupManager:
    """Creates, lists, verifies, prunes, and stages restores of DB snapshots."""

    DEFAULT_RETENTION = DEFAULT_RETENTION

    def __init__(
        self,
        db_path: str,
        target_store: BackupTargetStore,
        *,
        backup_dir: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.target_store = target_store
        self.backup_dir = backup_dir or os.path.join(
            os.path.dirname(os.path.abspath(db_path)) or ".", "backups"
        )
        self._local = LocalDestination(self.backup_dir)

    async def _active_targets(self) -> list[tuple[str, BackupDestination]]:
        """Local (always active) + every enabled configured target.

        A row whose ``kind``/``config`` fails to build a destination is
        logged and skipped rather than raising, so one bad remote config
        never blocks listing/backup for local or the other targets.
        """

        targets: list[tuple[str, BackupDestination]] = [("local", self._local)]
        for row in await self.target_store.list():
            if not row["enabled"]:
                continue
            try:
                dest = build_destination(row["kind"], row["config"])
            except Exception:  # noqa: BLE001 - one bad target must not break the rest
                logger.exception("failed to build backup destination %s (%s)", row["id"], row["kind"])
                continue
            targets.append((row["id"], dest))
        return targets

    async def _destination(self, target_id: str) -> BackupDestination | None:
        if target_id == "local":
            return self._local
        row = await self.target_store.get(target_id)
        if row is None or not row["enabled"]:
            return None
        try:
            return build_destination(row["kind"], row["config"])
        except Exception:  # noqa: BLE001 - surfaced as "target not found" to callers
            logger.exception("failed to build backup destination %s (%s)", row["id"], row["kind"])
            return None

    async def create(self, trigger: str) -> dict[str, Any]:
        """Create a fresh consistent snapshot and push it to every active target."""

        name = f"kenny-backup-{_utc_basic_now()}.sqlite"
        os.makedirs(self.backup_dir, exist_ok=True)
        fd, staging_path = tempfile.mkstemp(dir=self.backup_dir, suffix=".tmp")
        os.close(fd)
        os.remove(staging_path)  # VACUUM INTO refuses to write to an existing file
        try:
            conn = await aiosqlite.connect(self.db_path)
            try:
                await _configure_connection(conn)
                await conn.execute("VACUUM INTO ?", (staging_path,))
            finally:
                await conn.close()

            size = os.path.getsize(staging_path)
            sha256 = await asyncio.to_thread(_sha256_file, staging_path)
            integrity = await _quick_check(staging_path)
            created_at = datetime.now(timezone.utc).isoformat()
            meta: dict[str, Any] = {
                "name": name,
                "created_at": created_at,
                "size": size,
                "sha256": sha256,
                "integrity": integrity,
                "trigger": trigger,
            }

            push_status: list[dict[str, Any]] = []
            targets = await self._active_targets()
            for target_id, dest in targets:
                if target_id == "local":
                    try:
                        await dest.store(staging_path, name, meta)
                    except Exception as exc:  # noqa: BLE001 - re-raised with context below
                        raise RuntimeError(f"local backup store failed: {exc}") from exc
                    push_status.append({"target": "local", "ok": True})
                    continue
                try:
                    await dest.store(staging_path, name, meta)
                    push_status.append({"target": target_id, "ok": True})
                except Exception as exc:  # noqa: BLE001 - remote push is best-effort
                    logger.exception("backup push to target %s failed", target_id)
                    push_status.append({"target": target_id, "ok": False, "error": str(exc)})
        finally:
            if os.path.exists(staging_path):
                os.remove(staging_path)

        await self.prune()
        return {**meta, "push_status": push_status}

    async def prune(self, retention: int | None = None) -> dict[str, int]:
        """Prune every active target down to ``retention`` newest backups."""

        retention = retention if retention is not None else self.DEFAULT_RETENTION
        results: dict[str, int] = {}
        for target_id, dest in await self._active_targets():
            try:
                results[target_id] = await dest.prune(retention)
            except Exception:  # noqa: BLE001 - one bad target must not break pruning
                logger.exception("prune failed for target %s", target_id)
                results[target_id] = 0
        return results

    async def list(self) -> list[dict[str, Any]]:
        """Aggregate ``list()`` across every active target, merged by backup name."""

        merged: dict[str, dict[str, Any]] = {}
        for target_id, dest in await self._active_targets():
            try:
                metas = await dest.list()
            except Exception:  # noqa: BLE001 - one bad target must not break listing
                logger.exception("list failed for target %s", target_id)
                continue
            for meta in metas:
                name = meta.get("name")
                if not name:
                    continue
                entry = merged.setdefault(name, {"targets": []})
                # Union of fields: fill in anything not yet set from a fuller record.
                for key, value in meta.items():
                    entry.setdefault(key, value)
                entry["targets"].append({"target": target_id, **meta})
        return sorted(merged.values(), key=lambda m: m.get("created_at") or "", reverse=True)

    async def verify(self, name: str, source: str = "local") -> dict[str, Any]:
        """Retrieve ``name`` from ``source`` into a temp file and quick_check it."""

        _validate_name(name)
        dest = await self._destination(source)
        if dest is None:
            raise ValueError(f"unknown or disabled backup target: {source!r}")

        fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            await dest.retrieve(name, tmp_path)
            integrity = await _quick_check(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return {"name": name, "source": source, "integrity": integrity}

    async def retrieve(self, name: str, source: str = "local") -> str:
        """Retrieve ``name`` from ``source`` into a fresh temp file; caller deletes it.

        A minimal extension of :meth:`verify`'s destination-resolution for
        callers (the download route) that need the actual bytes rather than
        just an integrity verdict.
        """

        _validate_name(name)
        dest = await self._destination(source)
        if dest is None:
            raise ValueError(f"unknown or disabled backup target: {source!r}")

        fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        await dest.retrieve(name, tmp_path)
        return tmp_path

    async def delete(self, name: str, target: str | None = None) -> dict[str, bool]:
        """Delete ``name`` from one target, or from every active target."""

        _validate_name(name)
        results: dict[str, bool] = {}
        if target is not None:
            dest = await self._destination(target)
            if dest is None:
                raise ValueError(f"unknown or disabled backup target: {target!r}")
            results[target] = await dest.delete(name)
            return results
        for target_id, dest in await self._active_targets():
            try:
                results[target_id] = await dest.delete(name)
            except Exception:  # noqa: BLE001 - one bad target must not break the rest
                logger.exception("delete failed for target %s", target_id)
                results[target_id] = False
        return results

    async def stage_restore(self, name: str, source: str = "local") -> None:
        """Retrieve + verify ``name`` from ``source``, then stage it for restore-on-boot."""

        _validate_name(name)
        dest = await self._destination(source)
        if dest is None:
            raise ValueError(f"unknown or disabled backup target: {source!r}")

        fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        pending_path = f"{self.db_path}.restore-pending"
        marker_path = f"{self.db_path}.restore-marker"
        try:
            await dest.retrieve(name, tmp_path)
            integrity = await _quick_check(tmp_path)
            if integrity != "ok":
                raise ValueError("backup failed integrity check, refusing to stage restore")

            def _stage() -> None:
                import shutil

                shutil.copy2(tmp_path, pending_path)
                with open(marker_path, "w", encoding="utf-8") as fh:
                    fh.write(pending_path + "\n")
                    fh.write(name + "\n")

            await asyncio.to_thread(_stage)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


def apply_pending_restore(db_path: str) -> str | None:
    """Apply a staged restore if one is pending. Synchronous; call before any store connects.

    Returns the applied backup's name (best-effort identifier for an audit
    log) if a restore was applied, else ``None``. Never raises over a missing
    or stale pending file — it logs a warning and cleans up instead, so a
    corrupted restore marker cannot block boot.
    """

    marker_path = f"{db_path}.restore-marker"
    if not os.path.exists(marker_path):
        return None

    logger_ = logging.getLogger("kenny.backup")
    try:
        with open(marker_path, encoding="utf-8") as fh:
            lines = [line.rstrip("\n") for line in fh.readlines()]
    except OSError as exc:
        logger_.warning("failed to read restore marker %s: %s", marker_path, exc)
        Path(marker_path).unlink(missing_ok=True)
        return None

    pending_path = lines[0] if lines else ""
    applied_name = lines[1] if len(lines) > 1 else Path(pending_path).name

    if not pending_path or not os.path.exists(pending_path):
        logger_.warning(
            "restore marker %s points at a missing pending file %r; removing marker",
            marker_path,
            pending_path,
        )
        Path(marker_path).unlink(missing_ok=True)
        return None

    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    os.replace(pending_path, db_path)
    Path(marker_path).unlink(missing_ok=True)
    logger_.info("applied pending restore: %s", applied_name)
    return applied_name

"""Pluggable backup destinations: local disk + remote push targets.

:class:`BackupDestination` is the uniform interface :mod:`kenny_server.backup`
fans a snapshot out to — local storage is always active; HTTP/SCP(SFTP)/FTP(S)
targets are operator-configured (see ``BackupTargetStore`` in ``store.py``).
Every implementation:

* stores a snapshot alongside a ``<name>.meta.json`` sidecar (HTTP posts the
  metadata as part of the same request instead), so listing never needs to
  reopen the sqlite file itself;
* never lets a raw transport exception escape ``store``/``list``/``retrieve``/
  ``delete``/``prune`` — those are wrapped into a clear ``RuntimeError`` so
  :class:`~kenny_server.backup.BackupManager` can catch and record a per-target
  failure without the whole backup run (or the other targets) dying;
* implements ``test()`` as a never-raising connectivity/write probe, returning
  ``{"ok": bool, "message": str}`` — this is what powers a "Test connection"
  button in the dashboard (Phase B).

See ADR (backup/restore, Phase A/B/C) for the trust-boundary rationale: the
server now initiates outbound connections and carries full DB copies to
operator-configured destinations; destination credentials live in the DB like
other secrets (``AgentTokenStore``, ``KeyStore``, ``OAuthStore``).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import posixpath
import shutil
from abc import ABC, abstractmethod
from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path
from typing import Any

import asyncssh
import httpx

logger = logging.getLogger("kenny.backup.targets")


class BackupDestination(ABC):
    """Uniform interface every backup target (local or remote) implements."""

    @abstractmethod
    async def store(self, local_path: str, name: str, meta: dict[str, Any]) -> None:
        """Upload/place ``local_path`` at this destination as ``name`` with ``meta``."""

    @abstractmethod
    async def list(self) -> list[dict[str, Any]]:
        """Enumerate backups held at this destination (each a meta dict)."""

    @abstractmethod
    async def retrieve(self, name: str, dest_local_path: str) -> None:
        """Pull backup ``name`` back to ``dest_local_path`` on local disk."""

    @abstractmethod
    async def delete(self, name: str) -> bool:
        """Delete backup ``name``. Returns True if something was removed."""

    async def prune(self, retention: int) -> int:
        """Keep the ``retention`` newest backups (by ``created_at``), delete the rest.

        Default implementation built on ``list``/``delete`` — this is what the
        plan calls "client-side" pruning and is sufficient for every backend
        here (none assumes a server-side prune endpoint).
        """

        metas = await self.list()
        ordered = sorted(metas, key=lambda m: m.get("created_at") or "", reverse=True)
        excess = ordered[retention:] if retention >= 0 else []
        deleted = 0
        for meta in excess:
            name = meta.get("name")
            if not name:
                continue
            try:
                if await self.delete(name):
                    deleted += 1
            except Exception:  # noqa: BLE001 - one bad delete must not abort pruning
                logger.exception("prune: failed to delete %s", name)
        return deleted

    @abstractmethod
    async def test(self) -> dict[str, Any]:
        """Connectivity/write check. Never raises; returns {"ok", "message"}."""


# --- local ----------------------------------------------------------------


class LocalDestination(BackupDestination):
    """Plain filesystem destination. Always active (holds the fast-restore copy)."""

    def __init__(self, backup_dir: str) -> None:
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.backup_dir / name

    def _meta_path(self, name: str) -> Path:
        return self.backup_dir / f"{name}.meta.json"

    async def store(self, local_path: str, name: str, meta: dict[str, Any]) -> None:
        def _do() -> None:
            shutil.copy2(local_path, self._path(name))
            self._meta_path(name).write_text(json.dumps(meta), encoding="utf-8")

        await asyncio.to_thread(_do)

    async def list(self) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for entry in os.listdir(self.backup_dir):
                if not entry.endswith(".meta.json"):
                    continue
                try:
                    meta = json.loads((self.backup_dir / entry).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                out.append(meta)
            return out

        return await asyncio.to_thread(_do)

    async def retrieve(self, name: str, dest_local_path: str) -> None:
        def _do() -> None:
            shutil.copy2(self._path(name), dest_local_path)

        await asyncio.to_thread(_do)

    async def delete(self, name: str) -> bool:
        def _do() -> bool:
            deleted = False
            path = self._path(name)
            if path.exists():
                os.remove(path)
                deleted = True
            meta_path = self._meta_path(name)
            if meta_path.exists():
                os.remove(meta_path)
            return deleted

        return await asyncio.to_thread(_do)

    async def test(self) -> dict[str, Any]:
        try:
            probe = self.backup_dir / ".kenny_backup_write_test"

            def _do() -> None:
                probe.write_text("ok", encoding="utf-8")
                os.remove(probe)

            await asyncio.to_thread(_do)
            return {"ok": True, "message": f"{self.backup_dir} is writable"}
        except OSError as exc:
            return {"ok": False, "message": str(exc)}


# --- HTTP -------------------------------------------------------------------


class HttpDestination(BackupDestination):
    """Push snapshots to an HTTP API via ``httpx``."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.url = str(config["url"]).rstrip("/")
        self.token = config.get("token") or None
        self.verify_tls = bool(config.get("verify_tls", True))
        self.timeout = float(config.get("timeout", 30))
        # Test-only hook: an httpx.BaseTransport (e.g. httpx.MockTransport) to
        # avoid real network calls. Never set from persisted target config.
        self._transport = config.get("transport")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.url,
            verify=self.verify_tls,
            timeout=self.timeout,
            headers=self._headers(),
            transport=self._transport,
        )

    async def store(self, local_path: str, name: str, meta: dict[str, Any]) -> None:
        try:
            async with self._client() as client, _open_binary(local_path) as fh:
                resp = await client.post(
                    "/",
                    files={"file": (name, fh, "application/octet-stream")},
                    data={"meta": json.dumps(meta)},
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP destination store failed: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"HTTP destination could not read {local_path}: {exc}") from exc

    async def list(self) -> list[dict[str, Any]]:
        try:
            async with self._client() as client:
                resp = await client.get("/")
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP destination list failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"HTTP destination returned invalid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise RuntimeError("HTTP destination list did not return a JSON array")
        return data

    async def retrieve(self, name: str, dest_local_path: str) -> None:
        try:
            async with self._client() as client:
                async with client.stream("GET", f"/{name}") as resp:
                    resp.raise_for_status()
                    with open(dest_local_path, "wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            fh.write(chunk)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP destination retrieve failed: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(
                f"HTTP destination could not write {dest_local_path}: {exc}"
            ) from exc

    async def delete(self, name: str) -> bool:
        try:
            async with self._client() as client:
                resp = await client.delete(f"/{name}")
                if resp.status_code == 404:
                    return False
                resp.raise_for_status()
                return True
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP destination delete failed: {exc}") from exc

    async def test(self) -> dict[str, Any]:
        try:
            async with self._client() as client:
                resp = await client.get("/")
                resp.raise_for_status()
            return {"ok": True, "message": f"connected to {self.url}"}
        except Exception as exc:  # noqa: BLE001 - connectivity probe must never raise
            return {"ok": False, "message": str(exc)}


class _open_binary:
    """Tiny sync-file async-context-manager helper so ``store`` can ``async with`` it."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._fh: Any = None

    async def __aenter__(self) -> Any:
        self._fh = open(self._path, "rb")  # noqa: SIM115 - closed in __aexit__
        return self._fh

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._fh is not None:
            self._fh.close()


# --- SCP/SFTP -----------------------------------------------------------------


class ScpDestination(BackupDestination):
    """SFTP destination via ``asyncssh``.

    # TODO(security): ``known_hosts=None`` disables host-key verification (no
    # pinning / trust-on-first-use). This is a deliberate v1 simplification —
    # revisit before treating this as anything more than best-effort offsite
    # copies on a trusted network. Will be documented as a known limitation in
    # the backup/restore ADR.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.host = config["host"]
        self.port = int(config.get("port", 22))
        self.username = config.get("username")
        self.password = config.get("password") or None
        self.private_key = config.get("private_key") or None
        self.remote_dir = config.get("remote_dir", "").rstrip("/") or "."

    def _remote_path(self, name: str) -> str:
        return posixpath.join(self.remote_dir, name)

    def _meta_remote_path(self, name: str) -> str:
        return self._remote_path(f"{name}.meta.json")

    async def _connect(self) -> asyncssh.SSHClientConnection:
        client_keys = None
        if self.private_key:
            client_keys = [asyncssh.import_private_key(self.private_key)]
        return await asyncssh.connect(
            self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            client_keys=client_keys,
            known_hosts=None,  # TODO(security): see class docstring
        )

    async def store(self, local_path: str, name: str, meta: dict[str, Any]) -> None:
        try:
            async with await self._connect() as conn, conn.start_sftp_client() as sftp:
                await sftp.put(local_path, self._remote_path(name))
                async with sftp.open(self._meta_remote_path(name), "w") as fh:
                    await fh.write(json.dumps(meta))
        except (asyncssh.Error, OSError) as exc:
            raise RuntimeError(f"SCP destination store failed: {exc}") from exc

    async def list(self) -> list[dict[str, Any]]:
        try:
            async with await self._connect() as conn, conn.start_sftp_client() as sftp:
                entries = await sftp.listdir(self.remote_dir)
                out: list[dict[str, Any]] = []
                for entry in entries:
                    if not entry.endswith(".meta.json"):
                        continue
                    try:
                        async with sftp.open(
                            posixpath.join(self.remote_dir, entry), "r"
                        ) as fh:
                            raw = await fh.read()
                        out.append(json.loads(raw))
                    except (asyncssh.Error, OSError, ValueError):
                        continue
                return out
        except (asyncssh.Error, OSError) as exc:
            raise RuntimeError(f"SCP destination list failed: {exc}") from exc

    async def retrieve(self, name: str, dest_local_path: str) -> None:
        try:
            async with await self._connect() as conn, conn.start_sftp_client() as sftp:
                await sftp.get(self._remote_path(name), dest_local_path)
        except (asyncssh.Error, OSError) as exc:
            raise RuntimeError(f"SCP destination retrieve failed: {exc}") from exc

    async def delete(self, name: str) -> bool:
        try:
            async with await self._connect() as conn, conn.start_sftp_client() as sftp:
                deleted = False
                try:
                    await sftp.remove(self._remote_path(name))
                    deleted = True
                except asyncssh.SFTPError:
                    pass
                try:
                    await sftp.remove(self._meta_remote_path(name))
                except asyncssh.SFTPError:
                    pass
                return deleted
        except (asyncssh.Error, OSError) as exc:
            raise RuntimeError(f"SCP destination delete failed: {exc}") from exc

    async def test(self) -> dict[str, Any]:
        try:
            async with await self._connect() as conn, conn.start_sftp_client() as sftp:
                await sftp.listdir(self.remote_dir)
            return {"ok": True, "message": f"connected to {self.host}:{self.port}"}
        except Exception as exc:  # noqa: BLE001 - connectivity probe must never raise
            return {"ok": False, "message": str(exc)}


# --- FTP/FTPS -----------------------------------------------------------------


class FtpDestination(BackupDestination):
    """FTP/FTPS destination via stdlib ``ftplib`` (sync, wrapped in ``to_thread``)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.host = config["host"]
        self.port = int(config.get("port", 21))
        self.username = config.get("username")
        self.password = config.get("password")
        self.remote_dir = config.get("remote_dir", "").rstrip("/") or "."
        self.use_tls = bool(config.get("use_tls", True))

    def _connect_sync(self) -> FTP:
        ftp_cls = FTP_TLS if self.use_tls else FTP
        ftp = ftp_cls()
        ftp.connect(self.host, self.port, timeout=30)
        ftp.login(self.username or "", self.password or "")
        if self.use_tls:
            ftp.prot_p()
        if self.remote_dir not in (".", ""):
            ftp.cwd(self.remote_dir)
        return ftp

    def _meta_name(self, name: str) -> str:
        return f"{name}.meta.json"

    def _store_sync(self, local_path: str, name: str, meta: dict[str, Any]) -> None:
        ftp = self._connect_sync()
        try:
            with open(local_path, "rb") as fh:
                ftp.storbinary(f"STOR {name}", fh)
            payload = io.BytesIO(json.dumps(meta).encode("utf-8"))
            ftp.storbinary(f"STOR {self._meta_name(name)}", payload)
        finally:
            ftp.quit()

    def _list_sync(self) -> list[dict[str, Any]]:
        ftp = self._connect_sync()
        try:
            names = ftp.nlst()
            out: list[dict[str, Any]] = []
            for entry in names:
                base = entry.rsplit("/", 1)[-1]
                if not base.endswith(".meta.json"):
                    continue
                buf = io.BytesIO()
                try:
                    ftp.retrbinary(f"RETR {base}", buf.write)
                    out.append(json.loads(buf.getvalue().decode("utf-8")))
                except (error_perm, ValueError):
                    continue
            return out
        finally:
            ftp.quit()

    def _retrieve_sync(self, name: str, dest_local_path: str) -> None:
        ftp = self._connect_sync()
        try:
            with open(dest_local_path, "wb") as fh:
                ftp.retrbinary(f"RETR {name}", fh.write)
        finally:
            ftp.quit()

    def _delete_sync(self, name: str) -> bool:
        ftp = self._connect_sync()
        try:
            deleted = False
            try:
                ftp.delete(name)
                deleted = True
            except error_perm:
                pass
            try:
                ftp.delete(self._meta_name(name))
            except error_perm:
                pass
            return deleted
        finally:
            ftp.quit()

    def _test_sync(self) -> dict[str, Any]:
        ftp = self._connect_sync()
        try:
            ftp.nlst()
            return {"ok": True, "message": f"connected to {self.host}:{self.port}"}
        finally:
            ftp.quit()

    async def store(self, local_path: str, name: str, meta: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._store_sync, local_path, name, meta)
        except (OSError, error_perm) as exc:
            raise RuntimeError(f"FTP destination store failed: {exc}") from exc

    async def list(self) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._list_sync)
        except (OSError, error_perm) as exc:
            raise RuntimeError(f"FTP destination list failed: {exc}") from exc

    async def retrieve(self, name: str, dest_local_path: str) -> None:
        try:
            await asyncio.to_thread(self._retrieve_sync, name, dest_local_path)
        except (OSError, error_perm) as exc:
            raise RuntimeError(f"FTP destination retrieve failed: {exc}") from exc

    async def delete(self, name: str) -> bool:
        try:
            return await asyncio.to_thread(self._delete_sync, name)
        except (OSError, error_perm) as exc:
            raise RuntimeError(f"FTP destination delete failed: {exc}") from exc

    async def test(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._test_sync)
        except Exception as exc:  # noqa: BLE001 - connectivity probe must never raise
            return {"ok": False, "message": str(exc)}


# --- factory --------------------------------------------------------------

_KINDS: dict[str, type[BackupDestination]] = {
    "local": LocalDestination,
    "http": HttpDestination,
    "scp": ScpDestination,
    "ftp": FtpDestination,
}


def build_destination(kind: str, config: dict[str, Any]) -> BackupDestination:
    """Construct a :class:`BackupDestination` for ``kind`` (one of local/http/scp/ftp).

    ``local`` is normally constructed directly by :class:`~kenny_server.backup.BackupManager`
    (it needs a directory, not a generic ``config`` dict) but is dispatchable here too
    for symmetry/testing: pass ``{"backup_dir": ...}``.
    """

    if kind == "local":
        return LocalDestination(config["backup_dir"])
    cls = _KINDS.get(kind)
    if cls is None:
        raise ValueError(f"unknown backup destination kind: {kind!r}")
    return cls(config)

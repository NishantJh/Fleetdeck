"""Backups of the data folder: the config, the keys and the job history, in one encrypted file.

A backup holds every secret the dashboard has (its SSH key, the Proxmox token, tunnel keys), so it
is only ever written encrypted: AES-256-GCM with a key derived from a passphrase the admin chooses
(scrypt). Nothing is kept on the server; the browser downloads it.

File layout: ``MAGIC | scrypt n, r, p (3 x uint32) | salt (16) | nonce (12) | ciphertext``, where
the plaintext is a gzipped tar of ``manifest.json``, ``config.yaml``, ``keys/...`` and ``state.db``
(a consistent SQLite snapshot). The header is authenticated as associated data.

Restoring writes the files back over the current ones (after saving the current ones as an
encrypted backup next to them) and restarts the app with them.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import sqlite3
import struct
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from . import onboarding as ob
from .config import AppConfig, validate_raw
from .envvars import env
from .setup_routes import refuse_while_busy
from .store import Store

log = logging.getLogger(__name__)

MAGIC = b"HLMBACKUP1\n"
_HEADER = struct.Struct(">III16s12s")
_SCRYPT = (2**15, 8, 1)
# Settings a backup may have been written with: old files stay readable if the default goes up.
_KNOWN_SCRYPT = {(2**15, 8, 1), (2**17, 8, 1)}
KEEP_SAFETY_COPIES = 5
MIN_PASSPHRASE = 12
MAX_BACKUP_BYTES = 100 * 2**20  # uploaded file, and everything inside it once unpacked
SUFFIX = ".hlmbackup"
VERSION = 1


class BackupError(ValueError):
    """The file isn't a backup this app can read, or the passphrase is wrong."""


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # scrypt needs 128 * n * r bytes; allow twice that.
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r, dklen=32)


def encrypt(plain: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + _HEADER.pack(*_SCRYPT, salt, nonce)
    return header + AESGCM(_key(passphrase, salt, *_SCRYPT)).encrypt(nonce, plain, header)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    size = len(MAGIC) + _HEADER.size
    if len(blob) < size or not blob.startswith(MAGIC):
        raise BackupError("this isn't a Fleetdeck backup file")
    n, r, p, salt, nonce = _HEADER.unpack(blob[len(MAGIC) : size])
    # Only settings we know; anything else is a newer version's, or a damaged or crafted file (a huge
    # n would eat memory).
    if (n, r, p) not in _KNOWN_SCRYPT:
        raise BackupError("this backup uses encryption settings this version doesn't know (made by a newer version?)")
    try:
        return AESGCM(_key(passphrase, salt, n, r, p)).decrypt(nonce, blob[size:], blob[:size])
    except InvalidTag:
        raise BackupError("wrong passphrase, or the file is damaged") from None


def _snapshot_db(store: Store | None) -> bytes | None:
    """A consistent copy of the live database (SQLite's online backup, so a write mid-copy is safe)."""
    if store is None:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        try:
            store.db.backup(dst)
        finally:
            dst.close()
        return Path(tmp).read_bytes()
    finally:
        os.unlink(tmp)


def _key_files(keys: Path) -> list[Path]:
    if not keys.is_dir():
        return []
    # Regular files only; dot files are half-written temporaries.
    return sorted(p for p in keys.rglob("*") if p.is_file() and not p.is_symlink() and not p.name.startswith("."))


def pack(paths: ob.Paths, db: bytes | None) -> bytes:
    """The unencrypted archive of the data folder."""
    files: dict[str, bytes] = {}
    if paths.config.exists():
        files["config.yaml"] = paths.config.read_bytes()
    for p in _key_files(paths.keys):
        files["keys/" + p.relative_to(paths.keys).as_posix()] = p.read_bytes()
    if db is not None:
        files["state.db"] = db
    manifest = {
        "version": VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": sorted(files),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("manifest.json", json.dumps(manifest, indent=1).encode()), *files.items()]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _allowed(name: str) -> bool:
    if name in ("manifest.json", "config.yaml", "state.db"):
        return True
    p = PurePosixPath(name)
    return (
        len(p.parts) >= 2
        and p.parts[0] == "keys"
        and not p.is_absolute()
        and all(part not in ("", ".", "..") and not part.startswith(".") for part in p.parts)
    )


def unpack(archive: bytes) -> dict[str, bytes]:
    """Read the archive's files by name, refusing anything that isn't one of ours."""
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for m in tar:
                if not m.isfile() or not _allowed(m.name) or m.name in files:
                    raise BackupError(f"unexpected entry in the backup: {m.name!r}")
                total += m.size
                if total > MAX_BACKUP_BYTES:
                    raise BackupError("the backup is too large")
                f = tar.extractfile(m)
                files[m.name] = f.read() if f else b""
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BackupError(f"the backup is damaged: {e}") from None
    try:
        manifest = json.loads(files.get("manifest.json", b""))
    except ValueError:
        raise BackupError("the backup has no readable manifest") from None
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION:
        raise BackupError("this backup was made by a newer version of Fleetdeck")
    return files


def check_config(text: bytes) -> AppConfig:
    """The backup's config, checked the way the setup page checks a hand-edited one."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise BackupError(f"the backup's config.yaml isn't valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise BackupError("the backup's config.yaml is empty or not a mapping")
    try:
        cfg = validate_raw(data, "the backup's config.yaml")
    except (ValueError, ValidationError) as e:
        raise BackupError(str(e)) from None
    if cfg.auth is None and not cfg.allow_no_auth:
        raise BackupError("the backup's config has no login (auth:), so nobody could log in after restoring it")
    return cfg


def summary(files: dict[str, bytes], cfg: AppConfig | None) -> dict:
    manifest = json.loads(files["manifest.json"])
    return {
        "created_at": manifest.get("created_at"),
        "files": sorted(n for n in files if n != "manifest.json"),
        "username": cfg.auth.username if cfg and cfg.auth else None,
        "machines": len(cfg.hosts) + len(cfg.proxmox) + len(cfg.incus) if cfg else 0,
        "history": "state.db" in files,
    }


def _check_db(data: bytes) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.db"
        path.write_bytes(data)
        try:
            db = sqlite3.connect(path)
            try:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("integrity check failed")
            finally:
                db.close()
        except sqlite3.DatabaseError as e:
            raise BackupError(f"the backup's job history is damaged: {e}") from None


def _restore_db(data: bytes, store: Store | None, state_file: str | None) -> None:
    """Replace the job history: in place through the live connection, so nothing holds a stale file."""
    if store is None and not state_file:
        return  # history kept in memory only; nothing to restore into
    with tempfile.TemporaryDirectory() as d:
        src_path = Path(d) / "state.db"
        src_path.write_bytes(data)
        src = sqlite3.connect(src_path)
        try:
            if store is not None:
                src.backup(store.db)
            else:
                Path(state_file).parent.mkdir(parents=True, exist_ok=True)
                dst = sqlite3.connect(state_file)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
        finally:
            src.close()


def restore_files(paths: ob.Paths, files: dict[str, bytes]) -> None:
    """Write keys first and the config last, each atomically. Keys missing from the backup are kept."""
    keys = paths.keys.resolve()
    for name, data in files.items():
        if name.startswith("keys/"):
            dest = (paths.keys / PurePosixPath(name).relative_to("keys")).resolve()
            if not dest.is_relative_to(keys):  # _allowed already rules this out; belt and braces
                raise BackupError(f"unexpected entry in the backup: {name!r}")
            ob._write_private(dest, data)
    if "config.yaml" in files:
        ob._write_private(paths.config, files["config.yaml"])


def _state_file(new: AppConfig, paths: ob.Paths) -> str | None:
    """The restored config's state file, resolved the way ``load_config`` will after the restart."""
    if env("STATE"):
        return env("STATE")
    if not new.state_file:
        return None
    p = Path(new.state_file)
    return str(p if p.is_absolute() else paths.config.absolute().parent / p)


def _prune_safety_copies(folder: Path) -> None:
    """Keep only the newest few: each holds every secret."""
    for old in sorted(folder.glob(f"before-restore-*{SUFFIX}"))[:-KEEP_SAFETY_COPIES]:
        old.unlink(missing_ok=True)


class BackupRequest(BaseModel):
    passphrase: str = Field(min_length=MIN_PASSPHRASE, max_length=1024)


class RestoreRequest(BaseModel):
    passphrase: str = Field(min_length=1, max_length=1024)
    data: str = Field(max_length=MAX_BACKUP_BYTES * 4 // 3 + 4)  # base64 of the file
    # False: only open the backup and say what's in it. True: restore it and restart.
    confirm: bool = False


def add_backup_routes(
    app: FastAPI,
    cfg: AppConfig,
    paths: ob.Paths,
    store: Store | None,
    restart: Callable[[], None] | None,
    busy: Callable[[], list[str]] = list,
    first_run: bool = False,
) -> None:
    """``first_run``: also offer the restore on the setup page, behind the one-time setup link, so a
    fresh install can come back from a backup without creating a login first."""
    lock = asyncio.Lock()

    def admin_only() -> None:
        # The guard already demands the login; without one, anyone could take every key.
        if cfg.auth is None:
            raise HTTPException(403, "set up an admin login before making or restoring backups")

    @app.post("/api/backup")
    async def download_backup(req: BackupRequest):
        admin_only()
        db = _snapshot_db(store)  # on the event loop: the store's connection is only ever used from here
        blob = await asyncio.to_thread(lambda: encrypt(pack(paths, db), req.passphrase))
        stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
        log.info("Backup downloaded (%d bytes).", len(blob))
        return Response(
            blob,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="homelab-backup-{stamp}{SUFFIX}"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/backup/restore")
    async def restore_backup(req: RestoreRequest):
        admin_only()
        return await restore(req)

    if first_run:
        # The guard lets /api/setup/* through only with the token from the logs.
        @app.post("/api/setup/restore")
        async def restore_on_setup(req: RestoreRequest):
            return await restore(req)

    async def restore(req: RestoreRequest) -> dict:
        try:
            blob = base64.b64decode(req.data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "the upload is not valid base64") from None
        try:
            files = await asyncio.to_thread(lambda: unpack(decrypt(blob, req.passphrase)))
            new_cfg = check_config(files["config.yaml"]) if "config.yaml" in files else None
            if new_cfg is None:
                raise BackupError("the backup has no config.yaml")
            if "state.db" in files:
                await asyncio.to_thread(_check_db, files["state.db"])
        except BackupError as e:
            raise HTTPException(400, str(e)) from None
        info = summary(files, new_cfg)
        if not req.confirm:
            return {"restored": False, "backup": info}

        if not paths.writable():
            raise HTTPException(409, f"{paths.config} can't be written; restore the files by hand")
        refuse_while_busy(busy)
        async with lock:
            # Keep what is there now, encrypted with the same passphrase, in case this was a mistake.
            db = _snapshot_db(store)
            safety = (
                paths.config.parent
                / "backups"
                / (f"before-restore-{datetime.now(UTC).strftime('%Y-%m-%d-%H%M%S')}{SUFFIX}")
            )
            current = await asyncio.to_thread(lambda: encrypt(pack(paths, db), req.passphrase))
            await asyncio.to_thread(ob._write_private, safety, current)
            await asyncio.to_thread(_prune_safety_copies, safety.parent)
            await asyncio.to_thread(restore_files, paths, files)
            if "state.db" in files:
                # Where the restored config keeps its history, which may not be where this one does.
                state_file = _state_file(new_cfg, paths) or cfg.state_file
                same = cfg.state_file and Path(state_file).absolute() == Path(cfg.state_file).absolute()
                live = store if same else None
                _restore_db(files["state.db"], live, state_file)
        log.warning("Restored a backup from %s; the previous data was saved to %s.", info["created_at"], safety)
        if restart is not None:
            asyncio.get_running_loop().call_later(0.3, restart)
        return {
            "restored": True,
            "backup": info,
            "previous": str(safety),
            "restarting": restart is not None,
        }

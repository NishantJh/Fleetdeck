"""Encrypted secrets, kept in ``state.db`` and referenced from config.yaml as ``${SECRET:name}``.

Each value is AES-256-GCM encrypted with a key that is not in the database: ``FLEETDECK_SECRET_KEY`` or
``FLEETDECK_SECRET_KEY_FILE`` (a Docker secret) if set, else ``keys/secrets.key`` beside the config (created on
first use, readable only by the app). The key file is part of the dashboard backup, like the SSH key.
The secret's name is bound to its ciphertext, so a value can't be swapped to another name.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .envvars import env

NAME = re.compile(r"[A-Za-z0-9_.-]{1,120}")
KEY_FILE = "secrets.key"


class SecretError(Exception):
    pass


def secret_name(*parts: str) -> str:
    """A safe secret name from readable parts (``services``, ``NAS``, ``token`` -> ``services.NAS.token``)."""
    return ".".join(re.sub(r"[^A-Za-z0-9_-]+", "_", p).strip("_") or "x" for p in parts)[:120]


def load_key(keys_dir: Path, create: bool = False) -> bytes | None:
    """The 32-byte key, or None if there is none (and ``create`` is false)."""
    if value := env("SECRET_KEY"):
        return hashlib.sha256(value.encode()).digest()
    if path := env("SECRET_KEY_FILE"):
        try:
            return hashlib.sha256(Path(path).read_text().strip().encode()).digest()
        except OSError as e:
            raise SecretError(f"can't read FLEETDECK_SECRET_KEY_FILE: {e.strerror or e}") from None
    file = keys_dir / KEY_FILE
    try:
        data = file.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
        keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = os.urandom(32)
        try:
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process won the race
            return file.read_bytes()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return data
    except OSError as e:
        raise SecretError(f"can't read {file}: {e.strerror or e}") from None
    if len(data) != 32:
        raise SecretError(f"{file} is not a 32-byte key")
    return data


class SecretStore:
    def __init__(self, db_path: Path | str, keys_dir: Path | str):
        self.db_path, self.keys_dir = Path(db_path), Path(keys_dir)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.execute("CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, data BLOB NOT NULL)")
        return db

    def get(self, name: str) -> str | None:
        if not self.db_path.exists():
            return None
        with contextlib.closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)) as db:
            try:
                row = db.execute("SELECT data FROM secrets WHERE name = ?", (name,)).fetchone()
            except sqlite3.OperationalError:  # no table yet
                return None
        if row is None:
            return None
        key = load_key(self.keys_dir)
        if key is None:
            raise SecretError("the secret key is missing: restore keys/secrets.key or set FLEETDECK_SECRET_KEY")
        blob = bytes(row[0])
        try:
            return AESGCM(key).decrypt(blob[:12], blob[12:], name.encode()).decode()
        except Exception:
            raise SecretError(f"can't decrypt secret {name}: wrong key?") from None

    def set(self, name: str, value: str) -> None:
        if not NAME.fullmatch(name):
            raise SecretError("invalid secret name")
        key = load_key(self.keys_dir, create=True)
        assert key is not None
        nonce = os.urandom(12)
        blob = nonce + AESGCM(key).encrypt(nonce, value.encode(), name.encode())
        with contextlib.closing(self._connect()) as db:
            db.execute("INSERT OR REPLACE INTO secrets (name, data) VALUES (?, ?)", (name, blob))

    def delete(self, name: str) -> None:
        if self.db_path.exists():
            with contextlib.closing(self._connect()) as db:
                db.execute("DELETE FROM secrets WHERE name = ?", (name,))

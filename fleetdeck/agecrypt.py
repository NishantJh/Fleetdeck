"""Writes files in the age format (https://age-encryption.org/v1) encrypted with a passphrase.

The `age` program can only take a passphrase from a terminal, so the dashboard writes passphrase-encrypted
backups itself. The result is a normal age file: ``age -d file.tar.gz.age > file.tar.gz`` asks for the
passphrase and opens it. Only writing is here; nothing in the dashboard ever decrypts these.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INTRO = b"age-encryption.org/v1\n"
# age's own default: 2**18 scrypt rounds, 256 MiB of memory for about a second.
WORK_FACTOR = 18
CHUNK = 64 * 1024


def _b64(data: bytes) -> bytes:
    return base64.b64encode(data).rstrip(b"=")


def _hkdf(key: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(key)


def _scrypt(passphrase: str, salt: bytes, log_n: int) -> bytes:
    n, r = 1 << log_n, 8
    return hashlib.scrypt(
        passphrase.encode(), salt=b"age-encryption.org/v1/scrypt" + salt, n=n, r=r, p=1, maxmem=256 * n * r, dklen=32
    )


def header(file_key: bytes, passphrase: str, log_n: int = WORK_FACTOR) -> bytes:
    """The age header with one scrypt stanza that wraps ``file_key``, and its MAC."""
    salt = os.urandom(16)
    wrapped = ChaCha20Poly1305(_scrypt(passphrase, salt, log_n)).encrypt(b"\0" * 12, file_key, None)
    # 32 bytes of body are 43 base64 characters, a single line shorter than the 64 the format wraps at.
    head = INTRO + b"-> scrypt " + _b64(salt) + b" " + str(log_n).encode() + b"\n" + _b64(wrapped) + b"\n---"
    mac = hmac.new(_hkdf(file_key, b"", b"header"), head, hashlib.sha256).digest()
    return head + b" " + _b64(mac) + b"\n"


def encrypt_file(plain: Path, out: Path, passphrase: str, log_n: int = WORK_FACTOR) -> None:
    """Encrypt ``plain`` with ``passphrase`` into ``out`` (created readable by its owner only)."""
    if not passphrase:
        raise ValueError("the backup password is empty")
    file_key = os.urandom(16)
    nonce = os.urandom(16)
    aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
    with open(plain, "rb") as src, open(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as dst:
        dst.write(header(file_key, passphrase, log_n) + nonce)
        counter = 0
        chunk = src.read(CHUNK)
        while True:
            # A chunk is the last one when nothing follows it; only an empty file has an empty chunk.
            nxt = src.read(CHUNK)
            last = not nxt
            chunk_nonce = counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")
            dst.write(aead.encrypt(chunk_nonce, chunk, None))
            if last:
                break
            chunk, counter = nxt, counter + 1
        dst.flush()
        os.fsync(dst.fileno())

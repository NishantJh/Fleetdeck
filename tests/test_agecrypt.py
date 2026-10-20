import base64
import hashlib
import hmac
import os
import shutil
import stat
import subprocess

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from fleetdeck import agecrypt


def _unb64(s: bytes) -> bytes:
    return base64.b64decode(s + b"=" * (-len(s) % 4))


def decrypt(data: bytes, passphrase: str) -> bytes:
    """Opens an age passphrase file, written from the spec (age-encryption.org/v1) rather than from agecrypt."""
    head, rest = data.split(b"\n---", 1)
    mac_b64, body = rest[1:].split(b"\n", 1)
    lines = head.split(b"\n")
    assert lines[0] == b"age-encryption.org/v1"
    tag, kind, salt, log_n = lines[1].split(b" ")
    assert (tag, kind) == (b"->", b"scrypt") and len(lines) == 3
    n = 1 << int(log_n)
    key = hashlib.scrypt(
        passphrase.encode(), salt=b"age-encryption.org/v1/scrypt" + _unb64(salt), n=n, r=8, p=1, maxmem=256 * n * 8,
        dklen=32,
    )  # fmt: skip
    file_key = ChaCha20Poly1305(key).decrypt(b"\0" * 12, _unb64(lines[2]), None)
    mac_key = agecrypt._hkdf(file_key, b"", b"header")
    assert hmac.compare_digest(hmac.new(mac_key, head + b"\n---", hashlib.sha256).digest(), _unb64(mac_b64))
    nonce, payload = body[:16], body[16:]
    aead = ChaCha20Poly1305(agecrypt._hkdf(file_key, nonce, b"payload"))
    out, i, size = b"", 0, agecrypt.CHUNK + 16
    while True:
        chunk, payload = payload[:size], payload[size:]
        last = not payload
        out += aead.decrypt(i.to_bytes(11, "big") + (b"\x01" if last else b"\x00"), chunk, None)
        if last:
            return out
        i += 1


@pytest.mark.parametrize("size", [0, 1, agecrypt.CHUNK, agecrypt.CHUNK + 1, 3 * agecrypt.CHUNK])
def test_round_trip_at_chunk_edges(tmp_path, size):
    plain, out = tmp_path / "p", tmp_path / "p.age"
    data = os.urandom(size)
    plain.write_bytes(data)
    agecrypt.encrypt_file(plain, out, "correct horse", log_n=10)
    assert decrypt(out.read_bytes(), "correct horse") == data
    if os.name != "nt":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_wrong_passphrase_does_not_open_it(tmp_path):
    plain, out = tmp_path / "p", tmp_path / "p.age"
    plain.write_bytes(b"secret")
    agecrypt.encrypt_file(plain, out, "correct horse", log_n=10)
    assert b"secret" not in out.read_bytes()
    with pytest.raises(InvalidTag):
        decrypt(out.read_bytes(), "wrong")


def test_default_work_factor_is_ages_own(tmp_path):
    plain, out = tmp_path / "p", tmp_path / "p.age"
    plain.write_bytes(b"x")
    agecrypt.encrypt_file(plain, out, "correct horse")
    assert out.read_bytes().split(b"\n")[1].endswith(b" 18")


def test_empty_passphrase_is_refused(tmp_path):
    (tmp_path / "p").write_bytes(b"x")
    with pytest.raises(ValueError):
        agecrypt.encrypt_file(tmp_path / "p", tmp_path / "p.age", "")


@pytest.mark.skipif(not (shutil.which("age") and shutil.which("script")), reason="needs age and script")
def test_the_age_program_opens_it(tmp_path):
    plain, out = tmp_path / "p", tmp_path / "p.age"
    data = os.urandom(agecrypt.CHUNK + 5)
    plain.write_bytes(data)
    agecrypt.encrypt_file(plain, out, "correct horse", log_n=10)
    # age reads a passphrase only from a terminal; `script` gives it one.
    subprocess.run(
        ["script", "-qec", f"age -d -o {tmp_path / 'back'} {out}", "/dev/null"],
        input=b"correct horse\n",
        capture_output=True,
        timeout=60,
    )
    assert (tmp_path / "back").read_bytes() == data

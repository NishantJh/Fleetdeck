import asyncio
import base64
import io
import json
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homelab import backup
from homelab.app import _when_idle, create_app
from homelab.config import load_config
from homelab.jobs import _now
from homelab.models import Job
from homelab.security import hash_password

AUTH = ("admin", "s3cret-pass")
PASS = "correct horse battery"


def _data_dir(tmp_path: Path, username: str = "admin", password: str = AUTH[1], host: str = "pi4") -> Path:
    data = tmp_path / "data"
    (data / "keys" / "tunnels").mkdir(parents=True, exist_ok=True)
    cfg = {
        "auth": {"username": username, "password_hash": hash_password(password)},
        "hosts": [{"name": host, "host": "192.168.1.20"}],
        "state_file": "state.db",
    }
    (data / "config.yaml").write_text(yaml.safe_dump(cfg))
    (data / "keys" / "id_ed25519").write_text(f"private key of {host}\n")
    (data / "keys" / "tunnels" / "vps.conf").write_text(f"tunnel of {host}\n")
    (data / "keys" / ".id_ed25519.tmp").write_text("half-written\n")
    return data


def _client(data: Path, restart=None) -> TestClient:
    path = str(data / "config.yaml")
    return TestClient(create_app(load_config(path), config_file=path, restart=restart))


def _make_backup(client: TestClient) -> bytes:
    r = client.post("/api/backup", json={"passphrase": PASS}, auth=AUTH)
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].endswith('.hlmbackup"')
    assert r.headers["cache-control"] == "no-store"
    return r.content


def test_backup_is_encrypted_and_holds_the_data_folder(tmp_path):
    data = _data_dir(tmp_path)
    with _client(data) as client:
        client.app.state.jobs.store.db.execute("INSERT INTO checks VALUES ('pi4', '{}')")
        blob = _make_backup(client)
    # Nothing readable without the passphrase.
    assert blob.startswith(backup.MAGIC)
    for secret in (b"private key of", b"tunnel of", b"password_hash", b"192.168.1.20"):
        assert secret not in blob
    files = backup.unpack(backup.decrypt(blob, PASS))
    assert sorted(files) == [
        "config.yaml",
        "keys/id_ed25519",
        "keys/tunnels/vps.conf",
        "manifest.json",
        "state.db",
    ]
    assert files["keys/id_ed25519"] == b"private key of pi4\n"
    # The database snapshot is a complete, consistent SQLite file.
    db_file = tmp_path / "snap.db"
    db_file.write_bytes(files["state.db"])
    assert sqlite3.connect(db_file).execute("SELECT target_id FROM checks").fetchall() == [("pi4",)]
    with pytest.raises(backup.BackupError, match="wrong passphrase"):
        backup.decrypt(blob, "not the passphrase")


def test_backup_needs_login_and_a_long_passphrase(tmp_path):
    with _client(_data_dir(tmp_path)) as client:
        assert client.post("/api/backup", json={"passphrase": PASS}).status_code == 401
        assert client.post("/api/backup", json={"passphrase": "short"}, auth=AUTH).status_code == 422
        # Another site can't make the browser fetch one.
        r = client.post("/api/backup", json={"passphrase": PASS}, auth=AUTH, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403


def test_no_backups_without_a_login():
    from homelab.config import AppConfig

    with TestClient(create_app(AppConfig(demo=True, allow_no_auth=True))) as client:
        assert client.post("/api/backup", json={"passphrase": PASS}).status_code == 403


def test_restore_previews_then_replaces_and_restarts(tmp_path):
    old = _data_dir(tmp_path / "old", username="oldadmin", password="old-password", host="old-pi")
    with _client(old) as client:
        client.app.state.jobs.store.db.execute("INSERT INTO checks VALUES ('from-backup', '{}')")
        r = client.post("/api/backup", json={"passphrase": PASS}, auth=("oldadmin", "old-password"))
        blob = r.content

    data = _data_dir(tmp_path)
    (data / "keys" / "only-here").write_text("kept\n")
    restarts = []
    with _client(data, restart=lambda: restarts.append(1)) as client:
        req = {"passphrase": PASS, "data": base64.b64encode(blob).decode()}
        bad = client.post("/api/backup/restore", json={**req, "passphrase": "nope"}, auth=AUTH)
        assert bad.status_code == 400 and "wrong passphrase" in bad.text

        preview = client.post("/api/backup/restore", json=req, auth=AUTH).json()
        assert preview["restored"] is False
        assert preview["backup"]["username"] == "oldadmin"
        assert preview["backup"]["machines"] == 1 and preview["backup"]["history"] is True
        assert (data / "keys" / "id_ed25519").read_text() == "private key of pi4\n"  # nothing changed yet

        done = client.post("/api/backup/restore", json={**req, "confirm": True}, auth=AUTH).json()
        assert done["restored"] is True and done["restarting"] is True
        assert (data / "keys" / "id_ed25519").read_text() == "private key of old-pi\n"
        assert (data / "keys" / "tunnels" / "vps.conf").read_text() == "tunnel of old-pi\n"
        assert (data / "keys" / "only-here").read_text() == "kept\n"
        assert (data / "keys" / "id_ed25519").stat().st_mode & 0o077 == 0
        assert yaml.safe_load((data / "config.yaml").read_text())["auth"]["username"] == "oldadmin"
        # The history was swapped in through the live connection.
        rows = client.app.state.jobs.store.db.execute("SELECT target_id FROM checks").fetchall()
        assert rows == [("from-backup",)]
        # What was there before is kept, encrypted with the same passphrase.
        previous = Path(done["previous"])
        assert previous.parent == data / "backups"
        kept = backup.unpack(backup.decrypt(previous.read_bytes(), PASS))
        assert kept["keys/id_ed25519"] == b"private key of pi4\n"
        deadline = time.time() + 5
        while not restarts and time.time() < deadline:
            time.sleep(0.05)
    assert restarts == [1]


def _encrypted(files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return base64.b64encode(backup.encrypt(buf.getvalue(), PASS)).decode()


MANIFEST = json.dumps({"version": 1}).encode()


@pytest.mark.parametrize("name", ["../evil", "keys/../../evil", "/etc/passwd", "keys/.hidden", "other.txt", "keys"])
def test_restore_refuses_files_outside_the_data_folder(tmp_path, name):
    data = _data_dir(tmp_path)
    with _client(data) as client:
        good = (data / "config.yaml").read_bytes()
        req = {"passphrase": PASS, "data": _encrypted({"manifest.json": MANIFEST, "config.yaml": good, name: b"x"})}
        r = client.post("/api/backup/restore", json={**req, "confirm": True}, auth=AUTH)
        assert r.status_code == 400 and "unexpected entry" in r.text
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize(
    ("config", "error"),
    [
        (b"hosts: [", "isn't valid YAML"),
        (b"hosts: []\n", "no login"),
        (b"auth: {username: admin}\n", "password"),
    ],
)
def test_restore_refuses_a_config_that_would_not_start(tmp_path, config, error):
    data = _data_dir(tmp_path)
    before = (data / "config.yaml").read_bytes()
    with _client(data) as client:
        req = {
            "passphrase": PASS,
            "data": _encrypted({"manifest.json": MANIFEST, "config.yaml": config}),
            "confirm": True,
        }
        r = client.post("/api/backup/restore", json=req, auth=AUTH)
        assert r.status_code == 400 and error in r.text
    assert (data / "config.yaml").read_bytes() == before


def test_restore_refuses_junk(tmp_path):
    with _client(_data_dir(tmp_path)) as client:
        for blob in (b"not a backup", backup.MAGIC + b"short"):
            req = {"passphrase": PASS, "data": base64.b64encode(blob).decode()}
            r = client.post("/api/backup/restore", json=req, auth=AUTH)
            assert r.status_code == 400 and "isn't a Homelab Manager backup" in r.text
        r = client.post("/api/backup/restore", json={"passphrase": PASS, "data": "%%%"}, auth=AUTH)
        assert r.status_code == 400
        good = (tmp_path / "data" / "config.yaml").read_bytes()
        damaged = _encrypted({"manifest.json": MANIFEST, "config.yaml": good, "state.db": b"not sqlite" * 100})
        r = client.post("/api/backup/restore", json={"passphrase": PASS, "data": damaged}, auth=AUTH)
        assert r.status_code == 400


def _running_job(client: TestClient, name: str = "pi4") -> Job:
    job = Job(id="j1", target_id=f"ssh:{name}", target_name=name, action="upgrade", status="running", created_at=_now())
    client.app.state.jobs.jobs[job.id] = job
    return job


def test_no_restore_or_apply_while_a_job_runs(tmp_path):
    # A restart cancels jobs; apt cut off halfway can leave a machine broken.
    data = _data_dir(tmp_path)
    restarts = []
    with _client(data, restart=lambda: restarts.append(1)) as client:
        blob = _make_backup(client)
        job = _running_job(client)
        req = {"passphrase": PASS, "data": base64.b64encode(blob).decode(), "confirm": True}
        r = client.post("/api/backup/restore", json=req, auth=AUTH)
        assert r.status_code == 409 and "pi4" in r.text
        assert not (data / "backups").exists()
        r = client.post("/api/setup/apply", auth=AUTH)
        assert r.status_code == 409 and "wait for the running jobs" in r.text
        job.status = "succeeded"
        assert client.post("/api/backup/restore", json=req, auth=AUTH).status_code == 200
        deadline = time.time() + 5
        while not restarts and time.time() < deadline:
            time.sleep(0.05)
    assert restarts == [1]


async def test_restart_waits_for_a_job_that_started_meanwhile():
    restarts, running = [], ["pi4"]
    go = _when_idle(lambda: restarts.append(1), lambda: list(running))
    go()
    await asyncio.sleep(0.1)
    assert restarts == []
    running.clear()
    await asyncio.sleep(2.1)
    assert restarts == [1]


def test_old_kdf_settings_stay_readable_and_unknown_ones_are_refused(monkeypatch):
    monkeypatch.setattr(backup, "_SCRYPT", (2**17, 8, 1))
    blob = backup.encrypt(b"secret", PASS)
    monkeypatch.setattr(backup, "_SCRYPT", (2**15, 8, 1))
    assert backup.decrypt(blob, PASS) == b"secret"
    crafted = backup.MAGIC + backup._HEADER.pack(2**30, 8, 1, b"s" * 16, b"n" * 12) + b"x" * 32
    with pytest.raises(backup.BackupError, match="encryption settings"):
        backup.decrypt(crafted, PASS)


def test_history_goes_where_the_restored_config_keeps_it(tmp_path):
    data = _data_dir(tmp_path)
    with _client(data) as client:
        client.app.state.jobs.store.db.execute("INSERT INTO checks VALUES ('kept', '{}')")
        files = backup.unpack(backup.decrypt(_make_backup(client), PASS))
        cfg = yaml.safe_load(files["config.yaml"])
        cfg["state_file"] = "history/other.db"
        files["config.yaml"] = yaml.safe_dump(cfg).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        req = {"passphrase": PASS, "data": base64.b64encode(backup.encrypt(buf.getvalue(), PASS)).decode()}
        client.app.state.jobs.store.db.execute("DELETE FROM checks")
        assert client.post("/api/backup/restore", json={**req, "confirm": True}, auth=AUTH).status_code == 200
        # The running app's history is untouched; the restored config's file has the backup's.
        assert client.app.state.jobs.store.db.execute("SELECT * FROM checks").fetchall() == []
    rows = sqlite3.connect(data / "history" / "other.db").execute("SELECT target_id FROM checks").fetchall()
    assert rows == [("kept",)]


def test_only_the_newest_safety_copies_are_kept(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(8):
        (folder / f"before-restore-2026-10-0{i + 1}-120000{backup.SUFFIX}").write_text("x")
    (folder / "mine.hlmbackup").write_text("not ours")
    backup._prune_safety_copies(folder)
    left = sorted(p.name for p in folder.iterdir())
    assert left == [f"before-restore-2026-10-0{i}-120000{backup.SUFFIX}" for i in range(4, 9)] + ["mine.hlmbackup"]

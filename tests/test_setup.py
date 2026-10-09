import json
import os
import subprocess
import time

import asyncssh
import pytest
import yaml
from fastapi.testclient import TestClient

import homelab.onboarding as ob
from homelab.__main__ import drop_privileges
from homelab.app import create_app
from homelab.config import AppConfig, load_config
from homelab.models import ExecResult
from homelab.security import hash_password, verify_password

PASSWORD = "correct horse battery"


@pytest.fixture
def fresh(tmp_path):
    """A first-run app on an empty data folder, plus a counter of restart requests."""
    path = str(tmp_path / "config.yaml")
    restarts = []
    app = create_app(load_config(path), config_file=path, restart=lambda: restarts.append(1))
    with TestClient(app) as client:
        client.headers["X-Setup-Token"] = app.state.setup_token
        yield client, tmp_path, restarts


def _admin(client, restarts):
    assert client.post("/api/setup/admin", json={"username": "me", "password": PASSWORD}).json() == {"restarting": True}
    deadline = time.time() + 5
    while not restarts and time.time() < deadline:
        time.sleep(0.05)
    assert restarts


@pytest.fixture
def configured(fresh):
    """The app as it comes back after the admin login was created."""
    client, tmp_path, restarts = fresh
    _admin(client, restarts)
    path = str(tmp_path / "config.yaml")
    app = create_app(load_config(path), config_file=path, restart=lambda: restarts.append(1))
    with TestClient(app) as c:
        c.auth = ("me", PASSWORD)
        yield c, tmp_path


def test_password_hash():
    h = hash_password("pw")
    assert h.startswith("scrypt$") and verify_password(b"pw", h)
    assert not verify_password(b"pW", h)
    assert not verify_password(b"pw", "scrypt$broken")
    assert hash_password("pw") != h  # salted


def test_first_run_creates_keys_and_login(fresh):
    client, tmp_path, restarts = fresh
    st = client.get("/api/setup").json()
    assert st["first_run"] and st["writable"] and st["machines"] == []
    assert st["public_key"].startswith("ssh-ed25519 ") and st["public_key"].endswith("homelab-manager")
    keys = tmp_path / "keys"
    assert oct(keys.stat().st_mode & 0o777) == "0o700"
    for f in ("id_ed25519", "incus-client.key", "incus-client.crt"):
        assert oct((keys / f).stat().st_mode & 0o777) == "0o600"
    # Machines need the login first.
    assert (
        client.post("/api/setup/connect", json={"kind": "host", "name": "pi", "address": "10.0.0.2"}).status_code == 409
    )
    assert client.post("/api/setup/admin", json={"password": "short"}).status_code == 422

    _admin(client, restarts)
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert cfg.auth.username == "me" and cfg.auth.password is None and cfg.auth.password_hash
    assert cfg.known_hosts == str(keys / "known_hosts")
    # Only one admin, ever, from the first-run page.
    assert client.post("/api/setup/admin", json={"password": PASSWORD}).status_code in (409, 403)


def test_login_works_after_restart(configured):
    client, _ = configured
    assert client.get("/api/targets").status_code == 200
    assert client.get("/api/targets", auth=("me", "wrong password")).status_code == 401
    st = client.get("/api/setup").json()
    assert not st["first_run"] and not st["restart_pending"]
    assert client.post("/api/setup/admin", json={"password": PASSWORD}).status_code == 409


def test_script(configured):
    client, _ = configured
    m = {"kind": "host", "name": "pi4", "address": "192.168.1.30", "allow_from": "192.168.1.50, 10.0.0.0/8"}
    script = client.post("/api/setup/script", json=m).json()["script"]
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    assert 'restrict,from="192.168.1.50/32,10.0.0.0/8" ssh-ed25519 ' in script
    assert "NOPASSWD: ALL" in script and "visudo -cqf" in script
    plain = client.post("/api/setup/script", json={**m, "allow_from": ""}).json()["script"]
    assert "'restrict ssh-ed25519 " in plain
    assert client.post("/api/setup/script", json={**m, "allow_from": "my house"}).status_code == 400
    for bad in ({"name": "a b"}, {"address": "x; rm -rf /"}, {"kind": "windows"}, {"port": 0}):
        assert client.post("/api/setup/script", json={**m, **bad}).status_code == 422


def _fake_machine(monkeypatch, host_key, outputs):
    async def fetch(host, port):
        return host_key

    async def run(self, script, on_line=None, timeout=0):
        for marker, out in outputs.items():
            if marker in script:
                return out if isinstance(out, ExecResult) else ExecResult(rc=0, stdout=out, stderr="")
        raise AssertionError(f"unexpected script {script!r}")

    monkeypatch.setattr(ob, "fetch_host_key", fetch)
    monkeypatch.setattr(ob.SSHExecutor, "run", run)


PVE_OUT = (
    'TFA...\n{"full-tokenid":"homelab@pve!dashboard","info":{"privsep":"0"},"value":"secret-uuid"}\n'
    "__FP__=sha256 Fingerprint=" + ":".join(["AB"] * 32) + "\n"
)


def test_connect_proxmox_pins_and_writes_config(configured, monkeypatch):
    client, tmp_path = configured
    key = asyncssh.generate_private_key("ssh-ed25519")
    _fake_machine(monkeypatch, key, {"id -un": "root\n", "pveum": PVE_OUT})
    r = client.post("/api/setup/connect", json={"kind": "proxmox", "name": "pve", "address": "192.168.1.10"}).json()
    assert r["ok"], r
    assert r["fingerprint"] == key.get_fingerprint("sha256")
    assert (tmp_path / "keys" / "known_hosts").read_text().startswith("192.168.1.10 ssh-ed25519 ")

    cfg = load_config(str(tmp_path / "config.yaml"))
    pve = cfg.proxmox[0]
    assert pve.api_url == "https://192.168.1.10:8006" and pve.token_id == "homelab@pve!hm-pve"
    assert pve.token_secret == "secret-uuid" and pve.tls_fingerprint == "ab" * 32
    assert pve.ssh.user == "homelab" and pve.ssh.sudo and pve.ssh.known_hosts == str(tmp_path / "keys" / "known_hosts")
    st = client.get("/api/setup").json()
    assert st["restart_pending"] and st["machines"] == [
        {"kind": "proxmox", "name": "pve", "address": "192.168.1.10", "port": 22}
    ]
    assert st["running"] == []

    # A different host key next time is refused unless trusted on purpose.
    other = asyncssh.generate_private_key("ssh-ed25519")
    _fake_machine(monkeypatch, other, {"id -un": "root\n", "pveum": PVE_OUT})
    body = {"kind": "proxmox", "name": "pve", "address": "192.168.1.10"}
    r = client.post("/api/setup/connect", json=body).json()
    assert not r["ok"] and r["host_key_changed"]
    assert client.post("/api/setup/connect", json={**body, "replace_host_key": True}).json()["ok"]
    lines = (tmp_path / "keys" / "known_hosts").read_text().splitlines()
    assert len(lines) == 1 and other.export_public_key().decode().split()[1] in lines[0]


def test_several_machines_of_each_type(configured, monkeypatch):
    client, tmp_path = configured
    incus_out = "__ADDR__=:8443\n__FP__=" + "cd" * 32 + "\n"
    _fake_machine(
        monkeypatch,
        asyncssh.generate_private_key("ssh-ed25519"),
        {"id -un": "root\n", "pveum": PVE_OUT, "incus config": incus_out},
    )
    for i in (1, 2, 3):
        body = {"kind": "proxmox", "name": f"pve{i}", "address": f"10.0.1.{i}"}
        assert client.post("/api/setup/connect", json=body).json()["ok"]
    for i in (1, 2):
        body = {"kind": "incus", "name": f"incus{i}", "address": f"10.0.2.{i}"}
        assert client.post("/api/setup/connect", json=body).json()["ok"]
    cfg = load_config(str(tmp_path / "config.yaml"))
    # Each node gets its own token, so nodes of one cluster don't revoke each other's.
    assert [p.token_id for p in cfg.proxmox] == [f"homelab@pve!hm-pve{i}" for i in (1, 2, 3)]
    assert [i.url for i in cfg.incus] == ["https://10.0.2.1:8443", "https://10.0.2.2:8443"]
    assert len(client.get("/api/setup").json()["machines"]) == 5
    assert ob.pve_token("pve.lab") == "hm-pve-lab"


def test_connect_incus_and_host(configured, monkeypatch):
    client, tmp_path = configured
    _fake_machine(
        monkeypatch,
        asyncssh.generate_private_key("ssh-ed25519"),
        {"id -un": "root\n", "incus config": "__ADDR__=[::]:8444\n__FP__=" + "cd" * 32 + "\n"},
    )
    r = client.post("/api/setup/connect", json={"kind": "incus", "name": "incus01", "address": "192.168.1.20"})
    assert r.json()["ok"], r.json()
    r = client.post(
        "/api/setup/connect", json={"kind": "host", "name": "vps", "address": "vps.example.com", "port": 2222}
    )
    assert r.json()["ok"]
    assert "[vps.example.com]:2222 ssh-ed25519" in (tmp_path / "keys" / "known_hosts").read_text()
    cfg = load_config(str(tmp_path / "config.yaml"))
    inc = cfg.incus[0]
    assert inc.url == "https://192.168.1.20:8444" and inc.tls_fingerprint == "cd" * 32
    assert inc.client_cert == str(tmp_path / "keys" / "incus-client.crt")
    vps = cfg.hosts[0]
    assert (vps.host, vps.port, vps.user, vps.sudo) == ("vps.example.com", 2222, "homelab", True)

    # Re-adding a name under another type moves it rather than duplicating it.
    client.post("/api/setup/connect", json={"kind": "host", "name": "incus01", "address": "192.168.1.20"})
    cfg = load_config(str(tmp_path / "config.yaml"))
    assert not cfg.incus and {h.name for h in cfg.hosts} == {"vps", "incus01"}

    assert client.delete("/api/setup/machines/vps").status_code == 200
    assert client.delete("/api/setup/machines/vps").status_code == 404
    assert [h.name for h in load_config(str(tmp_path / "config.yaml")).hosts] == ["incus01"]


def test_connect_failures_explain_what_to_do(configured, monkeypatch):
    client, tmp_path = configured
    key = asyncssh.generate_private_key("ssh-ed25519")

    async def denied(self, script, on_line=None, timeout=0):
        raise asyncssh.PermissionDenied("nope")

    _fake_machine(monkeypatch, key, {})
    monkeypatch.setattr(ob.SSHExecutor, "run", denied)
    r = client.post("/api/setup/connect", json={"kind": "host", "name": "pi", "address": "10.0.0.2"}).json()
    assert not r["ok"] and "Run the setup script" in r["log"][-1]

    _fake_machine(monkeypatch, key, {"id -un": "homelab\n"})
    r = client.post("/api/setup/connect", json={"kind": "host", "name": "pi", "address": "10.0.0.2"}).json()
    assert not r["ok"] and "sudo" in r["log"][-1]

    _fake_machine(
        monkeypatch, key, {"id -un": "root\n", "pveum": ExecResult(rc=127, stdout="", stderr="pveum: not found")}
    )
    r = client.post("/api/setup/connect", json={"kind": "proxmox", "name": "pve", "address": "10.0.0.3"}).json()
    assert not r["ok"] and "pveum: not found" in r["log"][-1]
    assert load_config(str(tmp_path / "config.yaml")).hosts == []  # nothing saved on failure


async def test_unreachable_host():
    with pytest.raises(ob.SetupError, match="can't reach SSH"):
        await ob.fetch_host_key("127.0.0.1", 1)


def test_config_editor(configured):
    client, tmp_path = configured
    text = client.get("/api/setup/config").json()["yaml"]
    assert "password_hash" in text
    assert client.put("/api/setup/config", json={"yaml": "auth: [\n"}).status_code == 400
    r = client.put("/api/setup/config", json={"yaml": text + "max_parallel: 0\n"})
    assert r.status_code == 400 and "max_parallel" in r.json()["detail"]
    r = client.put("/api/setup/config", json={"yaml": "max_parallel: 2\n"})
    assert r.status_code == 400 and "auth" in r.json()["detail"]
    assert client.put("/api/setup/config", json={"yaml": text + "max_parallel: 2\n"}).status_code == 200
    assert load_config(str(tmp_path / "config.yaml")).max_parallel == 2
    assert client.get("/api/setup").json()["restart_pending"]
    assert client.post("/api/setup/apply").json() == {"restarting": True}


def test_read_only_legacy_config(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("auth: {password: x}\n")
    os.chmod(cfg_dir / "config.yaml", 0o400)
    os.chmod(cfg_dir, 0o500)
    try:
        if os.access(cfg_dir, os.W_OK):
            pytest.skip("running as root: permissions aren't enforced")
        path = str(cfg_dir / "config.yaml")
        with TestClient(create_app(load_config(path), config_file=path)) as client:
            client.auth = ("admin", "x")
            st = client.get("/api/setup").json()
            assert not st["writable"] and st["public_key"] is None
            assert (
                client.post("/api/setup/script", json={"kind": "host", "name": "a", "address": "1.2.3.4"}).status_code
                == 409
            )
    finally:
        os.chmod(cfg_dir, 0o700)


def test_parse_outputs():
    assert ob.parse_pve(PVE_OUT) == ("secret-uuid", "ab" * 32)
    with pytest.raises(ob.SetupError, match="API token"):
        ob.parse_pve("__FP__=x\n")
    assert ob.parse_incus(f"__ADDR__=:8443\n__FP__={'ef' * 32}\n") == (8443, "ef" * 32)
    assert ob.parse_incus(f"__ADDR__=10.0.0.5\n__FP__={'ef' * 32}\n")[0] == 8443
    with pytest.raises(ob.SetupError):
        ob.parse_incus("__ADDR__=:8443\n__FP__=\n")


def test_provisioning_scripts_are_valid_sh(tmp_path):
    paths = ob.Paths(tmp_path / "config.yaml")
    ob.ensure_identity(paths)
    for script in (
        ob.pve_script("hm-pve"),
        ob.incus_script(paths.incus_cert.read_text(), ob.incus_cert_fingerprint(paths)),
    ):
        assert subprocess.run(["sh", "-n", "-c", script]).returncode == 0
    m = ob.Machine(kind="host", name="pi", address="10.0.0.2")
    assert subprocess.run(["sh", "-n", "-c", ob.bootstrap_script(m, "ssh-ed25519 AAAA x", [])]).returncode == 0


def test_drop_privileges_only_in_the_image(monkeypatch):
    monkeypatch.delenv("HOMELAB_DATA", raising=False)
    calls = []
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(uid))
    drop_privileges()
    assert calls == []


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() != 0, reason="needs root")
def test_drop_privileges_fixes_ownership(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "keys").mkdir(parents=True)
    (data / "keys" / "f").write_text("x")
    monkeypatch.setenv("HOMELAB_DATA", str(data))
    monkeypatch.setenv("PUID", "12345")
    switched = []
    monkeypatch.setattr(os, "setgroups", lambda g: switched.append(("groups", g)))
    monkeypatch.setattr(os, "setgid", lambda g: switched.append(("gid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: switched.append(("uid", u)))
    monkeypatch.setitem(os.environ, "HOME", os.environ.get("HOME", "/root"))
    drop_privileges()
    for p in (data, data / "keys", data / "keys" / "f"):
        assert (p.stat().st_uid, p.stat().st_gid) == (12345, 12345)
    assert switched == [("groups", []), ("gid", 12345), ("uid", 12345)]


def test_written_config_round_trips(tmp_path):
    paths = ob.Paths(tmp_path / "config.yaml")
    data = ob.new_config(paths, "admin", hash_password("x" * 10))
    ob.upsert(data, "host", {"name": "pi", "host": "10.0.0.2", "user": "homelab", "sudo": True})
    ob.write_raw(paths, data)
    text = (tmp_path / "config.yaml").read_text()
    assert text.startswith(ob.HEADER)
    assert yaml.safe_load(text)["hosts"][0]["name"] == "pi"
    assert oct((tmp_path / "config.yaml").stat().st_mode & 0o777) == "0o600"
    assert isinstance(load_config(str(tmp_path / "config.yaml")), AppConfig)
    json.dumps(ob.machines(load_config(str(tmp_path / "config.yaml"))))

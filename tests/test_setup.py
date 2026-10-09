import base64
import hashlib
import json
import os
import re
import shlex
import shutil
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
    r = client.post("/api/setup/script", json=m).json()
    script, source = r["script"], r["source"]
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    assert subprocess.run(["sh", "-n"], input=source, text=True).returncode == 0
    # One line, which carries the source.
    assert script.count("\n") == 1 and base64.b64decode(script.split()[-1]).decode() == source
    assert 'restrict,from="192.168.1.50/32,10.0.0.0/8" ssh-ed25519 ' in source
    assert "NOPASSWD: ALL" in source and "visudo -cqf" in source
    plain = client.post("/api/setup/script", json={**m, "allow_from": ""}).json()["source"]
    assert "'restrict ssh-ed25519 " in plain
    assert client.post("/api/setup/script", json={**m, "allow_from": "my house"}).status_code == 400
    for bad in ({"name": "a b"}, {"address": "x; rm -rf /"}, {"kind": "windows"}, {"port": 0}):
        assert client.post("/api/setup/script", json={**m, **bad}).status_code == 422


def _connect(client, body):
    """Connect, confirming the host key fingerprint the first time, as the page does."""
    r = client.post("/api/setup/connect", json=body).json()
    if r.get("needs_confirm"):
        assert not r["ok"] and r["fingerprint"] in r["log"][-1]
        r = client.post("/api/setup/connect", json={**body, "trust_fingerprint": r["fingerprint"]}).json()
    return r


def _fake_machine(monkeypatch, host_key, outputs):
    async def fetch(host, port):
        return host_key

    # Another machine than the test's, and no older Proxmox tokens.
    all_outputs = {"random/boot_id": "another-boot\n", "token list": "[]", **outputs}

    async def run(self, script, on_line=None, timeout=0):
        for marker, out in all_outputs.items():
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
    body = {"kind": "proxmox", "name": "pve", "address": "192.168.1.10"}
    r = client.post("/api/setup/connect", json=body).json()
    # Nothing is pinned or provisioned before the fingerprint is confirmed.
    assert r["needs_confirm"] and not r["ok"] and not (tmp_path / "keys" / "known_hosts").exists()
    assert not client.get("/api/setup").json()["machines"]
    wrong = client.post("/api/setup/connect", json={**body, "trust_fingerprint": "SHA256:other"}).json()
    assert wrong["needs_confirm"] and not wrong["ok"]
    r = _connect(client, body)
    assert r["ok"], r
    assert r["fingerprint"] == key.get_fingerprint("sha256")
    assert (tmp_path / "keys" / "known_hosts").read_text().startswith("192.168.1.10 ssh-ed25519 ")

    cfg = load_config(str(tmp_path / "config.yaml"))
    pve = cfg.proxmox[0]
    assert pve.api_url == "https://192.168.1.10:8006" and re.fullmatch(r"homelab@pve!hm-pve-\d{14}", pve.token_id)
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
    r = client.post("/api/setup/connect", json=body).json()
    assert not r["ok"] and r["host_key_changed"] and not r["needs_confirm"]
    assert client.post("/api/setup/connect", json={**body, "trust_fingerprint": r["fingerprint"]}).json()["ok"]
    lines = (tmp_path / "keys" / "known_hosts").read_text().splitlines()
    assert len(lines) == 1 and other.export_public_key().decode().split()[1] in lines[0]


def test_several_machines_of_each_type(configured, monkeypatch):
    client, tmp_path = configured
    incus_out = "__ADDR__=:8443\n__FP__=" + "cd" * 32 + "\n"
    _fake_machine(
        monkeypatch,
        asyncssh.generate_private_key("ssh-ed25519"),
        {"id -un": "root\n", "pveum": PVE_OUT, "core.https_address": incus_out},
    )
    for i in (1, 2, 3):
        body = {"kind": "proxmox", "name": f"pve{i}", "address": f"10.0.1.{i}"}
        assert _connect(client, body)["ok"]
    for i in (1, 2):
        body = {"kind": "incus", "name": f"incus{i}", "address": f"10.0.2.{i}"}
        assert _connect(client, body)["ok"]
    cfg = load_config(str(tmp_path / "config.yaml"))
    # Each node gets its own token, so nodes of one cluster don't revoke each other's.
    assert [p.token_id.rsplit("-", 1)[0] for p in cfg.proxmox] == [f"homelab@pve!hm-pve{i}" for i in (1, 2, 3)]
    assert [i.url for i in cfg.incus] == ["https://10.0.2.1:8443", "https://10.0.2.2:8443"]
    assert len(client.get("/api/setup").json()["machines"]) == 5
    assert ob.pve_token("pve.lab").startswith("hm-pve-lab-")


def test_reconnect_revokes_only_unused_proxmox_tokens(configured, monkeypatch):
    client, tmp_path = configured
    _fake_machine(monkeypatch, asyncssh.generate_private_key("ssh-ed25519"), {"id -un": "root\n", "pveum": PVE_OUT})
    body = {"kind": "proxmox", "name": "pve", "address": "192.168.1.10"}
    assert _connect(client, body)["ok"]
    saved = load_config(str(tmp_path / "config.yaml")).proxmox[0].token_id.partition("!")[2]
    ran = []
    listed = json.dumps([{"tokenid": t} for t in ("hm-pve", saved, "hm-pve2")])
    _fake_machine(
        monkeypatch,
        asyncssh.generate_private_key("ssh-ed25519"),
        {"id -un": "root\n", "token list": listed, "token remove": "", "pveum": PVE_OUT},
    )
    orig = ob.SSHExecutor.run

    async def record(self, script, on_line=None, timeout=0):
        ran.append(script)
        return await orig(self, script, on_line, timeout)

    monkeypatch.setattr(ob.SSHExecutor, "run", record)
    r = _connect(client, {**body, "trust_fingerprint": None})
    if not r["ok"]:  # host key changed in this fake: trust it
        r = client.post("/api/setup/connect", json={**body, "trust_fingerprint": r["fingerprint"]}).json()
    assert r["ok"], r
    removed = [s.split()[-1] for s in ran if "token remove" in s]
    # The legacy token goes; the one the saved config still uses, and another node's, stay.
    assert removed == ["hm-pve"]
    assert "Revoked the old token homelab@pve!hm-pve" in r["log"]


def test_connect_incus_and_host(configured, monkeypatch):
    client, tmp_path = configured
    _fake_machine(
        monkeypatch,
        asyncssh.generate_private_key("ssh-ed25519"),
        {"id -un": "root\n", "core.https_address": "__ADDR__=[::]:8444\n__FP__=" + "cd" * 32 + "\n"},
    )
    r = _connect(client, {"kind": "incus", "name": "incus01", "address": "192.168.1.20"})
    assert r["ok"], r
    r = _connect(client, {"kind": "host", "name": "vps", "address": "vps.example.com", "port": 2222})
    assert r["ok"]
    assert "[vps.example.com]:2222 ssh-ed25519" in (tmp_path / "keys" / "known_hosts").read_text()
    cfg = load_config(str(tmp_path / "config.yaml"))
    inc = cfg.incus[0]
    assert inc.url == "https://192.168.1.20:8444" and inc.tls_fingerprint == "cd" * 32
    assert inc.client_cert == str(tmp_path / "keys" / "incus-client.crt")
    vps = cfg.hosts[0]
    assert (vps.host, vps.port, vps.user, vps.sudo) == ("vps.example.com", 2222, "homelab", True)

    # Re-adding a name under another type moves it rather than duplicating it.
    _connect(client, {"kind": "host", "name": "incus01", "address": "192.168.1.20"})
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
    r = _connect(client, {"kind": "host", "name": "pi", "address": "10.0.0.2"})
    assert not r["ok"] and "Run the setup script" in r["log"][-1]

    _fake_machine(monkeypatch, key, {"id -un": "homelab\n"})
    r = _connect(client, {"kind": "host", "name": "pi", "address": "10.0.0.2"})
    assert not r["ok"] and "sudo" in r["log"][-1]

    _fake_machine(
        monkeypatch, key, {"id -un": "root\n", "pveum": ExecResult(rc=127, stdout="", stderr="pveum: not found")}
    )
    r = _connect(client, {"kind": "proxmox", "name": "pve", "address": "10.0.0.3"})
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
    assert ob.parse_pve(PVE_OUT) == ("secret-uuid", "ab" * 32, False)
    assert ob.parse_pve("__CUSTOM__=1\n" + PVE_OUT)[2]
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
@pytest.mark.parametrize(
    ("owner", "env", "expected"),
    [
        (1000, {}, 1000),  # your own folder stays yours
        (0, {}, 10001),  # a folder Docker created as root goes to the app user
        (1000, {"PUID": "12345"}, 12345),
        (1000, {"PUID": "0"}, None),  # rootless Docker/Podman: stay root
    ],
)
def test_drop_privileges_ownership(tmp_path, monkeypatch, owner, env, expected):
    data = tmp_path / "data"
    (data / "keys").mkdir(parents=True)
    (data / "keys" / "f").write_text("x")
    os.chown(data, owner, owner)
    os.chmod(data, 0o755)
    monkeypatch.setenv("HOMELAB_DATA", str(data))
    monkeypatch.delenv("PUID", raising=False)
    monkeypatch.delenv("PGID", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    switched = []
    monkeypatch.setattr(os, "setgroups", lambda g: switched.append(("groups", g)))
    monkeypatch.setattr(os, "setgid", lambda g: switched.append(("gid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: switched.append(("uid", u)))
    monkeypatch.setitem(os.environ, "HOME", os.environ.get("HOME", "/root"))
    drop_privileges()
    if expected is None:
        assert switched == []
        return
    for p in (data, data / "keys", data / "keys" / "f"):
        assert (p.stat().st_uid, p.stat().st_gid) == (expected, expected)
    assert switched == [("groups", []), ("gid", expected), ("uid", expected)]
    assert oct(data.stat().st_mode & 0o777) == "0o755"  # the folder's mode is left alone


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


def test_review_fixes(configured, monkeypatch):
    client, tmp_path = configured
    # A ":" in the username could never log in with basic auth.
    with pytest.raises(ValueError, match="':'"):
        from homelab.config import AuthConfig

        AuthConfig(username="a:b", password="x")
    # The Incus API is opened only on the address the dashboard uses, when that is an IP.
    assert ob.incus_listen("10.0.0.5") == "10.0.0.5"
    assert ob.incus_listen("fd00::5") == "[fd00::5]"
    assert ob.incus_listen("incus.lan") == ""
    assert 'addr="10.0.0.5:$p"' in ob.incus_script("pem", "fp", ob.incus_listen("10.0.0.5"))

    # Removing a machine forgets its pinned host key, unless another entry still uses that address.
    _fake_machine(monkeypatch, asyncssh.generate_private_key("ssh-ed25519"), {"id -un": "root\n"})
    for name in ("a", "b"):
        assert _connect(client, {"kind": "host", "name": name, "address": "10.0.0.9"})["ok"]
    kh = tmp_path / "keys" / "known_hosts"
    client.delete("/api/setup/machines/a")
    assert "10.0.0.9 " in kh.read_text()
    client.delete("/api/setup/machines/b")
    assert "10.0.0.9 " not in kh.read_text()

    # A hand-broken config is reported, not a server error.
    (tmp_path / "config.yaml").write_text("hosts: [\n")
    assert client.get("/api/setup").status_code == 200
    assert client.delete("/api/setup/machines/x").status_code == 400
    body = {"kind": "host", "name": "c", "address": "10.0.0.9"}
    fp = client.post("/api/setup/connect", json=body).json()["fingerprint"]
    r = client.post("/api/setup/connect", json={**body, "trust_fingerprint": fp})
    assert r.status_code == 400 and "not valid YAML" in r.json()["detail"]


def test_identity_is_created_once_under_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    paths = ob.Paths(tmp_path / "config.yaml")
    with ThreadPoolExecutor(8) as pool:
        keys = set(pool.map(lambda _: ob.ensure_identity(paths), range(16)))
    assert len(keys) == 1
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    cert = x509.load_pem_x509_certificate(paths.incus_cert.read_bytes())
    key = load_pem_private_key(paths.incus_key.read_bytes(), None)
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert sorted(p.name for p in paths.keys.iterdir()) == [
        "id_ed25519",
        "id_ed25519.pub",
        "incus-client.crt",
        "incus-client.key",
    ]


PI = ob.Machine(kind="host", name="pi", address="10.0.0.2")


def test_bootstrap_checks_before_changing_anything():
    script = ob.bootstrap_source(PI, "ssh-ed25519 AAAA x", [])
    changes = script.index("# --- changes ---")
    for check in ("wasn't made by this script", "AuthenticationMethods", "AllowUsers", "includedir", "FIPS"):
        assert script.index(check) < changes
    # Changes only after the checks: nothing is installed, created or written before.
    for change in ("apt-get install -y", "useradd", "usermod -p", 'authorized_keys"', "mktemp /etc/sudoers.d"):
        assert script.index(change) > changes, change


def _run_checks(tmp_path, sshd_out="", user_exists=False):
    """Run the setup script with stub `id` and `sshd`, as root as far as it can tell; it must stop in the checks."""
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    (stubs / "id").write_text(
        f'#!/bin/sh\ncase "$1" in -u) echo 0 ;; homelab) exit {0 if user_exists else 1} ;; *) exit 1 ;; esac\n'
    )
    (stubs / "sshd").write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(sshd_out)}\n")
    for f in stubs.iterdir():
        f.chmod(0o755)
    script = ob.bootstrap_source(PI, "ssh-ed25519 AAAA x", []).replace("# --- changes ---", "exit 99\n")
    script = script.replace("/etc/sudoers.d/homelab-manager", str(tmp_path / "none"))
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
    return subprocess.run(["sh"], input=script, text=True, capture_output=True, env=env)


@pytest.mark.parametrize(
    ("sshd_out", "user_exists", "message"),
    [
        ("", True, "wasn't made by this script"),
        ("authenticationmethods publickey,password", False, "Match User homelab"),
        ("authorizedkeysfile /etc/ssh/keys/%u", False, "not ~/.ssh/authorized_keys"),
        ("allowusers alice\nallowusers bob", False, "Add homelab to AllowUsers"),
        ("denyusers homelab", False, "DenyUsers lists homelab"),
    ],
)
def test_bootstrap_stops_on_problems(tmp_path, sshd_out, user_exists, message):
    r = _run_checks(tmp_path, sshd_out, user_exists)
    assert r.returncode == 1 and message in r.stderr and "nothing was changed" in r.stderr


def test_bootstrap_accepts_usual_sshd_settings(tmp_path):
    ok = (
        "authenticationmethods any\nauthorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\n"
        "allowusers me homelab"
    )
    r = _run_checks(tmp_path, ok)
    # Past the sshd checks: it ends in the sudo check (whatever this machine has) or at the changes.
    assert "sshd" not in r.stderr and (r.returncode == 99 or "sudo" in r.stderr), r.stderr


def test_wrapper_runs_from_any_shell():
    line = ob.wrap('echo "ran as $(id -u)"')
    for shell in ("sh", "bash", "zsh", "fish", "csh", "tcsh"):
        if shutil.which(shell) and (os.getuid() == 0 or shutil.which("sudo")):
            r = subprocess.run([shell, "-c", line], capture_output=True, text=True, timeout=30)
            assert r.stdout.strip() == "ran as 0", (shell, r.stderr)


def test_uninstall_script(configured):
    client, _ = configured
    src = ob.uninstall_source("proxmox", {"name": "pve", "token_id": "homelab@pve!hm-pve-20261009101010"}, "")
    assert "grep -Ex 'hm-pve(-[0-9]{14})?'" in src and "pveum role delete" in src
    tun = {"name": "vps", "tunnel": {"peer_address": "10.99.2.1", "port": 51822, "target_port": 22}}
    for kind, entry in (("proxmox", {"name": "pve"}), ("incus", {"name": "i"}), ("host", tun)):
        src = ob.uninstall_source(kind, entry, "ab" * 32)
        assert subprocess.run(["sh", "-n"], input=src, text=True).returncode == 0
        assert src.index('rm -f "$SUDOERS"') > src.index("U=homelab")
    assert "wg-quick down hm2" in ob.uninstall_source("host", tun, "")
    assert client.get("/api/setup/machines/nope/uninstall").status_code == 404


def test_stale_pve_tokens():
    listed = json.dumps([{"tokenid": t} for t in ("hm-pve", "hm-pve-20261009101010", "hm-pve-2", "hm-pve-x", "other")])
    assert ob.stale_pve_tokens("pve", listed, {"hm-pve-20261009101010"}) == ["hm-pve"]
    assert ob.stale_pve_tokens("pve", "not json", set()) == []
    assert ob.pve_token("pve").startswith("hm-pve-") and len(ob.pve_token("pve")) == len("hm-pve-") + 14


def test_cluster_nodes_are_pinned_and_checked(configured, monkeypatch):
    client, tmp_path = configured
    node2 = asyncssh.generate_private_key("ssh-rsa")
    cluster = json.dumps(
        [
            {"type": "cluster", "name": "lab"},
            {"type": "node", "name": "pve1", "ip": "10.0.1.1", "local": 1},
            {"type": "node", "name": "pve2", "ip": "10.0.1.2", "local": 0},
            {"type": "node", "name": "pve3", "ip": "10.0.1.3", "local": 0},
        ]
    )
    out = PVE_OUT + f"__CLUSTER__={cluster}\n__NODEKEY__=pve2 pve2 {node2.export_public_key().decode().strip()}\n"
    _fake_machine(monkeypatch, asyncssh.generate_private_key("ssh-ed25519"), {"id -un": "root\n", "pveum": out})
    r = _connect(client, {"kind": "proxmox", "name": "pve1", "address": "10.0.1.1"})
    assert r["ok"], r
    assert any("pve2 (10.0.1.2): reachable" in line for line in r["log"])
    assert any("pve3 (10.0.1.3): no SSH host key" in line for line in r["log"])
    assert "10.0.1.2 ssh-rsa " in (tmp_path / "keys" / "known_hosts").read_text()


async def test_unresolvable_name():
    with pytest.raises(ob.SetupError, match="enter its IP"):
        await ob.fetch_host_key("no-such-host.invalid", 22)


def test_zone_ids_rejected():
    with pytest.raises(ValueError, match="zone"):
        ob.Machine(kind="host", name="a", address="fe80::1%eth0")


def test_port_free_check_runs_here():
    """The shell port check finds a port that is in use, and passes a free one."""
    import socket as so

    with so.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        used = srv.getsockname()[1]
        script = ob.PORT_FREE_SH + f"port_free tcp {used} && echo free || echo used; port_free tcp 1 && echo free\n"
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True).stdout.split()
    assert out == ["used", "free"]


def test_script_download_link(configured):
    client, _ = configured
    m = {"kind": "host", "name": "pi4", "address": "192.168.1.30"}
    r = client.post("/api/setup/script", json=m).json()
    assert r["download"]["path"].startswith("/install/")
    # The machine fetches it without a login; it runs the same script.
    anon = TestClient(client.app)
    got = anon.get(r["download"]["path"])
    assert hashlib.sha256(got.content).hexdigest() == r["download"]["sha256"]
    assert got.status_code == 200 and base64.b64decode(got.text.split("S=")[1].split()[0]).decode() == r["source"]
    assert subprocess.run(["sh", "-n"], input=got.text, text=True).returncode == 0
    assert anon.get("/install/not-a-real-link").status_code == 404
    assert anon.get("/api/setup").status_code == 401
    # A tunnel's script holds keys: no link.
    tun = client.post("/api/setup/script", json={**m, "name": "vps", "tunnel": True}).json()
    assert tun["download"] is None


def test_downloaded_script_runs_and_removes_itself(tmp_path):
    f = tmp_path / "homelab-setup.sh"
    f.write_text(ob.download('echo "ran as $(id -u)"'))
    if os.getuid() != 0 and not shutil.which("sudo"):
        pytest.skip("needs root or sudo")
    r = subprocess.run(["sh", str(f)], capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "ran as 0" and not f.exists()

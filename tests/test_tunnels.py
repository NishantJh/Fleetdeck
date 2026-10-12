import subprocess
from pathlib import Path

import asyncssh
import pytest
from fastapi.testclient import TestClient
from test_setup import PASSWORD, _admin, _fake_machine

import fleetdeck.onboarding as ob
import fleetdeck.tunnels as tun
from fleetdeck.app import create_app
from fleetdeck.config import load_config


def test_pending_keeps_keys_and_picks_free_subnets(tmp_path):
    a = tun.pending(tmp_path, "vps", "vps.example.com", 51821, 22, used=set())
    again = tun.pending(tmp_path, "vps", "vps.example.com", 51822, 2222, used=set())
    assert again["index"] == a["index"] == 1 and again["private_key"] == a["private_key"]
    assert (again["port"], again["target_port"]) == (51822, 2222)
    b = tun.pending(tmp_path, "vps2", "vps2.example.com", 51821, 22, used={2})
    assert b["index"] == 3  # 1 is pending for vps, 2 is used by a connected machine
    t = tun.to_config(b)
    assert (t.address, t.peer_address, t.local_port) == ("10.99.3.2", "10.99.3.1", 22003)
    assert "peer_private_key" not in t.model_dump()
    conf = tun.wireproxy_conf(t)
    assert "Endpoint = vps2.example.com:51821" in conf and "Target = 10.99.3.1:22" in conf
    assert "BindAddress = 127.0.0.1:22003" in conf
    tun.forget(tmp_path, "vps")
    assert tun.pending(tmp_path, "vps3", "x.example.com", 51821, 22, used=set())["index"] == 1


def test_tunnel_script(tmp_path):
    data = tun.pending(tmp_path, "vps", "vps.example.com", 51821, 22, used=set())
    m = ob.Machine(kind="host", name="vps", address="vps.example.com", tunnel=True)
    script = ob.bootstrap_source(m, "ssh-ed25519 AAAA x", ["192.168.1.0/24"], data)
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    # The key only works through the tunnel, and the tunnel part runs before anything is changed.
    assert 'restrict,from="10.99.1.2/32" ssh-ed25519' in script
    assert script.index("port_free udp 51821") < script.index("# --- changes ---") < script.index('cat > "$WG_CONF"')
    assert data["peer_private_key"] in script and data["public_key"] in script
    assert "ListenPort = 51821" in script and "Address = 10.99.1.1/30" in script
    with pytest.raises(ValueError, match="Linux hosts"):
        ob.Machine(kind="proxmox", name="pve", address="10.0.0.1", tunnel=True)


def test_connect_through_tunnel(tmp_path, monkeypatch):
    path = str(tmp_path / "config.yaml")
    restarts = []
    app = create_app(load_config(path), config_file=path, restart=lambda: restarts.append(1))
    with TestClient(app) as client:
        client.headers["X-Setup-Token"] = app.state.setup_token
        _admin(client, restarts)
    started = []
    monkeypatch.setattr(tun.TunnelManager, "ensure", lambda self, name, t, wait=5.0: started.append((name, t)))
    _fake_machine(monkeypatch, asyncssh.generate_private_key("ssh-ed25519"), {"id -un": "root\n"})
    app = create_app(load_config(path), config_file=path)
    with TestClient(app) as client:
        client.auth = ("me", PASSWORD)
        body = {"kind": "host", "name": "vps", "address": "203.0.113.7", "port": 2222, "tunnel": True}
        assert "hm1" in client.post("/api/setup/script", json=body).json()["source"]
        r = client.post("/api/setup/connect", json=body).json()
        assert r["needs_confirm"] and "203.0.113.7 presents" in r["log"][0]
        r = client.post("/api/setup/connect", json={**body, "trust_fingerprint": r["fingerprint"]}).json()
        assert r["ok"] and "tunnel" in r["log"][-1]
        assert started[-1][0] == "vps" and started[-1][1].local_port == 22001
        cfg = load_config(path)
        h = cfg.hosts[0]
        assert (h.host, h.port, h.tunnel.endpoint, h.tunnel.target_port) == ("127.0.0.1", 22001, "203.0.113.7", 2222)
        assert client.get("/api/setup").json()["machines"] == [
            {"kind": "host", "name": "vps", "address": "203.0.113.7", "port": 2222, "tunnel": True, "udp_port": 51821}
        ]
        assert client.delete("/api/setup/machines/vps").status_code == 200
        assert not (tmp_path / "keys" / "tunnels" / "vps.json").exists()


def test_manager_runs_and_stops_a_process(tmp_path, monkeypatch):
    """With a stand-in for wireproxy that just listens on the local port."""
    fake = tmp_path / "fake-wireproxy"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import socket, sys, time\n"
        "conf = open(sys.argv[-1]).read()\n"
        "port = int(conf.split('BindAddress = 127.0.0.1:')[1].split()[0])\n"
        "s = socket.socket(); s.bind(('127.0.0.1', port)); s.listen(); time.sleep(60)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(tun, "WIREPROXY", str(fake))
    data = tun.pending(tmp_path, "vps", "vps.example.com", 51821, 22, used=set())
    t = tun.to_config(data).model_copy(update={"local_port": 0})
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        t = t.model_copy(update={"local_port": s.getsockname()[1]})
    m = tun.TunnelManager(tmp_path)
    m.ensure("vps", t)
    assert not tun.local_port_free(t.local_port)
    proc = m._procs["vps"][1]
    m.ensure("vps", t)  # unchanged: left running
    assert m._procs["vps"][1] is proc
    m.sync({})
    assert proc.poll() is not None and not m._procs
    assert oct(Path(tmp_path / "vps.wireproxy.conf").stat().st_mode & 0o777) == "0o600"


def test_tunnels_typed_on_the_way_are_dropped(tmp_path):
    for name in ("v", "vp", "vps"):
        tun.pending(tmp_path, name, "203.0.113.7", 51821, 22, used=set())
    assert sorted(p.stem for p in tun.tunnel_dir(tmp_path).iterdir()) == ["vps"]
    # A connected machine's tunnel stays, whatever else is typed.
    tun.pending(tmp_path, "other", "203.0.113.7", 51821, 22, used=set(), connected=frozenset({"vps"}))
    assert sorted(p.stem for p in tun.tunnel_dir(tmp_path).iterdir()) == ["other", "vps"]


def test_tunnel_checks_come_first(tmp_path):
    data = tun.pending(tmp_path, "vps", "vps.example.com", 51821, 22, used=set())
    m = ob.Machine(kind="host", name="vps", address="vps.example.com", tunnel=True)
    script = ob.bootstrap_source(m, "ssh-ed25519 AAAA x", [], data)
    changes = script.index("# --- changes ---")
    for check in ("type wireguard", "listenaddress", "port_free udp"):
        assert script.index(check) < changes
    assert "|| tunnel_rollback" in script[changes:]

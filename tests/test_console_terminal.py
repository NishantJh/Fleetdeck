import base64
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fleetdeck import console_routes
from fleetdeck.app import create_app
from fleetdeck.config import AppConfig, AuthConfig, ConsoleConfig, SSHConfig
from fleetdeck.executors import PctExecutor, PtySession, SSHExecutor

BASIC = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
ORIGIN = {"origin": "http://testserver"}


def _client(client=("192.168.1.20", 50000), **kw) -> TestClient:
    cfg = AppConfig(
        demo=True, auth=AuthConfig(username="admin", password="s3cret"), console=ConsoleConfig(enabled=True, **kw)
    )
    return TestClient(create_app(cfg), headers=BASIC, client=client)


def _setup(c: TestClient):
    token = c.post("/api/console/unlock", json={"password": "s3cret"}).json()["token"]
    target = next(t["target"]["id"] for t in c.get("/api/targets").json()["targets"] if t["target"]["name"] == "pi4")
    return token, target


def _hello(token, target, **extra):
    return json.dumps({"token": token, "target": target, "cols": 80, "rows": 24, **extra})


def _closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        ws.receive_bytes()
    return e.value.code


def test_terminal_in_the_demo_echoes_and_is_audited():
    with _client() as c:
        token, target = _setup(c)
        assert target in c.get("/api/console").json()["terminals"]
        with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as ws:
            ws.send_text(_hello(token, target))
            assert b"Demo shell on pi4" in ws.receive_bytes()
            ws.send_bytes(b"ls\r")
            seen = b""
            while b"(demo) ls" not in seen:
                seen += ws.receive_bytes()
            ws.send_text(json.dumps({"resize": [100, 30]}))
        rows = c.get("/api/console/audit?kind=terminal", headers={"X-Console-Token": token}).json()["rows"]
        assert rows[0]["target"] == "pi4" and rows[0]["status"] == "closed" and rows[0]["http"] is True
        assert not c.app.state.console.terminals


def test_terminal_in_a_container():
    with _client() as c:
        token, target = _setup(c)
        h = {"X-Console-Token": token}
        box = c.get(f"/api/targets/{target}/containers", headers=h).json()["containers"][0]
        with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as ws:
            ws.send_text(_hello(token, target, container=box["id"]))
            assert f"Demo shell on {box['name']}".encode() in ws.receive_bytes()
        assert c.app.state.console.audit_rows(1, "terminal")[0]["container"] == box["name"]


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda t, tg: {"token": "nope"}, "locked"),
        (lambda t, tg: {"target": "nope"}, "unknown machine"),
        (lambda t, tg: {"container": "x; rm -rf"}, "bad container id"),
        (lambda t, tg: {"container": "deadbeef0000"}, "not on this machine"),
    ],
)
def test_terminal_refusals(mutate, reason):
    with _client() as c:
        token, target = _setup(c)
        msg = {"token": token, "target": target, "cols": 80, "rows": 24, **mutate(token, target)}
        with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as ws:
            ws.send_text(json.dumps(msg))
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_bytes()
        assert e.value.code == 4403 and reason in e.value.reason


def test_terminal_needs_our_origin():
    with _client() as c:
        for headers in ({}, {"origin": "http://evil.example"}):
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect("/api/console/terminal", headers=headers):
                    pass


def test_terminal_gates():
    with _client(terminal=False) as c:
        token, target = _setup(c)
        assert c.get("/api/console").json()["terminals"] == []
        with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as ws:
            ws.send_text(_hello(token, target))
            assert _closed_with(ws) == 4403
    with _client(client=("8.8.8.8", 1)) as c:
        with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as ws:
            ws.send_text(_hello("x", "y"))
            assert _closed_with(ws) == 4403
    with _client(allow_plain_http=False, client=("127.0.0.1", 1)) as c:  # loopback is secure
        token, target = _setup(c)
        with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as ws:
            ws.send_text(_hello(token, target))
            assert b"Demo shell" in ws.receive_bytes()


def test_at_most_two_terminals_at_once():
    with _client() as c:
        token, target = _setup(c)
        with (
            c.websocket_connect("/api/console/terminal", headers=ORIGIN) as a,
            c.websocket_connect("/api/console/terminal", headers=ORIGIN) as b,
        ):
            for ws in (a, b):
                ws.send_text(_hello(token, target))
                assert b"Demo shell" in ws.receive_bytes()
            with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as third:
                third.send_text(_hello(token, target))
                with pytest.raises(WebSocketDisconnect) as e:
                    third.receive_bytes()
                assert "2 terminals" in e.value.reason


def test_idle_terminal_is_closed(monkeypatch):
    monkeypatch.setattr(console_routes, "TERMINAL_IDLE", 0.2)
    monkeypatch.setattr(console_routes, "WATCH_EVERY", 0.1)
    with _client() as c:
        token, target = _setup(c)
        with c.websocket_connect("/api/console/terminal", headers=ORIGIN) as ws:
            ws.send_text(_hello(token, target))
            ws.receive_bytes()
            with pytest.raises(WebSocketDisconnect) as e:
                while True:
                    ws.receive_bytes()
        assert e.value.code == 4000 and "without activity" in e.value.reason


def test_terminal_page_has_its_own_policy():
    with _client() as c:
        r = c.get("/terminal")
        assert r.status_code == 200 and "style-src 'self' 'unsafe-inline'" in r.headers["content-security-policy"]
        assert "unsafe-inline" not in c.get("/").headers["content-security-policy"]


class _Proc:
    def __init__(self):
        self.closed = False
        self.size = None

    def change_terminal_size(self, cols, rows):
        self.size = (cols, rows)

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self):
        self.proc_args = None
        self.proc = _Proc()
        self.closed = False

    async def create_process(self, command, **kw):
        self.proc_args = (command, kw)
        return self.proc

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


async def test_ssh_terminal_asks_for_a_pty_and_a_root_shell(monkeypatch):
    conn = _Conn()

    async def connect(**kw):
        return conn

    monkeypatch.setattr("fleetdeck.executors.asyncssh.connect", connect)
    ssh = SSHExecutor(SSHConfig(host="10.0.0.2", user="homelab", sudo=True, insecure_skip_host_key_check=True))
    session = await ssh.open_terminal(100, 30)
    command, kw = conn.proc_args
    assert command == "sudo -n -i" and kw["term_size"] == (100, 30) and kw["term_type"] == "xterm-256color"
    assert isinstance(session, PtySession)
    session.resize(120, 40)
    assert conn.proc.size == (120, 40)
    await session.close()
    assert conn.proc.closed and conn.closed

    await ssh.open_terminal(80, 24, "docker exec -it abc sh")
    assert conn.proc_args[0] == "sudo -n docker exec -it abc sh"

    root = SSHExecutor(SSHConfig(host="10.0.0.2", user="root", insecure_skip_host_key_check=True))
    await root.open_terminal(80, 24)
    assert conn.proc_args[0] is None  # the login shell


async def test_pct_terminal_enters_the_container(monkeypatch):
    conn = _Conn()

    async def connect(**kw):
        return conn

    monkeypatch.setattr("fleetdeck.executors.asyncssh.connect", connect)
    node = SSHExecutor(SSHConfig(host="10.0.0.2", user="homelab", sudo=True, insecure_skip_host_key_check=True))
    await PctExecutor(node, 101).open_terminal(80, 24)
    assert conn.proc_args[0] == "sudo -n pct enter 101"
    await PctExecutor(node, 101).open_terminal(80, 24, "docker exec -it abc sh")
    assert conn.proc_args[0] == "sudo -n pct exec 101 -- sh -c 'docker exec -it abc sh'"

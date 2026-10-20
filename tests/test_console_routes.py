import base64
import time

import pytest
from fastapi.testclient import TestClient

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig, AuthConfig, ConsoleConfig, HostConfig

AUTH = AuthConfig(username="admin", password="s3cret")
BASIC = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}


def _client(console=True, client=("192.168.1.20", 50000), **kw) -> TestClient:
    cfg = AppConfig(demo=True, auth=AUTH, console=ConsoleConfig(enabled=console, **kw))
    return TestClient(create_app(cfg), headers=BASIC, client=client)


def _unlock(c: TestClient) -> dict:
    r = c.post("/api/console/unlock", json={"password": "s3cret"})
    assert r.status_code == 200
    return {"X-Console-Token": r.json()["token"]}


def _host(c: TestClient) -> str:
    return next(t["target"]["id"] for t in c.get("/api/targets").json()["targets"] if t["target"]["name"] == "pi4")


def _finish(c: TestClient, h: dict, run_id: str) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        r = c.get(f"/api/console/runs/{run_id}", headers=h).json()
        if r["status"] != "running":
            return r
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_off_by_default_means_no_routes():
    with _client(console=False) as c:
        assert c.get("/api/console").status_code == 404
        assert c.post("/api/console/run", json={}).status_code in (404, 405)
    with TestClient(create_app(AppConfig(demo=True, auth=AUTH)), headers=BASIC) as c:
        assert c.get("/api/console").status_code == 404


def test_stays_off_without_a_login_on_real_machines(caplog):
    cfg = AppConfig(
        hosts=[HostConfig(name="pi", host="10.0.0.2")], allow_no_auth=True, console=ConsoleConfig(enabled=True)
    )
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/console").status_code == 404
    assert "no `auth:` login" in caplog.text


def test_state_and_network_gate():
    with _client() as c:
        s = c.get("/api/console").json()
        assert s["allowed"] and not s["unlocked"] and s["warning"]  # plain HTTP warning
    with _client(client=("8.8.8.8", 1)) as c:
        assert c.get("/api/console").json()["allowed"] is False
        assert c.post("/api/console/unlock", json={"password": "s3cret"}).status_code == 403


def test_plain_http_can_be_blocked():
    with _client(allow_plain_http=False) as c:
        assert c.get("/api/console").json()["allowed"] is False
        assert c.post("/api/console/unlock", json={"password": "s3cret"}).status_code == 403
    with _client(allow_plain_http=False, client=("127.0.0.1", 1)) as c:  # loopback counts as secure
        assert c.post("/api/console/unlock", json={"password": "s3cret"}).status_code == 200


def test_locked_until_unlocked_and_wrong_passwords_are_throttled():
    with _client() as c:
        t = _host(c)
        assert c.post("/api/console/run", json={"target": t, "command": "uptime"}).status_code == 401
        for i in range(10):
            r = c.post("/api/console/unlock", json={"password": f"wrong{i}"})
        assert r.status_code in (403, 429)
        assert c.post("/api/console/unlock", json={"password": "s3cret"}).status_code == 429
        assert "unlock-failed" in [x["kind"] for x in c.app.state.console.audit_rows(20)]


def test_run_a_command_in_the_demo_and_audit_it():
    with _client() as c:
        h = _unlock(c)
        t = _host(c)
        r = c.post("/api/console/run", headers=h, json={"target": t, "command": "uptime"})
        assert r.status_code == 200
        done = _finish(c, h, r.json()["id"])
        assert done["status"] == "done" and done["rc"] == 0 and "load average" in "".join(done["lines"])
        rows = c.get("/api/console/audit?kind=run", headers=h).json()["rows"]
        assert rows[0]["command"] == "uptime" and rows[0]["user"] == "admin" and rows[0]["http"] is True
        assert c.post("/api/console/lock", headers=h).status_code == 200
        assert c.get(f"/api/console/runs/{r.json()['id']}", headers=h).status_code == 401


def test_guarded_command_needs_the_machine_name():
    with _client() as c:
        h = _unlock(c)
        t = _host(c)
        body = {"target": t, "command": "reboot"}
        r = c.post("/api/console/run", headers=h, json=body)
        assert r.status_code == 409 and "pi4" in r.json()["detail"]
        assert c.post("/api/console/run", headers=h, json={**body, "confirm": "nope"}).status_code == 409
        r = c.post("/api/console/run", headers=h, json={**body, "confirm": "pi4"})
        assert r.status_code == 200
        _finish(c, h, r.json()["id"])
        assert c.app.state.console.audit_rows(1, "run")[0]["guarded"] == ["power off or reboot"]


def test_containers_listed_and_checked():
    with _client() as c:
        h = _unlock(c)
        t = _host(c)
        found = c.get(f"/api/targets/{t}/containers", headers=h).json()["containers"]
        assert {x["name"] for x in found} == {"nginx", "postgres"}
        ok = c.post("/api/console/run", headers=h, json={"target": t, "command": "ls", "container": found[0]["id"]})
        assert ok.status_code == 200
        _finish(c, h, ok.json()["id"])
        assert c.app.state.console.audit_rows(1, "run")[0]["container"] == found[0]["name"]
        unknown = {"target": t, "command": "ls", "container": "deadbeef0000"}
        assert c.post("/api/console/run", headers=h, json=unknown).status_code == 404
        injected = {"target": t, "command": "ls", "container": "x; rm"}
        assert c.post("/api/console/run", headers=h, json=injected).status_code == 400


def test_excluded_machine_and_bad_input():
    with _client(exclude=["pi4"]) as c:
        h = _unlock(c)
        t = _host(c)

        def run(**body):
            return c.post("/api/console/run", headers=h, json={"target": t, "command": "ls", **body}).status_code

        assert run() == 403
        assert run(target="nope") == 404
        assert run(command="") == 422
        assert run(command="x" * 20000) == 422
        assert run(timeout=99999) == 422


def test_one_command_per_machine_at_a_time():
    with _client() as c:
        h = _unlock(c)
        t = _host(c)
        first = c.post("/api/console/run", headers=h, json={"target": t, "command": "uptime"})
        assert first.status_code == 200
        assert c.post("/api/console/run", headers=h, json={"target": t, "command": "uptime"}).status_code == 429
        _finish(c, h, first.json()["id"])


def test_cross_site_post_is_refused():
    with _client() as c:
        r = c.post("/api/console/unlock", json={"password": "s3cret"}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403


@pytest.mark.parametrize("path", ["/api/console/audit", "/api/console/runs/x"])
def test_reads_need_the_unlock_too(path):
    with _client() as c:
        assert c.get(path).status_code == 401

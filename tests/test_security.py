import pytest
from fastapi.testclient import TestClient
from test_jobs import _finish, _inventory, _target

from homelab.app import create_app
from homelab.config import AppConfig, AuthConfig, HostConfig
from homelab.inventory import Inventory
from homelab.jobs import JobManager
from homelab.models import ExecResult
from homelab.security import LoginThrottle

PI = HostConfig(name="pi", host="10.0.0.2")


def test_refuses_to_start_without_auth_for_real_machines():
    with pytest.raises(SystemExit, match="auth"):
        create_app(AppConfig(hosts=[PI]))
    create_app(AppConfig(hosts=[PI], allow_no_auth=True))
    create_app(AppConfig(demo=True))  # nothing real to protect
    create_app(AppConfig(hosts=[PI], auth=AuthConfig(password="x")))


def test_allowed_hosts_blocks_dns_rebinding():
    with TestClient(create_app(AppConfig(demo=True, allowed_hosts=["homelab.lan"]))) as client:
        assert client.get("/api/jobs", headers={"Host": "evil.example"}).status_code == 400
        assert client.get("/api/jobs", headers={"Host": "homelab.lan"}).status_code == 200


def test_security_headers_on_every_response():
    with TestClient(create_app(AppConfig(demo=True, auth=AuthConfig(password="s3cret")))) as client:
        for r in (client.get("/api/jobs"), client.get("/", auth=("admin", "s3cret"))):
            assert r.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
            assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_cross_site_posts_are_refused():
    body = {"action": "check", "target_ids": []}
    with TestClient(create_app(AppConfig(demo=True))) as client:
        assert client.post("/api/actions", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/actions", json=body, headers={"Origin": "null"}).status_code == 403
        assert client.post("/api/actions", json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/api/actions", json=body, headers={"Origin": "http://testserver"}).status_code == 200
        assert client.post("/api/actions", json=body).status_code == 200  # curl and scripts send no Origin
        # Reads stay open to any origin (the browser's same-origin policy keeps the answer from other sites).
        assert client.get("/api/jobs", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_too_many_ids_rejected():
    with TestClient(create_app(AppConfig(demo=True))) as client:
        r = client.post("/api/actions", json={"action": "check", "target_ids": ["x"] * 1001})
        assert r.status_code == 422


def test_login_throttle(monkeypatch):
    import homelab.security

    now = {"t": 1000.0}
    monkeypatch.setattr(homelab.security.time, "monotonic", lambda: now["t"])
    t = LoginThrottle(max_failures=3, window=60, block=120)
    t.failed("a")
    t.failed("a")
    now["t"] += 61  # first two expire
    t.failed("a")
    t.failed("a")
    assert t.blocked_for("a") == 0
    t.failed("a")
    assert t.blocked_for("a") == 120 and t.blocked_for("b") == 0
    now["t"] += 121
    assert t.blocked_for("a") == 0


def test_login_throttle_bounded_memory():
    t = LoginThrottle(max_failures=100, max_ips=10)
    for i in range(50):
        t.failed(f"10.0.0.{i}")
    assert len(t._failures) <= 10


def test_app_blocks_after_failed_logins():
    with TestClient(create_app(AppConfig(demo=True, auth=AuthConfig(password="s3cret")))) as client:
        for _ in range(3):
            assert client.get("/api/jobs").status_code == 401  # no credentials: not counted
        for _ in range(10):
            assert client.get("/api/jobs", auth=("admin", "guess")).status_code == 401
        r = client.get("/api/jobs", auth=("admin", "s3cret"))
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


async def test_forced_refresh_is_rate_limited():
    calls = {"n": 0}

    class Counting:
        name = "c"

        async def discover(self):
            calls["n"] += 1
            return []

    inv = Inventory([Counting()])
    await inv.refresh(force=True)
    await inv.refresh(force=True)
    assert calls["n"] == 1
    inv.min_interval = 0
    await inv.refresh(force=True)
    assert calls["n"] == 2


async def test_job_log_is_capped(monkeypatch):
    import homelab.jobs

    monkeypatch.setattr(homelab.jobs, "MAX_LOG_LINES", 50)

    class Chatty:
        async def run(self, script, on_line=None, timeout=0):
            for i in range(1000):
                on_line("stdout", f"line {i}")
            return ExecResult(rc=0, stdout="__PM__=apt\n__REBOOT__=no\n")

    jobs = JobManager(_inventory((_target(), Chatty())))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert len(job.log) < 60
    assert any("truncated after 50 lines" in line for line in job.log)
    assert job.status == "succeeded"

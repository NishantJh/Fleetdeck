from fastapi.testclient import TestClient
from test_jobs import _finish, _inventory, _target

from homelab.app import create_app
from homelab.config import AppConfig, AuthConfig, HostConfig
from homelab.inventory import Inventory
from homelab.jobs import JobManager
from homelab.models import ExecResult
from homelab.security import LoginThrottle

PI = HostConfig(name="pi", host="10.0.0.2")


def test_locked_until_setup_without_auth_for_real_machines(tmp_path):
    app = create_app(AppConfig(hosts=[PI]), config_file=str(tmp_path / "config.yaml"))
    assert app.state.setup_token
    with TestClient(app) as client:
        assert client.get("/api/targets").status_code == 403
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/setup"
        assert client.get("/setup").status_code == 200
        assert client.get("/api/setup").status_code == 403
        assert client.get("/api/setup", headers={"X-Setup-Token": "wrong"}).status_code == 403
        assert client.get("/api/setup", headers={"X-Setup-Token": app.state.setup_token}).status_code == 200
    assert create_app(AppConfig(hosts=[PI], allow_no_auth=True)).state.setup_token is None
    assert create_app(AppConfig(demo=True)).state.setup_token is None  # nothing real to protect
    assert create_app(AppConfig(hosts=[PI], auth=AuthConfig(password="x"))).state.setup_token is None


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


def test_login_throttle_counts_a_resent_password_once():
    t = LoginThrottle(max_failures=3)
    for _ in range(20):  # the browser resends the same typo with every request
        t.failed("a", "Basic dHlwbw==")
    assert t.blocked_for("a") == 0
    t.failed("a", "Basic Z3Vlc3Mx")
    t.failed("a", "Basic Z3Vlc3My")
    assert t.blocked_for("a") > 0


def test_login_throttle_bounded_memory():
    t = LoginThrottle(max_failures=100, max_ips=10)
    for i in range(50):
        t.failed(f"10.0.0.{i}")
    assert len(t._failures) <= 10


def test_app_blocks_after_failed_logins():
    with TestClient(create_app(AppConfig(demo=True, auth=AuthConfig(password="s3cret")))) as client:
        for _ in range(3):
            assert client.get("/api/jobs").status_code == 401  # no credentials: not counted
        for i in range(10):
            assert client.get("/api/jobs", auth=("admin", f"guess{i}")).status_code == 401
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


def test_server_options_trust_forwarded_headers_only_from_proxies():
    from homelab.__main__ import server_options

    assert server_options(AppConfig(demo=True))["forwarded_allow_ips"] == "127.0.0.1"
    opts = server_options(AppConfig(demo=True, trusted_proxies=["10.0.0.2", "172.18.0.0/16"]))
    assert opts["proxy_headers"] is True and opts["forwarded_allow_ips"] == "10.0.0.2,172.18.0.0/16"


async def test_forwarded_for_is_ignored_unless_peer_is_trusted():
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["client"][0])

    app = ProxyHeadersMiddleware(inner, trusted_hosts="10.0.0.2")
    for peer in ("10.0.0.2", "10.0.0.9"):
        scope = {
            "type": "http", "client": (peer, 1234), "scheme": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.7")],
        }  # fmt: skip
        await app(scope, None, None)
    assert seen == ["203.0.113.7", "10.0.0.9"]

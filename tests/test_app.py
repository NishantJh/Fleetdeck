import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from homelab.app import create_app
from homelab.config import AppConfig
from homelab.models import ExecResult
from homelab.updates import CHECK_SCRIPT, UPGRADE_SCRIPT


def _wait_for_jobs(client: TestClient, timeout: float = 20) -> list[dict]:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get("/api/jobs").json()
        if jobs and all(j["status"] in ("succeeded", "failed") for j in jobs):
            return jobs
        time.sleep(0.2)
    raise AssertionError("jobs did not finish")


def test_demo_check_then_upgrade():
    with TestClient(create_app(AppConfig(demo=True))) as client:
        targets = client.get("/api/targets").json()["targets"]
        assert {t["target"]["kind"] for t in targets} == {"host", "lxc", "vm", "container"}
        pi = next(t for t in targets if t["target"]["name"] == "pi4")["target"]["id"]

        r = client.post("/api/actions", json={"action": "check", "target_ids": [pi, "nope"]}).json()
        assert r["rejected"] == {"nope": "unknown target"}
        _wait_for_jobs(client)
        check = next(t for t in client.get("/api/targets").json()["targets"] if t["target"]["id"] == pi)["check"]
        assert len(check["packages"]) == 3

        client.post("/api/actions", json={"action": "upgrade", "target_ids": [pi]})
        jobs = _wait_for_jobs(client)
        assert jobs[0]["status"] == "succeeded"
        log = client.get(f"/api/jobs/{jobs[0]['id']}").json()["log"]
        assert any("Setting up curl" in line for line in log)
        check = next(t for t in client.get("/api/targets").json()["targets"] if t["target"]["id"] == pi)["check"]
        assert check["packages"] == []


def test_basic_auth():
    from homelab.config import AuthConfig

    with TestClient(create_app(AppConfig(demo=True, auth=AuthConfig(password="s3cret")))) as client:
        assert client.get("/api/targets").status_code == 401
        assert client.get("/api/targets", auth=("admin", "s3cret")).status_code == 200
        assert client.get("/api/targets", auth=("admin", "wrong")).status_code == 401
        assert client.get("/api/targets", auth=("root", "s3cret")).status_code == 401
        for header in ("Basic !!!", "Bearer abc", "Basic", "Basic \u00e9".encode("latin-1")):
            assert client.get("/api/targets", headers={"Authorization": header}).status_code == 401
        # The scheme is case-insensitive.
        assert client.get("/api/targets", headers={"Authorization": "basic YWRtaW46czNjcmV0"}).status_code == 200


def test_job_query_validation():
    with TestClient(create_app(AppConfig(demo=True))) as client:
        assert client.get("/api/jobs?limit=-1").status_code == 422
        assert client.get("/api/jobs/x?since=-1").status_code == 422


@pytest.mark.skipif(shutil.which("apt-get") is None, reason="needs a Debian-family box")
def test_check_script_runs_locally():
    """Smoke-test the real script against this machine (read-only apart from apt-get update)."""
    from homelab.updates import parse_check

    p = subprocess.run(["sh", "-c", CHECK_SCRIPT], capture_output=True, text=True, timeout=300)
    res = parse_check(ExecResult(rc=p.returncode, stdout=p.stdout, stderr=p.stderr))
    assert res.package_manager == "apt"
    assert res.os


def test_scripts_are_valid_sh():
    for script in (CHECK_SCRIPT, UPGRADE_SCRIPT):
        assert subprocess.run(["sh", "-n", "-c", script]).returncode == 0


def test_reboot_detection_compares_kernels(tmp_path):
    from homelab.updates import REBOOT_SCRIPT

    if os.path.exists("/var/run/reboot-required"):
        pytest.skip("this machine already needs a reboot")
    running = os.uname().release
    flavour = running.rsplit("-", 1)[-1]

    def reboot(*dirs):
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        for name in dirs:
            (d / name).mkdir()
        env = {**os.environ, "MODULES_DIR": str(d)}
        return subprocess.run(["sh", "-c", REBOOT_SCRIPT], env=env, capture_output=True, text=True).stdout.strip()

    assert reboot() == "__REBOOT__=no"  # container: no kernels installed
    assert reboot(running) == "__REBOOT__=no"
    assert reboot(running, f"999.0.0-1-{flavour}") == "__REBOOT__=yes"
    assert reboot(running, "999.0.0-1-otherflavour") == "__REBOOT__=no"


def test_check_script_uses_documented_apk_and_apt_lock_options():
    assert "apk list -u" in CHECK_SCRIPT
    for script in (CHECK_SCRIPT, UPGRADE_SCRIPT):
        assert "DPkg::Lock::Timeout" in script
    assert "yum) yum -y upgrade ;;" in UPGRADE_SCRIPT  # yum has no --refresh

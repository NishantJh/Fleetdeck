import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from homelab.app import create_app
from homelab.config import AppConfig, AuthConfig, HostConfig
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


def test_demo_backup_only_on_hosts():
    with TestClient(create_app(AppConfig(demo=True))) as client:
        targets = {t["target"]["name"]: t["target"] for t in client.get("/api/targets").json()["targets"]}
        assert targets["pve"]["can_backup"] and not targets["pihole"]["can_backup"]
        r = client.post(
            "/api/actions", json={"action": "backup", "target_ids": [targets["pve"]["id"], targets["pihole"]["id"]]}
        )
        assert list(r.json()["rejected"]) == [targets["pihole"]["id"]]
        jobs = _wait_for_jobs(client)
        assert jobs[0]["status"] == "succeeded"
        log = client.get(f"/api/jobs/{jobs[0]['id']}").json()["log"]
        assert log[0] == "== Backing up pve" and log[-1] == "== Backup finished"


def test_demo_backup_folder_is_checked_and_remembered_per_host():
    with TestClient(create_app(AppConfig(demo=True))) as client:
        targets = {t["target"]["name"]: t["target"] for t in client.get("/api/targets").json()["targets"]}
        pve, pi = targets["pve"]["id"], targets["pi4"]["id"]
        bad = client.post("/api/actions", json={"action": "backup", "target_ids": [pve], "destination": "nas/../x"})
        assert bad.status_code == 400
        r = client.post("/api/actions", json={"action": "backup", "target_ids": [pve], "destination": "/mnt/nas"})
        assert r.json()["jobs"][0]["destination"] == "/mnt/nas"
        _wait_for_jobs(client)
        assert client.put("/api/backup-places", json={"target_id": pve, "path": "/mnt/nas"}).status_code == 200
        assert client.put("/api/backup-places", json={"target_id": "nope", "path": "/x"}).status_code == 404
        assert client.put("/api/backup-places", json={"target_id": pi, "path": "relative"}).status_code == 400
        assert client.get("/api/targets").json()["backup_places"] == {pve: "/mnt/nas"}
        client.put("/api/backup-places", json={"target_id": pve, "path": None})
        assert client.get("/api/targets").json()["backup_places"] == {}


def test_basic_auth():

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
    from homelab.updates import BOOT_ID_SCRIPT, REBOOT_NOW_SCRIPT

    for script in (CHECK_SCRIPT, UPGRADE_SCRIPT, BOOT_ID_SCRIPT, REBOOT_NOW_SCRIPT):
        assert subprocess.run(["sh", "-n", "-c", script]).returncode == 0


def test_boot_id_script_runs_locally():
    from homelab.updates import BOOT_ID_SCRIPT, parse_boot_id

    p = subprocess.run(["sh", "-c", BOOT_ID_SCRIPT], capture_output=True, text=True)
    boot = parse_boot_id(p.stdout)
    assert boot and boot.split("-")[-1].isdigit()  # boot_id plus PID 1's start time


def test_demo_reboot(monkeypatch):
    import homelab.jobs

    monkeypatch.setattr(homelab.jobs, "REBOOT_POLL", 0)
    with TestClient(create_app(AppConfig(demo=True))) as client:
        targets = client.get("/api/targets").json()["targets"]
        pve = next(t for t in targets if t["target"]["name"] == "pve")["target"]["id"]
        client.post("/api/actions", json={"action": "upgrade", "target_ids": [pve], "reboot_if_required": True})
        jobs = _wait_for_jobs(client)
        assert jobs[0]["status"] == "succeeded" and jobs[0]["reboot_if_required"]
        log = client.get(f"/api/jobs/{jobs[0]['id']}").json()["log"]
        assert "== Reboot finished" in log


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
    assert reboot(f"1.0.0-1-{flavour}") == "__REBOOT__=no"  # running kernel is newer than any installed


def test_check_script_uses_documented_apk_and_apt_lock_options():
    assert "apk list -u" in CHECK_SCRIPT
    for script in (CHECK_SCRIPT, UPGRADE_SCRIPT):
        assert "DPkg::Lock::Timeout" in script
    assert "yum) yum -y upgrade ;;" in UPGRADE_SCRIPT  # yum has no --refresh


def test_start_log_says_where_to_go_once_setup_is_done(caplog):
    # The one-time setup link is gone once a login exists; the log still says what to open.
    cfg = AppConfig(demo=True, auth=AuthConfig(username="nishant", password="s3cret"))
    with caplog.at_level("INFO", logger="homelab"), TestClient(create_app(cfg)):
        pass
    assert "Setup is done: open http://<this machine>:" in caplog.text and "'nishant'" in caplog.text
    assert "setup#token" not in caplog.text and "Setup code" not in caplog.text


def test_first_run_log_prints_the_setup_code(caplog, tmp_path):
    app = create_app(AppConfig(hosts=[HostConfig(name="pi", host="10.0.0.2")]), config_file=str(tmp_path / "c.yaml"))
    with caplog.at_level("INFO", logger="homelab"), TestClient(app):
        pass
    assert f"Setup code: {app.state.setup_token}" in caplog.text
    assert f"/setup#token={app.state.setup_token}" in caplog.text


def test_error_report_lists_errors_without_secrets():
    from datetime import UTC, datetime

    from homelab.config import NotifyConfig
    from homelab.models import CheckResult

    cfg = AppConfig(demo=True, notify=[NotifyConfig(type="gotify", url="https://g.example", token="tok-abc123")])
    app = create_app(cfg)
    with TestClient(app) as client:
        targets = client.get("/api/targets").json()["targets"]
        tid = targets[0]["target"]["id"]
        app.state.jobs.checks[tid] = CheckResult(
            checked_at=datetime.now(UTC), error="agent not running (sent tok-abc123)"
        )
        r = client.get("/api/report")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert targets[0]["target"]["name"] in r.text
        assert "agent not running (sent [removed])" in r.text
        assert "tok-abc123" not in r.text

import subprocess

from fastapi.testclient import TestClient
from test_jobs import FakeExecutor, _inventory, _target
from test_scheduler import PkgExecutor, RecordingNotifier, _noop_refresh

import homelab.app
from homelab.app import create_app
from homelab.config import AppConfig
from homelab.jobs import JobManager
from homelab.models import ExecResult
from homelab.scheduler import Scheduler
from homelab.store import Store
from homelab.updates import parse_check, self_script

CID = "0123456789abcdef" * 4


def _mountinfo(root: str) -> str:
    return (
        "22 1 0:21 / / rw,relatime - overlay overlay rw\n"
        f"585 566 259:1 {root} /etc/hostname rw,relatime - ext4 /dev/sda1 rw\n"
    )


def test_self_script_finds_the_container_folder_on_the_machine_that_runs_us(tmp_path):
    own = tmp_path / "containers" / CID
    script = self_script(_mountinfo(f"{own}/hostname"))
    run = lambda: subprocess.run(["sh", "-c", script], capture_output=True, text=True).stdout  # noqa: E731
    assert run() == ""  # another machine: no such folder
    own.mkdir(parents=True)
    assert run() == "__SELF__=yes\n"


def test_self_script_paths():
    docker = self_script(_mountinfo(f"/var/lib/docker/containers/{CID}/hostname"))
    assert f"/var/lib/docker/containers/{CID} " in docker
    podman = self_script(
        _mountinfo(f"/home/me/.local/share/containers/storage/overlay-containers/{CID}/userdata/hostname")
    )
    assert f"/home/me/.local/share/containers/storage/overlay-containers/{CID} " in podman
    # Not in a container, or nothing that looks like a container ID: nothing to look for.
    assert self_script("22 1 0:21 / / rw - ext4 /dev/sda1 rw\n") == ""
    assert self_script(_mountinfo("/etc/hostname")) == ""
    # Odd characters never reach the shell; the standard folders are still tried.
    odd = self_script(_mountinfo(f"/x;rm\\040-rf/{CID}/hostname"))  # mountinfo escapes spaces
    assert ";rm" not in odd and f"/var/lib/docker/containers/{CID}" in odd


def test_parse_check_reports_the_dashboards_machine():
    out = "__PM__=apt\n__OS__=Debian\n__BEGIN__\n__END__\n__REBOOT__=no\n"
    assert not parse_check(ExecResult(rc=0, stdout=out)).runs_dashboard
    assert parse_check(ExecResult(rc=0, stdout=out + "__SELF__=yes\n")).runs_dashboard


def test_hidden_survives_a_restart(tmp_path):
    inv = _inventory((_target("a"), FakeExecutor()), (_target("b"), FakeExecutor()))
    s = Store(tmp_path / "state.db")
    JobManager(inv, store=s).set_hidden(["a", "b"], True)
    JobManager(inv, store=s).set_hidden(["b"], False)
    s.close()
    assert JobManager(inv, store=Store(tmp_path / "state.db")).hidden == {"a"}


async def test_scheduled_checks_skip_hidden_machines(monkeypatch):
    import homelab.scheduler

    monkeypatch.setattr(homelab.scheduler, "POLL", 0)
    inv = _inventory((_target("pi4"), PkgExecutor(["curl"])), (_target("dash"), PkgExecutor(["docker-ce"])))
    inv.refresh = _noop_refresh
    jobs = JobManager(inv)
    jobs.set_hidden(["dash"], True)
    notifier = RecordingNotifier()
    await Scheduler(3600, inv, jobs, notifier).run_once()
    assert notifier.messages == [("Homelab: 1 machine needs attention", "pi4: 1 update")]
    assert "dash" not in jobs.checks


def test_hide_and_unhide_from_the_dashboard(monkeypatch):
    monkeypatch.setattr(homelab.app.socket, "gethostname", lambda: "Pi4.lan")
    with TestClient(create_app(AppConfig(demo=True))) as client:
        targets = {t["target"]["name"]: t for t in client.get("/api/targets").json()["targets"]}
        pi = targets["pi4"]
        # Named like the machine the dashboard runs on.
        assert pi["runs_dashboard"] and not pi["hidden"]
        assert not any(t["runs_dashboard"] for n, t in targets.items() if n != "pi4")

        r = client.post("/api/hidden", json={"target_ids": [pi["target"]["id"]]})
        assert r.json() == {"hidden": [pi["target"]["id"]]}
        assert next(t for t in client.get("/api/targets").json()["targets"] if t["target"]["name"] == "pi4")["hidden"]

        assert client.post("/api/hidden", json={"target_ids": ["nope"]}).status_code == 404
        r = client.post("/api/hidden", json={"target_ids": [pi["target"]["id"]], "hidden": False})
        assert r.json() == {"hidden": []}

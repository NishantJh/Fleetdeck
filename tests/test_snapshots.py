import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_app import _wait_for_jobs
from test_jobs import FakeExecutor, _finish, _inventory, _target

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig
from fleetdeck.jobs import JobManager
from fleetdeck.snapshots import PREFIX, IncusSnapshots, PveSnapshots, prune, snapshot_name


class FakeSnapshots:
    def __init__(self, existing=(), fail=None, warning=None):
        self.names = list(existing)
        self.fail = fail
        self.warning = warning
        self.rolled_back: list[str] = []

    async def create(self, name, description):
        if self.fail:
            raise RuntimeError(self.fail)
        self.names.append(name)
        return self.warning

    async def rollback(self, name):
        self.rolled_back.append(name)
        return self.warning

    async def list_ours(self):
        return [n for n in self.names if n.startswith(PREFIX)]

    async def delete(self, name):
        self.names.remove(name)


def _with_snapshots(inv, **snaps):
    inv.snapshots = dict(snaps)
    return inv


def test_snapshot_names_are_valid_for_proxmox():
    name = snapshot_name()
    assert name[0].isalpha() and len(name) <= 40 and all(c.isalnum() or c in "-_" for c in name)


async def test_prune_keeps_newest_and_ignores_other_snapshots():
    s = FakeSnapshots([f"{PREFIX}20260101_000000", "manual", f"{PREFIX}20260301_000000", f"{PREFIX}20260201_000000"])
    assert await prune(s, keep=2) == [f"{PREFIX}20260101_000000"]
    assert s.names == ["manual", f"{PREFIX}20260301_000000", f"{PREFIX}20260201_000000"]


async def test_upgrade_snapshots_first_and_can_roll_back():
    snaps = FakeSnapshots([f"{PREFIX}2000{i}" for i in range(3)])
    jobs = JobManager(_with_snapshots(_inventory((_target(), FakeExecutor())), t1=snaps), snapshot_keep=3)
    job = jobs.submit("t1", "upgrade", snapshot_first=True)
    await _finish(jobs)
    assert job.status == "succeeded" and job.snapshot and job.snapshot in snaps.names
    assert len(snaps.names) == 3  # oldest one pruned
    assert any("Deleted old snapshot" in line for line in job.log)

    rb = jobs.submit("t1", "rollback", snapshot=job.snapshot)
    await _finish(jobs)
    assert rb.status == "succeeded" and snaps.rolled_back == [job.snapshot]


async def test_failed_snapshot_stops_the_upgrade():
    class Recording(FakeExecutor):
        ran = []

        async def run(self, script, on_line=None, timeout=0):
            self.ran.append(script)
            return await super().run(script, on_line, timeout)

    ex = Recording()
    jobs = JobManager(_with_snapshots(_inventory((_target(), ex)), t1=FakeSnapshots(fail="disk full")))
    job = jobs.submit("t1", "upgrade", snapshot_first=True)
    await _finish(jobs)
    assert job.status == "failed" and "upgrade was not started" in job.error
    assert ex.ran == []


async def test_snapshot_skipped_for_machines_without_support():
    jobs = JobManager(_inventory((_target(), FakeExecutor())))
    job = jobs.submit("t1", "upgrade", snapshot_first=True)
    await _finish(jobs)
    assert job.status == "succeeded" and job.snapshot is None
    assert any("No snapshot" in line for line in job.log)
    with pytest.raises(ValueError, match="only supported"):
        jobs.submit("t1", "rollback", snapshot="x")


async def test_pve_snapshot_api_calls():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, req.content.decode()))
        if req.url.path.endswith("/status"):
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"name": f"{PREFIX}1"}, {"name": "mine"}, {"name": "current"}]})
        return httpx.Response(200, json={"data": "UPID:pve:1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pve/api2/json")
    s = PveSnapshots(client, "pve", "lxc", 101)
    await s.create("homelab_x", "before")
    await s.rollback("homelab_x")
    assert await s.list_ours() == [f"{PREFIX}1"]
    await s.delete("homelab_x")
    calls = [(m, p) for m, p, _ in seen if not p.endswith("/status")]
    assert calls == [
        ("POST", "/api2/json/nodes/pve/lxc/101/snapshot"),
        ("POST", "/api2/json/nodes/pve/lxc/101/snapshot/homelab_x/rollback"),
        ("GET", "/api2/json/nodes/pve/lxc/101/snapshot"),
        ("DELETE", "/api2/json/nodes/pve/lxc/101/snapshot/homelab_x"),
    ]
    assert "snapname=homelab_x" in seen[0][2] and "start=1" in seen[2][2]


async def test_pve_snapshot_task_failure_is_reported():
    def handler(req):
        if req.url.path.endswith("/status"):
            return httpx.Response(
                200, json={"data": {"status": "stopped", "exitstatus": "snapshot feature is not available"}}
            )
        return httpx.Response(200, json={"data": "UPID"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pve/api2/json")
    with pytest.raises(RuntimeError, match="feature is not available"):
        await PveSnapshots(client, "pve", "qemu", 200).create("homelab_x", "d")


async def test_incus_snapshot_api_calls():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, req.url.params.get("project"), req.content))
        if req.url.path.endswith("/wait"):
            return httpx.Response(200, json={"metadata": {"status": "Success"}})
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "metadata": [
                        f"/1.0/instances/web/snapshots/{PREFIX}1?project=lab",
                        "/1.0/instances/web/snapshots/snap0",
                    ]
                },
            )
        return httpx.Response(202, json={"operation": "/1.0/operations/abc"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    s = IncusSnapshots(client, "web", "lab")
    await s.create("homelab_x", "d")
    await s.rollback("homelab_x")
    assert await s.list_ours() == [f"{PREFIX}1"]
    assert all(project == "lab" for _, _, project, _ in seen)
    assert json.loads(seen[2][3]) == {"restore": "homelab_x"} and seen[2][0] == "PUT"


def test_demo_upgrade_with_snapshot_and_rollback():
    with TestClient(create_app(AppConfig(demo=True))) as client:
        data = client.get("/api/targets").json()
        assert data["settings"]["snapshot_before_upgrade"] is True
        pihole = next(t for t in data["targets"] if t["target"]["name"] == "pihole")["target"]
        assert pihole["can_snapshot"]
        client.post("/api/actions", json={"action": "upgrade", "target_ids": [pihole["id"]], "snapshot_first": True})
        (job,) = _wait_for_jobs(client)
        assert job["status"] == "succeeded" and job["snapshot"].startswith(PREFIX)
        assert client.post("/api/actions", json={"action": "rollback", "target_ids": [pihole["id"]]}).status_code == 400

        rb = client.post(f"/api/jobs/{job['id']}/rollback").json()
        assert rb["action"] == "rollback" and rb["snapshot"] == job["snapshot"]
        _wait_for_jobs(client)
        check = next(t for t in client.get("/api/targets").json()["targets"] if t["target"]["name"] == "pihole")[
            "check"
        ]
        assert len(check["packages"]) == 2  # the updates are pending again

        client.app.state.inventory.snapshots[pihole["id"]].saved.clear()  # pruned meanwhile
        r = client.post(f"/api/jobs/{job['id']}/rollback")
        assert r.status_code == 400 and "no longer exists" in r.json()["detail"]


async def test_storage_without_snapshots_upgrades_anyway():
    jobs = JobManager(
        _with_snapshots(
            _inventory((_target(), FakeExecutor())), t1=FakeSnapshots(fail="snapshot feature is not available")
        )
    )
    job = jobs.submit("t1", "upgrade", snapshot_first=True)
    await _finish(jobs)
    assert job.status == "succeeded" and job.snapshot is None
    assert any("can't take snapshots" in line for line in job.log)


async def test_rollback_works_when_the_guest_cannot_run_commands():
    stopped = _target("stopped", updatable=False, note="not running")
    no_agent = _target("noagent", updatable=False, note="QEMU guest agent is not enabled")
    no_ssh = _target("nossh", updatable=False, note="needs node ssh for pct exec")
    snaps = {t: FakeSnapshots([f"{PREFIX}1"]) for t in ("stopped", "noagent", "nossh")}
    inv = _with_snapshots(_inventory((stopped, FakeExecutor()), (no_agent, FakeExecutor()), (no_ssh, None)), **snaps)
    jobs = JobManager(inv)
    submitted = [jobs.submit(t, "rollback", snapshot=f"{PREFIX}1") for t in snaps]
    await _finish(jobs)
    assert [j.status for j in submitted] == ["succeeded"] * 3
    assert all(s.rolled_back == [f"{PREFIX}1"] for s in snaps.values())
    # Ordinary jobs on those guests are still refused.
    with pytest.raises(ValueError, match="not running"):
        jobs.submit("stopped", "upgrade")


async def test_snapshot_warnings_are_logged_not_failures():
    snaps = FakeSnapshots(warning="snapshot finished with warnings: 1")
    jobs = JobManager(_with_snapshots(_inventory((_target(), FakeExecutor())), t1=snaps))
    job = jobs.submit("t1", "upgrade", snapshot_first=True)
    await _finish(jobs)
    assert job.status == "succeeded" and job.snapshot
    assert "! snapshot finished with warnings: 1" in job.log


async def test_pve_task_with_warnings_counts_as_done():
    def handler(req):
        if req.url.path.endswith("/status"):
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "WARNINGS: 1"}})
        return httpx.Response(200, json={"data": "UPID"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pve/api2/json")
    warning = await PveSnapshots(client, "pve", "qemu", 200).create("homelab_x", "d")
    assert "warnings: 1" in warning

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig
from fleetdeck.inventory import Inventory
from fleetdeck.jobs import JobManager
from fleetdeck.models import Job, Target
from fleetdeck.power import ProxmoxGuestPower


@pytest.mark.parametrize("kind", ["qemu", "lxc"])
async def test_start_uses_only_fixed_guest_endpoint_and_confirms_task_and_running(kind):
    calls = []
    states = iter(["stopped", "running"])

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/current"):
            data = {"status": next(states)}
        elif request.method == "POST":
            assert request.content == b""  # no force or skiplock
            data = "UPID:test:123"
        else:
            data = {"status": "stopped", "exitstatus": "OK"}
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(
        base_url="https://pve.invalid/api2/json", transport=httpx.MockTransport(handle)
    ) as client:
        await ProxmoxGuestPower(client, "pve", kind, 101).start()
    assert calls == [
        ("GET", f"/api2/json/nodes/pve/{kind}/101/status/current"),
        ("POST", f"/api2/json/nodes/pve/{kind}/101/status/start"),
        ("GET", "/api2/json/nodes/pve/tasks/UPID:test:123/status"),
        ("GET", f"/api2/json/nodes/pve/{kind}/101/status/current"),
    ]


@pytest.mark.parametrize("status", ["running", "paused", "unknown"])
async def test_stale_running_or_unknown_status_never_posts_start(status):
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={"data": {"status": status}})

    async with httpx.AsyncClient(base_url="https://pve.invalid", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match="no longer stopped"):
            await ProxmoxGuestPower(client, "pve", "qemu", 101).start()
    assert calls == ["GET"]


@pytest.mark.parametrize("failure", ["permission", "task", "not_running", "missing_task", "timeout"])
async def test_start_reports_denial_failed_task_and_uncertain_results_without_retry(failure):
    writes = []
    reads = 0

    def handle(request):
        nonlocal reads
        if request.method == "POST":
            writes.append(request)
            return httpx.Response(200, json={"data": None if failure == "missing_task" else "UPID:test"})
        if failure == "permission":
            return httpx.Response(403, text="secret upstream body")
        if request.url.path.endswith("/current"):
            reads += 1
            data = {"status": "stopped"}
        else:
            data = {
                "status": "running" if failure == "timeout" else "stopped",
                "exitstatus": "ERROR" if failure == "task" else "OK",
            }
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(base_url="https://pve.invalid", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises((RuntimeError, TimeoutError)) as error:
            await ProxmoxGuestPower(client, "pve", "qemu", 101, timeout=0.01).start()
        assert "secret upstream body" not in str(error.value)
    assert len(writes) == (0 if failure == "permission" else 1)


class FakePower:
    def __init__(self):
        self.calls = 0

    async def start(self):
        self.calls += 1


def stopped_inventory():
    inv = Inventory([], ttl=600)
    target = Target(
        id="vm1",
        name="guest",
        source="lab",
        platform="proxmox",
        kind="vm",
        parent="pve",
        status="stopped",
        updatable=False,
        can_start=True,
    )
    power = FakePower()
    inv.targets[target.id] = (target, None)
    inv.power[target.id] = power
    inv._refreshed = time.monotonic()
    return inv, target, power


async def test_start_job_needs_no_guest_executor_and_rechecks_queued_policy():
    inv, target, power = stopped_inventory()
    jobs = JobManager(inv)
    first = jobs.submit(target.id, "start")
    assert jobs.submit(target.id, "start") is first
    await asyncio.gather(*jobs._tasks)
    assert first.status == "succeeded" and power.calls == 1
    assert not first.resumable and not inv._refreshed
    # Permission changed while queued: execution must reject before any platform write.
    target.can_start = True
    second = jobs.submit(target.id, "start")
    target.can_start = False
    await asyncio.gather(*jobs._tasks)
    assert second.status == "failed" and power.calls == 1


@pytest.mark.parametrize(
    "change",
    [{"platform": "incus"}, {"kind": "host"}, {"status": "offline"}, {"status": "running"}, {"can_start": False}],
)
async def test_start_rejects_noneligible_targets(change):
    inv, target, _ = stopped_inventory()
    for key, value in change.items():
        setattr(target, key, value)
    with pytest.raises(ValueError, match="eligible stopped"):
        JobManager(inv).submit(target.id, "start")


async def test_start_cannot_use_a_retained_stale_handle_or_race_host_upgrade():
    inv, target, _ = stopped_inventory()
    jobs = JobManager(inv)
    inv.power.clear()
    with pytest.raises(ValueError):
        jobs.submit(target.id, "start")
    inv.power[target.id] = FakePower()
    host = Target(id="host", name="pve", platform="proxmox", kind="host", source="lab")
    inv.targets[host.id] = (host, None)
    jobs.jobs["host-job"] = Job(
        id="host-job",
        target_id=host.id,
        target_name=host.name,
        action="upgrade",
        status="running",
        created_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="host is busy"):
        jobs.submit(target.id, "start")


def test_boot_api_uses_existing_job_endpoint_with_no_real_provider():
    inv, target, power = stopped_inventory()
    app = create_app(AppConfig(demo=True), inventory=inv)
    with TestClient(app) as client:
        result = client.post("/api/actions", json={"action": "start", "target_ids": [target.id]}).json()
        assert not result["rejected"] and len(result["jobs"]) == 1
        for _ in range(50):
            job = client.get("/api/jobs").json()[0]
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert job["status"] == "succeeded" and power.calls == 1


async def test_discovery_advertises_start_only_for_stopped_unprotected_guests():
    from fleetdeck.config import ProxmoxConfig
    from fleetdeck.providers.proxmox import ProxmoxProvider

    data = {
        "/api2/json/cluster/status": [],
        "/api2/json/nodes": [{"node": "pve", "status": "online"}],
        "/api2/json/nodes/pve/lxc": [
            {"vmid": 301, "name": "stopped-ct", "status": "stopped"},
            {"vmid": 302, "name": "protected-ct", "status": "stopped"},
            {"vmid": 303, "name": "template-ct", "status": "stopped", "template": 1},
            {"vmid": 304, "name": "excluded-ct", "status": "stopped"},
        ],
        "/api2/json/nodes/pve/qemu": [
            {"vmid": 201, "name": "stopped-vm", "status": "stopped"},
            {"vmid": 202, "name": "protected-vm", "status": "stopped"},
            {"vmid": 203, "name": "template-vm", "status": "stopped", "template": 1},
        ],
    }
    cfg = ProxmoxConfig(
        name="lab",
        api_url="https://pve.invalid",
        token_id="test",
        token_secret="fake",
        protect=[302, 202],
        exclude=[304],
        backup_max_age_hours=0,
    )
    provider = ProxmoxProvider(cfg)
    await provider.client.aclose()
    async with httpx.AsyncClient(
        base_url="https://pve.invalid/api2/json",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": data[request.url.path]})),
    ) as client:
        provider.client = client
        targets = {t.name: t for t, _ in await provider.discover()}
    assert {t.name for t in targets.values() if t.can_start} == {"stopped-ct", "stopped-vm"}
    assert set(provider.power) == {targets["stopped-ct"].id, targets["stopped-vm"].id}
    assert not targets["protected-ct"].can_start and not targets["protected-vm"].can_start
    assert not targets["template-ct"].can_start
    assert "template-vm" not in targets and "excluded-ct" not in targets


async def test_failed_discovery_keeps_display_history_but_revokes_start_handles():
    inv, target, power = stopped_inventory()

    class BrokenProvider:
        name = "lab"

        async def discover(self):
            raise RuntimeError("provider unavailable")

    inv.providers = [BrokenProvider()]
    inv.invalidate()
    await inv.refresh()
    assert target.id in inv.targets and not inv.power
    with pytest.raises(ValueError):
        JobManager(inv).submit(target.id, "start")
    assert power.calls == 0


def test_inventory_api_hides_start_capability_without_a_current_provider_handle():
    inv, target, _ = stopped_inventory()
    inv.power.clear()
    with TestClient(create_app(AppConfig(demo=True), inventory=inv)) as client:
        view = client.get("/api/targets").json()["targets"][0]
        assert view["target"]["id"] == target.id and not view["target"]["can_start"]
        result = client.post("/api/actions", json={"action": "start", "target_ids": [target.id]}).json()
        assert not result["jobs"] and target.id in result["rejected"]


@pytest.mark.parametrize("kind", ["vm", "lxc"])
def test_completed_boot_refreshes_stopped_inventory_on_next_dashboard_poll(monkeypatch, kind):
    from types import SimpleNamespace

    # Model a recently booted dashboard without freezing the event loop's clock.
    monkeypatch.setattr("fleetdeck.inventory.time", SimpleNamespace(monotonic=lambda: 1.0))
    inv, target, _ = stopped_inventory()
    target.kind = kind
    inv._refreshed = 1.0

    class Provider:
        name = "lab"
        running = False
        power = {}

        async def discover(self):
            current = (
                target.model_copy(update={"status": "running", "updatable": True, "can_start": False})
                if self.running
                else target
            )
            self.power = {} if self.running else {target.id: power}
            return [(current, None)]

    provider = Provider()

    class Power:
        async def start(self):
            provider.running = True  # The platform has confirmed the completed boot.

    power = Power()
    inv.providers = [provider]
    inv.power[target.id] = power
    with TestClient(create_app(AppConfig(demo=True), inventory=inv)) as client:
        before = client.get("/api/targets").json()["targets"][0]
        assert before["target"]["status"] == "stopped"
        result = client.post("/api/actions", json={"action": "start", "target_ids": [target.id]}).json()
        assert not result["rejected"]
        for _ in range(50):
            job = client.get("/api/jobs").json()[0]
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert job["status"] == "succeeded"
        after = client.get("/api/targets").json()["targets"][0]
        assert after["target"]["status"] == "running"
        assert not after["target"]["can_start"]
        assert after["active_job"] is None

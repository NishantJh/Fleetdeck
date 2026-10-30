import httpx

from fleetdeck.config import HostConfig, IncusConfig, ProxmoxConfig, SSHConfig
from fleetdeck.models import ExecResult
from fleetdeck.providers.incus import IncusProvider
from fleetdeck.providers.proxmox import ProxmoxProvider
from fleetdeck.providers.ssh_hosts import SSHHostsProvider

PVE = {
    "/api2/json/cluster/status": [{"type": "node", "name": "pve", "ip": "10.0.0.1"}],
    "/api2/json/nodes": [{"node": "pve", "status": "online"}],
    "/api2/json/nodes/pve/lxc": [
        {"vmid": "101", "name": "pihole", "status": "running"},
        {"vmid": "102", "name": "old", "status": "stopped"},
        {"vmid": "199", "name": "hidden", "status": "running"},
        {"vmid": "103", "name": "arcane", "status": "running"},
    ],
    "/api2/json/nodes/pve/qemu": [
        {"vmid": 200, "name": "docker", "status": "running"},
        {"vmid": 201, "name": "win11", "status": "running"},
        {"vmid": 202, "name": "noagent", "status": "running"},
        {"vmid": 9000, "name": "tmpl", "status": "stopped", "template": 1},
    ],
    "/api2/json/nodes/pve/qemu/200/config": {"agent": "enabled=1,fstrim_cloned_disks=1", "ostype": "l26"},
    "/api2/json/nodes/pve/qemu/201/config": {"agent": "1", "ostype": "win11"},
    "/api2/json/nodes/pve/qemu/202/config": {"ostype": "l26"},
}
PVE["/api2/json/nodes/pve/qemu"].append({"vmid": 203, "name": "broken", "status": "running"})


async def test_proxmox_discovery():
    cfg = ProxmoxConfig(
        name="lab", api_url="https://pve:8006", token_id="root@pam!t", token_secret="x",
        ssh=SSHConfig(host="pve", known_hosts="/kh"), exclude=[199], protect=[103],
        monitor_only=[200],
    )  # fmt: skip
    p = ProxmoxProvider(cfg)

    def handler(req):
        assert req.headers["Authorization"] == "PVEAPIToken=root@pam!t=x"
        if req.url.path not in PVE:
            return httpx.Response(500, json={"data": None})
        return httpx.Response(200, json={"data": PVE[req.url.path]})

    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=p.client.base_url, headers=p.client.headers
    )
    found = {t.name: t for t, _ in await p.discover()}
    assert set(found) == {"pve", "pihole", "old", "arcane", "docker", "win11", "noagent", "broken"}
    assert found["pve"].kind == "host" and found["pve"].updatable and not found["pve"].check_only
    assert not found["broken"].updatable and "VM config" in found["broken"].note
    assert found["pihole"].updatable and not found["old"].updatable
    assert not found["arcane"].updatable and "protected" in found["arcane"].note
    assert "arcane" in found["pve"].no_reboot
    assert "docker" not in found["pve"].no_reboot  # a monitor-only guest leaves its node's reboot alone
    assert found["docker"].updatable and found["docker"].monitor_only
    assert not found["pihole"].monitor_only
    assert not found["win11"].updatable and "Windows" in found["win11"].note
    assert not found["noagent"].updatable and "agent" in found["noagent"].note


async def test_proxmox_node_without_ssh_is_check_only():
    p = ProxmoxProvider(ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x"))
    data = {"/api2/json/nodes": [{"node": "pve", "status": "online"}], "/api2/json/nodes/pve/lxc": []}
    data["/api2/json/cluster/status"] = []
    data["/api2/json/nodes/pve/qemu"] = []
    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": data[req.url.path]})),
        base_url=p.client.base_url,
    )
    ((host, _),) = await p.discover()
    assert host.updatable and host.check_only


async def test_incus_discovery_without_ssh():
    p = IncusProvider(IncusConfig(name="incus01", socket="/nonexistent", monitor_only=["lab/vm1"]))

    def handler(req):
        return httpx.Response(
            200,
            json={
                "metadata": [
                    {"name": "web", "project": "default", "type": "container", "status": "Running"},
                    {"name": "vm1", "project": "lab", "type": "virtual-machine", "status": "Stopped"},
                ]
            },
        )

    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    found = {t.name: t for t, _ in await p.discover()}
    assert not found["incus01"].updatable  # host needs ssh settings
    assert found["web"].kind == "container" and found["web"].updatable
    assert found["lab/vm1"].kind == "vm" and not found["lab/vm1"].updatable
    assert found["lab/vm1"].monitor_only and not found["web"].monitor_only


async def test_pve_node_check_uses_pve_api(monkeypatch):
    import asyncio

    from fleetdeck.providers.proxmox import PveNodeExecutor

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _: real_sleep(0))
    upid = "UPID:pve:0001:0002:0003:aptupdate::root@pam!t:"
    polls = {"n": 0}

    def handler(req):
        path = req.url.path.removeprefix("/api2/json/nodes/pve")
        if req.method == "POST":
            assert path == "/apt/update"
            return httpx.Response(200, json={"data": upid})
        if path == "/version":
            return httpx.Response(200, json={"data": {"version": "8.3.0"}})
        if path == "/apt/repositories":
            standard = [
                {"handle": "enterprise", "name": "Enterprise", "status": 1},
                {"handle": "no-subscription", "name": "No-Subscription", "status": 1},
                {"handle": "test", "name": "Test", "status": 0},
            ]
            return httpx.Response(200, json={"data": {"standard-repos": standard, "errors": []}})
        if path == "/subscription":
            return httpx.Response(200, json={"data": {"status": "notfound"}})
        if path == f"/tasks/{upid}/status":
            polls["n"] += 1
            done = polls["n"] > 1
            return httpx.Response(200, json={"data": {"status": "stopped" if done else "running", "exitstatus": "OK"}})
        if path == f"/tasks/{upid}/log":
            return httpx.Response(200, json={"data": [{"n": 1, "t": "starting apt-get update"}]})
        if path == "/apt/update":
            pkgs = [{"Package": "pve-manager", "OldVersion": "8.3.0", "Version": "8.3.1"}]
            return httpx.Response(200, json={"data": pkgs})
        raise AssertionError(path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pve:8006/api2/json")
    lines = []
    res = await PveNodeExecutor(client, "pve", None).check(lambda s, line: lines.append(line))
    assert res.error is None
    assert res.os == "Proxmox VE 8.3.0"
    assert [(p.name, p.current, p.available) for p in res.packages] == [("pve-manager", "8.3.0", "8.3.1")]
    assert res.notes[0] == "Repositories (from PVE): Enterprise, No-Subscription"
    assert "without an active subscription" in res.notes[1]
    assert "starting apt-get update" in lines


async def test_pve_node_upgrade_needs_ssh():
    import pytest

    from fleetdeck.providers.proxmox import PveNodeExecutor

    with pytest.raises(RuntimeError, match="ssh"):
        await PveNodeExecutor(httpx.AsyncClient(), "pve", None).run("true")


async def test_proxmox_cluster_uses_each_nodes_address():
    from fleetdeck.executors import PctExecutor

    data = {
        "/api2/json/cluster/status": [
            {"type": "cluster", "name": "lab"},
            {"type": "node", "name": "pve1", "ip": "10.0.0.1"},
            {"type": "node", "name": "pve2", "ip": "10.0.0.2"},
        ],
        "/api2/json/nodes": [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "online"}],
        "/api2/json/nodes/pve1/lxc": [],
        "/api2/json/nodes/pve2/lxc": [{"vmid": "300", "name": "ct", "status": "running"}],
        "/api2/json/nodes/pve1/qemu": [],
        "/api2/json/nodes/pve2/qemu": [],
    }
    cfg = ProxmoxConfig(
        name="lab", api_url="https://pve1:8006", token_id="t", token_secret="x",
        ssh=SSHConfig(host="10.0.0.1", key_file="/k", known_hosts="/kh"),
        node_ssh={"pve1": SSHConfig(host="pve1.lan", known_hosts="/kh")},
    )  # fmt: skip
    data["/api2/json/nodes"].append({"node": "pve3", "status": "online"})
    data["/api2/json/cluster/status"].append({"type": "node", "name": "pve3", "ip": "10.0.0.3"})
    data["/api2/json/nodes/pve3/lxc"] = []
    data["/api2/json/nodes/pve3/qemu"] = []
    cfg.ssh.host = "pve3"  # the node named by ssh.host keeps it
    p = ProxmoxProvider(cfg)
    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": data[req.url.path]})),
        base_url=p.client.base_url,
    )
    found = {t.name: ex for t, ex in await p.discover()}
    assert found["pve1"].ssh.cfg.host == "pve1.lan"  # explicit override wins
    ct = found["ct"]
    assert isinstance(ct, PctExecutor)
    assert ct.node_ssh.cfg.host == "10.0.0.2" and ct.node_ssh.cfg.key_file == "/k"
    assert found["pve3"].ssh.cfg.host == "pve3"


def _pve_with_backups(storages, contents, not_backed_up, calls):
    import time

    now = int(time.time())
    base = {
        "/api2/json/cluster/status": [],
        "/api2/json/nodes": [{"node": "pve", "status": "online"}],
        "/api2/json/nodes/pve/lxc": [
            {"vmid": "101", "name": "pihole", "status": "running"},
            {"vmid": "102", "name": "old", "status": "stopped"},
        ],
        "/api2/json/nodes/pve/qemu": [{"vmid": 200, "name": "docker", "status": "stopped"}],
        "/api2/json/nodes/pve/storage": storages,
        "/api2/json/cluster/backup-info/not-backed-up": not_backed_up,
    }
    for name, items in contents.items():
        base[f"/api2/json/nodes/pve/storage/{name}/content"] = [
            {**i, "ctime": now - i.pop("age_h", 0) * 3600} for i in items
        ]

    def handler(req):
        calls.append(req.url.path)
        if req.url.path not in base:
            return httpx.Response(403, json={"data": None})
        return httpx.Response(200, json={"data": base[req.url.path]})

    return handler, now


async def test_proxmox_backups():
    calls: list[str] = []
    handler, _ = _pve_with_backups(
        storages=[{"storage": "local", "active": 1}, {"storage": "pbs", "active": 1, "shared": 1}],
        contents={
            "local": [{"volid": "local:backup/ct101-a", "vmid": 101, "age_h": 30}],
            "pbs": [
                {"volid": "pbs:ct/101/x", "vmid": 101, "age_h": 2, "verification": {"state": "ok"}},
                {"volid": "pbs:vm/200/y", "vmid": 200, "age_h": 100, "verification": {"state": "failed"}},
            ],
        },
        not_backed_up=[{"vmid": 102}],
        calls=calls,
    )
    p = ProxmoxProvider(ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.client.base_url)
    found = {t.name: t for t, _ in await p.discover()}
    pihole, old, docker = found["pihole"].backup, found["old"].backup, found["docker"].backup
    assert pihole.count == 2 and pihole.storage == "pbs" and pihole.verified and not pihole.stale and pihole.in_job
    assert old.last is None and old.stale and old.in_job is False
    assert docker.stale and docker.verified is False
    # Nodes aren't backed up by vzdump; without SSH their own check can't run.
    assert found["pve"].backup.not_set_up and "SSH" in found["pve"].backup.note

    before = len(calls)
    await p.discover()
    assert not any("/storage" in c for c in calls[before:])  # cached between discoveries


async def test_proxmox_backups_without_storage_permission():
    calls: list[str] = []
    handler, _ = _pve_with_backups(storages=[], contents={}, not_backed_up=[], calls=calls)
    p = ProxmoxProvider(ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.client.base_url)
    found = {t.name: t for t, _ in await p.discover()}
    b = found["pihole"].backup
    assert b.last is None and not b.stale and "Datastore.Audit" in b.note


async def test_proxmox_backups_hidden_by_missing_backup_privileges():
    # PVE lists storages but leaves out every backup when the token lacks VM.Backup/AllocateSpace.
    perms = {"/": {"VM.Audit": 1, "Datastore.Audit": 1}}
    for held, hidden in ((perms, True), ({"/": {**perms["/"], "VM.Backup": 1, "Datastore.AllocateSpace": 1}}, False)):
        calls: list[str] = []
        handler, _ = _pve_with_backups(
            storages=[{"storage": "pbs", "active": 1, "shared": 1}], contents={"pbs": []}, not_backed_up=[], calls=calls
        )

        def with_perms(req, handler=handler, held=held):
            if req.url.path == "/api2/json/access/permissions":
                return httpx.Response(200, json={"data": held})
            return handler(req)

        p = ProxmoxProvider(ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x"))
        p.client = httpx.AsyncClient(transport=httpx.MockTransport(with_perms), base_url=p.client.base_url)
        b = {t.name: t for t, _ in await p.discover()}["pihole"].backup
        if hidden:
            assert b.last is None and not b.stale and "VM.Backup" in b.note
        else:
            assert b.stale and b.note is None  # really no backup


async def test_pve_role_can_list_backups():
    from fleetdeck.onboarding import PVE_PRIVS
    from fleetdeck.providers.proxmox import BACKUP_PRIVS

    assert set(BACKUP_PRIVS) <= set(PVE_PRIVS.split())


async def test_proxmox_backup_tracking_can_be_turned_off():
    calls: list[str] = []
    handler, _ = _pve_with_backups(storages=[], contents={}, not_backed_up=[], calls=calls)
    cfg = ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x", backup_max_age_hours=0)
    p = ProxmoxProvider(cfg)
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.client.base_url)
    found = {t.name: t for t, _ in await p.discover()}
    assert found["pihole"].backup is None and not any("storage" in c for c in calls)


async def test_proxmox_backups_inactive_storage_and_failures_are_not_cached():
    calls: list[str] = []
    handler, _ = _pve_with_backups(
        storages=[{"storage": "nas", "active": 0}, {"storage": "pbs", "active": 1, "shared": 1}],
        contents={},  # pbs content answers 403
        not_backed_up=[],
        calls=calls,
    )
    p = ProxmoxProvider(ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.client.base_url)
    found = {t.name: t for t, _ in await p.discover()}
    note = found["pihole"].backup.note
    assert "nas on pve: storage inactive" in note and "pbs:" in note and not found["pihole"].backup.stale
    before = len(calls)
    await p.discover()
    assert any(c.endswith("/storage") for c in calls[before:])  # tried again, not cached


async def test_proxmox_backups_on_two_nodes():
    import time

    now = int(time.time())
    local = [{"volid": "local:backup/vzdump-lxc-101.tar.zst", "vmid": 101, "ctime": now}]
    pbs = [{"volid": "pbs:ct/101/x", "vmid": 101, "ctime": now}]
    data = {
        "/api2/json/cluster/status": [],
        "/api2/json/cluster/backup-info/not-backed-up": [],
        "/api2/json/nodes": [{"node": "a", "status": "online"}, {"node": "b", "status": "online"}],
        "/api2/json/nodes/a/lxc": [{"vmid": "101", "name": "ct", "status": "running"}],
        "/api2/json/nodes/a/qemu": [],
        "/api2/json/nodes/b/lxc": [],
        "/api2/json/nodes/b/qemu": [],
    }
    for node in ("a", "b"):
        data[f"/api2/json/nodes/{node}/storage"] = [{"storage": "local"}, {"storage": "pbs", "shared": 1}]
        data[f"/api2/json/nodes/{node}/storage/local/content"] = local
        data[f"/api2/json/nodes/{node}/storage/pbs/content"] = pbs
    calls: list[str] = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"data": data[req.url.path]})

    p = ProxmoxProvider(ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.client.base_url)
    found = {t.name: t for t, _ in await p.discover()}
    # Two node-local backups with the same volid, plus one on the shared PBS read only once.
    assert found["ct"].backup.count == 3
    assert sum(c.endswith("/pbs/content") for c in calls) == 1


async def test_shared_storage_inactive_on_one_node_is_read_from_another():
    import time

    now = int(time.time())
    data = {
        "/api2/json/cluster/status": [],
        "/api2/json/cluster/backup-info/not-backed-up": [],
        "/api2/json/nodes": [{"node": "a", "status": "online"}, {"node": "b", "status": "online"}],
        "/api2/json/nodes/a/lxc": [{"vmid": "101", "name": "ct", "status": "running"}],
        "/api2/json/nodes/a/qemu": [],
        "/api2/json/nodes/b/lxc": [],
        "/api2/json/nodes/b/qemu": [],
        "/api2/json/nodes/a/storage": [{"storage": "nfs", "shared": 1, "active": 0}],
        "/api2/json/nodes/b/storage": [{"storage": "nfs", "shared": 1, "active": 1}],
        "/api2/json/nodes/b/storage/nfs/content": [{"volid": "nfs:backup/x", "vmid": 101, "ctime": now}],
    }

    def handler(req):
        return httpx.Response(200, json={"data": data[req.url.path]})

    p = ProxmoxProvider(ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x"))
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.client.base_url)
    found = {t.name: t for t, _ in await p.discover()}
    assert found["ct"].backup.count == 1 and found["ct"].backup.note is None


async def test_incus_restricted_certificate_lists_per_project():
    p = IncusProvider(IncusConfig(name="incus01", socket="/nonexistent"))

    def handler(req):
        if req.url.path == "/1.0/projects":
            return httpx.Response(200, json={"metadata": ["/1.0/projects/default", "/1.0/projects/lab"]})
        if req.url.params.get("all-projects") == "true":
            return httpx.Response(403, json={"error": "Forbidden"})
        project = req.url.params["project"]
        return httpx.Response(
            200, json={"metadata": [{"name": f"{project}-ct", "type": "container", "status": "Running"}]}
        )

    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    names = {t.name for t, _ in await p.discover()}
    assert names == {"incus01", "default-ct", "lab/lab-ct"}


async def test_unknown_ssh_host_key_is_shown_on_the_dashboard():
    from fleetdeck.config import HostConfig
    from fleetdeck.providers.ssh_hosts import SSHHostsProvider

    cfg = ProxmoxConfig(
        name="lab", api_url="https://pve:8006", token_id="t", token_secret="x", ssh=SSHConfig(host="10.0.0.1")
    )
    p = ProxmoxProvider(cfg)
    data = {"/api2/json/nodes": [{"node": "pve", "status": "online"}], "/api2/json/cluster/status": []}
    data["/api2/json/nodes/pve/lxc"] = [{"vmid": 101, "name": "ct", "status": "running"}]
    data["/api2/json/nodes/pve/qemu"] = []
    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": data[req.url.path]})),
        base_url=p.client.base_url,
    )
    found = {t.name: (t, ex) for t, ex in await p.discover()}
    host, ct = found["pve"][0], found["ct"][0]
    assert host.check_only and "known_hosts" in host.note
    assert not ct.updatable and "known_hosts" in ct.note and found["ct"][1] is None

    ((pi, _),) = await SSHHostsProvider([HostConfig(name="pi", host="10.0.0.2")]).discover()
    assert not pi.updatable and "add 10.0.0.2 to known_hosts" in pi.note
    ((ok, _),) = await SSHHostsProvider([HostConfig(name="pi", host="10.0.0.2", known_hosts="/kh")]).discover()
    assert ok.updatable and ok.note is None and not ok.monitor_only
    cfg = HostConfig(name="omv", host="10.0.0.3", known_hosts="/kh", monitor_only=True)
    ((omv, _),) = await SSHHostsProvider([cfg]).discover()
    assert omv.monitor_only


async def test_pve_node_check_flags_security_updates_over_ssh():
    from fleetdeck.providers.proxmox import APT_LIST_SCRIPT, PveNodeExecutor

    def handler(req):
        path = req.url.path.removeprefix("/api2/json/nodes/pve")
        if req.method == "POST":
            return httpx.Response(200, json={"data": "UPID:x"})
        if path == "/version":
            return httpx.Response(200, json={"data": {"version": "8.3.0"}})
        if path == "/apt/repositories":
            return httpx.Response(200, json={"data": {"standard-repos": [], "errors": []}})
        if path == "/tasks/UPID:x/status":
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
        if path == "/tasks/UPID:x/log":
            return httpx.Response(200, json={"data": []})
        if path == "/apt/update":
            pkgs = [
                {"Package": "openssl", "OldVersion": "3.0.14-1", "Version": "3.0.15-1"},
                {"Package": "pve-manager", "OldVersion": "8.3.0", "Version": "8.3.1"},
            ]
            return httpx.Response(200, json={"data": pkgs})
        raise AssertionError(path)

    class FakeSSH:
        async def run(self, script, on_line=None, timeout=0):
            if script == APT_LIST_SCRIPT:
                return ExecResult(
                    rc=0,
                    stdout="Listing...\n"
                    "openssl/stable-security 3.0.15-1 amd64 [upgradable from: 3.0.14-1]\n"
                    "pve-manager/stable 8.3.1 all [upgradable from: 8.3.0]\n",
                )
            return ExecResult(rc=0, stdout="__REBOOT__=no\n")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pve:8006/api2/json")
    res = await PveNodeExecutor(client, "pve", FakeSSH()).check()
    assert {p.name: p.security for p in res.packages} == {"openssl": True, "pve-manager": False}


async def test_incus_backups_from_snapshots_and_exports():
    from datetime import UTC, datetime, timedelta

    p = IncusProvider(IncusConfig(name="incus01", socket="/nonexistent"))
    now = datetime.now(UTC)
    iso = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")  # noqa: E731
    data = {
        "/1.0/instances": [
            {
                "name": "web",
                "project": "default",
                "type": "container",
                "status": "Running",
                "expanded_config": {"snapshots.schedule": "@daily"},
            },
            {"name": "old", "project": "default", "type": "container", "status": "Running"},
            {"name": "none", "project": "default", "type": "container", "status": "Running"},
        ],
        "/1.0/instances/web/snapshots": [
            {"name": "snap0", "created_at": iso(30)},
            {"name": "snap1", "created_at": iso(5)},
        ],
        "/1.0/instances/web/backups": [{"name": "b0", "created_at": iso(50)}],
        # Only the dashboard's own pre-upgrade snapshot, which doesn't count, and an old export.
        "/1.0/instances/old/snapshots": [{"name": "fleetdeck_20261009_101010", "created_at": iso(1)}],
        "/1.0/instances/old/backups": [{"name": "b0", "created_at": iso(100)}],
        "/1.0/instances/none/snapshots": [],
        "/1.0/instances/none/backups": [],
    }
    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"metadata": data[req.url.path]})),
        base_url="http://incus",
    )
    found = {t.name: t.backup for t, _ in await p.discover()}
    assert found["incus01"].not_set_up
    web, old, none = found["web"], found["old"], found["none"]
    assert web.storage == "snapshot" and web.count == 3 and not web.stale and web.in_job
    assert old.storage == "export" and old.count == 1 and old.stale and old.in_job is None
    assert none.last is None and none.stale and none.note is None


async def test_incus_backups_unreadable_is_unknown_not_none():
    p = IncusProvider(IncusConfig(name="incus01", socket="/nonexistent"))

    def handler(req):
        if req.url.path == "/1.0/instances":
            return httpx.Response(200, json={"metadata": [{"name": "web", "type": "container", "status": "Running"}]})
        return httpx.Response(403, json={"error": "not authorized"})

    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    found = {t.name: t for t, _ in await p.discover()}
    b = found["web"].backup
    assert b.last is None and not b.stale and "could not list snapshots" in b.note


async def test_nodes_and_incus_host_run_their_own_backup_check(monkeypatch):
    from fleetdeck.config import HostBackupConfig
    from fleetdeck.host_backups import HostBackups
    from fleetdeck.models import BackupInfo

    seen = []

    async def fake_get(self, name, cfg, ex):
        seen.append((name, cfg.path, ex.cfg.host))
        return BackupInfo(storage=cfg.path, count=1)

    monkeypatch.setattr(HostBackups, "get", fake_get)
    ssh = SSHConfig(host="10.0.0.5", insecure_skip_host_key_check=True, backup=HostBackupConfig(type="path", path="/b"))

    pve = ProxmoxProvider(
        ProxmoxConfig(
            name="lab", api_url="https://pve:8006", token_id="t", token_secret="x", ssh=ssh, backup_max_age_hours=0
        )
    )
    data = {
        "/api2/json/nodes": [{"node": "pve", "status": "online"}, {"node": "pve2", "status": "offline"}],
        "/api2/json/cluster/status": [],
        "/api2/json/nodes/pve/lxc": [],
        "/api2/json/nodes/pve/qemu": [],
    }
    pve.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"data": data[req.url.path]})),
        base_url=pve.client.base_url,
    )
    nodes = {t.name: t.backup for t, _ in await pve.discover()}
    assert nodes["pve"].storage == "/b" and nodes["pve2"].note == "offline"

    inc = IncusProvider(IncusConfig(name="incus01", socket="/nonexistent", ssh=ssh))
    inc.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"metadata": []})), base_url="http://incus"
    )
    ((host, _),) = await inc.discover()
    assert host.backup.storage == "/b"
    assert seen == [("pve", "/b", "10.0.0.5"), ("incus01", "/b", "10.0.0.5")]


async def test_incus_backup_failure_retries_only_that_instance(monkeypatch):
    from fleetdeck.providers import incus as incus_mod

    p = IncusProvider(IncusConfig(name="incus01", socket="/nonexistent"))
    calls = []

    def handler(req):
        if req.url.path == "/1.0/instances":
            insts = [{"name": n, "type": "container", "status": "Running"} for n in ("good", "bad")]
            return httpx.Response(200, json={"metadata": insts})
        calls.append(req.url.path)
        if "/bad/" in req.url.path:
            return httpx.Response(403, json={"error": "not authorized"})
        return httpx.Response(200, json={"metadata": []})

    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    await p.discover()
    assert len(calls) == 4
    calls.clear()
    await p.discover()
    assert calls == []  # the failure is kept for a short while too
    monkeypatch.setattr(incus_mod, "BACKUP_RETRY", 0.0)
    found = {t.name: t.backup for t, _ in await p.discover()}
    assert sorted(calls) == ["/1.0/instances/bad/backups", "/1.0/instances/bad/snapshots"]
    assert found["good"].note is None and "could not list" in found["bad"].note


async def test_update_commands_by_vmid_name_or_node():
    cfg = ProxmoxConfig(
        name="lab", api_url="https://pve:8006", token_id="t", token_secret="x",
        ssh=SSHConfig(host="pve", known_hosts="/kh"), update_commands={101: "pihole -up", "docker": "d", "pve": "n"},
    )  # fmt: skip
    p = ProxmoxProvider(cfg)
    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: (
                httpx.Response(200, json={"data": PVE[req.url.path]})
                if req.url.path in PVE
                else httpx.Response(500, json={"data": None})
            )
        ),
        base_url=p.client.base_url,
    )
    found = {t.name: t.update_command for t, _ in await p.discover()}
    assert found["pihole"] == "pihole -up" and found["docker"] == "d" and found["pve"] == "n"
    assert found["old"] is None

    inc = IncusProvider(IncusConfig(name="incus01", socket="/x", update_commands={"lab/vm1": "a", "incus01": "h"}))
    meta = [{"name": "vm1", "project": "lab", "type": "virtual-machine", "status": "Running"}]
    inc.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"metadata": meta})), base_url="http://i"
    )
    assert {t.name: t.update_command for t, _ in await inc.discover()} == {"incus01": "h", "lab/vm1": "a"}

    ((host, _),) = await SSHHostsProvider([HostConfig(name="nas", host="nas", update_command="omv-upgrade")]).discover()
    assert host.update_command == "omv-upgrade"

import httpx

from homelab.config import IncusConfig, ProxmoxConfig, SSHConfig
from homelab.models import ExecResult
from homelab.providers.incus import IncusProvider
from homelab.providers.proxmox import ProxmoxProvider

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
    assert found["docker"].updatable
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
    p = IncusProvider(IncusConfig(name="incus01", socket="/nonexistent"))

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


async def test_pve_node_check_uses_pve_api(monkeypatch):
    import asyncio

    from homelab.providers.proxmox import PveNodeExecutor

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

    from homelab.providers.proxmox import PveNodeExecutor

    with pytest.raises(RuntimeError, match="ssh"):
        await PveNodeExecutor(httpx.AsyncClient(), "pve", None).run("true")


async def test_proxmox_cluster_uses_each_nodes_address():
    from homelab.executors import PctExecutor

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
    from homelab.config import HostConfig
    from homelab.providers.ssh_hosts import SSHHostsProvider

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
    assert ok.updatable and ok.note is None


async def test_pve_node_check_flags_security_updates_over_ssh():
    from homelab.providers.proxmox import APT_LIST_SCRIPT, PveNodeExecutor

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

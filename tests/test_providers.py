import httpx

from homelab.config import IncusConfig, ProxmoxConfig, SSHConfig
from homelab.providers.incus import IncusProvider
from homelab.providers.proxmox import ProxmoxProvider

PVE = {
    "/api2/json/nodes": [{"node": "pve", "status": "online"}],
    "/api2/json/nodes/pve/lxc": [
        {"vmid": "101", "name": "pihole", "status": "running"},
        {"vmid": "102", "name": "old", "status": "stopped"},
        {"vmid": "199", "name": "hidden", "status": "running"},
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


async def test_proxmox_discovery():
    cfg = ProxmoxConfig(
        name="lab", api_url="https://pve:8006", token_id="root@pam!t", token_secret="x",
        ssh=SSHConfig(host="pve"), exclude=[199],
    )  # fmt: skip
    p = ProxmoxProvider(cfg)

    def handler(req):
        assert req.headers["Authorization"] == "PVEAPIToken=root@pam!t=x"
        return httpx.Response(200, json={"data": PVE[req.url.path]})

    p.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=p.client.base_url, headers=p.client.headers
    )
    found = {t.name: t for t, _ in await p.discover()}
    assert set(found) == {"pve", "pihole", "old", "docker", "win11", "noagent"}
    assert found["pve"].kind == "host" and found["pve"].updatable
    assert found["pihole"].updatable and not found["old"].updatable
    assert found["docker"].updatable
    assert not found["win11"].updatable and "Windows" in found["win11"].note
    assert not found["noagent"].updatable and "agent" in found["noagent"].note


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

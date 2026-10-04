from __future__ import annotations

import httpx

from ..config import ProxmoxConfig, SSHConfig
from ..executors import PctExecutor, PveAgentExecutor, SSHExecutor
from ..models import Target
from . import Discovered

PROTECTED_NOTE = "protected: runs this dashboard, update it from its own console"


class ProxmoxProvider:
    """Discovers nodes, LXCs and VMs through the Proxmox VE API.

    * Node (host): SSH to the node.
    * LXC: `pct exec` over the node's SSH connection.
    * VM: QEMU guest agent exec through the API.
    """

    def __init__(self, cfg: ProxmoxConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.client = httpx.AsyncClient(
            base_url=cfg.api_url.rstrip("/") + "/api2/json",
            headers={"Authorization": f"PVEAPIToken={cfg.token_id}={cfg.token_secret}"},
            verify=cfg.verify_ssl,
            timeout=30,
        )

    def _node_ssh(self, node: str) -> SSHConfig | None:
        return self.cfg.node_ssh.get(node) or self.cfg.ssh

    async def _get(self, path: str):
        r = await self.client.get(path)
        r.raise_for_status()
        return r.json()["data"]

    async def discover(self) -> Discovered:
        found: Discovered = []
        for node in await self._get("/nodes"):
            nname = node["node"]
            online = node.get("status") == "online"
            ssh_cfg = self._node_ssh(nname)
            ssh = SSHExecutor(ssh_cfg) if ssh_cfg else None
            prefix = f"proxmox:{self.name}:{nname}"
            host = Target(
                id=prefix,
                name=nname,
                platform="proxmox",
                source=self.name,
                kind="host",
                status=node.get("status", "unknown"),
                updatable=online and ssh is not None,
                note=None if ssh else "add ssh settings to update this node",
            )
            found.append((host, ssh))  # type: ignore[arg-type]
            if not online:
                continue

            for ct in await self._get(f"/nodes/{nname}/lxc"):
                vmid = int(ct["vmid"])
                if vmid in self.cfg.exclude:
                    continue
                running = ct.get("status") == "running"
                updatable = running and ssh is not None
                note = (None if ssh else "needs node ssh for pct exec") if running else "not running"
                if vmid in self.cfg.protect:
                    updatable, note = False, PROTECTED_NOTE
                t = Target(
                    id=f"{prefix}:lxc:{vmid}",
                    name=ct.get("name") or f"ct{vmid}",
                    platform="proxmox",
                    source=self.name,
                    kind="lxc",
                    parent=nname,
                    status=ct.get("status", "unknown"),
                    updatable=updatable,
                    note=note,
                )
                found.append((t, PctExecutor(ssh, vmid) if ssh else None))  # type: ignore[arg-type]

            for vm in await self._get(f"/nodes/{nname}/qemu"):
                vmid = int(vm["vmid"])
                if vmid in self.cfg.exclude or vm.get("template"):
                    continue
                running = vm.get("status") == "running"
                note = None if running else "not running"
                updatable = running
                if vmid in self.cfg.protect:
                    updatable, note = False, PROTECTED_NOTE
                elif running:
                    conf = await self._get(f"/nodes/{nname}/qemu/{vmid}/config")
                    if str(conf.get("ostype", "")).startswith("w"):
                        updatable, note = False, "Windows guests are not supported yet"
                    elif not str(conf.get("agent", "0")).startswith(("1", "enabled=1")):
                        updatable, note = False, "QEMU guest agent is not enabled"
                t = Target(
                    id=f"{prefix}:vm:{vmid}",
                    name=vm.get("name") or f"vm{vmid}",
                    platform="proxmox",
                    source=self.name,
                    kind="vm",
                    parent=nname,
                    status=vm.get("status", "unknown"),
                    updatable=updatable,
                    note=note,
                )
                found.append((t, PveAgentExecutor(self.client, nname, vmid)))
        return found

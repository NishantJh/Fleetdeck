from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx

from ..config import ProxmoxConfig, SSHConfig
from ..executors import DEFAULT_TIMEOUT, LineCallback, PctExecutor, PveAgentExecutor, SSHExecutor
from ..models import CheckResult, ExecResult, Package, Target
from ..updates import REBOOT_SCRIPT, parse_reboot
from . import Discovered

PROTECTED_NOTE = "protected: runs this dashboard, update it from its own console"


class PveNodeExecutor:
    """The Proxmox host itself.

    Checks go through the PVE API, exactly like Updates > Refresh in the web UI: PVE refreshes
    package lists from the repositories configured under Updates > Repositories and reports
    what is upgradable. The API has no non-interactive upgrade call (the UI opens a shell for
    that), so the upgrade itself is `apt-get dist-upgrade` over SSH, against the same repositories.
    """

    def __init__(self, client: httpx.AsyncClient, node: str, ssh: SSHExecutor | None):
        self.client = client
        self.node = node
        self.ssh = ssh

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        if self.ssh is None:
            raise RuntimeError("upgrading the Proxmox host needs ssh settings for this node")
        return await self.ssh.run(script, on_line, timeout)

    async def _get(self, path: str, **params):
        r = await self.client.get(f"/nodes/{self.node}{path}", params=params or None)
        r.raise_for_status()
        return r.json()["data"]

    async def _wait_task(self, upid: str, timeout: float = 600) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = await self._get(f"/tasks/{upid}/status")
            if status.get("status") == "stopped":
                return status
            if time.monotonic() > deadline:
                raise TimeoutError("PVE package refresh task did not finish")
            await asyncio.sleep(1)

    async def check(self, on_line: LineCallback | None = None) -> CheckResult:
        def say(line: str) -> None:
            if on_line:
                on_line("stdout", line)

        notes: list[str] = []
        version = await self._get("/version")
        os_name = f"Proxmox VE {version.get('version', '?')}"

        repos = await self._get("/apt/repositories")
        enabled = [r["name"] for r in repos.get("standard-repos", []) if r.get("status") == 1]
        notes.append("Repositories (from PVE): " + (", ".join(enabled) or "no standard Proxmox repository enabled"))
        for err in repos.get("errors", []):
            notes.append(f"Repository error in {err.get('path')}: {err.get('error')}")
        if any("Enterprise" in name for name in enabled):
            sub = await self._get("/subscription")
            if str(sub.get("status", "")).lower() != "active":
                notes.append(
                    "An Enterprise repository is enabled without an active subscription, so refreshing it "
                    "fails. Switch to No-Subscription under Updates > Repositories in PVE."
                )
        for note in notes:
            say(f"== {note}")

        say("== Refreshing package lists through the Proxmox API")
        r = await self.client.post(f"/nodes/{self.node}/apt/update")
        if r.status_code == 403:
            raise RuntimeError("the API token needs Sys.Modify on /nodes to refresh package lists")
        r.raise_for_status()
        upid = r.json()["data"]
        status = await self._wait_task(upid)
        for entry in await self._get(f"/tasks/{upid}/log", limit=1000):
            say(entry.get("t", ""))
        exit_status = status.get("exitstatus", "")
        error = None if exit_status == "OK" or exit_status.startswith("WARNINGS") else f"refresh failed: {exit_status}"

        packages = [
            Package(name=p["Package"], current=p.get("OldVersion"), available=p["Version"])
            for p in await self._get("/apt/update")
        ]

        # The API doesn't say whether a reboot is pending (e.g. a newer kernel is installed).
        reboot = None
        if self.ssh is not None:
            try:
                reboot = parse_reboot((await self.ssh.run(REBOOT_SCRIPT, timeout=60)).stdout)
            except Exception as e:  # informational only; don't fail the check
                say(f"! could not check for a pending reboot: {e}")
        return CheckResult(
            package_manager="apt (via PVE)",
            os=os_name,
            packages=packages,
            reboot_required=reboot,
            checked_at=datetime.now(UTC),
            error=error,
            notes=notes,
        )


class ProxmoxProvider:
    """Discovers nodes, LXCs and VMs through the Proxmox VE API.

    * Node (host): checked through the PVE API, upgraded over SSH (see PveNodeExecutor).
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

    def _node_ssh(self, node: str, node_ips: dict[str, str]) -> SSHConfig | None:
        if node in self.cfg.node_ssh:
            return self.cfg.node_ssh[node]
        if self.cfg.ssh is None:
            return None
        # `pct exec` only works on the node that runs the container, so in a cluster reuse the
        # shared ssh settings but point them at each node's own (cluster) address. The node the
        # user's ssh.host already names keeps it; set node_ssh if cluster IPs aren't reachable.
        if len(node_ips) > 1 and node in node_ips and self.cfg.ssh.host not in (node, node_ips[node]):
            return self.cfg.ssh.model_copy(update={"host": node_ips[node]})
        return self.cfg.ssh

    async def _node_ips(self) -> dict[str, str]:
        try:
            status = await self._get("/cluster/status")
        except httpx.HTTPError:
            return {}
        return {e["name"]: e["ip"] for e in status if e.get("type") == "node" and e.get("ip")}

    async def _get(self, path: str):
        r = await self.client.get(path)
        r.raise_for_status()
        return r.json()["data"]

    async def _vm_blocker(self, node: str, vmid: int) -> str | None:
        """Why a running VM can't be updated through the guest agent, or None if it can."""
        try:
            conf = await self._get(f"/nodes/{node}/qemu/{vmid}/config")
        except httpx.HTTPError as e:
            # One unreadable VM shouldn't hide the rest of the node.
            return f"could not read VM config: {e}"
        if str(conf.get("ostype", "")).startswith("w"):
            return "Windows guests are not supported yet"
        if not str(conf.get("agent", "0")).startswith(("1", "enabled=1")):
            return "QEMU guest agent is not enabled"
        return None

    async def discover(self) -> Discovered:
        found: Discovered = []
        node_ips = await self._node_ips()
        for node in await self._get("/nodes"):
            nname = node["node"]
            online = node.get("status") == "online"
            ssh_cfg = self._node_ssh(nname, node_ips)
            ssh = SSHExecutor(ssh_cfg) if ssh_cfg else None
            prefix = f"proxmox:{self.name}:{nname}"
            host = Target(
                id=prefix,
                name=nname,
                platform="proxmox",
                source=self.name,
                kind="host",
                status=node.get("status", "unknown"),
                updatable=online,
                check_only=ssh is None,
                note=(None if ssh else "check only: add ssh settings to upgrade this node") if online else "offline",
            )
            found.append((host, PveNodeExecutor(self.client, nname, ssh)))
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
                    note = await self._vm_blocker(nname, vmid)
                    updatable = note is None
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

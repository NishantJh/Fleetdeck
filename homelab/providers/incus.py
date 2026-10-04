from __future__ import annotations

import httpx

from ..config import IncusConfig
from ..executors import IncusExecutor, SSHExecutor
from ..models import Target
from ..snapshots import IncusSnapshots
from . import Discovered


class IncusProvider:
    """Discovers the Incus host plus its containers and VMs via the Incus REST API."""

    def __init__(self, cfg: IncusConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.snapshots: dict[str, IncusSnapshots] = {}
        if cfg.socket:
            self.client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=cfg.socket), base_url="http://incus", timeout=30
            )
        else:
            cert = (cfg.client_cert, cfg.client_key) if cfg.client_cert and cfg.client_key else None
            self.client = httpx.AsyncClient(base_url=cfg.url.rstrip("/"), cert=cert, verify=cfg.verify_ssl, timeout=30)

    async def discover(self) -> Discovered:
        found: Discovered = []
        snapshots: dict[str, IncusSnapshots] = {}
        ssh = SSHExecutor(self.cfg.ssh) if self.cfg.ssh else None
        prefix = f"incus:{self.name}"
        found.append(
            (
                Target(
                    id=prefix,
                    name=self.name,
                    platform="incus",
                    source=self.name,
                    kind="host",
                    updatable=ssh is not None,
                    note=None if ssh else "add ssh settings to update this host",
                ),
                ssh,  # type: ignore[arg-type]
            )
        )

        r = await self.client.get("/1.0/instances", params={"recursion": 1, "all-projects": "true"})
        r.raise_for_status()
        for inst in r.json()["metadata"]:
            project = inst.get("project", "default")
            name = inst["name"]
            if name in self.cfg.exclude or f"{project}/{name}" in self.cfg.exclude:
                continue
            is_vm = inst.get("type") == "virtual-machine"
            running = inst.get("status") == "Running"
            label = name if project == "default" else f"{project}/{name}"
            t = Target(
                id=f"{prefix}:{'vm' if is_vm else 'container'}:{project}/{name}",
                name=label,
                platform="incus",
                source=self.name,
                kind="vm" if is_vm else "container",
                parent=self.name,
                status=inst.get("status", "unknown").lower(),
                updatable=running,
                note=None if running else "not running",
                can_snapshot=True,
            )
            snapshots[t.id] = IncusSnapshots(self.client, name, project)
            found.append((t, IncusExecutor(self.client, name, project)))
        self.snapshots = snapshots
        return found

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from ..config import IncusConfig
from ..executors import IncusExecutor, SSHExecutor
from ..models import BackupInfo, Target
from ..snapshots import PREFIX, IncusSnapshots
from ..tls import PinnedTransport
from . import Discovered

log = logging.getLogger(__name__)

# Snapshots and exports are listed per instance, so they're read less often than discovery.
BACKUP_TTL = 300.0


class IncusProvider:
    """Discovers the Incus host plus its containers and VMs via the Incus REST API."""

    def __init__(self, cfg: IncusConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.snapshots: dict[str, IncusSnapshots] = {}
        self._backups: dict[str, BackupInfo] = {}
        self._backups_at = 0.0
        if cfg.socket:
            self.client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=cfg.socket), base_url="http://incus", timeout=30
            )
        else:
            cert = (cfg.client_cert, cfg.client_key) if cfg.client_cert and cfg.client_key else None
            pinned = PinnedTransport(cfg.url, cfg.tls_fingerprint, cert) if cfg.tls_fingerprint else None
            self.client = httpx.AsyncClient(
                base_url=cfg.url.rstrip("/"), cert=cert, verify=cfg.verify_ssl, transport=pinned, timeout=30
            )

    async def _instances(self) -> list[dict]:
        r = await self.client.get("/1.0/instances", params={"recursion": 1, "all-projects": "true"})
        if r.status_code != 403:
            r.raise_for_status()
            return r.json()["metadata"]
        # A certificate restricted to some projects may not list across all of them: go project by
        # project through the ones it can see.
        p = await self.client.get("/1.0/projects")
        p.raise_for_status()
        out: list[dict] = []
        for url in p.json()["metadata"]:
            project = url.rstrip("/").rsplit("/", 1)[-1]
            r = await self.client.get("/1.0/instances", params={"recursion": 1, "project": project})
            r.raise_for_status()
            out += [{**inst, "project": inst.get("project") or project} for inst in r.json()["metadata"]]
        return out

    async def _list(self, name: str, project: str, what: str) -> list[dict]:
        r = await self.client.get(f"/1.0/instances/{name}/{what}", params={"recursion": 1, "project": project})
        r.raise_for_status()
        return r.json()["metadata"] or []

    async def _backup(self, inst: dict, project: str) -> BackupInfo:
        """Newest snapshot or export (`incus export` / `incus backup`) the server keeps for the instance.

        The dashboard's own pre-upgrade snapshots don't count: they're short-lived and pruned."""
        name = inst["name"]
        # snapshots.schedule set (on the instance or a profile) means Incus takes them on its own.
        in_job = True if (inst.get("expanded_config") or {}).get("snapshots.schedule") else None
        found: list[tuple[datetime, str]] = []
        errors: list[str] = []
        for what, label in (("snapshots", "snapshot"), ("backups", "export")):
            try:
                items = await self._list(name, project, what)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                errors.append(f"could not list {what}: {e}")
                continue
            for item in items:
                if what == "snapshots" and item.get("name", "").rsplit("/", 1)[-1].startswith(PREFIX):
                    continue
                if when := item.get("created_at"):
                    found.append((datetime.fromisoformat(when.replace("Z", "+00:00")), label))
        note = "; ".join(errors) or None
        if not found:
            # Without a readable list, "no backup" would be a guess.
            return BackupInfo(stale=note is None, in_job=in_job, note=note)
        last, label = max(found)
        stale = datetime.now(UTC) - last > timedelta(hours=self.cfg.backup_max_age_hours)
        return BackupInfo(last=last, storage=label, count=len(found), stale=stale, in_job=in_job, note=note)

    async def _backup_index(self, instances: list[tuple[dict, str, str]]) -> dict[str, BackupInfo]:
        fresh = time.monotonic() - self._backups_at < BACKUP_TTL
        if fresh and set(self._backups) == {tid for _, _, tid in instances}:
            return self._backups
        infos = await asyncio.gather(*(self._backup(inst, project) for inst, project, _ in instances))
        index = {tid: info for (_, _, tid), info in zip(instances, infos, strict=True)}
        # Don't keep failures: the next discovery tries again.
        if not any(i.note for i in infos):
            self._backups, self._backups_at = index, time.monotonic()
        return index

    async def discover(self) -> Discovered:
        found: Discovered = []
        snapshots: dict[str, IncusSnapshots] = {}
        ssh_problem = self.cfg.ssh.host_key_problem if self.cfg.ssh else None
        ssh = SSHExecutor(self.cfg.ssh) if self.cfg.ssh and not ssh_problem else None
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
                    note=None if ssh else ssh_problem or "add ssh settings to update this host",
                ),
                ssh,  # type: ignore[arg-type]
            )
        )

        instances = []
        for inst in await self._instances():
            project = inst.get("project", "default")
            name = inst["name"]
            if name in self.cfg.exclude or f"{project}/{name}" in self.cfg.exclude:
                continue
            is_vm = inst.get("type") == "virtual-machine"
            instances.append((inst, project, f"{prefix}:{'vm' if is_vm else 'container'}:{project}/{name}"))
        backups: dict[str, BackupInfo] = {}
        if self.cfg.backup_max_age_hours and instances:
            try:
                backups = await self._backup_index(instances)
            except Exception as e:  # noqa: BLE001 - backups are extra; never hide the instances
                log.warning("%s: could not read backups: %s", self.name, e)

        for inst, project, tid in instances:
            name = inst["name"]
            is_vm = inst.get("type") == "virtual-machine"
            running = inst.get("status") == "Running"
            label = name if project == "default" else f"{project}/{name}"
            t = Target(
                id=tid,
                name=label,
                platform="incus",
                source=self.name,
                kind="vm" if is_vm else "container",
                parent=self.name,
                status=inst.get("status", "unknown").lower(),
                updatable=running,
                note=None if running else "not running",
                can_snapshot=True,
                backup=backups.get(tid),
            )
            snapshots[t.id] = IncusSnapshots(self.client, name, project)
            found.append((t, IncusExecutor(self.client, name, project)))
        self.snapshots = snapshots
        return found

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .backup_runs import BackupRun
from .config import AppConfig
from .executors import Executor
from .models import Target
from .power import GuestPower
from .providers import Provider
from .providers.demo import DemoProvider
from .providers.incus import IncusProvider
from .providers.proxmox import ProxmoxProvider
from .providers.ssh_hosts import SSHHostsProvider
from .snapshots import Snapshots

log = logging.getLogger(__name__)


def build_providers(cfg: AppConfig) -> list[Provider]:
    providers: list[Provider] = []
    # Where the Incus settings a backup saves are kept: next to the state file, in the data volume.
    dump_dir = Path(cfg.state_file).parent / "host-backups" if cfg.state_file else None
    providers += [ProxmoxProvider(p) for p in cfg.proxmox]
    providers += [IncusProvider(i, dump_dir) for i in cfg.incus]
    if cfg.hosts:
        providers.append(SSHHostsProvider(cfg.hosts))
    if cfg.demo:
        providers.append(DemoProvider())
    return providers


class Inventory:
    """Holds the discovered targets; re-discovers on demand (cached briefly)."""

    def __init__(self, providers: list[Provider], ttl: float = 30.0, min_interval: float = 5.0):
        self.providers = providers
        self.ttl = ttl
        # Even a forced refresh waits this long after the last one, so a client hammering
        # ?refresh=true can't turn into a flood of API calls against Proxmox/Incus.
        self.min_interval = min_interval
        self.targets: dict[str, tuple[Target, Executor | None]] = {}
        self.errors: dict[str, str] = {}
        # Snapshot handles of the guests that have them. Separate from the executor because a
        # rollback goes through the platform's API, so it works on a guest that can't run commands.
        self.snapshots: dict[str, Snapshots] = {}
        # What the "Back up" button runs, for the hosts that have one.
        self.backup_runs: dict[str, BackupRun] = {}
        self.power: dict[str, GuestPower] = {}
        self._refreshed = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            age = time.monotonic() - self._refreshed
            if self._refreshed and age < (self.min_interval if force else self.ttl):
                return
            results = await asyncio.gather(*(p.discover() for p in self.providers), return_exceptions=True)
            targets: dict[str, tuple[Target, Executor | None]] = {}
            snapshots: dict[str, Snapshots] = {}
            backup_runs: dict[str, BackupRun] = {}
            power: dict[str, GuestPower] = {}
            errors: dict[str, str] = {}
            for provider, res in zip(self.providers, results, strict=True):
                if isinstance(res, BaseException):
                    log.warning("discovery failed for %s: %s", provider.name, res)
                    errors[provider.name] = str(res) or type(res).__name__
                    # Keep what we knew before so one flaky provider doesn't blank the page.
                    targets.update({k: v for k, v in self.targets.items() if v[0].source == provider.name})
                    snapshots.update({k: v for k, v in self.snapshots.items() if k in targets})
                    backup_runs.update({k: v for k, v in self.backup_runs.items() if k in targets})
                    continue
                for target, executor in res:
                    targets[target.id] = (target, executor)
                snapshots.update(getattr(provider, "snapshots", {}))
                backup_runs.update(getattr(provider, "backup_runs", {}))
                power.update(getattr(provider, "power", {}))
            self.targets, self.snapshots, self.backup_runs, self.errors = targets, snapshots, backup_runs, errors
            self.power = power
            self._refreshed = time.monotonic()

    def invalidate(self) -> None:
        self._refreshed = 0.0

    def get(self, target_id: str) -> tuple[Target, Executor | None]:
        return self.targets[target_id]

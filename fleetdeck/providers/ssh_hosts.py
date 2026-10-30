from __future__ import annotations

import asyncio

from ..backup_runs import BackupRun, SshHostBackup
from ..config import HostConfig
from ..executors import SSHExecutor
from ..host_backups import HostBackups
from ..models import Target
from . import Discovered


class SSHHostsProvider:
    """Standalone machines reached over SSH, such as a Raspberry Pi."""

    name = "hosts"

    def __init__(self, hosts: list[HostConfig]):
        self.hosts = hosts
        self.backups = HostBackups()
        self.backup_runs: dict[str, BackupRun] = {}

    async def discover(self) -> Discovered:
        self.backup_runs = {
            f"ssh:{h.name}": SshHostBackup(SSHExecutor(h), h.backup, name=h.name)
            for h in self.hosts
            if h.host_key_problem is None
        }
        backups = await asyncio.gather(*(self.backups.for_machine(h.name, h) for h in self.hosts))
        return [
            (
                Target(
                    id=f"ssh:{h.name}",
                    name=h.name,
                    platform="ssh",
                    source="hosts",
                    update_command=h.update_command,
                    kind="host",
                    updatable=h.host_key_problem is None,
                    note=h.host_key_problem,
                    backup=backup,
                    can_backup=h.host_key_problem is None,
                    monitor_only=h.monitor_only,
                ),
                SSHExecutor(h),
            )
            for h, backup in zip(self.hosts, backups, strict=True)
        ]

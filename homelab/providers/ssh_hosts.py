from __future__ import annotations

import asyncio

from ..config import HostConfig
from ..executors import SSHExecutor
from ..host_backups import NOT_SET_UP, HostBackups
from ..models import BackupInfo, Target
from . import Discovered


class SSHHostsProvider:
    """Standalone machines reached over SSH, such as a Raspberry Pi."""

    name = "hosts"

    def __init__(self, hosts: list[HostConfig]):
        self.hosts = hosts
        self.backups = HostBackups()

    async def _backup(self, h: HostConfig, ex: SSHExecutor) -> BackupInfo:
        if h.backup is None:
            return BackupInfo(not_set_up=True, note=NOT_SET_UP)
        if h.host_key_problem:
            return BackupInfo(note=h.host_key_problem)
        return await self.backups.get(h.name, h.backup, ex)

    async def discover(self) -> Discovered:
        executors = [SSHExecutor(h) for h in self.hosts]
        backups = await asyncio.gather(*(self._backup(h, ex) for h, ex in zip(self.hosts, executors, strict=True)))
        return [
            (
                Target(
                    id=f"ssh:{h.name}",
                    name=h.name,
                    platform="ssh",
                    source="hosts",
                    kind="host",
                    updatable=h.host_key_problem is None,
                    note=h.host_key_problem,
                    backup=backup,
                ),
                ex,
            )
            for h, ex, backup in zip(self.hosts, executors, backups, strict=True)
        ]

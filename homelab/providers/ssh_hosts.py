from __future__ import annotations

import asyncio

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

    async def discover(self) -> Discovered:
        backups = await asyncio.gather(*(self.backups.for_machine(h.name, h) for h in self.hosts))
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
                SSHExecutor(h),
            )
            for h, backup in zip(self.hosts, backups, strict=True)
        ]

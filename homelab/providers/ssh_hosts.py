from __future__ import annotations

from ..config import HostConfig
from ..executors import SSHExecutor
from ..models import Target
from . import Discovered


class SSHHostsProvider:
    """Standalone machines reached over SSH, such as a Raspberry Pi."""

    name = "hosts"

    def __init__(self, hosts: list[HostConfig]):
        self.hosts = hosts

    async def discover(self) -> Discovered:
        return [
            (
                Target(id=f"ssh:{h.name}", name=h.name, platform="ssh", source="hosts", kind="host"),
                SSHExecutor(h),
            )
            for h in self.hosts
        ]

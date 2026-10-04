"""Fake targets so the dashboard can be explored without real infrastructure."""

from __future__ import annotations

import asyncio
import random

from ..models import ExecResult, Target
from ..updates import CHECK_SCRIPT
from . import Discovered

_APT = [
    "curl/stable-security 7.88.1-10+deb12u8 amd64 [upgradable from: 7.88.1-10+deb12u7]",
    "libssl3/stable-security 3.0.15-1~deb12u1 amd64 [upgradable from: 3.0.14-1~deb12u2]",
    "openssh-server/stable 1:9.2p1-2+deb12u4 amd64 [upgradable from: 1:9.2p1-2+deb12u3]",
    "tzdata/stable-updates 2025a-0+deb12u1 all [upgradable from: 2024b-0+deb12u1]",
    "proxmox-kernel-6.8/stable 6.8.12-5 amd64 [upgradable from: 6.8.12-4]",
]
_APK = ["busybox-1.36.1-r31 x86_64 {busybox} (GPL-2.0-only) [upgradable from: busybox-1.36.1-r29]"]


class DemoSnapshots:
    def __init__(self, executor: DemoExecutor):
        self.executor = executor
        self.saved: dict[str, list[str]] = {}

    async def create(self, name: str, description: str) -> None:
        await asyncio.sleep(0.5)
        self.saved[name] = list(self.executor.lines)

    async def rollback(self, name: str) -> None:
        await asyncio.sleep(1)
        self.executor.lines = list(self.saved[name])

    async def list_ours(self) -> list[str]:
        return list(self.saved)

    async def delete(self, name: str) -> None:
        del self.saved[name]


class DemoExecutor:
    def __init__(self, pm: str, os_name: str, lines: list[str]):
        self.pm, self.os_name, self.lines = pm, os_name, lines

    async def run(self, script, on_line=None, timeout=3600) -> ExecResult:
        await asyncio.sleep(random.uniform(0.3, 1.2))
        head = f"__PM__={self.pm}\n__OS__={self.os_name}\n"
        if script == CHECK_SCRIPT:
            body = "\n".join(self.lines)
            reboot = "yes" if any("kernel" in line for line in self.lines) else "no"
            return ExecResult(rc=0, stdout=f"{head}__BEGIN__\n{body}\n__END__\n__REBOOT__={reboot}\n")
        for line in self.lines:
            name = line.split("/")[0].split(" ")[0]
            if on_line:
                on_line("stdout", f"Unpacking {name} ...")
            await asyncio.sleep(0.4)
            if on_line:
                on_line("stdout", f"Setting up {name} ...")
        self.lines = []
        return ExecResult(rc=0, stdout=f"{head}__REBOOT__=no\n")


class DemoProvider:
    name = "demo"

    def __init__(self) -> None:
        self._found: Discovered | None = None

    async def discover(self) -> Discovered:
        if self._found is not None:
            return self._found
        deb = "Debian GNU/Linux 12 (bookworm)"
        items = [
            ("proxmox", "pve", "host", None, "apt", "Proxmox VE 8.3 (Debian 12)", _APT),
            ("proxmox", "pihole", "lxc", "pve", "apt", deb, _APT[:2]),
            ("proxmox", "alpine-dns", "lxc", "pve", "apk", "Alpine Linux v3.20", _APK),
            ("proxmox", "docker-vm", "vm", "pve", "apt", "Ubuntu 24.04.1 LTS", _APT[:4]),
            ("incus", "incus01", "host", None, "apt", deb, _APT[1:3]),
            ("incus", "web", "container", "incus01", "apt", deb, []),
            ("incus", "fedora-vm", "vm", "incus01", "dnf", "Fedora Linux 40", []),
            ("ssh", "pi4", "host", None, "apt", "Raspberry Pi OS (Debian 12)", _APT[:3]),
        ]
        found: Discovered = []
        for platform, name, kind, parent, pm, os_name, lines in items:
            t = Target(
                id=f"demo:{platform}:{name}",
                name=name,
                platform=platform,  # type: ignore[arg-type]
                source="demo",
                kind=kind,  # type: ignore[arg-type]
                parent=parent,
                can_snapshot=kind != "host" and platform != "ssh",
            )
            ex = DemoExecutor(pm, os_name, list(lines))
            if t.can_snapshot:
                ex.snapshots = DemoSnapshots(ex)
            found.append((t, ex))
        self._found = found
        return found

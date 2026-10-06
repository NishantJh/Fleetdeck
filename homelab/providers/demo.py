"""Fake targets so the dashboard can be explored without real infrastructure."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

from ..models import BackupInfo, ExecResult, Target
from ..updates import BOOT_ID_SCRIPT, CHECK_SCRIPT, REBOOT_NOW_SCRIPT
from . import Discovered

_APT = [
    "curl/stable-security 7.88.1-10+deb12u8 amd64 [upgradable from: 7.88.1-10+deb12u7]",
    "libssl3/stable-security 3.0.15-1~deb12u1 amd64 [upgradable from: 3.0.14-1~deb12u2]",
    "openssh-server/stable 1:9.2p1-2+deb12u4 amd64 [upgradable from: 1:9.2p1-2+deb12u3]",
    "tzdata/stable-updates 2025a-0+deb12u1 all [upgradable from: 2024b-0+deb12u1]",
    "proxmox-kernel-6.8/stable 6.8.12-5 amd64 [upgradable from: 6.8.12-4]",
]
_APK = ["busybox-1.36.1-r31 x86_64 {busybox} (GPL-2.0-only) [upgradable from: busybox-1.36.1-r29]"]


class DemoExecutor:
    def __init__(self, pm: str, os_name: str, lines: list[str]):
        self.pm, self.os_name, self.lines = pm, os_name, lines
        self.boot_id = uuid.uuid4().hex
        self.reboot_pending = any("kernel" in line for line in lines)

    async def run(self, script, on_line=None, timeout=3600) -> ExecResult:
        await asyncio.sleep(random.uniform(0.3, 1.2))
        head = f"__PM__={self.pm}\n__OS__={self.os_name}\n"
        if script == BOOT_ID_SCRIPT:
            return ExecResult(rc=0, stdout=f"__BOOT__={self.boot_id}\n")
        if script == REBOOT_NOW_SCRIPT:
            self.boot_id, self.reboot_pending = uuid.uuid4().hex, False
            return ExecResult(rc=0, stdout="== Reboot scheduled (demo)\n")
        reboot = "yes" if self.reboot_pending else "no"
        if script == CHECK_SCRIPT:
            body = "\n".join(self.lines)
            return ExecResult(rc=0, stdout=f"{head}__BEGIN__\n{body}\n__END__\n__REBOOT__={reboot}\n")
        for line in self.lines:
            name = line.split("/")[0].split(" ")[0]
            if on_line:
                on_line("stdout", f"Unpacking {name} ...")
            await asyncio.sleep(0.4)
            if on_line:
                on_line("stdout", f"Setting up {name} ...")
        self.lines = []
        return ExecResult(rc=0, stdout=f"{head}__REBOOT__={reboot}\n")


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
        now = datetime.now(UTC)
        backups = {
            "pihole": BackupInfo(last=now - timedelta(hours=5), storage="pbs", count=14, in_job=True, verified=True),
            "alpine-dns": BackupInfo(last=now - timedelta(days=4), storage="local", count=3, stale=True, in_job=True),
            "docker-vm": BackupInfo(stale=True, in_job=False),
        }
        found: Discovered = []
        for platform, name, kind, parent, pm, os_name, lines in items:
            t = Target(
                id=f"demo:{platform}:{name}",
                name=name,
                platform=platform,  # type: ignore[arg-type]
                source="demo",
                kind=kind,  # type: ignore[arg-type]
                parent=parent,
                backup=backups.get(name),
            )
            found.append((t, DemoExecutor(pm, os_name, list(lines))))
        self._found = found
        return found

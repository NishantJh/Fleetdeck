"""Fake targets so the dashboard can be explored without real infrastructure."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

from ..host_backups import NOT_SET_UP
from ..models import BackupInfo, ExecResult, Target
from ..stacks import SCAN_SCRIPT, US
from ..updates import (
    BOOT_ID_SCRIPT,
    CHECK_SCRIPT,
    COMMUNITY_SCRIPT_DETECT,
    COMMUNITY_UPDATE_SCRIPT,
    REBOOT_NOW_SCRIPT,
)
from . import Discovered

_APT = [
    "curl/stable-security 7.88.1-10+deb12u8 amd64 [upgradable from: 7.88.1-10+deb12u7]",
    "libssl3/stable-security 3.0.15-1~deb12u1 amd64 [upgradable from: 3.0.14-1~deb12u2]",
    "openssh-server/stable 1:9.2p1-2+deb12u4 amd64 [upgradable from: 1:9.2p1-2+deb12u3]",
    "tzdata/stable-updates 2025a-0+deb12u1 all [upgradable from: 2024b-0+deb12u1]",
    "proxmox-kernel-6.8/stable 6.8.12-5 amd64 [upgradable from: 6.8.12-4]",
]
_APK = ["busybox-1.36.1-r31 x86_64 {busybox} (GPL-2.0-only) [upgradable from: busybox-1.36.1-r29]"]


def _name(line: str) -> str:
    return line.split("/")[0].split(" ")[0]


class DemoSnapshots:
    def __init__(self, executor: DemoExecutor):
        self.executor = executor
        self.saved: dict[str, list[str]] = {}

    async def create(self, name: str, description: str) -> str | None:
        await asyncio.sleep(0.5)
        self.saved[name] = list(self.executor.lines)
        return None

    async def rollback(self, name: str) -> str | None:
        await asyncio.sleep(1)
        self.executor.lines = list(self.saved[name])
        return None

    async def list_ours(self) -> list[str]:
        return list(self.saved)

    async def delete(self, name: str) -> None:
        del self.saved[name]


class DemoExecutor:
    def __init__(self, pm: str, os_name: str, lines: list[str], app: str | None = None):
        self.pm, self.os_name, self.lines = pm, os_name, lines
        # Made by a community-scripts helper script for this app.
        self.app = app
        self.boot_id = uuid.uuid4().hex
        self.reboot_pending = any("kernel" in line for line in lines)
        # Demo Docker: stack name -> (services, behind). None: Docker is not installed here.
        self.stacks: dict[str, tuple[list[str], bool]] | None = None

    def _scan(self) -> str:
        if self.stacks is None:
            return "__DOCKER__=none\n"
        out = ["__DOCKER__=yes", "__COMPOSE__=v2"]
        images = {}
        for name, (services, behind) in self.stacks.items():
            for svc in services:
                image = f"{svc}:latest" if svc != "arcane" else "ghcr.io/getarcaneapp/arcane:latest"
                images[image] = "sha256:new"
                row = ["__C__", name, svc, f"/opt/{name}", f"/opt/{name}/compose.yaml", image]
                row += ["sha256:old" if behind else "sha256:new", "running", uuid.uuid4().hex * 2]
                out.append(US.join(row))
        out += [US.join(["__I__", image, image_id]) for image, image_id in images.items()]
        return "\n".join(out) + "\n"

    async def _docker(self, script: str, on_line) -> ExecResult:
        """The Docker check, pull and update scripts (see stacks.py), as a pretend run."""
        if script == SCAN_SCRIPT:
            return ExecResult(rc=0, stdout=self._scan())
        stacks = self.stacks or {}
        if " up -d " in script:
            name = script.split(" -p ", 1)[1].split(" ", 1)[0]
            for line in (f"== Pulling {name}", f"Image {name} Pulled", f"== Restarting what changed in {name}"):
                if on_line:
                    on_line("stdout", line)
                await asyncio.sleep(0.5)
            if name in stacks:
                stacks[name] = (stacks[name][0], False)
            return ExecResult(rc=0)
        for name in stacks:  # a check: pull every stack
            if on_line:
                on_line("stdout", f"== Checking {name}")
            await asyncio.sleep(0.3)
        return ExecResult(rc=0)

    def _check_output(self, body: str) -> list[tuple[str, str]]:
        """What the real check script prints, so the Activity log looks like a real run."""
        out = [("stdout", "__BEGIN__")]
        out += [("stdout", line) for line in body.splitlines()]
        out += [("stdout", "__END__"), ("stdout", f"__REBOOT__={'yes' if self.reboot_pending else 'no'}")]
        if self.pm == "apt":
            out += [
                ("stderr", "Hit:1 http://deb.debian.org/debian bookworm InRelease"),
                ("stderr", "Get:2 http://deb.debian.org/debian bookworm-updates InRelease [55.4 kB]"),
                ("stderr", "Get:3 http://deb.debian.org/debian-security bookworm-security InRelease [48.0 kB]"),
                ("stderr", "Fetched 103 kB in 1s (151 kB/s)"),
                ("stderr", "Reading package lists..."),
            ]
        return out

    async def run(self, script, on_line=None, timeout=3600) -> ExecResult:
        await asyncio.sleep(random.uniform(0.3, 1.2))
        head = f"__PM__={self.pm}\n__OS__={self.os_name}\n"
        if self.app:
            head += f"__CSAPP__={self.app}\n"
        if script == BOOT_ID_SCRIPT:
            return ExecResult(rc=0, stdout=f"__BOOT__={self.boot_id}\n")
        if script == SCAN_SCRIPT or "docker compose version" in script:
            return await self._docker(script, on_line)
        if script == REBOOT_NOW_SCRIPT:
            self.boot_id, self.reboot_pending = uuid.uuid4().hex, False
            return ExecResult(rc=0, stdout="== Reboot scheduled (demo)\n")
        if script == COMMUNITY_SCRIPT_DETECT:
            return ExecResult(rc=0, stdout=f"__CSAPP__={self.app}\n" if self.app else "")
        if script == COMMUNITY_UPDATE_SCRIPT:
            for line in (f"Updated {self.app}", f"Restarted {self.app}", "Updated successfully!"):
                if on_line:
                    on_line("stdout", f"\x1b[32m✔️\x1b[m {line}")
                await asyncio.sleep(0.4)
            return ExecResult(rc=0)
        reboot = "yes" if self.reboot_pending else "no"
        if script == CHECK_SCRIPT:
            body = "\n".join(self.lines)
            if on_line:
                for line in self._check_output(body):
                    on_line(*line)
            return ExecResult(rc=0, stdout=f"{head}__BEGIN__\n{body}\n__END__\n__REBOOT__={reboot}\n")
        if on_line and self.pm == "apt":
            on_line("stdout", "Reading package lists...")
            on_line("stdout", "Building dependency tree...")
            on_line("stdout", f"The following packages will be upgraded: {' '.join(_name(x) for x in self.lines)}")
        for line in self.lines:
            name = line.split("/")[0].split(" ")[0]
            if on_line:
                on_line("stdout", f"Unpacking {name} ...")
            await asyncio.sleep(0.4)
            if on_line:
                on_line("stdout", f"Setting up {name} ...")
        self.lines = []
        return ExecResult(rc=0, stdout=f"{head}__REBOOT__={reboot}\n")


class DemoBackup:
    """What the Back up button does in the demo: the steps of a real run, without touching anything."""

    def __init__(self, steps: list[str]):
        self.steps = steps

    async def run(self, on_line, destination: str | None = None, dashboard_folder=None, age_recipient=None) -> None:
        for step in self.steps:
            on_line("stdout", step)
            await asyncio.sleep(0.5)
        if destination:
            on_line(
                "stdout",
                f"(demo: nothing was written to {dashboard_folder if destination == 'dashboard' else destination})",
            )


_DEMO_BACKUPS = {
    "pve": [
        "== Backing up the node's config",
        "sending the node's config to the Proxmox Backup Server storage pbs",
    ],
    "incus01": ["== Saving the Incus settings", "== Archiving the host's config"],
    "pi4": ["== Archiving the machine's config", "wrote /var/backups/homelab-manager/homelab-config-pi4.tar.gz"],
}


class DemoProvider:
    name = "demo"

    def __init__(self) -> None:
        self._found: Discovered | None = None
        self.snapshots: dict[str, DemoSnapshots] = {}
        self.backup_runs: dict[str, DemoBackup] = {}

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
            "web": BackupInfo(last=now - timedelta(hours=20), storage="snapshot", count=7, in_job=True),
            "fedora-vm": BackupInfo(last=now - timedelta(days=9), storage="export", count=1, stale=True),
            "pi4": BackupInfo(last=now - timedelta(hours=11), storage="restic", count=30),
            "pve": BackupInfo(last=now - timedelta(days=1), storage="/mnt/nas/pve-etc", count=12),
            "incus01": BackupInfo(not_set_up=True, note=NOT_SET_UP),
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
                can_snapshot=kind != "host" and platform != "ssh",
                can_backup=name in _DEMO_BACKUPS,
            )
            if t.can_backup:
                self.backup_runs[t.id] = DemoBackup(_DEMO_BACKUPS[name])
            ex = DemoExecutor(pm, os_name, list(lines), app="pihole" if name == "pihole" else None)
            if name == "docker-vm":
                ex.stacks = {"arcane": (["arcane"], False), "media": (["jellyfin", "sonarr"], True)}
            elif name == "pi4":
                ex.stacks = {"pihole": (["pihole"], False)}
            if t.can_snapshot:
                self.snapshots[t.id] = DemoSnapshots(ex)
            found.append((t, ex))
        self._found = found
        return found

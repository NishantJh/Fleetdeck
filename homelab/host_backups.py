"""The newest backup of a plain machine (e.g. a Raspberry Pi), from a folder or a restic/borg repository.

Everything here only reads: restic runs with --no-lock and borg with --bypass-lock, so a check can
never get in the way of a backup that is running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from datetime import UTC, datetime, timedelta, timezone

from .config import HostBackupConfig, SSHConfig
from .executors import Executor, SSHExecutor
from .models import BackupInfo

log = logging.getLogger(__name__)

# Reading a repository can take a while, so the answer is kept this long.
TTL = 300.0
# How long discovery waits for a first answer before showing "checking" and carrying on.
FIRST_WAIT = 3.0
RUN_TIMEOUT = 60.0
NOT_SET_UP = (
    "Not set up. Add a backup: entry to this machine's SSH settings in config.yaml: a folder your "
    "backups go to (type: path), or a restic or borg repository."
)
NO_SSH = "Not set up. Add SSH settings for this machine, then a backup: entry under them in config.yaml."


# Nothing may wait for an answer: stdin is closed, and borg is told to say "no" to its questions
# (a moved or unencrypted repository) instead of asking.
NO_PROMPTS = (
    "exec </dev/null; export BORG_RELOCATED_REPO_ACCESS_IS_OK=no BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK=no; "
)


def _env(cfg: HostBackupConfig) -> str:
    # `set -a` exports what the file sets.
    if not cfg.env_file:
        return ""
    return f"set -a; . {shlex.quote(cfg.env_file)} || exit 3; set +a; "


def script(cfg: HostBackupConfig) -> str:
    if cfg.type == "path":
        p = shlex.quote(cfg.path or "")
        # Prints "<count> <newest mtime as epoch seconds>".
        # -H: follow the path itself when it's a symlink (a linked NAS mount, latest.tar.gz -> ...).
        return (
            f"[ -e {p} ] || {{ echo 'not found' >&2; exit 3; }}; "
            f"find -H {p} -type f -printf '%T@\\n' | sort -n | awk 'END {{ print NR, $0 }}'"
        )
    repo = shlex.quote(cfg.repository) if cfg.repository else None
    if cfg.type == "restic":
        return NO_PROMPTS + _env(cfg) + f"restic {f'-r {repo} ' if repo else ''}snapshots --json --no-lock"
    # borg 1.x prints local times without an offset, so print the machine's offset first.
    return NO_PROMPTS + _env(cfg) + f"date +%z; borg list --json --bypass-lock {repo or ''}".rstrip()


_FRACTION = re.compile(r"(\.\d{6})\d+")


def _when(value: str, offset: timezone | None = None) -> datetime:
    # restic has nanoseconds; Python reads at most microseconds.
    dt = datetime.fromisoformat(_FRACTION.sub(r"\1", value.replace("Z", "+00:00")))
    return dt.replace(tzinfo=offset or UTC) if dt.tzinfo is None else dt


def parse(cfg: HostBackupConfig, stdout: str) -> tuple[datetime | None, int]:
    """The newest backup's time and how many there are."""
    if cfg.type == "path":
        count, _, newest = stdout.strip().partition(" ")
        return (datetime.fromtimestamp(float(newest), UTC) if newest else None), int(count or 0)
    if cfg.type == "restic":
        times = [_when(s["time"]) for s in json.loads(stdout) or []]
    else:
        first, _, rest = stdout.strip().partition("\n")
        sign = -1 if first.startswith("-") else 1
        offset = timezone(sign * timedelta(hours=int(first[1:3]), minutes=int(first[3:5])))
        times = [_when(a.get("start") or a["time"], offset) for a in json.loads(rest).get("archives", [])]
    return (max(times) if times else None), len(times)


async def read(cfg: HostBackupConfig, ex: Executor) -> BackupInfo:
    where = {"path": cfg.path, "restic": "restic", "borg": "borg"}[cfg.type]
    try:
        r = await ex.run(script(cfg), timeout=RUN_TIMEOUT)
        if r.rc == 127 and cfg.type != "path":
            return BackupInfo(note=f"{cfg.type} is not installed on this machine")
        if r.rc != 0:
            err = (r.stderr.strip() or r.stdout.strip()).splitlines()
            return BackupInfo(note=f"could not read {where}: {err[-1] if err else f'exit code {r.rc}'}")
        last, count = parse(cfg, r.stdout)
    except TimeoutError:
        return BackupInfo(note=f"could not read {where}: no answer after {RUN_TIMEOUT:.0f} s")
    except Exception as e:  # noqa: BLE001 - backups are extra; never hide the machine
        return BackupInfo(note=f"could not read {where}: {e or type(e).__name__}")
    stale = last is None or datetime.now(UTC) - last > timedelta(hours=cfg.max_age_hours)
    return BackupInfo(last=last, storage=where, count=count, stale=stale)


class HostBackups:
    """Keeps each machine's last answer and refreshes it in the background, so a slow or offline
    machine never holds up the dashboard."""

    def __init__(self) -> None:
        self._info: dict[str, tuple[float, BackupInfo]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def for_machine(self, name: str, ssh: SSHConfig | None, online: bool = True) -> BackupInfo:
        """The backup of a machine reached over ``ssh``, or why there is none to show."""
        if ssh is None:
            return BackupInfo(not_set_up=True, note=NO_SSH)
        if ssh.backup is None:
            return BackupInfo(not_set_up=True, note=NOT_SET_UP)
        if ssh.host_key_problem:
            return BackupInfo(note=ssh.host_key_problem)
        if not online:
            cached = self._info.get(name)
            return cached[1] if cached else BackupInfo(note="offline")
        return await self.get(name, ssh.backup, SSHExecutor(ssh))

    async def get(self, name: str, cfg: HostBackupConfig, ex: Executor) -> BackupInfo:
        cached = self._info.get(name)
        if cached and time.monotonic() - cached[0] < TTL:
            return cached[1]
        task = self._tasks.get(name)
        if task is None:
            task = self._tasks[name] = asyncio.create_task(self._refresh(name, cfg, ex))
        if cached:
            return cached[1]
        try:
            return await asyncio.wait_for(asyncio.shield(task), FIRST_WAIT)
        except TimeoutError:
            return BackupInfo(note="still checking")

    async def _refresh(self, name: str, cfg: HostBackupConfig, ex: Executor) -> BackupInfo:
        try:
            info = await read(cfg, ex)
            self._info[name] = (time.monotonic(), info)
            return info
        finally:
            self._tasks.pop(name, None)

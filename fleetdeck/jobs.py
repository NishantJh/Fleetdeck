from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import detached
from .backup_runs import DASHBOARD
from .inventory import Inventory
from .models import CheckResult, DockerSettings, Job, JobAction, Stack, StacksResult, SuccessfulCheck, Target
from .snapshots import Snapshots, prune, snapshot_name, unsupported
from .stacks import SCAN_SCRIPT, _own_id, parse_pull_failures, parse_scan, pull_script, update_script
from .store import Store, mark_interrupted
from .updates import (
    BOOT_ID_SCRIPT,
    CHECK_SCRIPT,
    COMMUNITY_SCRIPT_DETECT,
    COMMUNITY_UPDATE_SCRIPT,
    DOCKER_DETECT_SCRIPT,
    REBOOT_NOW_SCRIPT,
    SECURITY_UPGRADE_SCRIPT,
    UPGRADE_SCRIPT,
    clean_line,
    custom_update_script,
    parse_boot_id,
    parse_check,
    parse_community_script,
    parse_docker_installed,
    parse_reboot,
)

log = logging.getLogger(__name__)
MAX_JOBS = 500
# Per job. A misbehaving guest could otherwise stream output until the dashboard runs out of memory.
MAX_LOG_LINES = 20_000
# How long a rebooting machine gets to come back, and how often we look.
REBOOT_TIMEOUT = 900.0
REBOOT_POLL = 5.0
# A machine can answer before all its services are up (a Proxmox node's API starts after sshd);
# the re-check after a reboot is retried this many times before it is reported as failed.
RECHECK_TRIES = 6
# What a guest can be doing that stops its host from being updated or rebooted.
GUEST_ACTIONS_BLOCKING_HOST = ("start", "upgrade", "stack-update", "dockhand-update", "reboot", "rollback")
# What a monitor-only machine never runs: installing OS, app or Docker updates.
UPDATES = ("upgrade", "stack-update")

# An upgrade running detached on its target gets this long before the dashboard stops waiting.
UPGRADE_TIMEOUT = 3600.0
# Pulling every stack's images on a machine.
PULL_TIMEOUT = 3600.0
# After a restart, how long to wait for a job's machine to show up in the inventory again.
RESUME_WAIT = 600.0
RESUME_POLL = 15.0


def _now() -> datetime:
    return datetime.now(UTC)


class _Slot:
    """One of the ``max_parallel`` slots, which a job can give up while it only waits."""

    def __init__(self, sem: asyncio.Semaphore):
        self.sem = sem
        self.held = False

    async def __aenter__(self) -> _Slot:
        await self.sem.acquire()
        self.held = True
        return self

    async def __aexit__(self, *exc) -> None:
        if self.held:
            self.held = False
            self.sem.release()

    @asynccontextmanager
    async def released(self):
        self.held = False
        self.sem.release()
        try:
            yield
        finally:
            await self.sem.acquire()
            self.held = True


class JobManager:
    """Runs checks and upgrades in the background, at most ``max_parallel`` at a time.

    Only one job per target runs at once; later ones wait their turn. With a ``store``, check
    results and finished jobs are saved and reloaded on the next start.
    """

    def __init__(
        self,
        inventory: Inventory,
        max_parallel: int = 4,
        store: Store | None = None,
        snapshot_keep: int = 3,
        community_scripts_update: bool = True,
        backup_folder: Path | None = None,
        age_recipient: str | None = None,
        backup_password: str | None = None,
        dockhand_targets: dict[str, str] | None = None,
    ):
        self.inventory = inventory
        # Where backups that are saved on the dashboard go (see backup_runs.DASHBOARD).
        self.backup_folder = backup_folder
        # Encrypt host config archives to this age public key (None: they are not encrypted).
        self.age_recipient = age_recipient
        # Encrypt archives saved on the dashboard with this password instead (see config.backup_password).
        self.backup_password = backup_password
        self.snapshot_keep = snapshot_keep
        self.community_scripts_update = community_scripts_update
        self.dockhand_targets = dockhand_targets or {}
        self.sem = asyncio.Semaphore(max_parallel)
        self.store = store
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.checks: dict[str, CheckResult] = {}
        # Machine id -> what its last Docker check found (see stacks.py).
        self.stacks: dict[str, StacksResult] = {}
        self.docker_settings: dict[str, DockerSettings] = {}
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        # Hosts that are rebooting, or waiting for their guests' jobs so they can reboot.
        self._rebooting: set[str] = set()
        # Ids of jobs that are only waiting for a guest's update to finish (not working on their machine).
        self._waiting_for_guests: set[str] = set()
        # Machines hidden from the dashboard: left out of scheduled checks and their notifications.
        self.hidden: set[str] = set()
        # Machines set to monitor-only on the dashboard (the config can set it too, see Target.monitor_only).
        self.monitor_only_ids: set[str] = set()
        # Host id -> the folder its Back up button is set to use every time.
        self.backup_places: dict[str, str] = {}
        # Machine id -> its own update command, set on the dashboard (the config can set one too, see update_command).
        self.update_commands: dict[str, str] = {}
        if store is not None:
            self.checks = store.load_checks()
            self.stacks = store.load_stacks()
            self.docker_settings = store.load_docker_settings()
            self.hidden = store.load_hidden()
            self.monitor_only_ids = store.load_monitor_only()
            self.backup_places = store.load_backup_places()
            self.update_commands = store.load_update_commands()
            self.jobs = OrderedDict((j.id, j) for j in store.load_jobs(MAX_JOBS))

    def _set_check(self, target_id: str, check: CheckResult) -> None:
        prev = self.checks.get(target_id)
        if check.error and prev:
            check.last_successful = (
                prev.last_successful
                if prev.error
                else SuccessfulCheck(
                    packages=prev.packages, reboot_required=prev.reboot_required, checked_at=prev.checked_at
                )
            )
            check.os = check.os or prev.os
            check.package_manager = check.package_manager or prev.package_manager
            check.runs_dashboard = prev.runs_dashboard
            check.community_script = prev.community_script
            if check.docker_installed is None:
                check.docker_installed = prev.docker_installed
        self.checks[target_id] = check
        if self.store is not None:
            self._persist(self.store.save_check, target_id, check)

    def get_docker_settings(self, target_id: str) -> DockerSettings:
        if target_id in self.dockhand_targets:
            return DockerSettings(
                mode="external", manager_name="Dockhand", manager_url=self.dockhand_targets[target_id]
            )
        return self.docker_settings.get(target_id, DockerSettings())

    def set_docker_settings(self, target_id: str, settings: DockerSettings) -> None:
        self.inventory.get(target_id)  # reject stale / unknown IDs
        if target_id in self.dockhand_targets:
            raise ValueError("This machine is mapped to Dockhand in configuration; change its mapping in Settings")
        if any(
            j.target_id == target_id and j.action in ("stacks", "stack-update") and j.status in ("queued", "running")
            for j in self.jobs.values()
        ):
            raise ValueError("Wait for this machine's Docker jobs to finish before changing its settings")
        self.stage_docker_settings(target_id, settings)

    def stage_docker_settings(self, target_id: str, settings: DockerSettings) -> None:
        """Persist a policy alongside a staged provider-config change. Live Dockhand mappings
        continue to override it until Apply restarts the app; native jobs cannot bypass them."""
        self.inventory.get(target_id)
        if self.active_job(target_id):
            raise ValueError("Wait for this machine's jobs to finish before changing its settings")
        # Unlike check history, a policy must be saved successfully before the UI reports success.
        if self.store is not None:
            self.store.save_docker_settings(target_id, settings)
        self.docker_settings[target_id] = settings

    def _ensure_docker_action(self, target_id: str, action: JobAction) -> None:
        if action not in ("stacks", "stack-update"):
            return
        settings = self.get_docker_settings(target_id)
        if settings.mode in ("external", "disabled"):
            raise ValueError(f"Docker management is {settings.mode} on this machine")
        if action == "stack-update" and settings.mode == "monitor":
            raise ValueError("Docker is monitor-only on this machine; stack updates are disabled")

    def _set_stacks(self, target_id: str, result: StacksResult) -> None:
        self.stacks[target_id] = result
        if self.store is not None:
            self._persist(self.store.save_stacks, target_id, result)

    def set_hidden(self, target_ids: list[str], hidden: bool) -> None:
        if hidden:
            self.hidden.update(target_ids)
        else:
            self.hidden.difference_update(target_ids)
        if self.store is not None:
            self._persist(self.store.set_hidden, target_ids, hidden)

    def set_monitor_only(self, target_ids: list[str], on: bool) -> None:
        if on:
            self.monitor_only_ids.update(target_ids)
        else:
            self.monitor_only_ids.difference_update(target_ids)
        if self.store is not None:
            self._persist(self.store.set_monitor_only, target_ids, on)

    def monitor_only(self, target: Target) -> bool:
        """Whether the target is only monitored: checks, reboots, backups and snapshots still run, updates don't."""
        return target.monitor_only or target.id in self.monitor_only_ids

    def _ensure_updates_allowed(self, target: Target, action: JobAction) -> None:
        if action in UPDATES and self.monitor_only(target):
            raise ValueError(f"{target.name}: monitor only, updates are not installed from here")

    def set_backup_place(self, target_id: str, path: str | None) -> None:
        if path is None:
            self.backup_places.pop(target_id, None)
        else:
            self.backup_places[target_id] = path
        if self.store is not None:
            self._persist(self.store.set_backup_place, target_id, path)

    def update_command(self, target: Target) -> str | None:
        """The command that updates this machine instead of the built-in OS upgrade, if it has one. The config's
        wins over the dashboard's."""
        return target.update_command or self.update_commands.get(target.id)

    def set_update_command(self, target_id: str, command: str | None) -> None:
        if command is None:
            self.update_commands.pop(target_id, None)
        else:
            self.update_commands[target_id] = command
        if self.store is not None:
            self._persist(self.store.set_update_command, target_id, command)

    def _save_job(self, job: Job) -> None:
        if self.store is not None:
            self._persist(self.store.save_job, job)

    @staticmethod
    def _persist(fn, *args) -> None:
        # A full disk or read-only volume must not break checks and upgrades themselves.
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("could not save state")

    async def shutdown(self) -> None:
        """Stop running jobs and record them as interrupted, before the store is closed.

        Jobs that can be picked up again (see :meth:`resume`) stay running.
        """
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self.jobs.values():
            if job.resumable:
                job.log.append("== Dashboard stopping; it picks this job up again when it starts")
                self._save_job(job)
            elif job.status in ("queued", "running"):
                mark_interrupted(job)
                self._save_job(job)

    def resume(self) -> None:
        """Pick up the jobs a restart cut off that are still going on their machines."""
        for job in self.jobs.values():
            if job.resumable and not any(t.get_name() == job.id for t in self._tasks):
                self._start(job, resume=True)

    def _start(self, job: Job, resume: bool = False) -> None:
        task = asyncio.create_task(self._run(job, resume), name=job.id)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def active_job(self, target_id: str) -> Job | None:
        for job in reversed(self.jobs.values()):
            if job.target_id == target_id and job.status in ("queued", "running"):
                return job
        return None

    def _ensure_can_start(self, target: Target) -> None:
        if (
            target.platform != "proxmox"
            or target.kind not in ("vm", "lxc")
            or target.status != "stopped"
            or not target.can_start
            or target.id not in self.inventory.power
        ):
            raise ValueError(f"{target.name}: only eligible stopped Proxmox VMs and LXCs can be started")
        host = self._host_id(target)
        parent_job = self.active_job(host) if host else None
        if parent_job and parent_job.action in ("upgrade", "reboot", "rollback"):
            raise ValueError(f"{target.name}: its host is busy; wait before booting the guest")
        if why := self.update_blocked(target):
            raise ValueError(f"{target.name}: {why}")

    def submit(
        self,
        target_id: str,
        action: JobAction,
        reboot_if_required: bool = False,
        snapshot_first: bool = False,
        snapshot: str | None = None,
        destination: str | None = None,
        stack: str | None = None,
        prune: bool = False,
        security_only: bool = False,
    ) -> Job:
        if action == "dockhand-update":
            raise ValueError("Use the confirmed Dockhand update endpoint")
        target, executor = self.inventory.get(target_id)
        self._ensure_docker_action(target_id, action)
        if action == "start":
            self._ensure_can_start(target)
        elif action == "rollback":
            _ensure_can_roll_back(target, self.inventory.snapshots.get(target_id))
            if not snapshot:
                raise ValueError(f"{target.name}: no snapshot to roll back to")
        elif action == "backup":
            _ensure_can_back_up(target, self.inventory.backup_runs.get(target_id))
        else:
            _ensure_runnable(target, executor, action)
            self._ensure_updates_allowed(target, action)
            if action == "stack-update":
                _ensure_stack_updatable(target, self.stacks.get(target_id), stack)
        host_id = self._host_id(target)
        if host_id in self._rebooting:
            raise ValueError(f"{target.name}: its host {target.parent} is rebooting")
        existing = self.active_job(target_id)
        if existing and existing.action == action and existing.stack == stack:
            # Asking again with "reboot if required" ticked still counts, even though the job exists.
            if action == "upgrade" and reboot_if_required:
                existing.reboot_if_required = True
            return existing
        if action in ("upgrade", "reboot") and (why := self.update_blocked(target)):
            raise ValueError(f"{target.name}: {why}")
        command = self.update_command(target) if action == "upgrade" else None
        if command and security_only:
            # Its own command may install anything, so it is never run as a "security only" update.
            raise ValueError(f"{target.name}: has its own update command, so security updates aren't installed alone")
        job = Job(
            id=uuid.uuid4().hex[:12],
            target_id=target_id,
            target_name=target.name,
            action=action,
            reboot_if_required=reboot_if_required and action == "upgrade",
            security_only=security_only and action == "upgrade",
            update_command=command,
            snapshot_first=snapshot_first and action == "upgrade",
            snapshot=snapshot if action == "rollback" else None,
            destination=destination if action == "backup" else None,
            stack=stack if action == "stack-update" else None,
            prune=prune and action == "stack-update",
            created_at=_now(),
        )
        self.jobs[job.id] = job
        self._save_job(job)
        self._evict()
        self._start(job)
        return job

    def submit_dockhand_update(self, target_id: str, operation) -> Job:
        """External deployment uses normal job history/locks but is NEVER replayed on restart."""
        target, _ = self.inventory.get(target_id)
        if self.active_job(target_id):
            raise ValueError("Wait for this machine's current job to finish")
        job = Job(
            id=uuid.uuid4().hex[:12],
            target_id=target_id,
            target_name=target.name,
            action="dockhand-update",
            created_at=_now(),
        )
        self.jobs[job.id] = job
        self._save_job(job)
        self._evict()

        async def run():
            async with self._target_locks.setdefault(target_id, asyncio.Lock()), self.sem:
                job.status, job.started_at = "running", _now()
                self._save_job(job)
                try:
                    await operation(lambda stream, line: job.log.append(line))
                    job.status = "succeeded"
                except Exception:
                    # Provider errors may contain secrets. Only fixed text goes into logs/history.
                    job.status, job.error = (
                        "failed",
                        "Dockhand update not fully confirmed; inspect Dockhand before retrying",
                    )
                    job.log.append(f"!! {job.error}")
                finally:
                    if job.status in ("succeeded", "failed"):
                        job.finished_at = _now()
                        self._save_job(job)

        task = asyncio.create_task(run(), name=job.id)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def _evict(self) -> None:
        """Forget the oldest finished jobs once there are too many; active ones are always kept."""
        excess = len(self.jobs) - MAX_JOBS
        if excess <= 0:
            return
        done = [jid for jid, j in self.jobs.items() if j.status in ("succeeded", "failed")]
        for jid in done[:excess]:
            del self.jobs[jid]
        if self.store is not None:
            self._persist(self.store.delete_jobs, done[:excess])

    async def _run(self, job: Job, resume: bool = False) -> None:
        lock = self._target_locks.setdefault(job.target_id, asyncio.Lock())
        async with lock, _Slot(self.sem) as slot:
            if not resume:
                job.status, job.started_at = "running", _now()
                self._save_job(job)

            def on_line(stream: str, line: str) -> None:
                # Stop recording (rather than trim, which would shift the offsets the UI pages by). The
                # job's own status lines and error are still appended after this.
                if len(job.log) == MAX_LOG_LINES:
                    job.log.append(f"! … output truncated after {MAX_LOG_LINES} lines")
                if len(job.log) <= MAX_LOG_LINES:
                    job.log.append(line if stream == "stdout" else f"! {line}")

            try:
                if resume:
                    job.log.append("== Dashboard restarted; picking this job up again")
                    self._ensure_docker_action(job.target_id, job.action)
                    async with slot.released():
                        target, executor = await self._find_again(job)
                    await self._continue(job, target, executor, on_line, slot)
                    job.status = "succeeded"
                    return
                # Look the target up again: the inventory may have changed while the job was queued
                # (guest stopped, protected, or removed).
                try:
                    target, executor = self.inventory.get(job.target_id)
                except KeyError:
                    raise RuntimeError("target no longer exists") from None
                if job.action == "start":
                    self._ensure_can_start(target)
                elif job.action == "rollback":
                    _ensure_can_roll_back(target, self.inventory.snapshots.get(job.target_id))
                elif job.action == "backup":
                    _ensure_can_back_up(target, self.inventory.backup_runs.get(job.target_id))
                else:
                    _ensure_runnable(target, executor, job.action)
                    self._ensure_docker_action(job.target_id, job.action)
                    self._ensure_updates_allowed(target, job.action)
                if job.action in ("upgrade", "reboot"):
                    await self._wait_unblocked(job, target, slot)
                if job.action == "start":
                    job.log.append(f"== Starting {target.name} through Proxmox")
                    try:
                        await self.inventory.power[target.id].start()
                        job.log.append("== Proxmox confirms the guest is running; guest services may still be starting")
                    finally:
                        self.inventory.invalidate()
                elif job.action == "backup":
                    job.log.append(
                        f"== Backing up {target.name}"
                        + (
                            f" to {self.backup_folder or 'the dashboard'}/"
                            if job.destination == DASHBOARD
                            else f" to {job.destination}"
                            if job.destination
                            else ""
                        )
                    )
                    if not self.age_recipient and not self.backup_password:
                        job.log.append(
                            "! The archive is not encrypted and holds secrets "
                            "(set backup_password, or backup_age_recipient to a public age key)"
                        )
                    await self.inventory.backup_runs[job.target_id].run(
                        on_line,
                        job.destination,
                        dashboard_folder=self.backup_folder,
                        age_recipient=self.age_recipient,
                        password=self.backup_password,
                    )
                    job.log.append("== Backup finished")
                elif job.action == "check":
                    check = await _check(
                        executor,
                        on_line,
                        discover_docker=job.target_id not in self.dockhand_targets,
                    )
                    self._set_check(job.target_id, check)
                    if check.error:
                        raise RuntimeError(check.error)
                    job.log.append(f"== {len(check.packages)} update(s) available")
                elif job.action == "reboot":
                    await self._reboot(job, target, executor, on_line, slot)
                elif job.action == "rollback":
                    job.log.append(f"== Rolling back to snapshot {job.snapshot}")
                    warning = await self.inventory.snapshots[job.target_id].rollback(job.snapshot)
                    if warning:
                        job.log.append(f"! {warning}")
                    if executor is None:
                        # E.g. an LXC whose node has no SSH: nothing to re-check with.
                        job.log.append("== Rolled back")
                    else:
                        job.log.append("== Rolled back; re-checking")
                        try:
                            self._set_check(
                                job.target_id,
                                await _check(
                                    executor,
                                    discover_docker=job.target_id not in self.dockhand_targets,
                                ),
                            )
                        except Exception as e:  # noqa: BLE001 - the rollback itself worked
                            job.log.append(f"! re-check failed: {e}")
                elif job.action == "stacks":
                    await self._stacks_check(job, executor, on_line)
                elif job.action == "stack-update":
                    await self._stack_update(job, executor, on_line)
                else:
                    if job.snapshot_first:
                        await self._snapshot(job)
                    if job.update_command:
                        # Instead of the OS upgrade and the helper-script app update; a failure fails the job.
                        job.log.append(f"== Running this machine's update command: {job.update_command}")
                        stdout = await self._upgrade(job, executor, on_line)
                        await self._after_upgrade(job, target, executor, on_line, slot, stdout, app_updated=True)
                    elif job.security_only:
                        # The community-scripts app update is a full update, not a security-only one.
                        job.log.append("== Installing security updates only")
                        stdout = await self._upgrade(job, executor, on_line)
                        await self._after_upgrade(job, target, executor, on_line, slot, stdout, app_updated=True)
                    elif app := await self._community_app(executor):
                        await self._app_first(job, target, executor, on_line, slot, app)
                    else:
                        job.log.append("== Running OS upgrade")
                        stdout = await self._upgrade(job, executor, on_line)
                        await self._after_upgrade(job, target, executor, on_line, slot, stdout)
                job.status = "succeeded"
            except Exception as e:  # noqa: BLE001 - surface every failure in the UI
                log.exception("job %s failed", job.id)
                job.status, job.error = "failed", str(e) or type(e).__name__
                job.log.append(f"!! {job.error}")
                if job.action == "check":
                    self._record_check_error(job.target_id, job.error)
            finally:
                # A cancelled job is still "running": shutdown() records it, as interrupted or to resume.
                if job.status in ("succeeded", "failed"):
                    job.finished_at = _now()
                    self._save_job(job)

    async def _wait_unblocked(self, job: Job, target: Target, slot: _Slot) -> None:
        """The check at submit is not enough: a job that was queued may start when a guest has begun
        updating since. Wait for it, without holding a slot."""
        if why := self.update_blocked(target):
            job.log.append(f"== Not starting yet: {why}")
            self._waiting_for_guests.add(job.id)
            try:
                async with slot.released():
                    while self.update_blocked(target):
                        await asyncio.sleep(1)
            finally:
                self._waiting_for_guests.discard(job.id)

    async def _find_again(self, job: Job) -> tuple[Target, object]:
        """The job's target after a restart, once discovery has found it (and it can run commands)."""
        deadline = time.monotonic() + RESUME_WAIT
        while True:
            found = self.inventory.targets.get(job.target_id)
            if found is None or found[1] is None:
                await self.inventory.refresh(force=True)
                found = self.inventory.targets.get(job.target_id)
            if found is not None and found[1] is not None:
                return found
            if time.monotonic() > deadline:
                what = "is no longer in the inventory" if found is None else "can't run commands"
                raise RuntimeError(f"after the dashboard restarted, this machine {what}")
            await asyncio.sleep(RESUME_POLL)

    async def _continue(self, job: Job, target: Target, executor, on_line, slot: _Slot) -> None:
        """Carry on with a job from where a restart cut it off."""
        if job.action == "stack-update":
            await self._stack_update(job, executor, on_line)
        elif job.remote is not None and job.app_update:
            await self._app_first(job, target, executor, on_line, slot, "the app")
        elif job.remote is not None:
            stdout = await self._upgrade(job, executor, on_line)
            await self._after_upgrade(
                job,
                target,
                executor,
                on_line,
                slot,
                stdout,
                app_updated=job.app_updated or job.security_only or bool(job.update_command),
            )
        else:  # waiting for a reboot, on its own or after an upgrade
            await self._reboot(job, target, executor, on_line, slot)

    async def _upgrade(self, job: Job, executor, on_line, script: str | None = None, what: str = "upgrade") -> str:
        """Runs the upgrade (or another long ``script``) and returns its output. Detached on the target
        where the executor allows, so it goes on if the dashboard restarts, and :meth:`resume`
        follows it again."""
        if script is None and job.update_command:
            script, what = custom_update_script(job.update_command), "update command"
        elif script is None:
            script = SECURITY_UPGRADE_SCRIPT if job.security_only else UPGRADE_SCRIPT
        if not getattr(executor, "detached", False):
            return await _attached(executor, on_line, script, what)
        if job.remote is None:
            # Saved before starting, so a restart right after it still finds the run.
            job.remote, job.remote_offset = job.id, 0
            self._save_job(job)
            try:
                how = await detached.start(executor, job.remote, script)
            except detached.NotStarted as e:
                job.remote = None
                self._save_job(job)
                job.log.append(f"! can't run it detached ({e}), so a dashboard restart would stop it")
                return await _attached(executor, on_line, script, what)
            job.log.append(f"== Running on the machine ({how}): a dashboard restart won't stop it")
        out: list[str] = []
        carry = ""

        def on_output(text: str, offset: int) -> None:
            nonlocal carry
            out.append(text)
            *lines, carry = (carry + text).split("\n")
            for line in lines:
                on_line("stdout", line.rstrip("\r"))
            # What was shown so far; a partial last line is read again after a restart.
            job.remote_offset = max(job.remote_offset, offset - len(carry.encode()))
            self._save_job(job)

        rc = await detached.follow(executor, job.remote, job.remote_offset, on_output, UPGRADE_TIMEOUT)
        if carry:
            on_line("stdout", carry.rstrip("\r"))
        run_id, job.remote = job.remote, None
        self._save_job(job)
        await detached.cleanup(executor, run_id)
        if rc != 0:
            raise RuntimeError(f"{what} exited with code {rc}")
        return "".join(out)

    async def _stacks_check(self, job: Job, executor, on_line) -> None:
        """Find the Compose stacks, pull their images (nothing restarts), and see which are behind."""
        job.log.append("== Looking for Docker Compose stacks")
        found = parse_scan(await executor.run(SCAN_SCRIPT), _own_id())
        if found.docker != "yes" or not found.stacks:
            self._set_stacks(job.target_id, found)
            if found.docker == "down":
                raise RuntimeError(found.error)
            job.log.append(f"== {found.error or 'No Docker Compose stacks found'}")
            return
        if found.compose == "none":
            self._set_stacks(job.target_id, found.model_copy(update={"error": "Docker Compose is not installed"}))
            raise RuntimeError("Docker Compose is not installed on this machine")
        running = [s for s in found.stacks if s.running]
        job.log.append(f"== Found {len(found.stacks)} stack(s); fetching newer images (nothing is restarted)")

        def shown(stream: str, line: str) -> None:
            if line := clean_line(line):
                if not line.startswith("__PULLFAIL__"):
                    on_line(stream, line)

        res = await executor.run(pull_script(running), shown, timeout=PULL_TIMEOUT)
        if res.rc != 0:
            raise RuntimeError(f"pulling images exited with code {res.rc}: {(res.stderr or res.stdout).strip()[-300:]}")
        failures = parse_pull_failures(res.stdout)
        result = parse_scan(await executor.run(SCAN_SCRIPT), _own_id())
        for s in result.stacks:
            s.error = failures.get(s.name)
            if s.error:
                job.log.append(f"! {s.name}: could not fetch images: {s.error}")
            elif s.update_available:
                job.log.append(f"== {s.name}: update available ({', '.join(x.name for x in s.services if x.outdated)})")
            else:
                job.log.append(f"== {s.name}: up to date")
        self._set_stacks(job.target_id, result)

    async def _stack_update(self, job: Job, executor, on_line) -> None:
        def cleaned(stream: str, line: str) -> None:
            if line := clean_line(line):
                on_line(stream, line)

        script = ""
        if job.remote is None:  # not a resume: the script is built from the last check
            stack = _ensure_stack_updatable(None, self.stacks.get(job.target_id), job.stack)
            script = update_script(stack, job.prune)
            job.log.append(f"== Updating Docker stack {stack.name}")
        await self._upgrade(job, executor, cleaned, script, "stack update")
        job.log.append("== Stack updated; checking it")
        try:
            result = parse_scan(await executor.run(SCAN_SCRIPT), _own_id())
        except Exception as e:  # noqa: BLE001 - the update itself worked
            job.log.append(f"! re-check failed: {e}")
            return
        # Keep the other stacks' pull errors; only this stack was just made current.
        old = {s.name: s.error for s in (self.stacks.get(job.target_id) or StacksResult(checked_at=_now())).stacks}
        for s in result.stacks:
            s.error = old.get(s.name) if s.name != job.stack else None
        self._set_stacks(job.target_id, result)
        now = next((s for s in result.stacks if s.name == job.stack), None)
        if now is None:
            job.log.append("! the stack is no longer there after the update")
        elif now.update_available:
            behind = ", ".join(x.name for x in now.services if x.outdated)
            job.log.append(f"! some services still run an older image: {behind}")
        elif now.state != "running":
            job.log.append(f"! the stack is {now.state}: some services are not running")
        else:
            job.log.append("== Stack is running the newest images")

    async def _after_upgrade(
        self, job: Job, target: Target, executor, on_line, slot: _Slot, stdout: str, app_updated: bool = False
    ) -> None:
        reboot = parse_reboot(stdout)
        job.log.append(
            ("== Upgrade finished" if stdout else "== Update finished") + ("; reboot required" if reboot else "")
        )
        if not app_updated and (app := parse_community_script(stdout)):
            await self._community_update(job, executor, app, on_line)
        job.log.append("== Re-checking for remaining updates")
        # The upgrade itself worked; a failed re-check is reported but doesn't fail the job.
        try:
            check = await _check(executor, discover_docker=job.target_id not in self.dockhand_targets)
        except Exception as e:  # noqa: BLE001
            check = CheckResult(checked_at=_now(), error=str(e) or type(e).__name__)
        self._set_check(job.target_id, check)
        if check.error:
            job.log.append(f"! re-check failed: {check.error}")
        else:
            job.log.append(f"== {len(check.packages)} update(s) still available")
        if job.reboot_if_required:
            # Trust the re-check when it worked; it also sees reboots pending from before.
            pending = check.reboot_required if not check.error else reboot
            if not pending:
                job.log.append("== No reboot needed")
            elif target.no_reboot:
                job.log.append(f"! Not rebooting: {target.no_reboot}")
            else:
                await self._reboot(job, target, executor, on_line, slot)

    def _host_id(self, target: Target) -> str | None:
        """The id of the host a guest runs on, if it is in the inventory."""
        if target.kind == "host" or not target.parent:
            return None
        for t, _ in self.inventory.targets.values():
            if t.kind == "host" and t.source == target.source and t.name == target.parent:
                return t.id
        return None

    def update_blocked(self, target: Target) -> str | None:
        """Why a machine can't be updated or rebooted right now, if it can't.

        Its own Docker stacks are updating, or (for a host) a guest on it is updating, updating Docker,
        rebooting or rolling back. A guest is never held back by its host updating.
        """
        busy = [
            j
            for j in self.jobs.values()
            if j.target_id == target.id and j.action in ("stack-update", "dockhand-update")
        ]
        if target.kind == "host":
            busy += [j for j in self._guest_jobs(target) if j.action in GUEST_ACTIONS_BLOCKING_HOST]
        busy = [j for j in busy if j.status in ("queued", "running")]
        if not busy:
            return None
        names = {t.id: t.name for t, _ in self.inventory.targets.values()}
        who = ", ".join(sorted({names.get(j.target_id, j.target_name) for j in busy}))
        return f"waiting for {who} to finish updating"

    def _guest_jobs(self, host: Target) -> list[Job]:
        guests = {
            t.id
            for t, _ in self.inventory.targets.values()
            if t.kind != "host" and t.source == host.source and t.parent == host.name
        }
        return [j for j in self.jobs.values() if j.target_id in guests and j.status in ("queued", "running")]

    async def _reboot(self, job: Job, target: Target, executor, on_line, slot: _Slot) -> None:
        """Reboot the target, wait until it is back (a new boot id), then re-check it.

        A host first waits for its guests' jobs, so it doesn't cut an upgrade off halfway, and
        no new guest jobs are accepted until it is back.
        """
        if target.kind == "host":
            self._rebooting.add(target.id)
        try:
            await self._reboot_now(job, target, executor, on_line, slot)
        finally:
            self._rebooting.discard(target.id)

    def _other_jobs(self, job: Job) -> list[Job]:
        """Active jobs besides ``job``, except hosts waiting for their guests to reboot (``job`` may be one) and
        jobs only waiting for a guest's update to finish (the dashboard machine may be that guest)."""
        return [
            j
            for j in self.jobs.values()
            if j is not job
            and j.status in ("queued", "running")
            and j.target_id not in self._rebooting
            and j.id not in self._waiting_for_guests
        ]

    async def _reboot_now(self, job: Job, target: Target, executor, on_line, slot: _Slot) -> None:
        if job.reboot_from is None:
            await self._start_reboot(job, target, executor, on_line, slot)
        before = job.reboot_from
        deadline = time.monotonic() + REBOOT_TIMEOUT
        # Waiting doesn't need a slot; other machines can be checked and updated meanwhile.
        async with slot.released():
            while True:
                await asyncio.sleep(REBOOT_POLL)
                try:
                    res = await asyncio.wait_for(executor.run(BOOT_ID_SCRIPT, timeout=30), 60)
                    now = parse_boot_id(res.stdout) if res.rc == 0 else None
                except Exception:  # noqa: BLE001 - unreachable while it reboots
                    now = None
                if now and now != before:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(f"not back after {REBOOT_TIMEOUT / 60:.0f} min")
        job.reboot_from = None
        self._save_job(job)
        job.log.append("== Back up; re-checking")
        for attempt in range(RECHECK_TRIES):
            if attempt:
                await asyncio.sleep(REBOOT_POLL)
            try:
                check = await _check(executor, discover_docker=job.target_id not in self.dockhand_targets)
            except Exception as e:  # noqa: BLE001
                check = CheckResult(checked_at=_now(), error=str(e) or type(e).__name__)
            if not check.error:
                break
        self._set_check(job.target_id, check)
        if check.error:
            job.log.append(f"! re-check failed: {check.error}")
        elif check.reboot_required:
            job.log.append("! still reports a pending reboot")
        else:
            job.log.append("== Reboot finished")

    async def _start_reboot(self, job: Job, target: Target, executor, on_line, slot: _Slot) -> None:
        if target.kind == "host" and (waiting := self._guest_jobs(target)):
            job.log.append(f"== Waiting for {len(waiting)} guest job(s) to finish")
            async with slot.released():
                while self._guest_jobs(target):
                    await asyncio.sleep(1)
        check = self.checks.get(target.id)
        if check is not None and check.runs_dashboard and (waiting := self._other_jobs(job)):
            # The dashboard goes down with this machine; let everything else finish first.
            job.log.append(f"== This machine runs the dashboard: waiting for {len(waiting)} other job(s) first")
            async with slot.released():
                while self._other_jobs(job):
                    await asyncio.sleep(1)
        before = parse_boot_id((await executor.run(BOOT_ID_SCRIPT, timeout=60)).stdout)
        if before is None:
            raise RuntimeError("could not read the boot id, so a reboot could not be confirmed")
        job.log.append("== Rebooting")
        # Saved first: if this machine runs the dashboard, the wait below is picked up after it is back.
        job.reboot_from = before
        self._save_job(job)
        try:
            res = await executor.run(REBOOT_NOW_SCRIPT, on_line, timeout=60)
            if res.rc != 0:
                raise RuntimeError(f"reboot command exited with code {res.rc}")
        except Exception:
            job.reboot_from = None
            raise
        job.log.append("== Waiting for it to come back")

    async def _community_app(self, executor) -> str | None:
        """The app's name when this machine is a community-scripts container whose own `update` is
        to be used, else None (also when it can't be told, so the plain OS upgrade runs)."""
        if not self.community_scripts_update:
            return None
        try:
            return parse_community_script((await executor.run(COMMUNITY_SCRIPT_DETECT, timeout=60)).stdout)
        except Exception:  # noqa: BLE001 - not worth failing the job; the OS upgrade is the fallback
            return None

    async def _app_first(self, job: Job, target: Target, executor, on_line, slot: _Slot, app: str) -> None:
        """A community-scripts container is updated by its own `update`, as its authors recommend, and then
        by the OS upgrade: many of these containers have more installed than the helper script put there.
        The OS upgrade runs even if the app's own update failed."""
        await self._community_update(job, executor, app, on_line, fallback=True)
        job.app_updated = True
        self._save_job(job)
        job.log.append("== Running OS upgrade")
        stdout = await self._upgrade(job, executor, on_line)
        await self._after_upgrade(job, target, executor, on_line, slot, stdout, app_updated=True)

    async def _community_update(self, job: Job, executor, app: str, on_line, fallback: bool = False) -> bool:
        """Run the app's own helper-script update, as `update` in the container would. A plain OS
        upgrade can leave such an app broken (e.g. Plex after its vendor moved its apt repository); its
        own update script repairs and restarts it. Returns whether it ran and succeeded."""
        if app == "unknown":
            app = "the app"  # its name couldn't be read from /usr/bin/update
        if not self.community_scripts_update:
            job.log.append(
                f"! {app} was installed by a community-scripts helper script and its own update was not run "
                "(community_scripts_update is off). If it misbehaves, run `update` inside it."
            )
            return False
        job.log.append(f"== Running {app}'s own update (community-scripts helper script, fetched from GitHub)")

        def cleaned(stream: str, line: str) -> None:
            if line := clean_line(line):
                on_line(stream, line)

        # Detached like the OS upgrade, so a dashboard restart halfway doesn't cut it off. The OS upgrade
        # already worked, so a failure here is a warning: the re-check and the reboot still follow.
        job.app_update = True
        try:
            await self._upgrade(job, executor, cleaned, COMMUNITY_UPDATE_SCRIPT, f"{app}'s own update")
        except Exception as e:  # noqa: BLE001 - not a cancel: a shutdown must leave app_update set for the resume
            job.app_update = False
            if fallback:
                job.log.append(f"! {app}'s own update failed ({e})")
            else:
                job.log.append(
                    f"! the OS upgrade worked, but {app}'s own update failed ({e}). "
                    "Run `update` inside it to finish, or roll back to the snapshot."
                )
            return False
        job.app_update = False
        job.log.append(f"== {app}'s own update finished")
        return True

    async def _snapshot(self, job: Job) -> None:
        snaps = self.inventory.snapshots.get(job.target_id)
        if snaps is None:
            job.log.append("== No snapshot: only Proxmox and Incus guests can be snapshotted")
            return
        name = snapshot_name()
        job.log.append(f"== Taking snapshot {name}")
        try:
            warning = await snaps.create(name, f"Before OS upgrade by Fleetdeck (job {job.id})")
        except Exception as e:
            if unsupported(e):
                # "Snapshot first" is on for everything by default; a guest that can never have one
                # is upgraded like before rather than failing every time.
                job.log.append("! this guest's storage can't take snapshots, so it is upgraded without one")
                return
            # The user asked for a safety net; don't upgrade without one.
            raise RuntimeError(f"snapshot failed, so the upgrade was not started: {e}") from e
        if warning:
            job.log.append(f"! {warning}")
        job.snapshot = name
        try:
            for old in await prune(snaps, self.snapshot_keep):
                job.log.append(f"== Deleted old snapshot {old}")
        except Exception as e:  # noqa: BLE001 - housekeeping only
            job.log.append(f"! could not delete old snapshots: {e}")

    def _record_check_error(self, target_id: str, error: str) -> None:
        """Record the latest attempt and retain a separate historical successful result."""
        prev = self.checks.get(target_id)
        self._set_check(
            target_id,
            CheckResult(
                os=prev.os if prev else None,
                package_manager=prev.package_manager if prev else None,
                docker_installed=prev.docker_installed if prev else None,
                runs_dashboard=prev.runs_dashboard if prev else False,
                community_script=prev.community_script if prev else None,
                checked_at=_now(),
                error=error,
            ),
        )


def _ensure_runnable(target: Target, executor, action: JobAction) -> None:
    if not target.updatable or executor is None:
        raise ValueError(f"{target.name}: {target.note or 'not updatable'}")
    if action in ("upgrade", "reboot", "stacks", "stack-update") and target.check_only:
        raise ValueError(f"{target.name}: {target.note or 'can only be checked'}")
    if action == "reboot" and target.no_reboot:
        raise ValueError(f"{target.name}: {target.no_reboot}")


def _ensure_stack_updatable(target: Target | None, result: StacksResult | None, name: str | None) -> Stack:
    who = f"{target.name}: " if target else ""
    if target is not None and target.check_only:
        raise ValueError(f"{who}{target.note or 'can only be checked'}")
    stack = next((s for s in result.stacks if s.name == name), None) if result else None
    if stack is None:
        raise ValueError(f"{who}no Docker stack {name!r} known: check Docker on this machine first")
    if stack.is_dashboard:
        raise ValueError(
            f"{who}{stack.name} runs this dashboard, which would stop in the middle of its own update. "
            "Update it from a terminal: docker compose pull && docker compose up -d"
        )
    if not stack.running:
        raise ValueError(f"{who}{stack.name} is stopped; it is not started by an update. Start it first")
    return stack


def _ensure_can_roll_back(target: Target, snaps: Snapshots | None) -> None:
    # Unlike other jobs this doesn't need the guest running or reachable: it goes through the
    # Proxmox/Incus API, and a guest that won't boot after an upgrade is exactly when it's needed.
    if snaps is None:
        raise ValueError(f"{target.name}: snapshots are only supported for Proxmox and Incus guests")


def _ensure_can_back_up(target: Target, run) -> None:
    # Goes through the platform's API (Proxmox, Incus) or SSH by itself, so unlike an upgrade it doesn't
    # need the executor.
    if run is None or not target.can_backup:
        raise ValueError(f"{target.name}: {target.note or 'cannot be backed up from here'}")


async def _attached(executor, on_line, script: str = UPGRADE_SCRIPT, what: str = "upgrade") -> str:
    res = await executor.run(script, on_line)
    if res.rc != 0:
        raise RuntimeError(f"{what} exited with code {res.rc}")
    return res.stdout


async def _check(executor, on_line=None, *, discover_docker: bool = False) -> CheckResult:
    # Some targets (the Proxmox host) know a better way to check than the generic script.
    if hasattr(executor, "check"):
        check = await executor.check(on_line)
    else:
        check = parse_check(await executor.run(CHECK_SCRIPT, on_line))
    if discover_docker and not check.error:
        try:
            res = await executor.run(DOCKER_DETECT_SCRIPT, timeout=30)
            if res.rc == 0:
                check.docker_installed = parse_docker_installed(res.stdout)
        except Exception:
            # Optional capability discovery must not fail a successful OS check.
            log.debug("Could not discover Docker capability", exc_info=True)
    return check

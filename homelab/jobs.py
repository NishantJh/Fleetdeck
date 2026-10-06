from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from .inventory import Inventory
from .models import CheckResult, Job, JobAction, Target
from .snapshots import Snapshots, prune, snapshot_name, unsupported
from .store import Store, mark_interrupted
from .updates import (
    BOOT_ID_SCRIPT,
    CHECK_SCRIPT,
    REBOOT_NOW_SCRIPT,
    UPGRADE_SCRIPT,
    parse_boot_id,
    parse_check,
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

    def __init__(self, inventory: Inventory, max_parallel: int = 4, store: Store | None = None, snapshot_keep: int = 3):
        self.inventory = inventory
        self.snapshot_keep = snapshot_keep
        self.sem = asyncio.Semaphore(max_parallel)
        self.store = store
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.checks: dict[str, CheckResult] = {}
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        # Hosts that are rebooting, or waiting for their guests' jobs so they can reboot.
        self._rebooting: set[str] = set()
        if store is not None:
            self.checks = store.load_checks()
            self.jobs = OrderedDict((j.id, j) for j in store.load_jobs(MAX_JOBS))

    def _set_check(self, target_id: str, check: CheckResult) -> None:
        self.checks[target_id] = check
        if self.store is not None:
            self._persist(self.store.save_check, target_id, check)

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
        """Stop running jobs and record them as interrupted, before the store is closed."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self.jobs.values():
            if job.status in ("queued", "running"):
                mark_interrupted(job)
                self._save_job(job)

    def active_job(self, target_id: str) -> Job | None:
        for job in reversed(self.jobs.values()):
            if job.target_id == target_id and job.status in ("queued", "running"):
                return job
        return None

    def submit(
        self,
        target_id: str,
        action: JobAction,
        reboot_if_required: bool = False,
        snapshot_first: bool = False,
        snapshot: str | None = None,
    ) -> Job:
        target, executor = self.inventory.get(target_id)
        if action == "rollback":
            _ensure_can_roll_back(target, self.inventory.snapshots.get(target_id))
            if not snapshot:
                raise ValueError(f"{target.name}: no snapshot to roll back to")
        else:
            _ensure_runnable(target, executor, action)
        host_id = self._host_id(target)
        if host_id in self._rebooting:
            raise ValueError(f"{target.name}: its host {target.parent} is rebooting")
        existing = self.active_job(target_id)
        if existing and existing.action == action:
            # Asking again with "reboot if required" ticked still counts, even though the job exists.
            if action == "upgrade" and reboot_if_required:
                existing.reboot_if_required = True
            return existing
        job = Job(
            id=uuid.uuid4().hex[:12],
            target_id=target_id,
            target_name=target.name,
            action=action,
            reboot_if_required=reboot_if_required and action == "upgrade",
            snapshot_first=snapshot_first and action == "upgrade",
            snapshot=snapshot if action == "rollback" else None,
            created_at=_now(),
        )
        self.jobs[job.id] = job
        self._save_job(job)
        self._evict()
        task = asyncio.create_task(self._run(job))
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

    async def _run(self, job: Job) -> None:
        lock = self._target_locks.setdefault(job.target_id, asyncio.Lock())
        async with lock, _Slot(self.sem) as slot:
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
                # Look the target up again: the inventory may have changed while the job was queued
                # (guest stopped, protected, or removed).
                try:
                    target, executor = self.inventory.get(job.target_id)
                except KeyError:
                    raise RuntimeError("target no longer exists") from None
                if job.action == "rollback":
                    _ensure_can_roll_back(target, self.inventory.snapshots.get(job.target_id))
                else:
                    _ensure_runnable(target, executor, job.action)
                if job.action == "check":
                    check = await _check(executor, on_line)
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
                            self._set_check(job.target_id, await _check(executor))
                        except Exception as e:  # noqa: BLE001 - the rollback itself worked
                            job.log.append(f"! re-check failed: {e}")
                else:
                    if job.snapshot_first:
                        await self._snapshot(job)
                    job.log.append("== Running OS upgrade")
                    res = await executor.run(UPGRADE_SCRIPT, on_line)
                    if res.rc != 0:
                        raise RuntimeError(f"upgrade exited with code {res.rc}")
                    reboot = parse_reboot(res.stdout)
                    job.log.append("== Upgrade finished" + ("; reboot required" if reboot else ""))
                    job.log.append("== Re-checking for remaining updates")
                    # The upgrade itself worked; a failed re-check is reported but doesn't fail the job.
                    try:
                        check = await _check(executor)
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
                job.status = "succeeded"
            except Exception as e:  # noqa: BLE001 - surface every failure in the UI
                log.exception("job %s failed", job.id)
                job.status, job.error = "failed", str(e) or type(e).__name__
                job.log.append(f"!! {job.error}")
                if job.action == "check":
                    self._record_check_error(job.target_id, job.error)
            finally:
                job.finished_at = _now()
                self._save_job(job)

    def _host_id(self, target: Target) -> str | None:
        """The id of the host a guest runs on, if it is in the inventory."""
        if target.kind == "host" or not target.parent:
            return None
        for t, _ in self.inventory.targets.values():
            if t.kind == "host" and t.source == target.source and t.name == target.parent:
                return t.id
        return None

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

    async def _reboot_now(self, job: Job, target: Target, executor, on_line, slot: _Slot) -> None:
        if target.kind == "host" and (waiting := self._guest_jobs(target)):
            job.log.append(f"== Waiting for {len(waiting)} guest job(s) to finish")
            async with slot.released():
                while self._guest_jobs(target):
                    await asyncio.sleep(1)
        before = parse_boot_id((await executor.run(BOOT_ID_SCRIPT, timeout=60)).stdout)
        if before is None:
            raise RuntimeError("could not read the boot id, so a reboot could not be confirmed")
        job.log.append("== Rebooting")
        res = await executor.run(REBOOT_NOW_SCRIPT, on_line, timeout=60)
        if res.rc != 0:
            raise RuntimeError(f"reboot command exited with code {res.rc}")
        job.log.append("== Waiting for it to come back")
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
        job.log.append("== Back up; re-checking")
        for attempt in range(RECHECK_TRIES):
            if attempt:
                await asyncio.sleep(REBOOT_POLL)
            try:
                check = await _check(executor)
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

    async def _snapshot(self, job: Job) -> None:
        snaps = self.inventory.snapshots.get(job.target_id)
        if snaps is None:
            job.log.append("== No snapshot: only Proxmox and Incus guests can be snapshotted")
            return
        name = snapshot_name()
        job.log.append(f"== Taking snapshot {name}")
        try:
            warning = await snaps.create(name, f"Before OS upgrade by Homelab Manager (job {job.id})")
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
        """Show a failed check as an error, keeping the OS name from the last good check."""
        prev = self.checks.get(target_id)
        if prev is not None and prev.error == error:
            return
        self._set_check(
            target_id,
            CheckResult(
                os=prev.os if prev else None,
                package_manager=prev.package_manager if prev else None,
                checked_at=_now(),
                error=error,
            ),
        )


def _ensure_runnable(target: Target, executor, action: JobAction) -> None:
    if not target.updatable or executor is None:
        raise ValueError(f"{target.name}: {target.note or 'not updatable'}")
    if action in ("upgrade", "reboot") and target.check_only:
        raise ValueError(f"{target.name}: {target.note or 'can only be checked'}")
    if action == "reboot" and target.no_reboot:
        raise ValueError(f"{target.name}: {target.no_reboot}")


def _ensure_can_roll_back(target: Target, snaps: Snapshots | None) -> None:
    # Unlike other jobs this doesn't need the guest running or reachable: it goes through the
    # Proxmox/Incus API, and a guest that won't boot after an upgrade is exactly when it's needed.
    if snaps is None:
        raise ValueError(f"{target.name}: snapshots are only supported for Proxmox and Incus guests")


async def _check(executor, on_line=None) -> CheckResult:
    # Some targets (the Proxmox host) know a better way to check than the generic script.
    if hasattr(executor, "check"):
        return await executor.check(on_line)
    return parse_check(await executor.run(CHECK_SCRIPT, on_line))

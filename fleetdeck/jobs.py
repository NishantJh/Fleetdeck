from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from . import detached
from .inventory import Inventory
from .models import CheckResult, Job, JobAction, Target
from .snapshots import Snapshots, prune, snapshot_name, unsupported
from .store import Store, mark_interrupted
from .updates import (
    BOOT_ID_SCRIPT,
    CHECK_SCRIPT,
    COMMUNITY_UPDATE_SCRIPT,
    COMMUNITY_UPDATE_UNPARSED,
    REBOOT_NOW_SCRIPT,
    UPGRADE_SCRIPT,
    clean_line,
    parse_boot_id,
    parse_check,
    parse_community_script,
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
# An upgrade running detached on its target gets this long before the dashboard stops waiting.
UPGRADE_TIMEOUT = 3600.0
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
    ):
        self.inventory = inventory
        self.snapshot_keep = snapshot_keep
        self.community_scripts_update = community_scripts_update
        self.sem = asyncio.Semaphore(max_parallel)
        self.store = store
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.checks: dict[str, CheckResult] = {}
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        # Hosts that are rebooting, or waiting for their guests' jobs so they can reboot.
        self._rebooting: set[str] = set()
        # Machines hidden from the dashboard: left out of scheduled checks and their notifications.
        self.hidden: set[str] = set()
        # Host id -> the folder its Back up button is set to use every time.
        self.backup_places: dict[str, str] = {}
        if store is not None:
            self.checks = store.load_checks()
            self.hidden = store.load_hidden()
            self.backup_places = store.load_backup_places()
            self.jobs = OrderedDict((j.id, j) for j in store.load_jobs(MAX_JOBS))

    def _set_check(self, target_id: str, check: CheckResult) -> None:
        self.checks[target_id] = check
        if self.store is not None:
            self._persist(self.store.save_check, target_id, check)

    def set_hidden(self, target_ids: list[str], hidden: bool) -> None:
        if hidden:
            self.hidden.update(target_ids)
        else:
            self.hidden.difference_update(target_ids)
        if self.store is not None:
            self._persist(self.store.set_hidden, target_ids, hidden)

    def set_backup_place(self, target_id: str, path: str | None) -> None:
        if path is None:
            self.backup_places.pop(target_id, None)
        else:
            self.backup_places[target_id] = path
        if self.store is not None:
            self._persist(self.store.set_backup_place, target_id, path)

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

    def submit(
        self,
        target_id: str,
        action: JobAction,
        reboot_if_required: bool = False,
        snapshot_first: bool = False,
        snapshot: str | None = None,
        destination: str | None = None,
    ) -> Job:
        target, executor = self.inventory.get(target_id)
        if action == "rollback":
            _ensure_can_roll_back(target, self.inventory.snapshots.get(target_id))
            if not snapshot:
                raise ValueError(f"{target.name}: no snapshot to roll back to")
        elif action == "backup":
            _ensure_can_back_up(target, self.inventory.backup_runs.get(target_id))
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
            destination=destination if action == "backup" else None,
            created_at=_now(),
        )
        self.jobs[job.id] = job
        self._save_job(job)
        self._evict()
        self._start(job)
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
                if job.action == "rollback":
                    _ensure_can_roll_back(target, self.inventory.snapshots.get(job.target_id))
                elif job.action == "backup":
                    _ensure_can_back_up(target, self.inventory.backup_runs.get(job.target_id))
                else:
                    _ensure_runnable(target, executor, job.action)
                if job.action == "backup":
                    job.log.append(
                        f"== Backing up {target.name}" + (f" to {job.destination}" if job.destination else "")
                    )
                    await self.inventory.backup_runs[job.target_id].run(on_line, job.destination)
                    job.log.append("== Backup finished")
                elif job.action == "check":
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
        if job.remote is not None:
            stdout = await self._upgrade(job, executor, on_line)
            await self._after_upgrade(job, target, executor, on_line, slot, stdout)
        else:  # waiting for a reboot, on its own or after an upgrade
            await self._reboot(job, target, executor, on_line, slot)

    async def _upgrade(self, job: Job, executor, on_line) -> str:
        """Runs the upgrade and returns its output. Detached on the target where the executor allows,
        so the upgrade goes on if the dashboard restarts, and :meth:`resume` follows it again."""
        if not getattr(executor, "detached", False):
            return await _attached(executor, on_line)
        if job.remote is None:
            # Saved before starting, so a restart right after it still finds the run.
            job.remote, job.remote_offset = job.id, 0
            self._save_job(job)
            try:
                how = await detached.start(executor, job.remote, UPGRADE_SCRIPT)
            except detached.NotStarted as e:
                job.remote = None
                self._save_job(job)
                job.log.append(f"! can't run it detached ({e}), so a dashboard restart would stop it")
                return await _attached(executor, on_line)
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
            raise RuntimeError(f"upgrade exited with code {rc}")
        return "".join(out)

    async def _after_upgrade(self, job: Job, target: Target, executor, on_line, slot: _Slot, stdout: str) -> None:
        reboot = parse_reboot(stdout)
        job.log.append("== Upgrade finished" + ("; reboot required" if reboot else ""))
        if app := parse_community_script(stdout):
            await self._community_update(job, executor, app, on_line)
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

    def _other_jobs(self, job: Job) -> list[Job]:
        """Active jobs besides ``job``, except hosts waiting for their guests to reboot (``job`` may be one)."""
        return [
            j
            for j in self.jobs.values()
            if j is not job and j.status in ("queued", "running") and j.target_id not in self._rebooting
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

    async def _community_update(self, job: Job, executor, app: str, on_line) -> None:
        """Run the app's own helper-script update after the OS upgrade, as `update` in the container
        would. A plain OS upgrade can leave such an app broken (e.g. Plex after its vendor moved
        its apt repository); its own update script repairs and restarts it."""
        if app == "unknown":
            app = "the app"  # its name couldn't be read from /usr/bin/update
        if not self.community_scripts_update:
            job.log.append(
                f"! {app} was installed by a community-scripts helper script and its own update was not run "
                "(community_scripts_update is off). If it misbehaves, run `update` inside it."
            )
            return
        job.log.append(f"== Running {app}'s own update (community-scripts helper script, fetched from GitHub)")

        def cleaned(stream: str, line: str) -> None:
            if line := clean_line(line):
                on_line(stream, line)

        res = await executor.run(COMMUNITY_UPDATE_SCRIPT, cleaned)
        if res.rc == COMMUNITY_UPDATE_UNPARSED:
            # The OS upgrade worked and we can't tell how to update the app: a warning, not a failure.
            job.log.append(
                f"! the OS upgrade worked, but Fleetdeck couldn't work out how to run {app}'s own update. "
                "If it misbehaves, run `update` inside it."
            )
            return
        if res.rc != 0:
            raise RuntimeError(
                f"the OS upgrade worked, but {app}'s own update exited with code {res.rc}. "
                "Run `update` inside it to finish, or roll back to the snapshot."
            )
        job.log.append(f"== {app}'s own update finished")

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


def _ensure_can_back_up(target: Target, run) -> None:
    # Goes through the platform's API (Proxmox, Incus) or SSH by itself, so unlike an upgrade it doesn't
    # need the executor.
    if run is None or not target.can_backup:
        raise ValueError(f"{target.name}: {target.note or 'cannot be backed up from here'}")


async def _attached(executor, on_line) -> str:
    res = await executor.run(UPGRADE_SCRIPT, on_line)
    if res.rc != 0:
        raise RuntimeError(f"upgrade exited with code {res.rc}")
    return res.stdout


async def _check(executor, on_line=None) -> CheckResult:
    # Some targets (the Proxmox host) know a better way to check than the generic script.
    if hasattr(executor, "check"):
        return await executor.check(on_line)
    return parse_check(await executor.run(CHECK_SCRIPT, on_line))

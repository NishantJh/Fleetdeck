from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from .inventory import Inventory
from .models import CheckResult, Job, JobAction, Target
from .updates import CHECK_SCRIPT, UPGRADE_SCRIPT, parse_check, parse_reboot

log = logging.getLogger(__name__)
MAX_JOBS = 500


def _now() -> datetime:
    return datetime.now(UTC)


class JobManager:
    """Runs checks and upgrades in the background, at most ``max_parallel`` at a time.

    Only one job per target runs at once; later ones wait their turn.
    """

    def __init__(self, inventory: Inventory, max_parallel: int = 4):
        self.inventory = inventory
        self.sem = asyncio.Semaphore(max_parallel)
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.checks: dict[str, CheckResult] = {}
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    def active_job(self, target_id: str) -> Job | None:
        for job in reversed(self.jobs.values()):
            if job.target_id == target_id and job.status in ("queued", "running"):
                return job
        return None

    def submit(self, target_id: str, action: JobAction) -> Job:
        target, executor = self.inventory.get(target_id)
        _ensure_runnable(target, executor, action)
        existing = self.active_job(target_id)
        if existing and existing.action == action:
            return existing
        job = Job(
            id=uuid.uuid4().hex[:12], target_id=target_id, target_name=target.name, action=action, created_at=_now()
        )
        self.jobs[job.id] = job
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

    async def _run(self, job: Job) -> None:
        lock = self._target_locks.setdefault(job.target_id, asyncio.Lock())
        async with lock, self.sem:
            job.status, job.started_at = "running", _now()

            def on_line(stream: str, line: str) -> None:
                job.log.append(line if stream == "stdout" else f"! {line}")

            try:
                # Look the target up again: the inventory may have changed while the job was queued
                # (guest stopped, protected, or removed).
                try:
                    target, executor = self.inventory.get(job.target_id)
                except KeyError:
                    raise RuntimeError("target no longer exists") from None
                _ensure_runnable(target, executor, job.action)
                if job.action == "check":
                    check = await _check(executor, on_line)
                    self.checks[job.target_id] = check
                    if check.error:
                        raise RuntimeError(check.error)
                    job.log.append(f"== {len(check.packages)} update(s) available")
                else:
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
                    self.checks[job.target_id] = check
                    if check.error:
                        job.log.append(f"! re-check failed: {check.error}")
                    else:
                        job.log.append(f"== {len(check.packages)} update(s) still available")
                job.status = "succeeded"
            except Exception as e:  # noqa: BLE001 - surface every failure in the UI
                log.exception("job %s failed", job.id)
                job.status, job.error = "failed", str(e) or type(e).__name__
                job.log.append(f"!! {job.error}")
                if job.action == "check":
                    self._record_check_error(job.target_id, job.error)
            finally:
                job.finished_at = _now()

    def _record_check_error(self, target_id: str, error: str) -> None:
        """Show a failed check as an error, keeping the OS name from the last good check."""
        prev = self.checks.get(target_id)
        if prev is not None and prev.error == error:
            return
        self.checks[target_id] = CheckResult(
            os=prev.os if prev else None,
            package_manager=prev.package_manager if prev else None,
            checked_at=_now(),
            error=error,
        )


def _ensure_runnable(target: Target, executor, action: JobAction) -> None:
    if not target.updatable or executor is None:
        raise ValueError(f"{target.name}: {target.note or 'not updatable'}")
    if action == "upgrade" and target.check_only:
        raise ValueError(f"{target.name}: {target.note or 'can only be checked'}")


async def _check(executor, on_line=None) -> CheckResult:
    # Some targets (the Proxmox host) know a better way to check than the generic script.
    if hasattr(executor, "check"):
        return await executor.check(on_line)
    return parse_check(await executor.run(CHECK_SCRIPT, on_line))

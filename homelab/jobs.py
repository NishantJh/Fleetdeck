from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from .inventory import Inventory
from .models import CheckResult, Job, JobAction
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
        if not target.updatable or executor is None:
            raise ValueError(f"{target.name}: {target.note or 'not updatable'}")
        existing = self.active_job(target_id)
        if existing and existing.action == action:
            return existing
        job = Job(
            id=uuid.uuid4().hex[:12], target_id=target_id, target_name=target.name, action=action, created_at=_now()
        )
        self.jobs[job.id] = job
        while len(self.jobs) > MAX_JOBS:
            self.jobs.popitem(last=False)
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job) -> None:
        lock = self._target_locks.setdefault(job.target_id, asyncio.Lock())
        async with lock, self.sem:
            job.status, job.started_at = "running", _now()
            _, executor = self.inventory.get(job.target_id)

            def on_line(stream: str, line: str) -> None:
                job.log.append(line if stream == "stdout" else f"! {line}")

            try:
                if job.action == "check":
                    res = await executor.run(CHECK_SCRIPT, on_line)
                    check = parse_check(res)
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
                    self.checks[job.target_id] = parse_check(await executor.run(CHECK_SCRIPT))
                job.status = "succeeded"
            except Exception as e:  # noqa: BLE001 - surface every failure in the UI
                log.exception("job %s failed", job.id)
                job.status, job.error = "failed", str(e) or type(e).__name__
                job.log.append(f"!! {job.error}")
                if job.action == "check" and job.target_id not in self.checks:
                    self.checks[job.target_id] = CheckResult(checked_at=_now(), error=job.error)
            finally:
                job.finished_at = _now()

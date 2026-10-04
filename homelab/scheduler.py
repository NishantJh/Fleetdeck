"""Checks every machine on a timer and sends a notification when something new needs attention."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta

from .inventory import Inventory
from .jobs import JobManager
from .notify import Notifier

log = logging.getLogger(__name__)
# Give the dashboard a moment after start-up before the first automatic check.
FIRST_RUN_DELAY = 60.0
POLL = 2.0
# Stop waiting for a scheduled round after this long; checks still running are reported as stuck.
RUN_TIMEOUT = 30 * 60.0

Fingerprint = frozenset[tuple[str, ...]]


class Scheduler:
    def __init__(self, interval: int | None, inventory: Inventory, jobs: JobManager, notifier: Notifier | None):
        self.interval = interval
        self.inventory = inventory
        self.jobs = jobs
        self.notifier = notifier
        self.next_run: datetime | None = None
        self.last_run: datetime | None = None
        self.last_notified: datetime | None = None
        # What the last round found. Kept in memory only: after a restart the first round notifies
        # about everything that is pending, which doubles as a "still needs attention" reminder.
        self._last_seen: Fingerprint = frozenset()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.interval and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        delay = FIRST_RUN_DELAY
        while True:
            self.next_run = datetime.now(UTC) + timedelta(seconds=delay)
            await asyncio.sleep(delay)
            self.next_run = None
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - keep the schedule alive whatever happens
                log.exception("scheduled check failed")
            delay = float(self.interval or 0)

    async def run_once(self) -> None:
        """Check every machine that can be checked, wait for the results, then notify if needed."""
        self.last_run = datetime.now(UTC)
        await self.inventory.refresh(force=True)
        submitted = []
        for target, executor in list(self.inventory.targets.values()):
            if not target.updatable or executor is None:
                continue
            try:
                submitted.append(self.jobs.submit(target.id, "check"))
            except (KeyError, ValueError):
                continue
        deadline = time.monotonic() + RUN_TIMEOUT
        while any(j.status in ("queued", "running") for j in submitted):
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(POLL)
        # A hung check (an SSH session that never returns) must not hold back the whole report.
        stuck = sorted({j.target_id for j in submitted if j.status in ("queued", "running")})
        done = sorted({j.target_id for j in submitted} - set(stuck))
        await self._report(done, stuck)

    def summarize(self, target_ids: list[str], stuck: list[str] = ()) -> tuple[str, str, Fingerprint]:
        lines: list[str] = []
        seen: set[tuple[str, ...]] = set()
        machines = 0
        for tid in stuck:
            entry = self.inventory.targets.get(tid)
            seen.add(("error", tid))
            lines.append(f"{entry[0].name if entry else tid}: check still running after {RUN_TIMEOUT / 60:.0f} min")
        for tid in target_ids:
            check = self.jobs.checks.get(tid)
            entry = self.inventory.targets.get(tid)
            if check is None or entry is None:
                continue
            name = entry[0].name
            if check.error:
                seen.add(("error", tid))
                lines.append(f"{name}: check failed: {check.error.splitlines()[0][:150]}")
                continue
            parts = []
            if check.packages:
                seen.update(("pkg", tid, p.name, p.available) for p in check.packages)
                n = len(check.packages)
                parts.append(f"{n} update{'s' if n > 1 else ''}")
            if check.reboot_required:
                seen.add(("reboot", tid))
                parts.append("reboot needed")
            if parts:
                machines += 1
                lines.append(f"{name}: {', '.join(parts)}")
        failed = sum(1 for s in seen if s[0] == "error")
        bits = []
        if machines:
            bits.append(f"{machines} machine{'s need' if machines > 1 else ' needs'} attention")
        if failed:
            bits.append(f"{failed} check{'s' if failed > 1 else ''} failed")
        title = "Homelab: " + (", ".join(bits) or "all up to date")
        return title, "\n".join(sorted(lines, key=str.lower)), frozenset(seen)

    async def _report(self, target_ids: list[str], stuck: list[str] = ()) -> None:
        title, body, seen = self.summarize(target_ids, stuck)
        # Only speak up when something new appeared: an update that wasn't pending at the last run,
        # a new pending reboot, or a machine whose check started failing. Things that were fixed
        # since drop out, so they notify again if they come back.
        new = seen - self._last_seen
        self._last_seen = seen
        if not new or self.notifier is None:
            return
        errors = await self.notifier.send(title, body)
        if len(errors) < len(self.notifier.targets):
            self.last_notified = datetime.now(UTC)

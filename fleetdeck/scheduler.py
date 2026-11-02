"""Checks every machine on a timer and sends a notification when something new needs attention."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from .dockhand import DockhandReader
from .inventory import Inventory
from .jobs import JobManager
from .models import Job
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)
# Give the dashboard a moment after start-up before the first automatic check.
FIRST_RUN_DELAY = 60.0
POLL = 2.0
# Stop waiting for a scheduled round after this long; checks still running are reported as stuck.
RUN_TIMEOUT = 30 * 60.0
# Same for each automatic security-update round (hosts, then guests).
UPDATE_TIMEOUT = 2 * 60 * 60.0

Fingerprint = frozenset[tuple[str, ...]]


class Scheduler:
    def __init__(
        self,
        interval: int | None,
        inventory: Inventory,
        jobs: JobManager,
        notifier: Notifier | None,
        store: Store | None = None,
        check_docker: bool = False,
        auto_security: bool = False,
        dockhand: DockhandReader | None = None,
    ):
        self.store = store
        self.check_docker = check_docker
        self.dockhand = dockhand
        self.auto_security = auto_security
        self.interval = interval
        self.inventory = inventory
        self.jobs = jobs
        self.notifier = notifier
        self.next_run: datetime | None = None
        self.last_run: datetime | None = None
        self.last_notified: datetime | None = None
        # What the last round found. Kept in the store, so a restart (a settings change, an image update,
        # a reboot) neither moves the schedule nor repeats a notification that was already sent.
        self._last_seen: Fingerprint = frozenset()
        self._load()
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

    def _load(self) -> None:
        if self.store is None:
            return
        try:
            for attr in ("last_run", "last_notified"):
                raw = self.store.get_value(attr)
                if raw:
                    setattr(self, attr, datetime.fromisoformat(raw))
            raw = self.store.get_value("last_seen")
            if raw:
                self._last_seen = frozenset(tuple(item) for item in json.loads(raw))
        except (ValueError, TypeError):
            log.warning("ignoring unreadable scheduler state")

    def _save(self) -> None:
        if self.store is None:
            return
        try:
            for attr in ("last_run", "last_notified"):
                value = getattr(self, attr)
                if value:
                    self.store.set_value(attr, value.isoformat())
            self.store.set_value("last_seen", json.dumps(sorted(self._last_seen)))
        except Exception:  # noqa: BLE001 - a full disk must not stop the checks
            log.exception("could not save the scheduler state")

    def _delay(self) -> float:
        """Seconds until the next round: ``interval`` after the last one started, or a minute after start-up
        when there is none yet or it is overdue."""
        if self.last_run is None or not self.interval:
            return FIRST_RUN_DELAY
        due = (self.last_run - datetime.now(UTC)).total_seconds() + self.interval
        if due > self.interval:  # the clock went backwards
            due = float(self.interval)
        return max(due, FIRST_RUN_DELAY)

    async def _loop(self) -> None:
        while True:
            delay = self._delay()
            self.next_run = datetime.now(UTC) + timedelta(seconds=delay)
            await asyncio.sleep(delay)
            self.next_run = None
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - keep the schedule alive whatever happens
                log.exception("scheduled check failed")

    async def run_once(self) -> None:
        """Check every machine that can be checked and isn't hidden, wait for the results, then notify if needed."""
        self.last_run = datetime.now(UTC)
        self._save()
        await self.inventory.refresh(force=True)
        submitted = []
        external_targets = []
        for target, executor in list(self.inventory.targets.values()):
            if not target.updatable or executor is None or target.id in self.jobs.hidden:
                continue
            try:
                submitted.append(self.jobs.submit(target.id, "check"))
            except (KeyError, ValueError):
                continue
            if self.check_docker and not target.check_only and target.id in self.jobs.dockhand_targets:
                external_targets.append(target.id)
            # Opt-in: the native Docker check pulls images. Skipped where the last look found no Docker.
            if (
                self.check_docker
                and not target.check_only
                and self.jobs.get_docker_settings(target.id).mode in ("managed", "monitor")
                and getattr(self.jobs.checks.get(target.id), "docker_installed", None) is not False
                and (
                    getattr(self.jobs.checks.get(target.id), "docker_installed", None) is True
                    or getattr(self.jobs.stacks.get(target.id), "docker", "") != "none"
                )
            ):
                with contextlib.suppress(KeyError, ValueError):
                    submitted.append(self.jobs.submit(target.id, "stacks"))
        if self.check_docker and self.dockhand is not None and self.dockhand.cfg.allow_checks:
            await self._check_external(external_targets)
        await self._wait(submitted, RUN_TIMEOUT)
        # A hung check (an SSH session that never returns) must not hold back the whole report.
        stuck = sorted({j.target_id for j in submitted if j.status in ("queued", "running")})
        done = sorted({j.target_id for j in submitted} - set(stuck))
        notes, skipped = await self._install_security_updates(done) if self.auto_security else ([], [])
        await self._report(done, stuck, notes, skipped)

    async def _check_external(self, target_ids: list[str]) -> None:
        """The existing scheduled-Docker flag controls provider checks, never deployments."""
        reader = self.dockhand
        if reader is None:
            return
        started = []
        try:
            async with asyncio.timeout(RUN_TIMEOUT):
                for target_id in target_ids:
                    if target_id in reader.tasks:
                        continue  # An explicit check is already doing the work.
                    if len(reader.tasks) >= 4:
                        await asyncio.wait(tuple(reader.tasks.values()), return_when=asyncio.FIRST_COMPLETED)
                    try:
                        reader.request_check(target_id, scheduled=True)
                    except (PermissionError, KeyError, ValueError):
                        log.info(
                            "scheduled external container check skipped for %s (policy or recent check)", target_id
                        )
                        continue
                    started.append(reader.tasks[target_id])
                if started:
                    await asyncio.gather(*(asyncio.shield(task) for task in started), return_exceptions=True)
        except TimeoutError:
            log.warning("scheduled external container checks exceeded the round time limit")

    async def _wait(self, jobs: list[Job], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while any(j.status in ("queued", "running") for j in jobs):
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(POLL)

    async def _install_security_updates(self, checked: list[str]) -> tuple[list[str], list[str]]:
        """Install the security updates the checks found: hosts first, then their containers and VMs.

        Each round finishes before the next starts, so a host is never updated while one of its guests is
        (a host's upgrade waits for its guests anyway, see JobManager.update_blocked). Never reboots, and
        leaves Docker stacks alone: they have no security-only updates. Machines with their own update command are
        left out. Returns lines for the notification: what was installed, and what was left out.
        """
        notes: list[str] = []
        skipped: list[str] = []
        for hosts in (True, False):
            submitted: list[Job] = []
            for tid in checked:
                entry = self.inventory.targets.get(tid)
                check = self.jobs.checks.get(tid)
                if entry is None or check is None or check.error or tid in self.jobs.hidden:
                    continue
                target, executor = entry
                if (target.kind == "host") != hosts or executor is None or target.check_only:
                    continue
                if self.jobs.monitor_only(target):
                    continue
                if not any(p.security for p in check.packages):
                    continue
                if self.jobs.update_command(target):
                    # Its own command may install more than security fixes: left for an Update by hand.
                    skipped.append(f"{target.name}: security updates not installed, it has its own update command")
                    continue
                try:
                    # A snapshot first where the guest can take one, like the Upgrade button.
                    submitted.append(self.jobs.submit(tid, "upgrade", snapshot_first=True, security_only=True))
                except (KeyError, ValueError) as e:
                    log.warning("not installing security updates on %s: %s", target.name, e)
            await self._wait(submitted, UPDATE_TIMEOUT)
            notes += self._outcomes(submitted, "security updates")
        return notes, skipped

    @staticmethod
    def _outcomes(jobs: list[Job], what: str) -> list[str]:
        notes = []
        for job in jobs:
            who = job.target_name
            if job.status == "succeeded":
                notes.append(f"{who}: installed {what}")
            else:
                why = job.error.splitlines()[0][:150] if job.error else "still running after the time limit"
                notes.append(f"{who}: {what} failed: {why}")
        return notes

    def summarize(
        self, target_ids: list[str], stuck: list[str] = (), notes: list[str] = (), skipped: list[str] = ()
    ) -> tuple[str, str, Fingerprint]:
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
        body = "\n".join(
            [*sorted(notes, key=str.lower), *sorted(skipped, key=str.lower), *sorted(lines, key=str.lower)]
        )
        return title, body, frozenset(seen)

    async def _report(
        self, target_ids: list[str], stuck: list[str] = (), notes: list[str] = (), skipped: list[str] = ()
    ) -> None:
        # ``skipped`` only explains: it is sent along when something else is new, never on its own.
        title, body, seen = self.summarize(target_ids, stuck, notes, skipped)
        # Only speak up when something new appeared: an update that wasn't pending at the last run,
        # a new pending reboot, or a machine whose check started failing. Things that were fixed
        # since drop out, so they notify again if they come back.
        new = seen - self._last_seen
        # What the dashboard installed by itself is always worth a message.
        if (new or notes) and self.notifier is not None:
            errors = await self.notifier.send(title, body)
            if len(errors) < len(self.notifier.targets):
                self.last_notified = datetime.now(UTC)
                self._last_seen = seen
            # Every target failed: keep what was known before, so the next round tries to tell about it again.
        else:
            self._last_seen = seen
        self._save()

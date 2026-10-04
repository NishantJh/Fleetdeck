"""Keeps check results and job history in SQLite so a restart doesn't lose them.

Writes are small and rare (one per finished check or job), so the stdlib ``sqlite3``
module is used directly from the event loop.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .models import CheckResult, Job

log = logging.getLogger(__name__)
# Upgrade logs can be long; keep the end, which is where errors are.
MAX_STORED_LOG_LINES = 5000
INTERRUPTED = "interrupted: the dashboard restarted while this job was running"


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Opened while the app is built, used from the event loop (maybe another thread, but never two
        # at once).
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS checks (target_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,"
            " data TEXT NOT NULL)"
        )

    def close(self) -> None:
        self.db.close()

    def save_check(self, target_id: str, check: CheckResult) -> None:
        self.db.execute(
            "INSERT INTO checks (target_id, data) VALUES (?, ?)"
            " ON CONFLICT(target_id) DO UPDATE SET data = excluded.data",
            (target_id, check.model_dump_json()),
        )

    def load_checks(self) -> dict[str, CheckResult]:
        out: dict[str, CheckResult] = {}
        for target_id, data in self.db.execute("SELECT target_id, data FROM checks"):
            try:
                out[target_id] = CheckResult.model_validate_json(data)
            except ValueError:
                log.warning("dropping unreadable stored check for %s", target_id)
        return out

    def save_job(self, job: Job) -> None:
        if len(job.log) > MAX_STORED_LOG_LINES:
            job = job.model_copy(update={"log": ["… (earlier output trimmed)", *job.log[-MAX_STORED_LOG_LINES:]]})
        # Keep the original insertion order (seq) when a job is saved again on completion.
        self.db.execute(
            "INSERT INTO jobs (id, data) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (job.id, job.model_dump_json()),
        )

    def delete_jobs(self, ids: list[str]) -> None:
        self.db.executemany("DELETE FROM jobs WHERE id = ?", [(i,) for i in ids])

    def load_jobs(self, limit: int) -> list[Job]:
        """The newest ``limit`` jobs, oldest first. Jobs that were still running are marked as failed."""
        rows = self.db.execute("SELECT data FROM jobs ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        jobs: list[Job] = []
        for (data,) in reversed(rows):
            try:
                job = Job.model_validate_json(data)
            except ValueError:
                log.warning("dropping unreadable stored job")
                continue
            if job.status in ("queued", "running"):
                job.status, job.error = "failed", INTERRUPTED
                job.log.append(f"!! {INTERRUPTED}")
                self.save_job(job)
            jobs.append(job)
        # Older rows beyond the limit are no longer reachable from the UI.
        self.db.execute("DELETE FROM jobs WHERE seq NOT IN (SELECT seq FROM jobs ORDER BY seq DESC LIMIT ?)", (limit,))
        return jobs

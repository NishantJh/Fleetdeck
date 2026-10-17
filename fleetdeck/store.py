"""Keeps check results, job history and hidden machines in SQLite so a restart doesn't lose them.

Writes are small and rare (one per finished check or job), so the stdlib ``sqlite3``
module is used directly from the event loop.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import CheckResult, Job, StacksResult

log = logging.getLogger(__name__)
# Upgrade logs can be long; keep the end, which is where errors are.
MAX_STORED_LOG_LINES = 5000
INTERRUPTED = "interrupted: the dashboard restarted while this job was running"
NEVER_STARTED = "not run: the dashboard restarted before this job started"


def mark_interrupted(job: Job) -> None:
    """Fail a job that a shutdown or restart cut off."""
    msg = INTERRUPTED if job.started_at else NEVER_STARTED
    job.status, job.error = "failed", msg
    job.finished_at = job.finished_at or datetime.now(UTC)
    job.log.append(f"!! {msg}")


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
        self.db.execute("CREATE TABLE IF NOT EXISTS stacks (target_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS hidden (target_id TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS backup_places (target_id TEXT PRIMARY KEY, path TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

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

    def save_stacks(self, target_id: str, result: StacksResult) -> None:
        self.db.execute(
            "INSERT INTO stacks (target_id, data) VALUES (?, ?)"
            " ON CONFLICT(target_id) DO UPDATE SET data = excluded.data",
            (target_id, result.model_dump_json()),
        )

    def load_stacks(self) -> dict[str, StacksResult]:
        out: dict[str, StacksResult] = {}
        for target_id, data in self.db.execute("SELECT target_id, data FROM stacks"):
            try:
                out[target_id] = StacksResult.model_validate_json(data)
            except ValueError:
                log.warning("dropping unreadable stored Docker check for %s", target_id)
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

    def load_hidden(self) -> set[str]:
        return {tid for (tid,) in self.db.execute("SELECT target_id FROM hidden")}

    def set_hidden(self, target_ids: list[str], hidden: bool) -> None:
        if hidden:
            sql = "INSERT OR IGNORE INTO hidden (target_id) VALUES (?)"
        else:
            sql = "DELETE FROM hidden WHERE target_id = ?"
        self.db.executemany(sql, [(t,) for t in target_ids])

    def get_value(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_value(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def load_backup_places(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT target_id, path FROM backup_places"))

    def set_backup_place(self, target_id: str, path: str | None) -> None:
        if path is None:
            self.db.execute("DELETE FROM backup_places WHERE target_id = ?", (target_id,))
        else:
            self.db.execute(
                "INSERT INTO backup_places (target_id, path) VALUES (?, ?)"
                " ON CONFLICT(target_id) DO UPDATE SET path = excluded.path",
                (target_id, path),
            )

    def load_jobs(self, limit: int) -> list[Job]:
        """The newest ``limit`` jobs, oldest first. Jobs that were still running are marked as failed,
        except those the job manager can pick up again (an upgrade running detached on its target, or
        a reboot it was waiting for)."""
        rows = self.db.execute("SELECT data FROM jobs ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        jobs: list[Job] = []
        for (data,) in reversed(rows):
            try:
                job = Job.model_validate_json(data)
            except ValueError:
                log.warning("dropping unreadable stored job")
                continue
            if job.status in ("queued", "running") and not job.resumable:
                mark_interrupted(job)
                self.save_job(job)
            jobs.append(job)
        # Older rows beyond the limit are no longer reachable from the UI.
        self.db.execute("DELETE FROM jobs WHERE seq NOT IN (SELECT seq FROM jobs ORDER BY seq DESC LIMIT ?)", (limit,))
        return jobs

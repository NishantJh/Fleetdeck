"""Keeps check results, job history and hidden machines in SQLite so a restart doesn't lose them.

Writes are small and rare (one per finished check or job), so the stdlib ``sqlite3``
module is used directly from the event loop.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import CheckResult, DockerSettings, Job, StacksResult

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
        self.db.execute("CREATE TABLE IF NOT EXISTS monitor_only (target_id TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS backup_places (target_id TEXT PRIMARY KEY, path TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS docker_settings (target_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS update_commands (target_id TEXT PRIMARY KEY, command TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS console_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,"
            " data TEXT NOT NULL)"
        )
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

    def load_docker_settings(self) -> dict[str, DockerSettings]:
        out: dict[str, DockerSettings] = {}
        for target_id, data in self.db.execute("SELECT target_id, data FROM docker_settings"):
            try:
                out[target_id] = DockerSettings.model_validate_json(data)
            except ValueError:
                # Fail closed: a corrupt policy must never silently re-enable stack updates.
                log.warning("unreadable Docker settings for %s; disabling Docker management", target_id)
                out[target_id] = DockerSettings(mode="disabled")
        return out

    def save_docker_settings(self, target_id: str, settings: DockerSettings) -> None:
        self.db.execute(
            "INSERT INTO docker_settings (target_id, data) VALUES (?, ?)"
            " ON CONFLICT(target_id) DO UPDATE SET data = excluded.data",
            (target_id, settings.model_dump_json()),
        )

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

    def load_monitor_only(self) -> set[str]:
        return {tid for (tid,) in self.db.execute("SELECT target_id FROM monitor_only")}

    def set_monitor_only(self, target_ids: list[str], on: bool) -> None:
        if on:
            sql = "INSERT OR IGNORE INTO monitor_only (target_id) VALUES (?)"
        else:
            sql = "DELETE FROM monitor_only WHERE target_id = ?"
        self.db.executemany(sql, [(t,) for t in target_ids])

    def get_value(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_value(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def add_console_log(self, at: str, data: str) -> int:
        return self.db.execute("INSERT INTO console_log (at, data) VALUES (?, ?)", (at, data)).lastrowid

    def update_console_log(self, row_id: int, data: str) -> None:
        self.db.execute("UPDATE console_log SET data = ? WHERE id = ?", (data, row_id))

    def load_console_log(self, limit: int) -> list[str]:
        """The newest ``limit`` audit rows (JSON text), newest first."""
        return [d for (d,) in self.db.execute("SELECT data FROM console_log ORDER BY id DESC LIMIT ?", (limit,))]

    def prune_console_log(self, before: str) -> None:
        self.db.execute("DELETE FROM console_log WHERE at < ?", (before,))

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

    def load_update_commands(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT target_id, command FROM update_commands"))

    def set_update_command(self, target_id: str, command: str | None) -> None:
        if command is None:
            self.db.execute("DELETE FROM update_commands WHERE target_id = ?", (target_id,))
        else:
            self.db.execute(
                "INSERT INTO update_commands (target_id, command) VALUES (?, ?)"
                " ON CONFLICT(target_id) DO UPDATE SET command = excluded.command",
                (target_id, command),
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

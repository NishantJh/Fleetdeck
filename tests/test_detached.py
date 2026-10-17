import asyncio
import os
import signal
from datetime import UTC, datetime

import pytest

from fleetdeck import detached
from fleetdeck.inventory import Inventory
from fleetdeck.jobs import JobManager
from fleetdeck.models import ExecResult, Target
from fleetdeck.store import Store
from fleetdeck.updates import BOOT_ID_SCRIPT, CHECK_SCRIPT, UPGRADE_SCRIPT


class LocalExecutor:
    """Runs scripts in a local shell, with the job folder moved into a temp dir."""

    detached = True

    def __init__(self, base, upgrade="echo hello; sleep 1; echo done"):
        self.base, self.upgrade = base, upgrade
        self.check_calls = 0

    async def run(self, script, on_line=None, timeout=0):
        if script == CHECK_SCRIPT:
            self.check_calls += 1
            return ExecResult(rc=0, stdout="__PM__=apt\n__OS__=Debian\n__BEGIN__\n__END__\n__REBOOT__=no\n")
        if script == BOOT_ID_SCRIPT:
            return ExecResult(rc=0, stdout="__BOOT__=b1\n")
        script = script.replace(detached.JOBS_DIR, self.base)
        script = script.replace(UPGRADE_SCRIPT, self.upgrade)
        p = await asyncio.create_subprocess_exec("sh", "-c", script, stdout=-1, stderr=-1)
        out, err = await p.communicate()
        return ExecResult(rc=p.returncode, stdout=out.decode(), stderr=err.decode())


def _jobs(ex, store=None):
    t = Target(id="t1", name="t1", platform="ssh", source="hosts", kind="host")
    inv = Inventory([])
    inv.targets = {"t1": (t, ex)}
    return JobManager(inv, store=store)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(detached, "WINDOW", 4)
    monkeypatch.setattr(detached, "RETRY_EVERY", 0.1)
    monkeypatch.setattr("fleetdeck.jobs.RESUME_POLL", 0.1)
    real = detached.follow
    monkeypatch.setattr(detached, "follow", lambda *a, **k: real(*a, **{**k, "window": 4}))


async def test_upgrade_runs_detached_and_streams_output(tmp_path):
    ex = LocalExecutor(str(tmp_path / "jobs"))
    jobs = _jobs(ex)
    job = jobs.submit("t1", "upgrade")
    await asyncio.gather(*jobs._tasks)
    assert job.status == "succeeded", job.error
    assert "hello" in job.log and "done" in job.log
    assert any("won't stop it" in line for line in job.log)
    assert job.remote is None
    assert os.listdir(tmp_path / "jobs") == []  # cleaned up


async def test_failing_upgrade_reports_exit_code(tmp_path):
    jobs = _jobs(LocalExecutor(str(tmp_path / "jobs"), upgrade="echo boom; exit 4"))
    job = jobs.submit("t1", "upgrade")
    await asyncio.gather(*jobs._tasks)
    assert job.status == "failed" and "code 4" in job.error and "boom" in job.log


async def test_dashboard_restart_keeps_upgrade_and_resumes(tmp_path):
    base = str(tmp_path / "jobs")
    store = Store(tmp_path / "state.db")
    jobs = _jobs(LocalExecutor(base, upgrade="echo first; sleep 3; echo second"), store)
    job = jobs.submit("t1", "upgrade")
    await asyncio.sleep(1.5)
    await jobs.shutdown()  # the dashboard stops; the upgrade does not
    assert job.status == "running" and job.remote
    store.close()

    store = Store(tmp_path / "state.db")
    jobs2 = _jobs(LocalExecutor(base), store)
    (again,) = jobs2.jobs.values()
    assert again.status == "running"  # not marked interrupted
    jobs2.resume()
    await asyncio.gather(*jobs2._tasks)
    assert again.status == "succeeded", again.error
    assert again.log.count("first") == 1 and "second" in again.log
    store.close()


async def test_upgrade_killed_with_the_machine_is_reported(tmp_path):
    base = str(tmp_path / "jobs")
    jobs = _jobs(LocalExecutor(base, upgrade="sleep 60"))
    job = jobs.submit("t1", "upgrade")
    await asyncio.sleep(1)
    os.kill(int(open(f"{base}/{job.id}/pid").read()), signal.SIGKILL)
    await asyncio.gather(*jobs._tasks)
    assert job.status == "failed" and "stopped before it finished" in job.error


async def test_waiting_for_reboot_is_resumed(tmp_path):
    class Rebooted(LocalExecutor):
        async def run(self, script, on_line=None, timeout=0):
            if script == BOOT_ID_SCRIPT:
                return ExecResult(rc=0, stdout="__BOOT__=b2\n")
            return await super().run(script, on_line, timeout)

    store = Store(tmp_path / "state.db")
    jobs = _jobs(Rebooted(str(tmp_path)), store)
    job = jobs.submit("t1", "check")
    await asyncio.gather(*jobs._tasks)
    from fleetdeck.models import Job

    stuck = Job(
        id="r1",
        target_id="t1",
        target_name="t1",
        action="reboot",
        status="running",
        reboot_from="b1",
        created_at=job.created_at,
    )
    jobs.jobs[stuck.id] = stuck
    jobs.resume()
    await asyncio.gather(*jobs._tasks)
    assert stuck.status == "succeeded", stuck.error
    assert stuck.reboot_from is None
    store.close()


async def test_restart_during_the_app_update_resumes_only_that_step(tmp_path, monkeypatch):
    from fleetdeck.models import Job
    from fleetdeck.updates import COMMUNITY_UPDATE_SCRIPT

    class Ex(LocalExecutor):
        async def run(self, script, on_line=None, timeout=0):
            assert script not in (UPGRADE_SCRIPT, COMMUNITY_UPDATE_SCRIPT)  # neither is started again
            return await super().run(script, on_line, timeout)

    async def follow(executor, run_id, offset, on_output, timeout, **kw):
        on_output("app done\n", 9)
        return 0

    async def cleanup(executor, run_id):
        return None

    monkeypatch.setattr(detached, "follow", follow)
    monkeypatch.setattr(detached, "cleanup", cleanup)
    jobs = _jobs(Ex(str(tmp_path / "jobs")))
    cut = Job(
        id="u1",
        target_id="t1",
        target_name="t1",
        action="upgrade",
        status="running",
        remote="u1",
        app_update=True,
        created_at=datetime.now(UTC),
    )
    jobs.jobs[cut.id] = cut
    jobs.resume()
    await asyncio.gather(*jobs._tasks)
    assert cut.status == "succeeded", cut.error
    assert "app done" in cut.log and not cut.app_update and cut.remote is None

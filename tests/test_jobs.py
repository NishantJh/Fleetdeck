import asyncio

import pytest

from homelab.inventory import Inventory
from homelab.jobs import JobManager
from homelab.models import ExecResult, Target
from homelab.updates import CHECK_SCRIPT


class FakeExecutor:
    def __init__(self, fail_check=False, gate: asyncio.Event | None = None):
        self.fail_check = fail_check
        self.gate = gate

    async def run(self, script, on_line=None, timeout=0):
        if self.gate:
            await self.gate.wait()
        if script == CHECK_SCRIPT:
            if self.fail_check:
                raise ConnectionRefusedError("connection refused")
            return ExecResult(rc=0, stdout="__PM__=apt\n__OS__=Debian\n__BEGIN__\n__END__\n__REBOOT__=no\n")
        return ExecResult(rc=0, stdout="__PM__=apt\n__REBOOT__=no\n")


def _inventory(*items) -> Inventory:
    inv = Inventory([])
    inv.targets = {t.id: (t, ex) for t, ex in items}
    return inv


def _target(tid="t1", **kw) -> Target:
    return Target(id=tid, name=tid, platform="ssh", source="hosts", kind="host", **kw)


async def _finish(jobs: JobManager):
    while jobs._tasks:
        await asyncio.gather(*jobs._tasks)


async def test_target_removed_while_queued_fails_instead_of_hanging():
    gate = asyncio.Event()
    inv = _inventory((_target("busy"), FakeExecutor(gate=gate)), (_target("gone"), FakeExecutor()))
    jobs = JobManager(inv, max_parallel=1)
    first = jobs.submit("busy", "check")
    second = jobs.submit("gone", "check")
    await asyncio.sleep(0)
    del inv.targets["gone"]
    gate.set()
    await _finish(jobs)
    assert first.status == "succeeded"
    assert second.status == "failed" and "no longer exists" in second.error and second.finished_at


async def test_failed_check_replaces_stale_result():
    ex = FakeExecutor()
    jobs = JobManager(_inventory((_target(), ex)))
    jobs.submit("t1", "check")
    await _finish(jobs)
    assert jobs.checks["t1"].error is None

    ex.fail_check = True
    jobs.submit("t1", "check")
    await _finish(jobs)
    assert "connection refused" in jobs.checks["t1"].error
    assert jobs.checks["t1"].os == "Debian"


async def test_failed_recheck_does_not_fail_successful_upgrade():
    ex = FakeExecutor(fail_check=True)
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "succeeded"
    assert "connection refused" in jobs.checks["t1"].error


async def test_check_only_target_rejects_upgrade():
    jobs = JobManager(_inventory((_target(check_only=True, note="check only"), FakeExecutor())))
    with pytest.raises(ValueError, match="check only"):
        jobs.submit("t1", "upgrade")
    jobs.submit("t1", "check")
    await _finish(jobs)


async def test_eviction_keeps_active_jobs(monkeypatch):
    import homelab.jobs

    monkeypatch.setattr(homelab.jobs, "MAX_JOBS", 2)
    gate = asyncio.Event()
    inv = _inventory(*[(_target(f"t{i}"), FakeExecutor(gate=gate)) for i in range(4)])
    jobs = JobManager(inv)
    submitted = [jobs.submit(f"t{i}", "check") for i in range(4)]
    assert all(j.id in jobs.jobs for j in submitted)  # all still active
    gate.set()
    await _finish(jobs)
    jobs.submit("t0", "upgrade")
    assert len(jobs.jobs) == 2
    await _finish(jobs)

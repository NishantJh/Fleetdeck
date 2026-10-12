import asyncio

import pytest

from homelab.inventory import Inventory
from homelab.jobs import JobManager
from homelab.models import ExecResult, Target
from homelab.updates import CHECK_SCRIPT, COMMUNITY_UPDATE_SCRIPT, UPGRADE_SCRIPT


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


class RebootingExecutor:
    """Fakes a machine that is unreachable for one poll after a reboot, then has a new boot id."""

    def __init__(self, kernel_pending=True):
        self.boot = "boot-1"
        self.down_polls = 0
        self.reboots = 0
        self.kernel_pending = kernel_pending

    async def run(self, script, on_line=None, timeout=0):
        from homelab.updates import BOOT_ID_SCRIPT, REBOOT_NOW_SCRIPT

        if script == REBOOT_NOW_SCRIPT:
            self.reboots += 1
            self.down_polls = 1
            self.boot = f"boot-{self.reboots + 1}"
            self.kernel_pending = False
            return ExecResult(rc=0)
        if self.down_polls:
            self.down_polls -= 1
            raise ConnectionRefusedError("connection refused")
        if script == BOOT_ID_SCRIPT:
            return ExecResult(rc=0, stdout=f"__BOOT__={self.boot}\n")
        reboot = "yes" if self.kernel_pending else "no"
        return ExecResult(rc=0, stdout=f"__PM__=apt\n__OS__=Debian\n__BEGIN__\n__END__\n__REBOOT__={reboot}\n")


@pytest.fixture
def fast_reboot(monkeypatch):
    import homelab.jobs

    monkeypatch.setattr(homelab.jobs, "REBOOT_POLL", 0)


async def test_reboot_waits_for_new_boot_and_rechecks(fast_reboot):
    ex = RebootingExecutor()
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "reboot")
    await _finish(jobs)
    assert job.status == "succeeded", job.log
    assert ex.reboots == 1
    assert jobs.checks["t1"].reboot_required is False
    assert "== Reboot finished" in job.log


async def test_reboot_times_out_when_machine_does_not_come_back(fast_reboot, monkeypatch):
    import homelab.jobs

    monkeypatch.setattr(homelab.jobs, "REBOOT_TIMEOUT", 0)

    class Stuck(RebootingExecutor):
        async def run(self, script, on_line=None, timeout=0):
            from homelab.updates import REBOOT_NOW_SCRIPT

            if script == REBOOT_NOW_SCRIPT:
                self.reboots += 1  # boot id never changes
                return ExecResult(rc=0)
            return await super().run(script, on_line, timeout)

    jobs = JobManager(_inventory((_target(), Stuck())))
    job = jobs.submit("t1", "reboot")
    await _finish(jobs)
    assert job.status == "failed" and "not back" in job.error


async def test_upgrade_reboots_only_when_required(fast_reboot):
    needs, clean = RebootingExecutor(kernel_pending=True), RebootingExecutor(kernel_pending=False)
    jobs = JobManager(_inventory((_target("a"), needs), (_target("b"), clean)))
    ja = jobs.submit("a", "upgrade", reboot_if_required=True)
    jb = jobs.submit("b", "upgrade", reboot_if_required=True)
    await _finish(jobs)
    assert ja.status == jb.status == "succeeded"
    assert needs.reboots == 1 and clean.reboots == 0
    assert "== No reboot needed" in jb.log


async def test_no_reboot_target(fast_reboot):
    ex = RebootingExecutor()
    jobs = JobManager(_inventory((_target(no_reboot="runs protected guest arcane"), ex)))
    with pytest.raises(ValueError, match="arcane"):
        jobs.submit("t1", "reboot")
    job = jobs.submit("t1", "upgrade", reboot_if_required=True)
    await _finish(jobs)
    assert job.status == "succeeded" and ex.reboots == 0
    assert any("Not rebooting" in line for line in job.log)


async def test_check_only_target_rejects_reboot():
    jobs = JobManager(_inventory((_target(check_only=True, note="check only"), FakeExecutor())))
    with pytest.raises(ValueError, match="check only"):
        jobs.submit("t1", "reboot")


def _guest(tid, parent="t1", **kw) -> Target:
    return Target(id=tid, name=tid, platform="ssh", source="hosts", kind="lxc", parent=parent, **kw)


async def test_host_reboot_waits_for_guest_jobs(fast_reboot):
    gate = asyncio.Event()
    host = RebootingExecutor()
    jobs = JobManager(_inventory((_target(), host), (_guest("g1"), FakeExecutor(gate=gate))), max_parallel=2)
    guest_job = jobs.submit("g1", "upgrade")
    reboot = jobs.submit("t1", "reboot")
    for _ in range(20):
        await asyncio.sleep(0)
    assert "== Waiting for 1 guest job(s) to finish" in reboot.log
    assert host.reboots == 0 and guest_job.status == "running"
    gate.set()
    await _finish(jobs)
    assert reboot.status == "succeeded" and host.reboots == 1
    assert guest_job.finished_at <= reboot.finished_at


async def test_host_reboot_holds_slot_free_and_rejects_new_guest_jobs(fast_reboot, monkeypatch):
    import homelab.jobs

    # Keep the host "rebooting" until the test lets it come back.
    back = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == homelab.jobs.REBOOT_POLL:
            await back.wait()
        await real_sleep(0)

    monkeypatch.setattr(homelab.jobs.asyncio, "sleep", sleep)
    jobs = JobManager(
        _inventory(
            (_target(), RebootingExecutor()), (_guest("g1"), FakeExecutor()), (_target("other"), FakeExecutor())
        ),
        max_parallel=1,
    )
    reboot = jobs.submit("t1", "reboot")
    for _ in range(20):
        await real_sleep(0)
    assert "== Waiting for it to come back" in reboot.log
    with pytest.raises(ValueError, match="rebooting"):
        jobs.submit("g1", "check")
    # The rebooting host doesn't hold the only slot while it waits.
    other = jobs.submit("other", "check")
    for _ in range(20):
        await real_sleep(0)
    assert other.status == "succeeded"
    back.set()
    await _finish(jobs)
    assert reboot.status == "succeeded"
    jobs.submit("g1", "check")
    await _finish(jobs)


async def test_resubmitting_upgrade_keeps_reboot_request():
    gate = asyncio.Event()
    jobs = JobManager(_inventory((_target(), FakeExecutor(gate=gate))))
    first = jobs.submit("t1", "upgrade")
    again = jobs.submit("t1", "upgrade", reboot_if_required=True)
    assert again is first and first.reboot_if_required
    gate.set()
    await _finish(jobs)


async def test_recheck_after_reboot_is_retried(fast_reboot):
    class SlowServices(RebootingExecutor):
        def __init__(self):
            super().__init__()
            self.failed_checks = 0

        async def run(self, script, on_line=None, timeout=0):
            if script == CHECK_SCRIPT and self.reboots and self.failed_checks < 2:
                self.failed_checks += 1
                raise ConnectionRefusedError("api not up yet")
            return await super().run(script, on_line, timeout)

    ex = SlowServices()
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "reboot")
    await _finish(jobs)
    assert job.status == "succeeded" and ex.failed_checks == 2
    assert jobs.checks["t1"].error is None


class CommunityExecutor:
    """A helper-script container: the upgrade reports the app, then its own update runs."""

    def __init__(self, update_rc=0):
        self.update_rc = update_rc
        self.scripts: list[str] = []

    async def run(self, script, on_line=None, timeout=0):
        self.scripts.append(script)
        if script == COMMUNITY_UPDATE_SCRIPT:
            if on_line:
                on_line("stdout", "\x1b[33m⏳\x1b[m Updating Plex\r\x1b[32m✔️\x1b[m Updated Plex")
            return ExecResult(rc=self.update_rc)
        if script == CHECK_SCRIPT:
            return ExecResult(rc=0, stdout="__PM__=apt\n__BEGIN__\n__END__\n__REBOOT__=no\n__CSAPP__=plex\n")
        return ExecResult(rc=0, stdout="__PM__=apt\n__REBOOT__=no\n__CSAPP__=plex\n")


async def test_upgrade_runs_helper_scripts_own_update_afterwards():
    ex = CommunityExecutor()
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "succeeded", job.error
    assert ex.scripts.index(COMMUNITY_UPDATE_SCRIPT) == ex.scripts.index(UPGRADE_SCRIPT) + 1
    assert "✔️ Updated Plex" in job.log  # colours and the spinner frame are dropped
    assert jobs.checks["t1"].community_script == "plex"


async def test_helper_script_update_failing_fails_the_job():
    ex = CommunityExecutor(update_rc=1)
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "failed" and "plex's own update exited with code 1" in job.error


async def test_helper_script_update_off_only_warns():
    ex = CommunityExecutor()
    jobs = JobManager(_inventory((_target(), ex)), community_scripts_update=False)
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "succeeded"
    assert COMMUNITY_UPDATE_SCRIPT not in ex.scripts
    assert any("run `update` inside it" in line for line in job.log)


async def test_plain_upgrade_does_not_run_helper_update():
    ex = FakeExecutor()
    seen = []
    orig = ex.run

    async def run(script, on_line=None, timeout=0):
        seen.append(script)
        return await orig(script, on_line, timeout)

    ex.run = run
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "succeeded" and COMMUNITY_UPDATE_SCRIPT not in seen

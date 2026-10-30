import asyncio
from datetime import UTC, datetime

import pytest

from fleetdeck.inventory import Inventory
from fleetdeck.jobs import JobManager
from fleetdeck.models import CheckResult, ExecResult, Job, Package, Target
from fleetdeck.updates import (
    CHECK_SCRIPT,
    COMMUNITY_UPDATE_SCRIPT,
    CUSTOM_MARK,
    UPGRADE_SCRIPT,
    custom_update_script,
)


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


async def test_failed_checks_keep_a_separate_successful_snapshot(tmp_path):
    from fleetdeck.store import Store

    store = Store(tmp_path / "state.db")
    jobs = JobManager(_inventory((_target(), FakeExecutor())), store=store)
    good = CheckResult(
        checked_at=datetime.now(UTC),
        os="Debian",
        reboot_required=True,
        packages=[Package(name="openssl", available="2", security=True)],
    )
    jobs._set_check("t1", good)
    # A parsed failure and a transport failure both keep the same last good result.
    jobs._set_check("t1", CheckResult(checked_at=datetime.now(UTC), error="agent unavailable"))
    jobs._record_check_error("t1", "agent unavailable")
    failed = jobs.checks["t1"]
    assert failed.packages == [] and failed.reboot_required is None
    assert failed.last_successful.packages == good.packages
    assert failed.last_successful.reboot_required is True
    assert failed.last_successful.checked_at == good.checked_at
    assert failed.checked_at > good.checked_at
    loaded = store.load_checks()["t1"]
    assert loaded == failed
    # A fresh successful result clears the history and becomes authoritative.
    jobs._set_check("t1", CheckResult(checked_at=datetime.now(UTC), reboot_required=False))
    assert jobs.checks["t1"].last_successful is None
    store.close()


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
    import fleetdeck.jobs

    monkeypatch.setattr(fleetdeck.jobs, "MAX_JOBS", 2)
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
        from fleetdeck.updates import BOOT_ID_SCRIPT, REBOOT_NOW_SCRIPT

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
    import fleetdeck.jobs

    monkeypatch.setattr(fleetdeck.jobs, "REBOOT_POLL", 0)


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
    import fleetdeck.jobs

    monkeypatch.setattr(fleetdeck.jobs, "REBOOT_TIMEOUT", 0)

    class Stuck(RebootingExecutor):
        async def run(self, script, on_line=None, timeout=0):
            from fleetdeck.updates import REBOOT_NOW_SCRIPT

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


async def test_host_update_and_reboot_refused_while_guest_updates(fast_reboot):
    gate = asyncio.Event()
    host = RebootingExecutor()
    jobs = JobManager(_inventory((_target(), host), (_guest("g1"), FakeExecutor(gate=gate))), max_parallel=2)
    guest_job = jobs.submit("g1", "upgrade")
    for action in ("reboot", "upgrade"):
        with pytest.raises(ValueError, match="waiting for g1 to finish updating"):
            jobs.submit("t1", action)
    assert jobs.update_blocked(jobs.inventory.get("t1")[0])
    jobs.submit("t1", "check")  # checking is harmless
    gate.set()
    await _finish(jobs)
    assert guest_job.status == "succeeded" and jobs.update_blocked(jobs.inventory.get("t1")[0]) is None
    jobs.submit("t1", "reboot")
    await _finish(jobs)
    assert host.reboots == 1


async def test_dashboard_guest_reboots_while_its_host_job_waits_for_it(fast_reboot):
    class DashboardGuest(RebootingExecutor):
        async def run(self, script, on_line=None, timeout=0):
            res = await super().run(script, on_line, timeout)
            if res.stdout.startswith("__PM__"):
                res.stdout += "__SELF__=yes\n"
            return res

    guest = DashboardGuest()
    jobs = JobManager(_inventory((_target(), RebootingExecutor()), (_guest("g1"), guest)), max_parallel=2)
    jobs.checks["g1"] = CheckResult(checked_at=datetime.now(UTC), runs_dashboard=True)
    # The host job is accepted first, then waits at start for the guest's update, which needs a reboot.
    host_job = jobs.submit("t1", "upgrade", reboot_if_required=True)
    guest_job = jobs.submit("g1", "upgrade", reboot_if_required=True)
    await asyncio.wait_for(_finish(jobs), 10)
    assert guest.reboots == 1
    assert guest_job.status == "succeeded", guest_job.log
    assert host_job.status == "succeeded", host_job.log


async def test_guest_may_update_and_reboot_while_its_host_updates():
    gate = asyncio.Event()
    jobs = JobManager(_inventory((_target(), FakeExecutor(gate=gate)), (_guest("g1"), FakeExecutor())), max_parallel=2)
    host_job = jobs.submit("t1", "upgrade")
    await asyncio.sleep(0.05)  # the host job has started
    guest = jobs.inventory.get("g1")[0]
    assert jobs.update_blocked(guest) is None
    for action in ("upgrade", "reboot", "check"):
        assert jobs.submit("g1", action)
    gate.set()
    await _finish(jobs)
    assert host_job.status == "succeeded" and jobs.update_blocked(guest) is None


async def test_queued_job_waits_at_start_for_a_guest_that_began_updating(fast_reboot):
    gate = asyncio.Event()
    # One slot: the host job is queued behind the guest's check, then the guest starts an upgrade.
    jobs = JobManager(_inventory((_target(), FakeExecutor()), (_guest("g1"), FakeExecutor(gate=gate))), max_parallel=1)
    blocker = jobs.submit("g1", "check")
    host_job = jobs.submit("t1", "upgrade")  # accepted: nothing is updating yet
    jobs.jobs["x"] = Job(
        id="x", target_id="g1", target_name="g1", action="upgrade", status="queued", created_at=datetime.now(UTC)
    )  # the guest's update is queued before the host job gets to start
    gate.set()
    await asyncio.sleep(0.2)
    assert blocker.status == "succeeded"
    assert host_job.status == "running" and any("Not starting yet" in line for line in host_job.log)
    jobs.jobs["x"].status = "succeeded"
    await asyncio.sleep(2.5)
    assert host_job.status == "succeeded", host_job.log


async def test_host_refused_while_its_docker_stacks_update():
    gate = asyncio.Event()
    jobs = JobManager(_inventory((_target(), FakeExecutor(gate=gate)), (_guest("g1"), FakeExecutor())))
    host = jobs.inventory.get("t1")[0]
    # A stack-update job on the host itself, and on a guest of the host.
    for tid in ("t1", "g1"):
        jobs.jobs.clear()
        job = Job(
            id=tid,
            target_id=tid,
            target_name=tid,
            action="stack-update",
            status="running",
            created_at=datetime.now(UTC),
        )
        jobs.jobs[job.id] = job
        assert jobs.update_blocked(host)
        with pytest.raises(ValueError, match="to finish updating"):
            jobs.submit("t1", "reboot")
    # A guest whose own Docker stacks are updating can't be updated or rebooted either.
    jobs.jobs.clear()
    job = Job(
        id="s", target_id="g1", target_name="g1", action="stack-update", status="running", created_at=datetime.now(UTC)
    )
    jobs.jobs[job.id] = job
    for action in ("upgrade", "reboot"):
        with pytest.raises(ValueError, match="waiting for g1"):
            jobs.submit("g1", action)
    jobs.jobs.clear()
    assert jobs.update_blocked(host) is None


async def test_host_reboot_holds_slot_free_and_rejects_new_guest_jobs(fast_reboot, monkeypatch):
    import fleetdeck.jobs

    # Keep the host "rebooting" until the test lets it come back.
    back = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == fleetdeck.jobs.REBOOT_POLL:
            await back.wait()
        await real_sleep(0)

    monkeypatch.setattr(fleetdeck.jobs.asyncio, "sleep", sleep)
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


async def test_helper_scripts_own_update_is_followed_by_the_os_upgrade():
    ex = CommunityExecutor()
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "succeeded", job.error
    assert ex.scripts.index(UPGRADE_SCRIPT) > ex.scripts.index(COMMUNITY_UPDATE_SCRIPT)
    assert ex.scripts.count(COMMUNITY_UPDATE_SCRIPT) == 1  # the OS upgrade's output doesn't start it again
    assert "✔️ Updated Plex" in job.log  # colours and the spinner frame are dropped
    assert "== Re-checking for remaining updates" in job.log
    assert jobs.checks["t1"].community_script == "plex"


async def test_resumed_os_upgrade_does_not_run_the_helper_scripts_update_again():
    ex = CommunityExecutor()
    jobs = JobManager(_inventory((_target(), ex)))
    job = Job(
        id="j1", target_id="t1", target_name="t1", action="upgrade", status="running", created_at=datetime.now(UTC)
    )
    job.app_updated, job.remote = True, "j1"  # the app's own update is done; the OS upgrade was running when we stopped
    jobs.jobs[job.id] = job

    async def finish(job_, executor, on_line, script=UPGRADE_SCRIPT, what="upgrade"):
        job_.remote = None
        return "__PM__=apt\n__REBOOT__=no\n__CSAPP__=plex\n"

    jobs._upgrade = finish
    target, executor = jobs.inventory.get("t1")
    await jobs._continue(job, target, executor, lambda *_: None, None)
    assert COMMUNITY_UPDATE_SCRIPT not in ex.scripts
    assert "== Re-checking for remaining updates" in job.log


async def test_os_upgrade_still_runs_if_helper_scripts_update_fails():
    ex = CommunityExecutor(update_rc=1)
    jobs = JobManager(_inventory((_target(), ex)))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "succeeded", job.error
    assert any("plex's own update failed" in line and "code 1" in line for line in job.log)
    assert "== Running OS upgrade" in job.log
    assert ex.scripts.index(UPGRADE_SCRIPT) > ex.scripts.index(COMMUNITY_UPDATE_SCRIPT)
    assert ex.scripts.count(COMMUNITY_UPDATE_SCRIPT) == 1  # not run again after the OS upgrade
    assert "== Re-checking for remaining updates" in job.log


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


async def test_upgrade_script_runs_once():
    class Counting(FakeExecutor):
        runs = 0

        async def run(self, script, on_line=None, timeout=0):
            self.runs += script == UPGRADE_SCRIPT
            return await super().run(script, on_line, timeout)

    ex = Counting()
    jobs = JobManager(_inventory((_target(), ex)))
    jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert ex.runs == 1


@pytest.mark.parametrize("action", ["upgrade", "reboot", "rollback", "stack-update"])
async def test_host_reboot_blocked_while_guest_runs_any_changing_job(action):
    jobs = JobManager(_inventory((_target(), FakeExecutor()), (_guest("g1"), FakeExecutor())))
    host = jobs.inventory.get("t1")[0]
    jobs.jobs["g"] = Job(
        id="g", target_id="g1", target_name="g1", action=action, status="running", created_at=datetime.now(UTC)
    )
    assert "waiting for g1" in jobs.update_blocked(host)
    with pytest.raises(ValueError, match="waiting for g1 to finish updating"):
        jobs.submit("t1", "reboot")
    jobs.jobs["g"].status = "succeeded"
    assert jobs.update_blocked(host) is None


async def test_monitor_only_machine_is_checked_and_rebooted_but_never_updated(fast_reboot):
    host = _target("pve")
    inv = _inventory(
        (host, RebootingExecutor()),
        (_guest("omv", "pve", monitor_only=True), RebootingExecutor()),
        (_guest("ct", "pve"), RebootingExecutor()),
    )
    jobs = JobManager(inv)
    jobs.set_monitor_only(["ct"], True)
    for tid in ("omv", "ct"):
        for action in ("upgrade", "stack-update"):
            with pytest.raises(ValueError, match="monitor only"):
                jobs.submit(tid, action, stack="web")
        jobs.submit(tid, "check")
    await _finish(jobs)
    assert jobs.checks["omv"].error is None and jobs.checks["ct"].error is None
    # Its own reboot, and its host's update and reboot, stay allowed.
    for tid, action in (("omv", "reboot"), ("pve", "upgrade"), ("pve", "reboot")):
        job = jobs.submit(tid, action)
        await _finish(jobs)
        assert job.status == "succeeded", (tid, action, job.error)

    jobs.set_monitor_only(["ct"], False)
    assert jobs.submit("ct", "upgrade")
    await _finish(jobs)


async def test_monitor_only_set_while_an_update_is_queued_stops_it():
    gate = asyncio.Event()
    inv = _inventory((_target("busy"), FakeExecutor(gate=gate)), (_target("omv"), FakeExecutor()))
    jobs = JobManager(inv, max_parallel=1)
    jobs.submit("busy", "check")
    queued = jobs.submit("omv", "upgrade")
    jobs.set_monitor_only(["omv"], True)
    gate.set()
    await _finish(jobs)
    assert queued.status == "failed" and "monitor only" in queued.error


def test_monitor_only_survives_a_restart(tmp_path):
    from fleetdeck.store import Store

    inv = _inventory((_target("a"), FakeExecutor()), (_target("b"), FakeExecutor()))
    s = Store(tmp_path / "state.db")
    JobManager(inv, store=s).set_monitor_only(["a", "b"], True)
    JobManager(inv, store=s).set_monitor_only(["b"], False)
    s.close()
    assert JobManager(inv, store=Store(tmp_path / "state.db")).monitor_only_ids == {"a"}


class OwnUpdaterExecutor(CommunityExecutor):
    """A machine with its own updater (e.g. OpenMediaVault's omv-upgrade)."""

    def __init__(self, rc=0):
        super().__init__()
        self.rc = rc

    async def run(self, script, on_line=None, timeout=0):
        if CUSTOM_MARK in script:
            self.scripts.append(script)
            if on_line:
                on_line("stdout", "Updating OMV")
            return ExecResult(rc=self.rc, stdout="__PM__=apt\n__REBOOT__=no\n")
        return await super().run(script, on_line, timeout)


async def test_own_update_command_runs_instead_of_os_upgrade_and_app_update():
    ex = OwnUpdaterExecutor()
    jobs = JobManager(_inventory((_target(update_command="omv-upgrade"), ex)))
    job = jobs.submit("t1", "upgrade")
    assert job.update_command == "omv-upgrade"
    await _finish(jobs)
    assert job.status == "succeeded", job.error
    assert ex.scripts[0] == custom_update_script("omv-upgrade")
    assert UPGRADE_SCRIPT not in ex.scripts and COMMUNITY_UPDATE_SCRIPT not in ex.scripts
    assert "== Running this machine's update command: omv-upgrade" in job.log and "Updating OMV" in job.log
    assert "== Re-checking for remaining updates" in job.log


async def test_failed_own_update_command_fails_the_job_without_falling_back():
    ex = OwnUpdaterExecutor(rc=2)
    jobs = JobManager(_inventory((_target(update_command="omv-upgrade"), ex)))
    job = jobs.submit("t1", "upgrade")
    await _finish(jobs)
    assert job.status == "failed" and job.error == "update command exited with code 2"
    assert UPGRADE_SCRIPT not in ex.scripts and CHECK_SCRIPT not in ex.scripts


async def test_update_command_set_on_dashboard_and_config_wins():
    ex = OwnUpdaterExecutor()
    jobs = JobManager(_inventory((_target(), ex), (_target("t2", update_command="from-config"), ex)))
    jobs.set_update_command("t1", "my-update")
    jobs.set_update_command("t2", "ignored")
    assert jobs.update_command(jobs.inventory.get("t1")[0]) == "my-update"
    assert jobs.update_command(jobs.inventory.get("t2")[0]) == "from-config"
    jobs.set_update_command("t1", None)
    assert jobs.update_command(jobs.inventory.get("t1")[0]) is None


async def test_security_only_update_refused_on_machine_with_own_command():
    jobs = JobManager(_inventory((_target(update_command="omv-upgrade"), OwnUpdaterExecutor())))
    with pytest.raises(ValueError, match="own update command"):
        jobs.submit("t1", "upgrade", security_only=True)


async def test_resumed_own_update_command_does_not_run_the_app_update():
    ex = OwnUpdaterExecutor()
    jobs = JobManager(_inventory((_target(), ex)))
    job = Job(
        id="j1", target_id="t1", target_name="t1", action="upgrade", status="running", created_at=datetime.now(UTC)
    )
    job.update_command, job.remote = "omv-upgrade", "j1"
    jobs.jobs[job.id] = job

    async def finish(job_, executor, on_line, script=None, what="upgrade"):
        job_.remote = None
        return "__PM__=apt\n__REBOOT__=no\n__CSAPP__=plex\n"

    jobs._upgrade = finish
    target, executor = jobs.inventory.get("t1")
    await jobs._continue(job, target, executor, lambda *_: None, None)
    assert COMMUNITY_UPDATE_SCRIPT not in ex.scripts
    assert "== Re-checking for remaining updates" in job.log

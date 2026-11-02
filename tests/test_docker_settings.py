from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_jobs import FakeExecutor, _finish, _inventory, _target

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig, ScheduleConfig
from fleetdeck.jobs import JobManager, _check
from fleetdeck.models import DockerSettings, ExecResult, Job, Stack, StackService, StacksResult
from fleetdeck.scheduler import Scheduler
from fleetdeck.store import Store
from fleetdeck.updates import CHECK_SCRIPT, DOCKER_DETECT_SCRIPT, parse_check, parse_docker_installed


@pytest.mark.parametrize("mode", ["disabled", "external"])
async def test_disabled_and_external_modes_block_all_docker_jobs_but_allow_os_checks(mode):
    jobs = JobManager(_inventory((_target(), FakeExecutor())))
    jobs.set_docker_settings("t1", DockerSettings(mode=mode))
    for action in ("stacks", "stack-update"):
        with pytest.raises(ValueError, match=mode):
            jobs.submit("t1", action, stack="web")
    check = jobs.submit("t1", "check")
    await _finish(jobs)
    assert check.status == "succeeded"


async def test_monitor_allows_checks_but_rejects_stack_updates():
    jobs = JobManager(_inventory((_target(), FakeExecutor())))
    jobs.set_docker_settings("t1", DockerSettings(mode="monitor"))
    jobs._set_stacks(
        "t1",
        StacksResult(
            checked_at=datetime.now(UTC),
            compose="v2",
            stacks=[Stack(name="web", services=[StackService(name="web", image="web:latest", status="running")])],
        ),
    )
    with pytest.raises(ValueError, match="monitor-only"):
        jobs.submit("t1", "stack-update", stack="web")
    job = jobs.submit("t1", "stacks")
    with pytest.raises(ValueError, match="finish"):
        jobs.set_docker_settings("t1", DockerSettings(mode="disabled"))
    await _finish(jobs)
    assert job.action == "stacks"


async def test_scheduler_skips_docker_on_externally_managed_hosts(monkeypatch):
    inv = _inventory((_target(), FakeExecutor()))
    jobs = JobManager(inv)
    jobs.set_docker_settings("t1", DockerSettings(mode="external"))
    monkeypatch.setattr(inv, "refresh", AsyncMock())
    scheduler = Scheduler(None, inv, jobs, None, check_docker=True)
    await scheduler.run_once()
    assert [job.action for job in jobs.jobs.values()] == ["check"]


def test_api_settings_persist_and_are_enforced_for_bulk_actions(tmp_path):
    cfg = AppConfig(demo=True, state_file=str(tmp_path / "state.db"))
    with TestClient(create_app(cfg)) as client:
        vm = next(v for v in client.get("/api/targets").json()["targets"] if v["target"]["name"] == "docker-vm")
        tid = vm["target"]["id"]
        data = {
            "target_id": tid,
            "mode": "external",
            "manager_name": "Test manager",
            "manager_url": "https://manager.example",
        }
        assert client.put("/api/docker-settings", json=data).status_code == 200
        result = client.post("/api/actions", json={"action": "stacks", "target_ids": [tid]}).json()
        assert not result["jobs"] and "external" in result["rejected"][tid]
        assert client.put("/api/docker-settings", json={**data, "target_id": "missing"}).status_code == 404
        invalid = client.put("/api/docker-settings", json={**data, "manager_url": "javascript:alert(1)"})
        assert invalid.status_code == 422
    with TestClient(create_app(cfg)) as client:
        vm = next(v for v in client.get("/api/targets").json()["targets"] if v["target"]["id"] == tid)
        assert vm["docker_settings"]["mode"] == "external"
        assert vm["docker_settings"]["manager_url"] == "https://manager.example"
        assert vm["docker_settings"]["manager_name"] == "Test manager"


def test_corrupt_saved_policy_fails_closed(tmp_path):
    store = Store(tmp_path / "state.db")
    store.db.execute("INSERT INTO docker_settings VALUES (?, ?)", ("t1", '{"mode":"typo"}'))
    assert store.load_docker_settings()["t1"].mode == "disabled"
    store.close()


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "//example.com", "https://user:secret@example.com", "https://exa\nmple.com"]
)
def test_external_manager_urls_reject_active_content_and_embedded_credentials(url):
    with pytest.raises(ValidationError):
        DockerSettings(manager_url=url)


@pytest.mark.parametrize("marker, expected", [("yes", True), ("no", False), ("unknown", None)])
def test_capability_detection_does_not_require_a_docker_pull(marker, expected):
    text = f"__PM__=apt\n__BEGIN__\n__END__\n__DOCKER_INSTALLED__={marker}\n"
    assert parse_check(ExecResult(rc=0, stdout=text)).docker_installed is expected
    assert parse_docker_installed(text) is expected


def test_native_support_is_available_by_default_and_host_disable_blocks_jobs():
    cfg = AppConfig(demo=True, schedule=ScheduleConfig(check_docker=True))
    app = create_app(cfg)
    with TestClient(app) as client:
        data = client.get("/api/targets").json()
        assert "docker_enabled" not in data["settings"]
        assert data["settings"]["containers_visible"] is True
        assert app.state.scheduler.check_docker is True  # Preserve the configured schedule flag.
        assert app.state.scheduler.dockhand is None
        tid = data["targets"][0]["target"]["id"]
        app.state.jobs.set_docker_settings(tid, DockerSettings(mode="disabled"))
        for action in ("stacks", "stack-update"):
            result = client.post("/api/actions", json={"action": action, "target_ids": [tid]}).json()
            assert not result["jobs"] and "disabled" in result["rejected"][tid]


async def test_docker_disabled_scheduler_only_checks_os(monkeypatch):
    inv = _inventory((_target(), FakeExecutor()))
    jobs = JobManager(inv)
    jobs.set_docker_settings("t1", DockerSettings(mode="disabled"))
    monkeypatch.setattr(inv, "refresh", AsyncMock())
    await Scheduler(None, inv, jobs, None, check_docker=True).run_once()
    assert [job.action for job in jobs.jobs.values()] == ["check"]


async def test_os_checks_do_not_discover_docker_unless_explicitly_enabled():
    class RecordingExecutor(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.scripts = []

        async def run(self, script, on_line=None, timeout=0):
            self.scripts.append(script)
            if script == DOCKER_DETECT_SCRIPT:
                return ExecResult(rc=0, stdout="__DOCKER_INSTALLED__=yes\n")
            return await super().run(script, on_line, timeout)

    ex = RecordingExecutor()
    assert (await _check(ex)).docker_installed is None
    assert ex.scripts == [CHECK_SCRIPT]
    assert (await _check(ex, discover_docker=True)).docker_installed is True
    assert ex.scripts == [CHECK_SCRIPT, CHECK_SCRIPT, DOCKER_DETECT_SCRIPT]


async def test_docker_disabled_does_not_resume_a_stack_deployment():
    class NoCommands(FakeExecutor):
        async def run(self, *args, **kwargs):
            pytest.fail("A disabled Docker job must not run remote commands")

    jobs = JobManager(_inventory((_target(), NoCommands())))
    jobs.set_docker_settings("t1", DockerSettings(mode="disabled"))
    job = Job(
        id="old",
        target_id="t1",
        target_name="t1",
        action="stack-update",
        stack="web",
        status="running",
        remote="old",
        created_at=datetime.now(UTC),
    )
    jobs.jobs[job.id] = job
    jobs.resume()
    await _finish(jobs)
    assert job.status == "failed" and "disabled" in job.error


async def test_queued_docker_job_rechecks_policy_before_starting():
    import asyncio

    gate = asyncio.Event()
    ex = FakeExecutor(gate=gate)
    jobs = JobManager(_inventory((_target(), ex)))
    jobs.submit("t1", "check")
    job = jobs.submit("t1", "stacks")
    # Simulate a policy/config change after enqueue; execution must still enforce it.
    jobs.docker_settings["t1"] = DockerSettings(mode="external")
    gate.set()
    await _finish(jobs)
    assert job.status == "failed" and "external" in job.error


async def test_regular_os_check_discovers_new_docker_installation_without_image_operations():
    class NewlyInstalledDocker(FakeExecutor):
        installed = False

        def __init__(self):
            super().__init__()
            self.scripts = []

        async def run(self, script, on_line=None, timeout=0):
            self.scripts.append(script)
            if script == DOCKER_DETECT_SCRIPT:
                return ExecResult(rc=0, stdout=f"__DOCKER_INSTALLED__={'yes' if self.installed else 'no'}\n")
            assert script == CHECK_SCRIPT
            return await super().run(script, on_line, timeout)

    ex = NewlyInstalledDocker()
    jobs = JobManager(_inventory((_target(), ex)))
    jobs.submit("t1", "check")
    await _finish(jobs)
    assert jobs.checks["t1"].docker_installed is False
    ex.installed = True
    jobs.submit("t1", "check")
    await _finish(jobs)
    assert jobs.checks["t1"].docker_installed is True
    assert not jobs.stacks
    assert ex.scripts == [CHECK_SCRIPT, DOCKER_DETECT_SCRIPT] * 2


async def test_fresh_positive_detection_overrides_old_negative_scan_for_scheduled_check(monkeypatch):
    from fleetdeck.models import CheckResult

    inv = _inventory((_target(), FakeExecutor()))
    inv.refresh = AsyncMock()
    jobs = JobManager(inv)
    jobs._set_check("t1", CheckResult(checked_at=datetime.now(UTC), docker_installed=True))
    jobs._set_stacks("t1", StacksResult(docker="none", checked_at=datetime.now(UTC)))
    called = []
    submit = jobs.submit

    def record(tid, action, *args, **kwargs):
        called.append(action)
        return submit(tid, action, *args, **kwargs)

    monkeypatch.setattr(jobs, "submit", record)
    await Scheduler(None, inv, jobs, None, check_docker=True).run_once()
    assert called == ["check", "stacks"]


def test_docker_global_enable_flag_is_not_part_of_configuration():
    assert "docker_enabled" not in AppConfig.model_fields
    assert ScheduleConfig().check_docker is False


def test_default_external_manager_is_provider_neutral():
    assert DockerSettings(mode="external").manager_name == "External manager"

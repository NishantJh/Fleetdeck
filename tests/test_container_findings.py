"""Compound findings must survive independent check failures and provider scheduling gates."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_dockhand import config, containers
from test_dockhand_history import execution, inventory
from test_jobs import FakeExecutor, _inventory, _target

from fleetdeck.dockhand import ContainerStatus, DockhandReader, normalize, retain_findings, scheduled_results
from fleetdeck.jobs import JobManager
from fleetdeck.scheduler import Scheduler


def test_pending_keeps_version_image_and_policy_findings_independent():
    rows = normalize(
        containers(),
        {
            "pendingUpdates": [
                {
                    "containerId": "a",
                    "hasImageUpdate": True,
                    "newerVersion": {"tag": "2.0"},
                    "checkedAt": "2026-10-30T10:00:00Z",
                },
                {
                    "containerId": "b",
                    "hasImageUpdate": False,
                    "releaseAgeRemainingHours": 6,
                    "checkedAt": "2026-10-30T10:00:00Z",
                },
            ]
        },
    )
    web = next(r for r in rows if r.container_id == "a")
    held = next(r for r in rows if r.container_id == "b")
    assert web.status == "update" and web.image_update is True and web.newer_version == "2.0" and web.advisory
    assert held.status == "deferred" and held.image_update is False and held.deferred_hours == 6


def test_scheduled_log_preserves_both_findings_and_a_later_error():
    run = execution(
        [
            "  Checking: web (web:latest)",
            "    UPDATE AVAILABLE",
            "    NEWER VERSION: 2.0 (patch)",
            "    Error: 429 PRIVATE-TOKEN",
        ]
    )
    result = scheduled_results(run, inventory(), 1)[0]
    assert result.status == "failed" and result.image_update is True and result.newer_version == "2.0"
    assert result.check_issue == "rate_limit" and result.reported_at
    assert "PRIVATE-TOKEN" not in result.model_dump_json()


async def test_failed_fresh_check_retains_known_update_and_advisory_then_success_clears_them():
    mode = "both"

    def respond(req):
        if req.method == "POST":
            record = {"containerId": "a", "hasUpdate": mode == "both"}
            if mode == "both":
                record["newerVersion"] = {"tag": "2.0"}
            if mode == "failed":
                record["error"] = "401 PRIVATE-TOKEN"
            return httpx.Response(
                200, json={"results": [record, {"containerId": "b", "isLocalImage": True, "hasUpdate": False}]}
            )
        if req.url.path == "/api/containers":
            return httpx.Response(200, json=inventory())
        if req.url.path == "/api/containers/pending-updates":
            return httpx.Response(200, json={"pendingUpdates": []})
        return httpx.Response(200, json={"executions": []})

    reader = DockhandReader(config(allow_checks=True), transport=httpx.MockTransport(respond))
    for mode in ["both", "failed", "current"]:
        reader.last_started.clear()
        reader.request_check("t1")
        await reader.tasks["t1"]
        web = next(c for c in reader.status["t1"].containers if c.container_id == "a")
        if mode == "both":
            assert web.image_update and web.newer_version == "2.0"
        if mode == "failed":
            assert web.status == "failed" and web.image_update and web.newer_version == "2.0"
            assert web.reported_at and web.check_issue == "authentication"
            assert "PRIVATE-TOKEN" not in web.model_dump_json()
        if mode == "current":
            assert web.status == "current" and web.image_update is False and web.newer_version is None
        await reader.refresh(force=True)
        assert next(c for c in reader.status["t1"].containers if c.container_id == "a").status == web.status
    await reader.aclose()


@pytest.mark.parametrize(
    "scheduled,native_enabled,external_allowed",
    [(False, False, True), (True, False, True), (True, True, True), (True, True, False)],
)
async def test_schedule_overall_flag_routes_each_provider_without_native_fallback(
    monkeypatch, scheduled, native_enabled, external_allowed
):
    import fleetdeck.scheduler

    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    inv = _inventory(
        (_target("native"), FakeExecutor()), (_target("external"), FakeExecutor()), (_target("hidden"), FakeExecutor())
    )
    inv.refresh = AsyncMock()
    jobs = JobManager(
        inv,
        dockhand_targets={"external": "https://dockhand.example", "hidden": "https://dockhand.example"},
    )
    from fleetdeck.models import DockerSettings

    if not native_enabled:
        jobs.set_docker_settings("native", DockerSettings(mode="disabled"))
    jobs.hidden.add("hidden")
    called = []
    submit = jobs.submit

    def record(tid, action, *args, **kwargs):
        called.append((tid, action))
        return submit(tid, action, *args, **kwargs)

    monkeypatch.setattr(jobs, "submit", record)
    checks = []
    reader = SimpleNamespace(cfg=SimpleNamespace(allow_checks=external_allowed), tasks={})

    def request(tid, *, scheduled=False):
        checks.append((tid, scheduled))

        async def finish():
            await asyncio.sleep(0)
            reader.tasks.pop(tid, None)

        reader.tasks[tid] = asyncio.create_task(finish())

    reader.request_check = request
    await Scheduler(None, inv, jobs, None, check_docker=scheduled, dockhand=reader).run_once()
    assert (("native", "stacks") in called) is (scheduled and native_enabled)
    assert ("external", "stacks") not in called and ("hidden", "stacks") not in called
    assert checks == ([("external", True)] if scheduled and external_allowed else [])
    assert ("native", "check") in called and ("external", "check") in called
    assert not any(tid == "hidden" for tid, _ in called)


async def test_scheduled_batch_checks_more_than_four_hosts_without_bypassing_capacity(monkeypatch):
    import fleetdeck.scheduler

    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    targets = [(_target(f"host-{i}"), FakeExecutor()) for i in range(6)]
    inv = _inventory(*targets)
    inv.refresh = AsyncMock()
    jobs = JobManager(inv, dockhand_targets={target.id: "https://dockhand.example" for target, _ in targets})
    checked = []
    reader = SimpleNamespace(cfg=SimpleNamespace(allow_checks=True), tasks={})

    def request(tid, *, scheduled=False):
        assert scheduled and len(reader.tasks) < 4
        checked.append(tid)

        async def finish():
            await asyncio.sleep(0)
            reader.tasks.pop(tid, None)

        reader.tasks[tid] = asyncio.create_task(finish())

    reader.request_check = request
    await Scheduler(None, inv, jobs, None, check_docker=True, dockhand=reader).run_once()
    assert len(checked) == 6 and not reader.tasks
    assert all(job.action == "check" for job in jobs.jobs.values())


def test_app_wires_scheduled_dockhand_flag_independently_of_native_enable(tmp_path):
    from fastapi.testclient import TestClient

    from fleetdeck.app import create_app
    from fleetdeck.config import AppConfig, ScheduleConfig

    cfg = AppConfig(
        allow_no_auth=True,
        dockhand=config(allow_checks=True),
        schedule=ScheduleConfig(check_docker=True),
        state_file=str(tmp_path / "state.db"),
    )
    app = create_app(cfg, _inventory((_target(), FakeExecutor())))
    assert app.state.scheduler.check_docker is True
    assert app.state.scheduler.dockhand is app.state.dockhand
    assert app.state.jobs.get_docker_settings("t1").mode == "external"
    with TestClient(app) as client:
        assert client.get("/api/schedule").json()["check_docker"] is True


def test_disabled_cooldown_cannot_create_held_advisory_from_old_pending_record():
    rows = normalize(
        containers(),
        {
            "minimumReleaseAgeHours": 0,
            "pendingUpdates": [
                {"containerId": "a", "hasImageUpdate": True, "releaseAgeRemainingHours": 6},
                {"containerId": "b", "hasImageUpdate": False, "releaseAgeRemainingHours": 6},
            ],
        },
    )
    web = next(row for row in rows if row.container_id == "a")
    other = next(row for row in rows if row.container_id == "b")
    assert web.status == "update" and web.image_update and web.deferred_hours is None
    assert other.status == "unknown" and not other.advisory and other.deferred_hours is None


def test_failed_digest_check_keeps_fresh_version_advisory_over_previous_version():
    old = ContainerStatus(
        container_id="a",
        name="web",
        image="web:1",
        state="running",
        status="update",
        image_update=True,
        newer_version="2.0",
        advisory=True,
    )
    latest = ContainerStatus(
        container_id="a",
        name="web",
        image="web:1",
        state="running",
        status="failed",
        newer_version="3.0",
        advisory=True,
        check_issue="rate_limit",
    )
    result = retain_findings(latest, old)
    assert result.newer_version == "3.0" and result.image_update and result.status == "failed"

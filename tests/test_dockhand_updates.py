from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig, DockhandConfig
from fleetdeck.dockhand import ContainerStatus, DockhandReader, DockhandStatus
from fleetdeck.dockhand_updates import ContainerSelection, excluded, install, validate_selection
from fleetdeck.inventory import Inventory
from fleetdeck.models import Job, Target


def row(**changes):
    return ContainerStatus(
        container_id="old-id",
        name="web",
        image="nginx:1.27",
        state="running",
        status="update",
        image_update=True,
        checked_at=datetime.now(UTC),
        **changes,
    )


def reader_for(response, *, containers=None):
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return response(request) if callable(response) else httpx.Response(200, json=response)
        if request.url.path == "/api/containers":
            return httpx.Response(
                200,
                json=containers
                or [{"id": "old-id", "name": "web", "image": "nginx:1.27", "state": "running", "labels": {}}],
            )
        return httpx.Response(
            200,
            json={
                "pendingUpdates": [
                    {"containerId": "old-id", "hasImageUpdate": True, "checkedAt": datetime.now(UTC).isoformat()}
                ]
            },
        )

    cfg = DockhandConfig(
        url="https://dockhand.invalid",
        token="test-secret",
        environments=[{"target_id": "host", "environment_id": 1, "allow_updates": True}],
    )
    reader = DockhandReader(cfg, transport=httpx.MockTransport(respond))
    r = row()
    reader.status["host"] = DockhandStatus(manager_url=cfg.url, stale=False, containers=[r])
    return reader, calls, [ContainerSelection(container_id=r.container_id, name=r.name, image=r.image, state=r.state)]


@pytest.mark.parametrize(
    "change",
    [
        {"status": "unknown"},
        {"status": "local"},
        {"state": "paused"},
        {"pinned_digest": True},
        {"newer_version": "2"},
        {"deferred_hours": 2},
        {"check_issue": "timeout"},
        {"advisory": True},
        {"name": "dockhand"},
        {"image": "ghcr.io/nishantjh/fleetdeck:latest"},
        {"checked_at": datetime.now(UTC) - timedelta(days=2)},
        {"checked_at": None},
    ],
)
def test_ineligible_results_cannot_authorize_deployment(change):
    r = row().model_copy(update=change)
    assert excluded(r)
    with pytest.raises(ValueError):
        validate_selection(
            DockhandStatus(manager_url="https://dockhand.invalid", stale=False, containers=[r]),
            [ContainerSelection(container_id=r.container_id, name=r.name, image=r.image, state=r.state)],
        )


async def test_batch_targets_exact_environment_and_selected_ids_without_retries():
    reader, calls, selections = reader_for(
        {"results": [{"containerId": "new-id", "containerName": "web", "success": True}]}
    )
    lines = []
    await install(reader, "host", selections, lambda: None, lambda _, line: lines.append(line))
    posts = [r for r in calls if r.method == "POST"]
    assert len(posts) == 1
    assert posts[0].url.path == "/api/containers/batch-update"
    assert posts[0].url.params["env"] == "1"
    assert posts[0].read() == b'{"containerIds":["old-id"]}'
    assert reader.status["host"].containers[0].status == "unknown"
    assert "web: updated" in "\n".join(lines)
    assert not reader.updating
    await reader.aclose()


@pytest.mark.parametrize(
    "result",
    [
        {"results": []},
        {"results": [{"containerName": "web", "success": False, "error": "test-secret"}]},
        {"results": [{"containerName": "web", "success": True, "error": "Skipped"}]},
        {"results": [{"containerName": "web", "success": True, "warnings": ["test-secret"]}]},
        {"results": [{"containerName": "other", "success": True}]},
    ],
)
async def test_partial_skipped_or_warning_responses_are_not_success(result):
    reader, calls, selections = reader_for(result)
    with pytest.raises(ValueError) as error:
        await install(reader, "host", selections, lambda: None, lambda *_: None)
    assert "test-secret" not in str(error.value)
    assert len([r for r in calls if r.method == "POST"]) == 1
    await reader.aclose()


async def test_changed_identity_and_policy_block_before_any_write():
    reader, calls, selections = reader_for(
        {}, containers=[{"id": "new-id", "name": "web", "image": "nginx:1.27", "state": "running"}]
    )
    with pytest.raises(ValueError, match="identity"):
        await install(reader, "host", selections, lambda: None, lambda *_: None)
    assert not any(r.method == "POST" for r in calls)
    await reader.aclose()
    reader, calls, selections = reader_for({})
    n = 0

    def guard():
        nonlocal n
        n += 1
        if n == 2:
            raise ValueError("Policy changed")

    with pytest.raises(ValueError, match="Policy changed"):
        await install(reader, "host", selections, guard, lambda *_: None)
    assert not any(r.method == "POST" for r in calls)
    await reader.aclose()


async def test_timeout_invalidates_results_and_does_not_retry():
    def timeout(request):
        raise httpx.ReadTimeout("test-secret", request=request)

    reader, calls, selections = reader_for(timeout)
    with pytest.raises(ValueError, match="may still be running"):
        await install(reader, "host", selections, lambda: None, lambda *_: None)
    assert reader.status["host"].containers[0].image_update is None
    assert len([r for r in calls if r.method == "POST"]) == 1
    await reader.aclose()


def app_for(tmp_path, allowed=True):
    cfg = AppConfig(
        demo=True,
        allow_no_auth=True,
        state_file=str(tmp_path / "state.db"),
        dockhand={
            "url": "https://dockhand.invalid",
            "token": "test-secret",
            "allow_checks": True,
            "environments": [{"target_id": "host", "environment_id": 1, "allow_updates": allowed}],
        },
    )
    inv = Inventory([])
    inv.refresh = AsyncMock()
    inv.targets["host"] = (Target(id="host", name="host", platform="ssh", source="demo", kind="host"), None)
    app = create_app(cfg, inventory=inv)
    app.state.demo_dockhand["host"] = DockhandStatus(manager_url=cfg.dockhand.url, stale=False, containers=[row()])
    return app


def test_confirmed_demo_update_is_a_job_and_never_contacts_a_manager(tmp_path, monkeypatch):
    async def no_network(*args, **kwargs):
        pytest.fail("Demo must not contact Dockhand")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_network)
    app = app_for(tmp_path)
    body = {
        "target_id": "host",
        "containers": [{"container_id": "old-id", "name": "web", "image": "nginx:1.27", "state": "running"}],
    }
    with TestClient(app) as client:
        assert client.post("/api/dockhand/update", json=body).status_code == 422
        assert len(client.get("/api/dockhand/update-options?target_id=host").json()["containers"]) == 1
        result = client.post("/api/dockhand/update", json={**body, "confirmed": True})
        assert result.status_code == 202, result.text
        job = result.json()["job"]
        assert job["action"] == "dockhand-update"
        assert not app.state.jobs.jobs[job["id"]].resumable
        client.get("/api/jobs")
        assert app.state.jobs.jobs[job["id"]].status == "succeeded"
        result = client.post("/api/actions", json={"action": "dockhand-update", "target_ids": ["host"]})
        assert not result.json()["jobs"]


@pytest.mark.parametrize("mode", ["disabled", "monitor", "busy", "check-only", "stopped"])
def test_api_rejects_disabled_permissions_and_machine_restrictions(tmp_path, mode):
    app = app_for(tmp_path, allowed=mode != "disabled")
    jobs = app.state.jobs
    target = app.state.inventory.targets["host"][0]
    if mode == "monitor":
        jobs.monitor_only_ids.add("host")
    if mode == "check-only":
        target.check_only = True
    if mode == "stopped":
        target.status = "stopped"
    if mode == "busy":
        jobs.jobs["busy"] = Job(
            id="busy", target_id="host", target_name="host", action="check", created_at=datetime.now(UTC)
        )
    with TestClient(app) as client:
        response = client.post(
            "/api/dockhand/update",
            json={
                "target_id": "host",
                "confirmed": True,
                "containers": [{"container_id": "old-id", "name": "web", "image": "nginx:1.27", "state": "running"}],
            },
        )
        assert response.status_code in (403, 409)


@pytest.mark.parametrize("state", ["stopped", "exited", "created"])
def test_confirmed_stopped_containers_are_eligible_and_state_changes_invalidate_confirmation(state):
    r = row().model_copy(update={"state": state})
    assert excluded(r) is None
    selection = ContainerSelection(container_id=r.container_id, name=r.name, image=r.image, state=state)
    status = DockhandStatus(manager_url="https://dockhand.invalid", stale=False, containers=[r])
    assert validate_selection(status, [selection]) == [r]
    status.containers[0] = r.model_copy(update={"state": "running"})
    with pytest.raises(ValueError, match="identity"):
        validate_selection(status, [selection])


async def test_real_reader_api_uses_job_lifecycle_and_mocked_dockhand_only(tmp_path):
    import asyncio

    mock_reader, calls, selections = reader_for(
        {"results": [{"containerId": "new-id", "containerName": "web", "success": True}]}
    )
    cfg = AppConfig(allow_no_auth=True, state_file=str(tmp_path / "state.db"), dockhand=mock_reader.cfg)
    inv = Inventory([])
    inv.refresh = AsyncMock()
    inv.targets["host"] = (Target(id="host", name="host", platform="ssh", source="test", kind="host"), None)
    app = create_app(cfg, inventory=inv)
    reader = app.state.dockhand
    await reader.client.aclose()
    reader.client = mock_reader.client
    reader.status = mock_reader.status
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"target_id": "host", "confirmed": True, "containers": [s.model_dump() for s in selections]}
        result = await client.post("/api/dockhand/update", json=body)
        assert result.status_code == 202, result.text
        await asyncio.gather(*app.state.jobs._tasks)
        job = app.state.jobs.jobs[result.json()["job"]["id"]]
        assert job.status == "succeeded", job.log
        assert len([r for r in calls if r.method == "POST"]) == 1
        assert reader.status["host"].containers[0].status == "unknown"
    await reader.aclose()
    app.state.jobs.store.close()


async def test_interrupted_external_jobs_are_failed_and_never_resumed(tmp_path):
    import asyncio

    from fleetdeck.jobs import JobManager
    from fleetdeck.store import Store

    inv = Inventory([])
    inv.targets["host"] = (Target(id="host", name="host", platform="ssh", source="test", kind="host"), None)
    store = Store(tmp_path / "state.db")
    jobs = JobManager(inv, store=store)
    started = asyncio.Event()

    async def operation(on_line):
        started.set()
        await asyncio.Event().wait()

    job = jobs.submit_dockhand_update("host", operation)
    await started.wait()
    with pytest.raises(ValueError, match="current job"):
        jobs.submit_dockhand_update("host", operation)
    assert jobs.update_blocked(inv.targets["host"][0])
    await jobs.shutdown()
    assert job.status == "failed"
    restored = JobManager(inv, store=store)
    restored.resume()
    assert restored.jobs[job.id].status == "failed"
    assert not restored._tasks
    store.close()


async def test_labels_can_revoke_an_update_before_it_is_sent():
    reader, calls, selections = reader_for(
        {},
        containers=[
            {
                "id": "old-id",
                "name": "web",
                "image": "nginx:1.27",
                "state": "running",
                "labels": {"dockhand.update": "false"},
            }
        ],
    )
    with pytest.raises(ValueError):
        await install(reader, "host", selections, lambda: None, lambda *_: None)
    assert not any(r.method == "POST" for r in calls)
    await reader.aclose()


def test_parent_jobs_block_deployment_and_normal_os_checks_do_not_block_dockhand_checks(tmp_path):
    app = app_for(tmp_path)
    target = app.state.inventory.targets["host"][0]
    target.kind, target.parent = "vm", "parent"
    app.state.inventory.targets["parent"] = (
        Target(id="parent", name="parent", platform="ssh", source="demo", kind="host"),
        None,
    )
    jobs = app.state.jobs
    jobs.jobs["parent-job"] = Job(
        id="parent-job", target_id="parent", target_name="parent", action="reboot", created_at=datetime.now(UTC)
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/dockhand/update",
            json={
                "target_id": "host",
                "confirmed": True,
                "containers": [{"container_id": "old-id", "name": "web", "image": "nginx:1.27", "state": "running"}],
            },
        )
        assert response.status_code == 409
        jobs.jobs.clear()
        jobs.jobs["check-job"] = Job(
            id="check-job", target_id="host", target_name="host", action="check", created_at=datetime.now(UTC)
        )
        assert client.post("/api/dockhand/check", json={"target_id": "host"}).status_code == 202


async def test_pre_update_poll_cannot_restore_invalidated_results():
    import asyncio

    reader, _, selections = reader_for({})
    reached, release = asyncio.Event(), asyncio.Event()
    pending_calls = 0

    async def respond(request):
        nonlocal pending_calls
        if request.method == "POST":
            return httpx.Response(
                200, json={"results": [{"containerId": "new-id", "containerName": "web", "success": True}]}
            )
        if request.url.path == "/api/containers":
            return httpx.Response(
                200, json=[{"id": "old-id", "name": "web", "image": "nginx:1.27", "state": "running"}]
            )
        if request.url.path == "/api/containers/pending-updates":
            pending_calls += 1
            if pending_calls == 1:
                reached.set()
                await release.wait()
            return httpx.Response(
                200,
                json={
                    "pendingUpdates": [
                        {"containerId": "old-id", "hasImageUpdate": True, "checkedAt": datetime.now(UTC).isoformat()}
                    ]
                },
            )
        return httpx.Response(404)

    await reader.client.aclose()
    reader.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    polling = asyncio.create_task(reader.refresh(force=True))
    await reached.wait()
    await install(reader, "host", selections, lambda: None, lambda *_: None)
    release.set()
    await polling
    assert reader.status["host"].containers[0].status == "unknown"
    assert "old-id" not in reader.checked["host"]
    await reader.aclose()


@pytest.mark.parametrize("identity_matches", [True, False])
@pytest.mark.parametrize("legacy_marker", [True, False])
async def test_confirmed_replacement_is_current_until_next_real_check(tmp_path, identity_matches, legacy_marker):
    from fleetdeck.store import Store

    reader, _, selections = reader_for({})
    reader.cfg.allow_checks = True  # Even with permission, deployment must not trigger a registry check.
    await reader.client.aclose()
    installed = False
    calls = []

    def respond(request):
        nonlocal installed
        calls.append(request)
        if request.url.path.endswith("batch-update"):
            installed = True
            return httpx.Response(
                200, json={"results": [{"containerId": "new-id", "containerName": "web", "success": True}]}
            )
        if request.url.path.endswith("check-updates"):
            return httpx.Response(200, json={"results": [{"containerId": "new-id", "hasUpdate": False}]})
        if request.url.path == "/api/containers":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "new-id" if installed else "old-id",
                        "name": "web" if identity_matches or not installed else "other",
                        "image": "nginx:1.27",
                        "state": "running",
                        "labels": {},
                    }
                ],
            )
        return httpx.Response(200, json={"pendingUpdates": []})

    reader.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    reader.store = Store(tmp_path / "marker.db")
    reader.checked["host"] = {"old-id": row()}
    await install(reader, "host", selections, lambda: None, lambda *_: None)
    replacement = reader.status["host"].containers[0]
    assert replacement.container_id == "new-id"
    assert replacement.status == ("current" if identity_matches else "unknown")
    assert replacement.checked_at is None
    assert replacement.image_update is (False if identity_matches else None)
    assert bool(replacement.updated_at) == identity_matches
    assert [r.url.path for r in calls if r.method == "POST"] == ["/api/containers/batch-update"]
    if legacy_marker and identity_matches:
        legacy = replacement.model_copy(update={"status": "updated", "image_update": None})
        reader.checked["host"]["new-id"] = legacy
        reader.status["host"].containers = [legacy]
        reader._save("host")
    await reader.aclose()

    restored = DockhandReader(reader.cfg, store=reader.store, transport=httpx.MockTransport(respond))
    fresh = replacement.model_copy(update={"status": "unknown", "updated_at": None, "source": None})
    merged = restored._merge("host", [fresh])[0]
    assert merged.status == replacement.status
    assert merged.updated_at == replacement.updated_at
    if identity_matches:
        positive = fresh.model_copy(update={"status": "update", "image_update": True})
        assert restored._merge("host", [positive])[0].status == "update"
        restored.checked["host"]["new-id"] = replacement
        local = fresh.model_copy(update={"status": "local"})
        assert restored._merge("host", [local])[0].status == "local"
        assert "new-id" not in restored.checked["host"]
        restored.checked["host"]["new-id"] = replacement
        await restored._check("host", 1)
        result = restored.status["host"].containers[0]
        assert result.status == "current" and result.image_update is False
        assert result.checked_at is not None and result.updated_at is None
    await restored.aclose()
    reader.store.close()

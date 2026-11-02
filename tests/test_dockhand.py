from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_jobs import FakeExecutor, _inventory, _target

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig, AuthConfig, DockhandConfig
from fleetdeck.dockhand import DockhandReader, normalize
from fleetdeck.jobs import JobManager
from fleetdeck.models import DockerSettings
from fleetdeck.scheduler import Scheduler
from fleetdeck.updates import DOCKER_DETECT_SCRIPT


def config(**kwargs):
    return DockhandConfig(
        url="https://dockhand.example",
        token="secret-test-token",
        environments=[
            {"target_id": "t1", "environment_id": 1},
        ],
        **kwargs,
    )


def containers():
    return [
        {
            "id": "a",
            "name": "web",
            "image": "web:latest",
            "state": "running",
            "labels": {
                "com.docker.compose.project": "media",
            },
        },
        {"id": "b", "name": "standalone", "image": "tool:latest", "state": "running"},
    ]


async def test_reader_gets_reports_and_optional_metadata_preserves_history_and_never_follows_redirects():
    calls = []
    fail = False

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer secret-test-token"
        if request.url.path == "/api/schedules/executions":
            assert request.url.params["environmentId"] == "1"
            return httpx.Response(404)
        assert request.url.params["env"] == "1"
        if fail:
            return httpx.Response(302, headers={"location": "https://other.example/steal"})
        if request.url.path == "/api/system":
            return httpx.Response(404)
        if request.url.path == "/api/containers":
            return httpx.Response(200, json=containers())
        assert request.url.path == "/api/containers/pending-updates"
        return httpx.Response(
            200,
            json={
                "pendingUpdates": [
                    {
                        "containerId": "a",
                        "hasImageUpdate": True,
                        "checkedAt": "2026-10-30T10:00:00Z",
                    }
                ]
            },
        )

    reader = DockhandReader(config(), transport=httpx.MockTransport(respond))
    await reader.refresh()
    assert len(calls) == 4
    result = reader.status["t1"]
    assert result.stale is False
    assert result.containers[0].status == "unknown"  # standalone record with no cached outcome
    assert result.containers[1].stack == "media"
    assert result.containers[1].status == "update"
    await reader.refresh()
    assert len(calls) == 4  # ordinary dashboard polling respects the interval
    await reader.refresh(force=True)
    assert len(calls) == 8  # explicit inventory refresh fetches cached GET status immediately
    assert all(req.method == "GET" for req in calls)
    result = reader.status["t1"]
    fail = True
    reader.next_poll = 0
    await reader.refresh()
    failed = reader.status["t1"]
    assert failed.stale and failed.error
    assert failed.synced_at == result.synced_at
    assert failed.containers == result.containers
    assert len(calls) == 9
    assert "secret-test-token" not in failed.model_dump_json()
    assert "other.example" not in failed.model_dump_json()
    await reader.aclose()


def test_normalizer_is_conservative_about_pending_advisories_and_policy():
    source = containers()
    source.extend(
        [
            {"id": "c", "name": "excluded", "labels": {"dockhand.update": "false"}},
            {"id": "d", "name": "hidden", "labels": {"dockhand.hidden": "true"}},
            {"id": "e", "name": "advice"},
            {"id": "f", "name": "waiting"},
        ]
    )
    rows = normalize(
        source,
        {
            "pendingUpdates": [
                {"containerId": "a"},  # older API record: presence alone is not proof
                {"containerId": "c", "hasImageUpdate": True},
                {"containerId": "e", "hasImageUpdate": False, "pinnedToDigest": True},
                {"containerId": "f", "hasImageUpdate": True, "releaseAgeRemainingHours": 3},
                {"containerId": "removed", "hasImageUpdate": True},
            ]
        },
    )
    by_name = {c.name: c for c in rows}
    assert "hidden" not in by_name
    assert len(rows) == 5
    assert by_name["web"].status == "unknown"
    assert by_name["excluded"].status == "excluded"
    assert by_name["advice"].status == "advisory"
    assert by_name["waiting"].status == "deferred"


@pytest.mark.parametrize("payload", [[], {}, {"pendingUpdates": {}}, {"pendingUpdates": None}])
def test_bad_pending_shapes_do_not_become_healthy(payload):
    with pytest.raises(ValueError):
        normalize(containers(), payload)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:secret@host", "//host", "https://host?token=x"])
def test_config_rejects_unsafe_urls(url):
    with pytest.raises(ValidationError):
        DockhandConfig(url=url, token="secret", environments=[{"target_id": "a", "environment_id": 1}])


async def test_mapping_blocks_native_jobs_even_when_native_enabled_and_policy_changes():
    jobs = JobManager(
        _inventory((_target(), FakeExecutor())),
        dockhand_targets={"t1": "https://dockhand.example"},
    )
    for action in ("stacks", "stack-update"):
        with pytest.raises(ValueError, match="external"):
            jobs.submit("t1", action)
        with pytest.raises(ValueError, match="external"):
            jobs._ensure_docker_action("t1", action)  # same guard used queued/resumed jobs
    with pytest.raises(ValueError, match="mapped to Dockhand"):
        jobs.set_docker_settings("t1", DockerSettings(mode="managed"))


def test_status_api_keeps_token_private_and_docker_disabled(tmp_path, monkeypatch):
    cfg = AppConfig(allow_no_auth=True, state_file=str(tmp_path / "state.db"), dockhand=config())
    app = create_app(cfg, _inventory((_target(), FakeExecutor())))
    monkeypatch.setattr(app.state.inventory, "refresh", AsyncMock())
    reader = app.state.dockhand
    monkeypatch.setattr(reader, "refresh", AsyncMock())
    with TestClient(app) as client:
        response = client.get("/api/targets")
        assert response.status_code == 200
        data = response.json()
        assert data["settings"]["containers_visible"] is True
        assert data["settings"]["dockhand_results_persistent"] is True
        assert "docker_enabled" not in data["settings"]
        assert data["targets"][0]["docker_settings"]["mode"] == "external"
        assert cfg.dockhand.token not in response.text
        rejected = client.post("/api/actions", json={"action": "stacks", "target_ids": ["t1"]}).json()
        assert not rejected["jobs"]
        assert "external" in rejected["rejected"]["t1"]
        reader.refresh.assert_awaited_with(force=False)
        assert client.get("/api/targets?refresh=true").status_code == 200
        reader.refresh.assert_awaited_with(force=True)


def test_demo_never_creates_external_reader(tmp_path):
    cfg = AppConfig(demo=True, dockhand=config(), state_file=str(tmp_path / "state.db"))
    app = create_app(cfg)
    assert app.state.dockhand is None


def test_unmatched_mapping_is_visible_instead_of_silently_showing_zero_hosts(tmp_path):
    cfg = AppConfig(demo=True, dockhand=config(), state_file=str(tmp_path / "state.db"))
    with TestClient(create_app(cfg)) as client:
        data = client.get("/api/targets").json()
        assert "Dockhand mapping" in data["errors"]
        assert "environment 1: t1" in data["errors"]["Dockhand mapping"]
        assert "exact target.id" in data["errors"]["Dockhand mapping"]
        assert "secret-test-token" not in str(data)


def test_docker_demo_config_shows_mixed_management_without_any_http_reader(tmp_path):
    from fleetdeck.config import load_config

    cfg = load_config("config.docker-demo.yaml")
    cfg.state_file = str(tmp_path / "state.db")
    app = create_app(cfg)
    assert app.state.dockhand is None
    with TestClient(app) as client:
        targets = client.get("/api/targets").json()["targets"]
        vm = next(v for v in targets if v["target"]["name"] == "docker-vm")
        assert vm["dockhand"]["simulated"] is True
        assert vm["docker_settings"]["mode"] == "external"
        assert any(c["stack"] is None for c in vm["dockhand"]["containers"])
        assert any(c["stack"] == "demo-app" for c in vm["dockhand"]["containers"])
        pi = next(v for v in targets if v["target"]["name"] == "pi4")
        assert pi["docker_settings"]["mode"] == "managed"
        response = client.post("/api/dockhand/check", json={"target_id": vm["target"]["id"]})
        assert response.status_code == 202 and response.json()["simulated"] is True
        checked = next(
            v for v in client.get("/api/targets").json()["targets"] if v["target"]["id"] == vm["target"]["id"]
        )
        assert checked["dockhand"]["last_check_at"]
        assert any(c["status"] == "current" for c in checked["dockhand"]["containers"])


def test_unauthenticated_requests_never_poll_dockhand(tmp_path, monkeypatch):
    cfg = AppConfig(auth=AuthConfig(password="test-password"), dockhand=config(), state_file=str(tmp_path / "state.db"))
    app = create_app(cfg)
    poll = AsyncMock()
    monkeypatch.setattr(app.state.dockhand, "refresh", poll)
    with TestClient(app) as client:
        assert client.get("/api/targets").status_code == 401
        poll.assert_not_awaited()


async def test_scheduled_os_checks_skip_native_discovery_and_pulls_on_dockhand_mapping(monkeypatch):
    executor = FakeExecutor()
    executor.run = AsyncMock(wraps=executor.run)
    inv = _inventory((_target(), executor))
    monkeypatch.setattr(inv, "refresh", AsyncMock())
    jobs = JobManager(inv, dockhand_targets={"t1": "https://dockhand.example"})
    scheduler = Scheduler(None, inv, jobs, None, check_docker=True)
    await scheduler.run_once()
    assert [job.action for job in jobs.jobs.values()] == ["check"]
    assert all(call.args[0] != DOCKER_DETECT_SCRIPT for call in executor.run.call_args_list)


async def test_explicit_check_captures_full_results_and_keeps_them_across_cached_refresh():
    calls = []

    def respond(req):
        calls.append(req)
        if req.url.path == "/api/system":
            return httpx.Response(404)
        if req.url.path == "/api/containers":
            return httpx.Response(200, json=containers())
        if req.url.path == "/api/containers/pending-updates":
            return httpx.Response(200, json={"pendingUpdates": []})
        if req.url.path == "/api/schedules/executions":
            return httpx.Response(404)
        assert req.url.path == "/api/containers/check-updates"
        assert req.method == "POST" and req.headers["accept"] == "application/json"
        assert req.url.params["env"] == "1"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"containerId": "a", "hasUpdate": False},
                    {"containerId": "b", "hasUpdate": False, "error": "secret-test-token must not leak"},
                ]
            },
        )

    reader = DockhandReader(config(allow_checks=True), transport=httpx.MockTransport(respond))
    reader.request_check("t1")
    assert reader.status["t1"].checking
    with pytest.raises(ValueError, match="running"):
        reader.request_check("t1")
    await reader.tasks["t1"]
    result = reader.status["t1"]
    assert not result.checking and result.last_check_at
    by_name = {c.name: c for c in result.containers}
    assert by_name["web"].status == "current"
    assert by_name["standalone"].status == "failed"
    assert all(c.checked_at for c in result.containers)
    assert "secret-test-token" not in result.model_dump_json()
    await reader.refresh()
    assert {c.status for c in reader.status["t1"].containers} == {"current", "failed"}
    assert reader.status["t1"].last_check_at == result.last_check_at
    assert sum(req.method == "POST" for req in calls) == 1
    replacement = containers()
    replacement[0]["id"] = "replacement-container"
    merged = reader._merge("t1", normalize(replacement, {"pendingUpdates": []}))
    assert next(c for c in merged if c.name == "web").status == "unknown"
    replacement = containers()
    replacement[0]["labels"]["dockhand.update"] = "false"
    merged = reader._merge("t1", normalize(replacement, {"pendingUpdates": []}))
    assert next(c for c in merged if c.name == "web").status == "excluded"
    await reader.aclose()


async def test_manual_check_is_opt_in_and_never_retries_mutations_or_redirects():
    calls = []

    def respond(req):
        calls.append(req)
        if req.method == "GET":
            return httpx.Response(200, json=containers())
        return httpx.Response(302, headers={"location": "https://foreign.example"})

    reader = DockhandReader(config(), transport=httpx.MockTransport(respond))
    with pytest.raises(PermissionError):
        reader.request_check("t1")
    assert calls == []
    reader.cfg.allow_checks = True
    with pytest.raises(KeyError):
        reader.request_check("other")
    reader.request_check("t1")
    await reader.tasks["t1"]
    assert reader.status["t1"].check_error
    assert reader.status["t1"].last_check_at is None
    assert len(calls) == 2
    assert all(req.url.host == "dockhand.example" for req in calls)
    await reader.aclose()


def test_manual_check_route_requires_login_csrf_opt_in_and_a_known_mapping(tmp_path, monkeypatch):
    cfg = AppConfig(
        auth=AuthConfig(password="test"), dockhand=config(allow_checks=True), state_file=str(tmp_path / "state.db")
    )
    app = create_app(cfg, _inventory((_target(), FakeExecutor())))
    monkeypatch.setattr(app.state.inventory, "refresh", AsyncMock())
    start = Mock()
    monkeypatch.setattr(app.state.dockhand, "request_check", start)
    with TestClient(app) as client:
        body = {"target_id": "t1"}
        assert client.post("/api/dockhand/check", json=body).status_code == 401
        auth = ("admin", "test")
        assert (
            client.post(
                "/api/dockhand/check", json=body, auth=auth, headers={"Origin": "https://foreign.example"}
            ).status_code
            == 403
        )
        start.assert_not_called()
        assert client.post("/api/dockhand/check", json={"target_id": "other"}, auth=auth).status_code == 404
        assert client.post("/api/dockhand/check", json=body, auth=auth).status_code == 202
        start.assert_called_once_with("t1")
        cfg.dockhand.allow_checks = False
        assert client.post("/api/dockhand/check", json=body, auth=auth).status_code == 403


@pytest.mark.parametrize(
    "status, metadata, expected",
    [
        (200, {"docker": {"serverVersion": "28.5.1", "version": "27.0.0"}}, "28.5.1"),
        (200, {"docker": {"version": "27.5.0"}}, "27.5.0"),
        (200, {"docker": None}, None),
        (200, {"docker": {"serverVersion": "<script>bad</script>"}}, None),
        (200, {"docker": {"serverVersion": "1" * 65}}, None),
        (403, {}, None),
        (404, {}, None),
        (302, {}, None),
    ],
)
async def test_engine_metadata_is_optional_read_only_and_never_breaks_reports(status, metadata, expected):
    calls = []

    def respond(req):
        calls.append(req)
        assert req.method == "GET"
        if req.url.path == "/api/containers":
            return httpx.Response(200, json=containers())
        if req.url.path == "/api/containers/pending-updates":
            return httpx.Response(200, json={"pendingUpdates": []})
        if req.url.path == "/api/system":
            assert req.url.params["env"] == "1"
            return httpx.Response(
                status, json=metadata, headers={"location": "https://other.example"} if status == 302 else {}
            )
        return httpx.Response(404)

    reader = DockhandReader(config(), transport=httpx.MockTransport(respond))
    try:
        await reader.refresh()
        result = reader.status["t1"]
        assert result.docker_version == expected
        assert not result.error and not result.stale
        assert result.containers
        assert all(req.url.host == "dockhand.example" for req in calls)
    finally:
        await reader.aclose()

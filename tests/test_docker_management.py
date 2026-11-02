from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from fleetdeck.app import create_app
from fleetdeck.config import load_config
from fleetdeck.inventory import Inventory
from fleetdeck.models import Job, Target


@pytest.fixture
def management(tmp_path, monkeypatch):
    async def no_external_requests(*args, **kwargs):
        pytest.fail("Provider configuration must not contact a manager")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_external_requests)

    def create(dockhand=None):
        path = tmp_path / "config.yaml"
        raw = {
            "demo": True,
            "allow_no_auth": True,
            "state_file": str(tmp_path / "state.db"),
            "schedule": {"check_every": None, "check_docker": False},
            "snapshot_keep": 7,
        }
        if dockhand:
            raw["dockhand"] = dockhand
        path.write_text(yaml.safe_dump(raw))
        inv = Inventory([])
        inv.refresh = AsyncMock()
        for tid in ("machine-a", "machine-b"):
            inv.targets[tid] = (Target(id=tid, name=tid, platform="ssh", source="test", kind="host"), None)
        app = create_app(load_config(str(path)), inventory=inv, config_file=str(path), restart=lambda: None)
        return app, path

    return create


def details(client, tid="machine-a"):
    response = client.get("/api/setup/docker-management", params={"target_id": tid})
    assert response.status_code == 200, response.text
    return response.json()


def body(client, **changes):
    return {
        "target_id": "machine-a",
        "revision": details(client)["revision"],
        "mode": "dockhand",
        "url": "https://dockhand.invalid",
        "token": "new-test-secret",
        "poll_seconds": 300,
        "allow_checks": True,
        "environment_id": 1,
        **changes,
    }


def test_menu_config_saves_mapping_without_contacting_manager_and_never_returns_token(management):
    app, path = management()
    with TestClient(app) as client:
        assert details(client)["mode"] == "managed"
        response = client.put("/api/setup/docker-management", json=body(client))
        assert response.status_code == 200 and response.json()["restart_pending"]
        saved = yaml.safe_load(path.read_text())
        assert saved["snapshot_keep"] == 7 and saved["schedule"]["check_docker"] is False
        assert saved["dockhand"]["environments"] == [{"target_id": "machine-a", "environment_id": 1}]
        assert saved["dockhand"]["token"] == "new-test-secret"
        assert saved["dockhand"]["allow_checks"] is True
        assert path.stat().st_mode & 0o777 == 0o600
        view = details(client)
        assert view["mode"] == "dockhand" and view["token_configured"] and view["shared_hosts"] == 1
        assert "token" not in view and "new-test-secret" not in response.text
        assert "new-test-secret" not in client.get("/api/setup/docker-management?target_id=machine-a").text
        assert app.state.jobs.get_docker_settings("machine-a").mode == "external"
        assert not app.state.jobs.jobs


def test_shared_connection_preserves_secret_and_url_environment_references(management, monkeypatch):
    monkeypatch.setenv("FLEETDECK_TEST_DH_TOKEN", "resolved-test-secret")
    monkeypatch.setenv("FLEETDECK_TEST_DH_URL", "https://dockhand.invalid")
    app, path = management(
        {
            "url": "${FLEETDECK_TEST_DH_URL}",
            "token": "${FLEETDECK_TEST_DH_TOKEN}",
            "environments": [{"target_id": "machine-b", "environment_id": 2}],
        }
    )
    with TestClient(app) as client:
        response = client.put("/api/setup/docker-management", json=body(client, token=None))
        assert response.status_code == 200, response.text
        saved = yaml.safe_load(path.read_text())["dockhand"]
        assert saved["url"] == "${FLEETDECK_TEST_DH_URL}" and saved["token"] == "${FLEETDECK_TEST_DH_TOKEN}"
        assert saved["environments"] == [
            {"target_id": "machine-b", "environment_id": 2},
            {"target_id": "machine-a", "environment_id": 1},
        ]
        assert "resolved-test-secret" not in str(details(client))


def test_url_change_requires_new_token_and_does_not_forward_old_secret(management):
    app, path = management(
        {
            "url": "https://old.invalid",
            "token": "old-secret",
            "environments": [{"target_id": "machine-a", "environment_id": 1}],
        }
    )
    before = path.read_text()
    with TestClient(app) as client:
        response = client.put("/api/setup/docker-management", json=body(client, url="https://new.invalid", token=None))
        assert response.status_code == 400 and "changing the Dockhand URL" in response.text
        assert "old-secret" not in response.text and path.read_text() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "javascript:alert(1)"},
        {"url": "https://user:pass@dockhand.invalid"},
        {"environment_id": 2},
        {"token": "bad token"},
        {"target_id": "unknown"},
    ],
)
def test_invalid_or_duplicate_mapping_leaves_file_and_policy_unchanged(management, changes):
    app, path = management(
        {
            "url": "https://dockhand.invalid",
            "token": "old-secret",
            "environments": [{"target_id": "machine-b", "environment_id": 2}],
        }
    )
    before = path.read_text()
    with TestClient(app) as client:
        response = client.put("/api/setup/docker-management", json=body(client, **changes))
        assert response.status_code in (400, 404), response.text
        assert path.read_text() == before and "machine-a" not in app.state.jobs.docker_settings


def test_stale_revision_and_busy_jobs_block_provider_changes(management):
    app, path = management()
    before = path.read_text()
    with TestClient(app) as client:
        request = body(client)
        response = client.put("/api/setup/docker-management", json={**request, "revision": "0" * 64})
        assert response.status_code == 409
        app.state.jobs.jobs["busy"] = Job(
            id="busy",
            target_id="machine-b",
            target_name="machine-b",
            action="check",
            status="running",
            created_at=datetime.now(UTC),
        )
        response = client.put("/api/setup/docker-management", json=request)
        assert response.status_code == 409 and path.read_text() == before
        app.state.jobs.jobs.clear()


def test_switch_back_to_fleetdeck_removes_only_its_mapping_and_keeps_live_guard_until_apply(management):
    app, path = management(
        {
            "url": "https://dockhand.invalid",
            "token": "old-secret",
            "environments": [
                {"target_id": "machine-a", "environment_id": 1},
                {"target_id": "machine-b", "environment_id": 2},
            ],
        }
    )
    with TestClient(app) as client:
        response = client.put("/api/setup/docker-management", json=body(client, mode="managed", token=None))
        assert response.status_code == 200
        assert yaml.safe_load(path.read_text())["dockhand"]["environments"] == [
            {"target_id": "machine-b", "environment_id": 2}
        ]
        assert app.state.jobs.get_docker_settings("machine-a").mode == "external"
        assert app.state.jobs.docker_settings["machine-a"].mode == "managed"
        assert details(client)["mode"] == "managed"


def test_read_only_config_and_failed_policy_persistence_do_not_change_config(management, monkeypatch):
    from fleetdeck.onboarding import Paths

    app, path = management()
    before = path.read_text()
    with TestClient(app) as client:
        request = body(client)
        with monkeypatch.context() as patch:
            patch.setattr(Paths, "writable", lambda self: False)
            assert details(client)["writable"] is False
            assert client.put("/api/setup/docker-management", json=request).status_code == 409

        def fail(*args):
            raise OSError("policy storage unavailable")

        monkeypatch.setattr(app.state.jobs, "stage_docker_settings", fail)
        with pytest.raises(OSError):
            client.put("/api/setup/docker-management", json=request)
        assert path.read_text() == before


def test_token_validation_never_echoes_submitted_secret(management):
    app, _ = management()
    with TestClient(app) as client:
        secret = "sensitive-marker-" * 300
        response = client.put("/api/setup/docker-management", json=body(client, token=secret))
        assert response.status_code == 422 and "sensitive-marker" not in response.text


def test_last_mapping_removal_clears_connection_and_secret(management):
    app, path = management(
        {
            "url": "https://dockhand.invalid",
            "token": "old-secret",
            "environments": [{"target_id": "machine-a", "environment_id": 1}],
        }
    )
    with TestClient(app) as client:
        assert (
            client.put("/api/setup/docker-management", json=body(client, mode="monitor", token=None)).status_code == 200
        )
        assert "dockhand" not in yaml.safe_load(path.read_text())
        assert details(client)["token_configured"] is False


def test_provider_settings_keep_csrf_guard_and_first_run_gate(management, tmp_path):
    app, path = management()
    before = path.read_text()
    with TestClient(app) as client:
        response = client.put(
            "/api/setup/docker-management", json=body(client), headers={"Origin": "https://other.invalid"}
        )
        assert response.status_code == 403 and path.read_text() == before
    locked_path = tmp_path / "fresh.yaml"
    with TestClient(create_app(config_file=str(locked_path))) as client:
        response = client.get("/api/setup/docker-management?target_id=machine-a")
        assert response.status_code in (401, 403)


def test_shared_provider_change_and_apply_wait_for_active_dockhand_checks(management):
    import asyncio

    from fleetdeck.dockhand import DockhandReader

    app, path = management(
        {
            "url": "https://dockhand.invalid",
            "token": "old-secret",
            "environments": [{"target_id": "machine-a", "environment_id": 1}],
        }
    )
    # Recreate without demo so the real reader's task guard is exercised; requests remain forbidden by the fixture.
    cfg = load_config(str(path)).model_copy(update={"demo": False})
    app = create_app(cfg, inventory=app.state.inventory, config_file=str(path), restart=lambda: None)
    with TestClient(app) as client:
        assert isinstance(app.state.dockhand, DockhandReader)

        def register():
            app.state.dockhand.tasks["machine-a"] = asyncio.create_task(asyncio.sleep(60))

        client.portal.call(register)
        before = path.read_text()
        response = client.put("/api/setup/docker-management", json=body(client, token=None))
        assert response.status_code == 409 and path.read_text() == before
        assert client.post("/api/setup/apply").status_code == 409


def test_install_permission_is_default_off_and_applies_only_to_selected_mapping(management):
    app, path = management(
        {
            "url": "https://dockhand.invalid",
            "token": "test-secret",
            "environments": [
                {"target_id": "machine-a", "environment_id": 1},
                {"target_id": "machine-b", "environment_id": 2},
            ],
        }
    )
    with TestClient(app) as client:
        assert details(client)["allow_updates"] is False
        request = body(client, token=None, allow_updates=True)
        assert client.put("/api/setup/docker-management", json=request).status_code == 200
        assert details(client)["allow_updates"] is True
        assert details(client, "machine-b")["allow_updates"] is False
        mappings = yaml.safe_load(path.read_text())["dockhand"]["environments"]
        assert next(m for m in mappings if m["target_id"] == "machine-a")["allow_updates"] is True

import json
from datetime import UTC, datetime, timedelta

from fleetdeck import selfupdate as su
from fleetdeck.models import Stack, StackService, StacksResult, Target

NOW = datetime.now(UTC)


def _stack(**changes):
    service = StackService(
        name="fleetdeck",
        image="ghcr.io/nishantjh/fleetdeck:1",
        status="running",
        container_id="c" * 64,
        outdated=True,
    )
    return Stack(
        name="fleetdeck",
        working_dir="/opt/fleetdeck",
        config_files=["/opt/fleetdeck/docker-compose.yml"],
        services=[service],
        is_dashboard=True,
        **changes,
    )


def test_find_dashboard_prefers_the_containers_own_id():
    other = StackService(name="sidecar", image="sidecar:1", status="running", container_id="d" * 64)
    stack = _stack()
    stack.services.insert(0, other)
    result = StacksResult(stacks=[stack], checked_at=NOW)
    found = su.find_dashboard({"ssh:pi": result}, "c" * 64)
    assert found is not None
    target_id, found_stack, service = found
    assert target_id == "ssh:pi" and found_stack is stack and service.name == "fleetdeck"


def test_info_is_unsupported_without_the_dashboard_stack():
    info = su.info({}, None, "2.0.0", {})
    assert info["supported"] is False
    assert "terminal" in info["reason"]


def test_info_reports_the_target_availability_and_last_result():
    result = StacksResult(stacks=[_stack()], checked_at=NOW)
    target = Target(id="ssh:pi", name="pi", platform="ssh", source="hosts", kind="host")
    info = su.info(
        {"ssh:pi": result},
        "c" * 64,
        "2.0.0",
        {"ssh:pi": (target, object())},
        {"state": "done", "to_version": "2.1.0"},
    )
    assert info["supported"] is True
    assert info["available"] is True
    assert (info["target"], info["stack"], info["service"]) == ("pi", "fleetdeck", "fleetdeck")
    assert info["last"]["state"] == "done"


def test_info_refuses_a_check_only_machine():
    result = StacksResult(stacks=[_stack()], checked_at=NOW)
    target = Target(id="ssh:pi", name="pi", platform="ssh", source="hosts", kind="host", check_only=True)
    info = su.info({"ssh:pi": result}, "c" * 64, "2.0.0", {"ssh:pi": (target, object())})
    assert info["supported"] is False and "check-only" in info["reason"]


def test_scripts_name_only_the_dashboard_service():
    stack = _stack()
    pull = su.pull_script(stack, "fleetdeck")
    recreate = su.recreate_script(stack, "fleetdeck")
    assert "pull --quiet" in pull and "fleetdeck" in pull
    assert "--no-deps fleetdeck" in recreate
    assert "setsid" in recreate and su.SELF_UPDATE_LOG in recreate
    assert "sidecar" not in pull + recreate


def test_the_marker_finishes_when_a_new_process_starts(tmp_path):
    su.begin(tmp_path, version="2.0.0", image="img", target="pi", service="fleetdeck", stack="fleetdeck")
    assert su.read_result(tmp_path)["state"] == "recreating"
    # The restarted process starts after the marker was written.
    su.finish_startup(tmp_path, datetime.now(UTC) + timedelta(seconds=1), "2.1.0")
    data = su.read_result(tmp_path)
    assert data["state"] == "done" and data["to_version"] == "2.1.0"
    assert data["finished_at"]


def test_a_stale_marker_is_marked_failed_while_the_old_process_still_runs(tmp_path):
    su.begin(tmp_path, version="2.0.0", image="img", target="pi", service="fleetdeck", stack="fleetdeck")
    path = tmp_path / su.MARKER
    data = json.loads(path.read_text())
    data["started_at"] = (NOW - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(data))
    # This process started ten minutes ago, so it is the old one and the recreate never replaced it.
    su.mark_stale(tmp_path, NOW - timedelta(minutes=10))
    data = su.read_result(tmp_path)
    assert data["state"] == "failed" and "did not restart" in data["detail"]

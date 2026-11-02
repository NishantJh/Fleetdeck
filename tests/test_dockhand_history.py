"""Read-only Dockhand history and restart persistence, with synthetic responses only."""

from copy import deepcopy
from datetime import timedelta

import httpx
import pytest
from test_dockhand import config, containers

from fleetdeck.dockhand import DockhandReader, scheduled_results
from fleetdeck.store import Store

START = "2026-10-30T10:00:00Z"
END = "2026-10-30T10:01:00Z"


def inventory():
    items = containers()
    for item in items:
        item["created"] = 1700000000
    return items


def execution(messages=None):
    messages = messages or [
        "  Checking: web (web:latest)",
        "    Up to date",
        "  Checking: standalone (tool:latest)",
        "    Error: PRIVATE-TOKEN",
    ]
    return {
        "id": 12,
        "environmentId": 1,
        "scheduleType": "env_update_check",
        "status": "success",
        "startedAt": START,
        "completedAt": END,
        "details": {"containersChecked": sum(m.startswith("  Checking:") for m in messages), "errors": 1},
        "logs": "\n".join(f"[2026-10-30T10:00:{i:02d}Z] {message}" for i, message in enumerate(messages)),
    }


def test_explicit_history_outcomes_not_whole_run_success():
    results = {r.name: r for r in scheduled_results(execution(), inventory(), 1)}
    assert results["web"].status == "current"
    assert results["standalone"].status == "failed"
    assert results["web"].checked_at and results["web"].source == "dockhand_schedule"
    assert "PRIVATE-TOKEN" not in str(results)
    assert scheduled_results(execution(["=== SUMMARY ===", "No digest updates"]), inventory(), 1) == []


@pytest.mark.parametrize(
    "change",
    [
        {"environmentId": 2},
        {"scheduleType": "container_update"},
        {"status": "running"},
        {"status": "failed"},
        {"startedAt": None},
        {"completedAt": None},
        {"completedAt": "2026-10-29T00:00:00Z"},
        {"logs": "Up to date"},
    ],
)
def test_unmapped_partial_and_unrecognised_history_stays_unknown(change):
    run = execution()
    run.update(change)
    assert scheduled_results(run, inventory(), 1) == []


@pytest.mark.parametrize(
    "change",
    [
        {"created": None},
        {"created": 9999999999},
        {"created": True},
        {"created": "1700000000"},
        {"image": "web:replacement"},
        {"labels": {"dockhand.update": "false"}},
        {"labels": {"dockhand.watch.digest": "false"}},
    ],
)
def test_replacements_missing_identity_and_exclusions_do_not_become_current(change):
    items = inventory()
    items[0].update(change)
    assert all(r.container_id != "a" for r in scheduled_results(execution(), items, 1))


def test_duplicate_names_and_broken_timestamps_fail_closed():
    items = inventory()
    items.append({**items[0], "id": "duplicate"})
    assert all(r.container_id != "a" for r in scheduled_results(execution(), items, 1))
    run = execution()
    run["logs"] = run["logs"].replace("10:00:01Z", "09:00:01Z")
    assert all(r.container_id != "a" for r in scheduled_results(run, inventory(), 1))


@pytest.mark.parametrize(
    "messages,expected",
    [
        (["    UPDATE AVAILABLE", "    NEWER VERSION: 2.0"], "update"),
        (["    Reported only - the digest is pinned in the compose file", "    Up to date"], "advisory"),
        (["    Up to date", "    NEWER VERSION: 2.0"], "advisory"),
        (["    Up to date", "  [web] Error: private error"], "failed"),
        (["    Update deferred: 3 hour(s) remain in image update cooldown"], "deferred"),
        (["    Local image - skipping update check"], "local"),
    ],
)
def test_advisories_failures_and_local_images_remain_distinct(messages, expected):
    run = execution(["  Checking: web (web:latest)", *messages])
    assert scheduled_results(run, inventory(), 1)[0].status == expected


async def test_poll_reads_history_only_and_restart_retains_results_without_secrets(tmp_path):
    calls = []
    run = execution()
    replacement = False
    deny = False

    def respond(req):
        calls.append(req)
        assert req.method == "GET"
        if req.url.path == "/api/system":
            return httpx.Response(404)
        if req.url.path == "/api/containers":
            items = inventory()
            if replacement:
                items[0]["id"] = "new-id"
                items[0]["created"] = 9999999999
            return httpx.Response(200, json=items)
        if req.url.path == "/api/containers/pending-updates":
            return httpx.Response(200, json={"pendingUpdates": []})
        if req.url.path == "/api/schedules/executions":
            assert req.url.params["environmentId"] == "1"
            assert req.url.params["scheduleType"] == "env_update_check"
            assert req.url.params["limit"] == "1"
            if deny:
                return httpx.Response(403, text="PRIVATE-TOKEN")
            return httpx.Response(200, json={"executions": [{k: v for k, v in run.items() if k != "logs"}]})
        assert req.url.path == "/api/schedules/executions/12"
        return httpx.Response(200, json=run)

    store = Store(tmp_path / "state.db")
    reader = DockhandReader(config(), store=store, transport=httpx.MockTransport(respond))
    await reader.refresh()
    result = reader.status["t1"]
    assert {c.status for c in result.containers} == {"current", "failed"}
    assert result.scheduled_errors == 1 and result.scheduled_checked == 2
    assert result.scheduled_check_at and result.last_check_at is None
    saved = store.get_value(reader._cache_key("t1"))
    assert "PRIVATE-TOKEN" not in saved and "secret-test-token" not in saved
    assert '"logs"' not in saved
    await reader.aclose()

    reader = DockhandReader(config(), store=store, transport=httpx.MockTransport(respond))
    assert reader.status["t1"].stale and not reader.status["t1"].checking
    assert {c.status for c in reader.status["t1"].containers} == {"current", "failed"}
    deny = True
    await reader.refresh()
    assert not reader.status["t1"].stale and reader.status["t1"].history_error
    assert {c.status for c in reader.status["t1"].containers} == {"current", "failed"}
    replacement = True
    await reader.refresh(force=True)
    assert next(c for c in reader.status["t1"].containers if c.container_id == "new-id").status == "unknown"
    assert "a" not in reader.checked["t1"]
    remapped = config()
    remapped.environments[0].environment_id = 2
    other = DockhandReader(remapped, store=store)
    assert other.status == {} and other.checked == {}
    store.set_value(reader._cache_key("t1"), "broken JSON")
    corrupt = DockhandReader(config(), store=store)
    assert corrupt.status == {}
    await other.aclose()
    await corrupt.aclose()
    await reader.aclose()
    store.close()


async def test_denied_redirected_and_wrong_environment_history_keeps_inventory_usable():
    for response in [
        httpx.Response(403),
        httpx.Response(404),
        httpx.Response(302, headers={"location": "https://other.example"}),
        httpx.Response(200, json={"executions": [{"id": 12, "environmentId": 2, "scheduleType": "env_update_check"}]}),
        httpx.Response(
            200,
            json={"executions": [{"id": "../../elsewhere", "environmentId": 1, "scheduleType": "env_update_check"}]},
        ),
    ]:
        calls = []

        def respond(req, calls=calls, response=response):
            calls.append(req)
            if req.url.path == "/api/system":
                return httpx.Response(404)
            if req.url.path == "/api/containers":
                return httpx.Response(200, json=inventory())
            if req.url.path == "/api/containers/pending-updates":
                return httpx.Response(200, json={"pendingUpdates": []})
            assert req.url.path == "/api/schedules/executions"
            return deepcopy(response)

        reader = DockhandReader(config(), transport=httpx.MockTransport(respond))
        await reader.refresh()
        result = reader.status["t1"]
        assert not result.error and not result.stale and result.history_error
        assert all(c.status == "unknown" for c in result.containers)
        assert all(req.url.host == "dockhand.example" and req.method == "GET" for req in calls)
        await reader.aclose()


async def test_explicit_button_results_survive_restart_and_newer_reports_win(tmp_path):
    pending = []

    def respond(req):
        if req.method == "POST":
            assert req.url.path == "/api/containers/check-updates"
            return httpx.Response(200, json={"results": [{"containerId": "a", "hasUpdate": False}]})
        if req.url.path == "/api/system":
            return httpx.Response(404)
        if req.url.path == "/api/containers":
            return httpx.Response(200, json=inventory())
        if req.url.path == "/api/containers/pending-updates":
            return httpx.Response(200, json={"pendingUpdates": pending})
        assert req.url.path == "/api/schedules/executions"
        return httpx.Response(200, json={"executions": []})

    store = Store(tmp_path / "state.db")
    reader = DockhandReader(config(allow_checks=True), store=store, transport=httpx.MockTransport(respond))
    reader.request_check("t1")
    await reader.tasks["t1"]
    checked_at = next(c.checked_at for c in reader.status["t1"].containers if c.container_id == "a")
    await reader.aclose()
    reader = DockhandReader(config(allow_checks=True), store=store, transport=httpx.MockTransport(respond))
    await reader.refresh()
    web = next(c for c in reader.status["t1"].containers if c.container_id == "a")
    assert web.status == "current" and web.checked_at == checked_at and web.source == "fleetdeck_button"

    pending.append(
        {"containerId": "a", "hasImageUpdate": True, "checkedAt": (checked_at + timedelta(hours=1)).isoformat()}
    )
    await reader.refresh(force=True)
    assert next(c for c in reader.status["t1"].containers if c.container_id == "a").status == "update"
    await reader.aclose()
    store.close()


def test_truncated_or_duplicated_log_structure_cannot_invent_a_success():
    run = execution()
    run["details"]["containersChecked"] = 10
    assert scheduled_results(run, inventory(), 1) == []
    run = execution(
        ["  Checking: web (web:latest)", "    Up to date", "  Checking: web (web:latest)", "    Up to date"]
    )
    assert scheduled_results(run, inventory(), 1) == []

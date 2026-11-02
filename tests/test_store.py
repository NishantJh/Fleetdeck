import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from test_jobs import FakeExecutor, _finish, _inventory, _target

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig
from fleetdeck.jobs import JobManager
from fleetdeck.models import CheckResult, Job, Package
from fleetdeck.store import INTERRUPTED, MAX_STORED_LOG_LINES, NEVER_STARTED, Store


def _job(jid: str, status="succeeded", log=None, started=False) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=jid, target_id="t1", target_name="t1", action="check", status=status,
        created_at=now, started_at=now if started else None, log=log or [],
    )  # fmt: skip


def test_checks_round_trip(tmp_path):
    s = Store(tmp_path / "state.db")
    check = CheckResult(
        checked_at=datetime.now(UTC),
        os="Debian",
        packages=[Package(name="curl", available="2", phased=True)],
    )
    s.save_check("t1", check)
    s.save_check("t1", check.model_copy(update={"os": "Debian 13"}))
    s.close()
    loaded = Store(tmp_path / "state.db").load_checks()
    assert loaded["t1"].os == "Debian 13" and loaded["t1"].packages[0].name == "curl"
    assert loaded["t1"].packages[0].phased is True


def test_jobs_keep_order_trim_and_mark_interrupted(tmp_path):
    s = Store(tmp_path / "sub" / "state.db")  # parent folder is created
    s.save_job(_job("a"))
    s.save_job(_job("b", status="running", started=True))
    s.save_job(_job("c", log=[str(i) for i in range(MAX_STORED_LOG_LINES + 10)]))
    s.save_job(_job("a", log=["saved again"]))  # an update keeps its place
    jobs = s.load_jobs(limit=10)
    assert [j.id for j in jobs] == ["a", "b", "c"]
    assert jobs[0].log == ["saved again"]
    assert jobs[1].status == "failed" and jobs[1].error == INTERRUPTED and jobs[1].finished_at
    assert len(jobs[2].log) == MAX_STORED_LOG_LINES + 1 and jobs[2].log[-1] == str(MAX_STORED_LOG_LINES + 9)
    # Reloading with a smaller limit drops the oldest for good.
    assert [j.id for j in s.load_jobs(limit=2)] == ["b", "c"]
    assert [j.id for j in s.load_jobs(limit=10)] == ["b", "c"]


async def test_job_manager_persists_and_reloads(tmp_path):
    path = tmp_path / "state.db"
    inv = _inventory((_target(), FakeExecutor()))
    jobs = JobManager(inv, store=Store(path))
    job = jobs.submit("t1", "check")
    await _finish(jobs)
    jobs.store.close()

    again = JobManager(inv, store=Store(path))
    assert again.checks["t1"].os == "Debian"
    assert again.jobs[job.id].status == "succeeded"
    assert any("update(s) available" in line for line in again.jobs[job.id].log)


async def test_eviction_deletes_stored_jobs(tmp_path, monkeypatch):
    import fleetdeck.jobs

    monkeypatch.setattr(fleetdeck.jobs, "MAX_JOBS", 2)
    store = Store(tmp_path / "state.db")
    jobs = JobManager(_inventory((_target(), FakeExecutor())), store=store)
    for _ in range(3):
        jobs.submit("t1", "check")
        await _finish(jobs)
    assert len(store.load_jobs(limit=10)) == 2


async def test_store_failure_does_not_fail_jobs(tmp_path):
    store = Store(tmp_path / "state.db")
    jobs = JobManager(_inventory((_target(), FakeExecutor())), store=store)
    store.close()  # every write now raises
    job = jobs.submit("t1", "check")
    await _finish(jobs)
    assert job.status == "succeeded"
    await asyncio.sleep(0)


def test_app_uses_state_file(tmp_path):
    cfg = AppConfig(demo=True, state_file=str(tmp_path / "state.db"))
    with TestClient(create_app(cfg)) as client:
        pi = next(t for t in client.get("/api/targets").json()["targets"] if t["target"]["name"] == "pi4")
        client.post("/api/actions", json={"action": "check", "target_ids": [pi["target"]["id"]]})
        from test_app import _wait_for_jobs

        _wait_for_jobs(client)
    with TestClient(create_app(cfg)) as client:
        assert len(client.get("/api/jobs").json()) == 1
        pi = next(t for t in client.get("/api/targets").json()["targets"] if t["target"]["name"] == "pi4")
        assert len(pi["check"]["packages"]) == 3


def test_unwritable_state_file_falls_back_to_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with TestClient(create_app(AppConfig(demo=True, state_file=str(blocker / "state.db")))) as client:
        assert client.get("/api/jobs").status_code == 200


def test_queued_job_is_marked_never_started(tmp_path):
    s = Store(tmp_path / "state.db")
    s.save_job(_job("q", status="queued"))
    (job,) = s.load_jobs(limit=10)
    assert job.status == "failed" and job.error == NEVER_STARTED and job.finished_at


async def test_shutdown_stops_jobs_and_records_them(tmp_path):
    gate = asyncio.Event()
    store = Store(tmp_path / "state.db")
    jobs = JobManager(_inventory((_target(), FakeExecutor(gate=gate))), store=store)
    running = jobs.submit("t1", "check")
    queued = jobs.submit("t1", "upgrade")
    await asyncio.sleep(0)
    await jobs.shutdown()
    assert running.error == INTERRUPTED and queued.error == NEVER_STARTED
    assert not jobs._tasks
    store.close()
    stored = {j.id: j for j in Store(tmp_path / "state.db").load_jobs(limit=10)}
    assert stored[running.id].error == INTERRUPTED and stored[queued.id].error == NEVER_STARTED


def test_update_commands_round_trip(tmp_path):
    s = Store(str(tmp_path / "s.db"))
    s.set_update_command("t1", "omv-upgrade")
    s.set_update_command("t1", "omv-upgrade --yes")
    s.set_update_command("t2", "x")
    s.set_update_command("t2", None)
    assert Store(str(tmp_path / "s.db")).load_update_commands() == {"t1": "omv-upgrade --yes"}

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from test_jobs import FakeExecutor, _finish, _inventory, _target

from homelab.app import create_app
from homelab.config import AppConfig
from homelab.jobs import JobManager
from homelab.models import CheckResult, Job, Package
from homelab.store import INTERRUPTED, MAX_STORED_LOG_LINES, Store


def _job(jid: str, status="succeeded", log=None) -> Job:
    return Job(
        id=jid, target_id="t1", target_name="t1", action="check", status=status,
        created_at=datetime.now(UTC), log=log or [],
    )  # fmt: skip


def test_checks_round_trip(tmp_path):
    s = Store(tmp_path / "state.db")
    check = CheckResult(checked_at=datetime.now(UTC), os="Debian", packages=[Package(name="curl", available="2")])
    s.save_check("t1", check)
    s.save_check("t1", check.model_copy(update={"os": "Debian 13"}))
    s.close()
    loaded = Store(tmp_path / "state.db").load_checks()
    assert loaded["t1"].os == "Debian 13" and loaded["t1"].packages[0].name == "curl"


def test_jobs_keep_order_trim_and_mark_interrupted(tmp_path):
    s = Store(tmp_path / "sub" / "state.db")  # parent folder is created
    s.save_job(_job("a"))
    s.save_job(_job("b", status="running"))
    s.save_job(_job("c", log=[str(i) for i in range(MAX_STORED_LOG_LINES + 10)]))
    s.save_job(_job("a", log=["saved again"]))  # an update keeps its place
    jobs = s.load_jobs(limit=10)
    assert [j.id for j in jobs] == ["a", "b", "c"]
    assert jobs[0].log == ["saved again"]
    assert jobs[1].status == "failed" and jobs[1].error == INTERRUPTED
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
    import homelab.jobs

    monkeypatch.setattr(homelab.jobs, "MAX_JOBS", 2)
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

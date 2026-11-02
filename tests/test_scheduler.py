import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_jobs import FakeExecutor, _inventory, _target

import fleetdeck.scheduler
from fleetdeck.app import create_app
from fleetdeck.config import AppConfig, NotifyConfig, ScheduleConfig
from fleetdeck.jobs import JobManager
from fleetdeck.models import ExecResult, Target
from fleetdeck.notify import Notifier
from fleetdeck.scheduler import Scheduler
from fleetdeck.updates import CHECK_SCRIPT, SECURITY_UPGRADE_SCRIPT


def test_schedule_durations():
    assert ScheduleConfig(check_every="6h").check_every == 6 * 3600
    assert ScheduleConfig(check_every="1d").check_every == 86400
    assert ScheduleConfig(check_every=900).check_every == 900
    assert ScheduleConfig().check_every is None
    for bad in ("1m", "soon", "6 hours"):
        with pytest.raises(ValidationError):
            ScheduleConfig(check_every=bad)


def test_notify_config_requires_fields():
    with pytest.raises(ValidationError, match="url"):
        NotifyConfig(type="ntfy")
    with pytest.raises(ValidationError, match="chat_id"):
        NotifyConfig(type="telegram", token="x")
    with pytest.raises(ValidationError, match="token"):
        NotifyConfig(type="gotify", url="https://gotify")
    NotifyConfig(type="telegram", token="x", chat_id="1")


async def test_notifier_payloads():
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request):
        sent.append(req)
        if "broken" in str(req.url):
            return httpx.Response(500, text="nope")
        return httpx.Response(200)

    targets = [
        NotifyConfig(type="ntfy", url="https://ntfy.sh/lab", token="tk"),
        NotifyConfig(type="gotify", url="https://gotify.lan/", token="app"),
        NotifyConfig(type="discord", url="https://discord.com/api/webhooks/1/x"),
        NotifyConfig(type="telegram", token="123:abc", chat_id="42"),
        NotifyConfig(type="webhook", url="https://hooks.lan/broken", name="mine"),
    ]
    n = Notifier(targets, "http://dash:8080", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    errors = await n.send("Homelab: 1 machine needs attention – pi4", "pi4: 3 updates")
    assert list(errors) == ["mine"] and "500" in errors["mine"]
    ntfy, gotify, discord, telegram, _ = sent
    assert ntfy.headers["Title"].startswith("Homelab: 1 machine") and ntfy.headers["Click"] == "http://dash:8080"
    assert ntfy.headers["Authorization"] == "Bearer tk" and ntfy.content == b"pi4: 3 updates"
    assert str(gotify.url) == "https://gotify.lan/message" and gotify.headers["X-Gotify-Key"] == "app"
    assert "pi4: 3 updates" in json.loads(discord.content)["content"]
    assert str(telegram.url) == "https://api.telegram.org/bot123:abc/sendMessage"
    assert json.loads(telegram.content)["chat_id"] == "42"


class PkgExecutor(FakeExecutor):
    def __init__(self, packages: list[str]):
        super().__init__()
        self.packages = packages

    async def run(self, script, on_line=None, timeout=0):
        assert script == CHECK_SCRIPT
        body = "\n".join(f"{p}/stable 2.0 amd64 [upgradable from: 1.0]" for p in self.packages)
        return ExecResult(rc=0, stdout=f"__PM__=apt\n__OS__=Debian\n__BEGIN__\n{body}\n__END__\n__REBOOT__=no\n")


class RecordingNotifier:
    targets = [object()]

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    async def send(self, title, body):
        self.messages.append((title, body))
        return {}


async def test_restart_keeps_the_schedule_and_does_not_repeat_a_notification(monkeypatch, tmp_path):
    import fleetdeck.scheduler
    from fleetdeck.store import Store

    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    store = Store(tmp_path / "state.db")
    inv = _inventory((_target("pi4"), PkgExecutor(["curl"])))
    inv.refresh = _noop_refresh
    notifier = RecordingNotifier()
    first = Scheduler(3600, inv, JobManager(inv), notifier, store)
    assert first._delay() == fleetdeck.scheduler.FIRST_RUN_DELAY  # never ran
    await first.run_once()
    assert len(notifier.messages) == 1

    again = Scheduler(3600, inv, JobManager(inv), notifier, store)  # the dashboard restarted
    assert again.last_run == first.last_run and again.last_notified == first.last_notified
    assert 3500 < again._delay() <= 3600  # an hour after the last round, not a minute after start-up
    await again.run_once()
    assert len(notifier.messages) == 1  # already told about the curl update

    again.last_run = datetime.now(UTC) - timedelta(hours=5)  # overdue: check soon after start
    assert again._delay() == fleetdeck.scheduler.FIRST_RUN_DELAY


async def test_notification_that_reached_nobody_is_tried_again(monkeypatch):
    import fleetdeck.scheduler

    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    inv = _inventory((_target("pi4"), PkgExecutor(["curl"])))
    inv.refresh = _noop_refresh

    class Down(RecordingNotifier):
        async def send(self, title, body):
            await super().send(title, body)
            return {"ntfy": "connection refused"}

    notifier = Down()
    sched = Scheduler(3600, inv, JobManager(inv), notifier)
    await sched.run_once()
    await sched.run_once()  # nobody heard about curl, so it is still new
    assert len(notifier.messages) == 2 and sched.last_notified is None
    notifier.send = RecordingNotifier.send.__get__(notifier)  # the target is back
    await sched.run_once()
    await sched.run_once()
    assert len(notifier.messages) == 3  # told once, then quiet


async def test_scheduled_run_notifies_only_about_new_things(monkeypatch):
    import fleetdeck.scheduler

    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    pi, vps = PkgExecutor(["curl"]), PkgExecutor([])
    inv = _inventory((_target("pi4"), pi), (_target("vps"), vps), (_target("off", updatable=False), FakeExecutor()))
    inv.refresh = _noop_refresh
    notifier = RecordingNotifier()
    sched = Scheduler(3600, inv, JobManager(inv), notifier)

    await sched.run_once()
    assert notifier.messages == [("Homelab: 1 machine needs attention", "pi4: 1 update")]
    assert sched.last_notified and sched.last_run

    await sched.run_once()  # nothing new
    assert len(notifier.messages) == 1

    pi.packages = []  # fixed...
    await sched.run_once()
    vps.packages = ["openssl"]
    await sched.run_once()
    assert len(notifier.messages) == 2
    assert notifier.messages[-1] == ("Homelab: 1 machine needs attention", "vps: 1 update")

    pi.packages = ["curl"]  # ...and back: worth telling again
    await sched.run_once()
    assert notifier.messages[-1][1] == "pi4: 1 update\nvps: 1 update"


async def _noop_refresh(force=False):
    return None


def test_schedule_api_and_test_notification(monkeypatch):
    cfg = AppConfig(demo=True, schedule=ScheduleConfig(check_every="6h"))
    with TestClient(create_app(cfg)) as client:
        sc = client.get("/api/schedule").json()
        assert sc["check_every"] == 6 * 3600 and sc["next_run"] and sc["notify_targets"] == 0
        assert client.post("/api/notify/test").status_code == 400

    async def fake_send(self, title, body):
        return {}

    monkeypatch.setattr(Notifier, "send", fake_send)
    cfg = AppConfig(demo=True, notify=[NotifyConfig(type="ntfy", url="https://ntfy.sh/x")])
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/schedule").json()["next_run"] is None  # no schedule set
        assert client.post("/api/notify/test").json() == {"sent": 1, "errors": {}}


async def test_hung_check_does_not_hold_back_the_report(monkeypatch):
    import asyncio

    import fleetdeck.scheduler

    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    monkeypatch.setattr(fleetdeck.scheduler, "RUN_TIMEOUT", 0)
    hung = FakeExecutor(gate=asyncio.Event())
    inv = _inventory((_target("pi4"), PkgExecutor(["curl"])), (_target("vps"), hung))
    inv.refresh = _noop_refresh
    notifier = RecordingNotifier()
    jobs = JobManager(inv)
    sched = Scheduler(3600, inv, jobs, notifier)
    await asyncio.wait_for(sched.run_once(), 5)
    ((title, body),) = notifier.messages
    assert "vps: check still running after 0 min" in body
    hung.gate.set()
    while jobs._tasks:
        await asyncio.gather(*jobs._tasks)


async def test_notification_tokens_stay_out_of_the_log(caplog):
    import logging

    from fleetdeck.__main__ import configure_logging

    configure_logging()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    n = Notifier([NotifyConfig(type="telegram", token="123:secret", chat_id="42")], client=client)
    with caplog.at_level(logging.INFO):
        await n.send("t", "b")
    assert "secret" not in caplog.text


async def test_scheduled_docker_check_is_opt_in_and_skips_machines_without_docker(monkeypatch):
    import fleetdeck.scheduler
    from fleetdeck.models import StacksResult

    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)

    async def run(**kw):
        inv = _inventory((_target("pi4"), PkgExecutor([])), (_target("vps"), PkgExecutor([])))
        inv.refresh = _noop_refresh
        jobs = JobManager(inv)
        jobs.stacks["vps"] = StacksResult(docker="none", checked_at=datetime.now(UTC))
        asked = []
        real = jobs.submit
        monkeypatch.setattr(
            jobs, "submit", lambda tid, action, *a, **k: asked.append((tid, action)) or real(tid, action, *a, **k)
        )
        await Scheduler(3600, inv, jobs, RecordingNotifier(), **kw).run_once()
        return sorted(asked)

    assert await run() == [("pi4", "check"), ("vps", "check")]
    assert await run(check_docker=True) == [("pi4", "check"), ("pi4", "stacks"), ("vps", "check")]


class SecExecutor(FakeExecutor):
    """Reports one security update until the security-only upgrade has run on it."""

    def __init__(self, name: str, calls: list[str], security: bool = True):
        super().__init__()
        self.name, self.calls, self.pending = name, calls, security

    async def run(self, script, on_line=None, timeout=0):
        if script == CHECK_SCRIPT:
            body = "openssl/stable-security 2.0 amd64 [upgradable from: 1.0]" if self.pending else ""
            return ExecResult(rc=0, stdout=f"__PM__=apt\n__OS__=Debian\n__BEGIN__\n{body}\n__END__\n__REBOOT__=no\n")
        from fleetdeck.updates import DOCKER_DETECT_SCRIPT

        if script == DOCKER_DETECT_SCRIPT:
            return ExecResult(rc=0, stdout="__DOCKER_INSTALLED__=no\n")
        self.calls.append(f"{self.name}:{'security' if script == SECURITY_UPGRADE_SCRIPT else 'full'}")
        await asyncio.sleep(0)
        self.pending = False
        return ExecResult(rc=0, stdout="__PM__=apt\n__REBOOT__=yes\n")


def _guest(tid: str, parent: str) -> Target:
    return Target(id=tid, name=tid, platform="proxmox", source="hosts", kind="lxc", parent=parent)


async def test_auto_security_updates_hosts_before_guests(monkeypatch):
    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    calls: list[str] = []
    # Guest listed first: the order comes from the scheduler, not the inventory.
    inv = _inventory(
        (_guest("ct1", "pve"), SecExecutor("ct1", calls)),
        (_guest("ct2", "pve"), SecExecutor("ct2", calls, security=False)),
        (_target("pve"), SecExecutor("pve", calls)),
    )
    inv.refresh = _noop_refresh
    notifier = RecordingNotifier()
    jobs = JobManager(inv)
    sched = Scheduler(3600, inv, jobs, notifier, auto_security=True)
    await sched.run_once()
    assert calls == ["pve:security", "ct1:security"]  # ct2 has nothing, and nothing ran the full upgrade
    assert all(j.security_only and j.reboot_if_required is False for j in jobs.jobs.values() if j.action == "upgrade")
    ((title, body),) = notifier.messages
    assert "pve: installed security updates" in body and "ct1: installed security updates" in body
    assert title == "Homelab: all up to date"


async def test_auto_security_updates_are_off_by_default(monkeypatch):
    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    assert ScheduleConfig().auto_security_updates is False
    calls: list[str] = []
    inv = _inventory((_target("pve"), SecExecutor("pve", calls)))
    inv.refresh = _noop_refresh
    await Scheduler(3600, inv, JobManager(inv), RecordingNotifier()).run_once()
    assert calls == []


async def test_failed_auto_security_update_is_reported(monkeypatch):
    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)

    class Failing(SecExecutor):
        async def run(self, script, on_line=None, timeout=0):
            if script == SECURITY_UPGRADE_SCRIPT:
                return ExecResult(rc=100, stdout="", stderr="E: dpkg was interrupted")
            return await super().run(script, on_line, timeout)

    inv = _inventory((_target("pve"), Failing("pve", [])))
    inv.refresh = _noop_refresh
    notifier = RecordingNotifier()
    await Scheduler(3600, inv, JobManager(inv), notifier, auto_security=True).run_once()
    ((_, body),) = notifier.messages
    assert "pve: security updates failed: upgrade exited with code 100" in body


async def test_auto_security_updates_skip_monitor_only_machines(monkeypatch):
    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    calls: list[str] = []
    inv = _inventory(
        (_target("pve"), SecExecutor("pve", calls)),
        (_guest("omv", "pve").model_copy(update={"monitor_only": True}), SecExecutor("omv", calls)),
        (_guest("ct1", "pve"), SecExecutor("ct1", calls)),
    )
    inv.refresh = _noop_refresh
    notifier = RecordingNotifier()
    jobs = JobManager(inv)
    jobs.set_monitor_only(["ct1"], True)
    await Scheduler(3600, inv, jobs, notifier, auto_security=True).run_once()
    assert calls == ["pve:security"]
    ((_, body),) = notifier.messages
    # Still reported, so their own updater can be run.
    assert "omv: 1 update" in body and "ct1: 1 update" in body


async def test_auto_security_updates_skip_machines_with_their_own_update_command(monkeypatch):
    monkeypatch.setattr(fleetdeck.scheduler, "POLL", 0)
    calls: list[str] = []
    inv = _inventory(
        (_target("nas", update_command="omv-upgrade"), SecExecutor("nas", calls)),
        (_target("pve"), SecExecutor("pve", calls)),
    )
    inv.refresh = _noop_refresh
    notifier = RecordingNotifier()
    sched = Scheduler(3600, inv, JobManager(inv), notifier, auto_security=True)
    await sched.run_once()
    assert calls == ["pve:security"]
    ((_, body),) = notifier.messages
    assert "nas: security updates not installed, it has its own update command" in body
    # Nothing new next time: the note alone doesn't send another message.
    await sched.run_once()
    assert len(notifier.messages) == 1

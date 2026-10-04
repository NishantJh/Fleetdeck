import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_jobs import FakeExecutor, _inventory, _target

from homelab.app import create_app
from homelab.config import AppConfig, NotifyConfig, ScheduleConfig
from homelab.jobs import JobManager
from homelab.models import ExecResult
from homelab.notify import Notifier
from homelab.scheduler import Scheduler
from homelab.updates import CHECK_SCRIPT


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


async def test_scheduled_run_notifies_only_about_new_things(monkeypatch):
    import homelab.scheduler

    monkeypatch.setattr(homelab.scheduler, "POLL", 0)
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

    import homelab.scheduler

    monkeypatch.setattr(homelab.scheduler, "POLL", 0)
    monkeypatch.setattr(homelab.scheduler, "RUN_TIMEOUT", 0)
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

    from homelab.__main__ import configure_logging

    configure_logging()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    n = Notifier([NotifyConfig(type="telegram", token="123:secret", chat_id="42")], client=client)
    with caplog.at_level(logging.INFO):
        await n.send("t", "b")
    assert "secret" not in caplog.text

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from fleetdeck import host_backups
from fleetdeck.config import HostBackupConfig, HostConfig
from fleetdeck.models import ExecResult
from fleetdeck.providers.ssh_hosts import SSHHostsProvider


class FakeExecutor:
    def __init__(self, result: ExecResult | None = None, local: bool = False):
        self.result, self.local, self.scripts = result, local, []

    async def run(self, script, on_line=None, timeout=0):
        self.scripts.append(script)
        if self.local:
            p = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
            return ExecResult(rc=p.returncode, stdout=p.stdout, stderr=p.stderr)
        return self.result


def test_config_needs_a_place():
    with pytest.raises(ValueError):
        HostBackupConfig(type="path")
    with pytest.raises(ValueError):
        HostBackupConfig(type="restic")
    HostBackupConfig(type="borg", env_file="/root/borg.env")
    for bad in ({"type": "path", "path": "backups"}, {"type": "path", "path": "-delete"}):
        with pytest.raises(ValueError, match="full path"):
            HostBackupConfig(**bad)
    with pytest.raises(ValueError, match="full path"):
        HostBackupConfig(type="restic", repository="/r", env_file="restic.env")


async def test_symlinked_folder_is_followed(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.tar").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    info = await host_backups.read(HostBackupConfig(type="path", path=str(tmp_path / "link")), FakeExecutor(local=True))
    assert info.count == 1 and not info.stale


async def test_repository_checks_never_wait_for_input():
    for cfg in (HostBackupConfig(type="restic", repository="/r"), HostBackupConfig(type="borg", repository="/r")):
        s = host_backups.script(cfg)
        assert s.startswith("exec </dev/null;") and "BORG_RELOCATED_REPO_ACCESS_IS_OK=no" in s
    # A prompt reading stdin gets end-of-file at once instead of waiting.
    r = await FakeExecutor(local=True).run(host_backups.NO_PROMPTS + "read answer; echo $?")
    assert r.stdout.strip() == "1"


async def test_timeout_says_so():
    class Hangs:
        async def run(self, script, on_line=None, timeout=0):
            raise TimeoutError

    info = await host_backups.read(HostBackupConfig(type="restic", repository="/r"), Hangs())
    assert info.note == "could not read restic: no answer after 60 s"


async def test_path_reads_newest_file(tmp_path):
    (tmp_path / "a").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "my file").write_text("y")
    info = await host_backups.read(HostBackupConfig(type="path", path=str(tmp_path)), FakeExecutor(local=True))
    assert info.count == 2 and not info.stale and info.note is None
    assert datetime.now(UTC) - info.last < timedelta(minutes=5)


async def test_path_missing_and_empty(tmp_path):
    missing = await host_backups.read(HostBackupConfig(type="path", path=str(tmp_path / "x")), FakeExecutor(local=True))
    assert "not found" in missing.note and missing.last is None
    empty = await host_backups.read(HostBackupConfig(type="path", path=str(tmp_path)), FakeExecutor(local=True))
    assert empty.last is None and empty.stale and empty.count == 0


async def test_restic():
    now = datetime.now(UTC)
    snaps = [{"time": "2020-01-01T00:00:00.123456789+02:00"}, {"time": (now - timedelta(hours=3)).isoformat()}]
    ex = FakeExecutor(ExecResult(rc=0, stdout=json.dumps(snaps)))
    cfg = HostBackupConfig(type="restic", repository="sftp:nas:/restic", env_file="/root/restic.env")
    info = await host_backups.read(cfg, ex)
    assert info.count == 2 and not info.stale and info.storage == "restic"
    assert "--no-lock" in ex.scripts[0] and ". /root/restic.env" in ex.scripts[0]


async def test_borg_local_time_and_missing_binary():
    out = "+0200\n" + json.dumps({"archives": [{"time": "2020-01-01T12:00:00.000000"}]})
    info = await host_backups.read(
        HostBackupConfig(type="borg", repository="/b"), FakeExecutor(ExecResult(rc=0, stdout=out))
    )
    assert info.last == datetime(2020, 1, 1, 10, tzinfo=UTC) and info.stale
    gone = await host_backups.read(HostBackupConfig(type="borg", repository="/b"), FakeExecutor(ExecResult(rc=127)))
    assert "not installed" in gone.note


async def test_hosts_without_backup_say_not_set_up(tmp_path):
    hosts = [
        HostConfig(name="pi", host="pi", insecure_skip_host_key_check=True),
        HostConfig(
            name="nas", host="nas", insecure_skip_host_key_check=True, backup={"type": "path", "path": str(tmp_path)}
        ),
    ]
    p = SSHHostsProvider(hosts)
    calls = []

    async def fake_get(name, cfg, ex):
        calls.append(name)
        return await host_backups.read(cfg, FakeExecutor(local=True))

    p.backups.get = fake_get
    found = {t.name: t.backup for t, _ in await p.discover()}
    assert found["pi"].not_set_up and "config.yaml" in found["pi"].note
    assert calls == ["nas"] and found["nas"].count == 0


async def test_slow_host_does_not_hold_up_discovery(monkeypatch):
    import asyncio

    monkeypatch.setattr(host_backups, "FIRST_WAIT", 0.01)
    gate = asyncio.Event()

    class Slow:
        async def run(self, script, on_line=None, timeout=0):
            await gate.wait()
            return ExecResult(rc=0, stdout="1 1700000000.5\n")

    cache, cfg = host_backups.HostBackups(), HostBackupConfig(type="path", path="/b")
    assert (await cache.get("pi", cfg, Slow())).note == "still checking"
    gate.set()
    await asyncio.sleep(0.01)
    assert (await cache.get("pi", cfg, Slow())).count == 1

import json
import os
import stat
import subprocess

import httpx
import pytest

from homelab import backup_runs
from homelab.backup_runs import IncusHostBackup, PveHostBackup, SshHostBackup, config_script
from homelab.config import HostBackupConfig, IncusConfig, ProxmoxConfig
from homelab.inventory import Inventory
from homelab.jobs import JobManager
from homelab.models import ExecResult, Target


class LocalExecutor:
    """Runs the script on this machine, the way SSH would on the real one."""

    def __init__(self, env=None):
        self.env, self.scripts = env, []

    async def run(self, script, on_line=None, timeout=0):
        self.scripts.append(script)
        p = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env={**os.environ, **(self.env or {})})
        for line in p.stdout.splitlines():
            on_line and on_line("stdout", line)
        for line in p.stderr.splitlines():
            on_line and on_line("stderr", line)
        return ExecResult(rc=p.returncode, stdout=p.stdout, stderr=p.stderr)


def lines():
    out = []
    return out, lambda stream, line: out.append(line if stream == "stdout" else f"! {line}")


@pytest.fixture
def tree(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "hosts").write_text("127.0.0.1 x")
    return tmp_path, etc


def script(cfg, tmp, etc, **kw):
    return config_script(cfg, paths=(str(etc),), local_dir=str(tmp / "out"), **kw)


async def test_plain_host_archives_config_and_keeps_the_newest_seven(tree):
    tmp, etc = tree
    out = tmp / "out"
    out.mkdir()
    for i in range(9):
        (out / f"homelab-config-old-{i}.tar.gz").write_text("x")
        os.utime(out / f"homelab-config-old-{i}.tar.gz", (1000 + i, 1000 + i))
    (out / "mine.txt").write_text("not ours")
    got, cb = lines()
    r = await LocalExecutor().run(script(None, tmp, etc), cb)
    assert r.rc == 0, got
    archives = sorted(out.glob("homelab-config-*.tar.gz"))
    assert len(archives) == 7 and (out / "mine.txt").exists()
    new = next(a for a in archives if "old" not in a.name)
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    names = subprocess.run(["tar", "-tzf", new], capture_output=True, text=True).stdout
    assert "hosts" in names and "packages.txt" in names
    assert not any("Removing leading" in line for line in got)


async def test_backup_folder_is_used_when_the_machine_has_one(tree):
    tmp, etc = tree
    dest = tmp / "nas"
    dest.mkdir()
    r = await LocalExecutor().run(script(HostBackupConfig(type="path", path=str(dest)), tmp, etc))
    assert r.rc == 0 and len(list(dest.glob("homelab-config-*.tar.gz"))) == 1
    assert not (tmp / "out").exists()


async def test_backup_path_that_is_a_file_falls_back_and_says_so(tree):
    tmp, etc = tree
    f = tmp / "latest.tar"
    f.write_text("x")
    r = await LocalExecutor().run(script(HostBackupConfig(type="path", path=str(f)), tmp, etc))
    assert r.rc == 0 and "is not a folder" in r.stderr and len(list((tmp / "out").glob("*.tar.gz"))) == 1
    assert f.read_text() == "x"


async def test_extra_paths_with_spaces_are_included(tree):
    tmp, etc = tree
    extra = tmp / "my apps"
    extra.mkdir()
    (extra / "compose.yml").write_text("x")
    cfg = HostBackupConfig(type="path", path=str(tmp / "nas"), include=[str(extra)])
    (tmp / "nas").mkdir()
    r = await LocalExecutor().run(script(cfg, tmp, etc))
    assert r.rc == 0
    names = subprocess.run(["tar", "-tzf", next((tmp / "nas").glob("*.tar.gz"))], capture_output=True, text=True).stdout
    assert "compose.yml" in names


def test_restic_and_borg_get_the_config_and_never_prompt():
    r = config_script(HostBackupConfig(type="restic", repository="/mnt/r", env_file="/root/r.env"))
    assert r.startswith("exec </dev/null;") and "restic -r /mnt/r backup --tag homelab-manager" in r
    assert ". /root/r.env" in r
    b = config_script(HostBackupConfig(type="borg", repository="/mnt/b"))
    assert "BORG_RELOCATED_REPO_ACCESS_IS_OK=no" in b and "borg create --stats /mnt/b::homelab-config-" in b


def pbs_env(tmp_path, *, with_client=True, extra_cfg=""):
    (tmp_path / "priv").mkdir()
    (tmp_path / "priv" / "pbs1.pw").write_text("s3cret\n")
    (tmp_path / "priv" / "pbs1.enc").write_text("{}")
    (tmp_path / "storage.cfg").write_text(
        "dir: local\n\tpath /var/lib/vz\n\tcontent iso,backup\n\n"
        "pbs: off\n\tserver 10.0.0.9\n\tdatastore nope\n\tusername a@pbs\n\tdisable\n\n"
        "pbs: pbs1\n\tserver 10.0.0.5\n\tdatastore store1\n\tusername root@pam\n\tfingerprint AA:BB\n"
        f"\tcontent backup\n{extra_cfg}\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if with_client:
        client = bin_dir / "proxmox-backup-client"
        client.write_text(
            '#!/bin/sh\necho "args: $*"\necho "repo=$PBS_REPOSITORY pw=$PBS_PASSWORD fp=$PBS_FINGERPRINT"\n'
        )
        client.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PVE_STORAGE_CFG": str(tmp_path / "storage.cfg"),
        "PVE_PRIV_DIR": str(tmp_path / "priv"),
    }


async def test_proxmox_node_config_goes_to_the_pbs_storage_with_its_own_credentials(tree):
    tmp, etc = tree
    got, cb = lines()
    r = await LocalExecutor(pbs_env(tmp)).run(script(None, tmp, etc, proxmox=True), cb)
    assert r.rc == 0, got
    out = r.stdout
    # The enabled PBS storage, not the disabled one.
    assert "repo=root@pam@10.0.0.5:8007:store1 pw=s3cret fp=AA:BB" in out
    assert "etc.pxar:/etc --include-dev /etc/pve --backup-type host --backup-id" in out
    assert f"--keyfile {tmp}/priv/pbs1.enc" in out
    assert not (tmp / "out").exists()


async def test_proxmox_node_without_pbs_falls_back_to_a_file(tree):
    tmp, etc = tree
    env = pbs_env(tmp, with_client=False)
    r = await LocalExecutor(env).run(script(None, tmp, etc, proxmox=True))
    assert r.rc == 0 and "not installed" in r.stderr and len(list((tmp / "out").glob("*.tar.gz"))) == 1
    # With the client but no PBS storage in storage.cfg.
    (tmp / "bin" / "proxmox-backup-client").write_text("#!/bin/sh\nexit 9\n")
    (tmp / "bin" / "proxmox-backup-client").chmod(0o755)
    (tmp / "storage.cfg").write_text("dir: local\n\tpath /var/lib/vz\n")
    r = await LocalExecutor(env).run(script(None, tmp, etc, proxmox=True))
    assert r.rc == 0 and "no Proxmox Backup Server storage" in r.stderr


async def test_failing_pbs_push_is_not_hidden_by_the_fallback(tree):
    tmp, etc = tree
    env = pbs_env(tmp)
    (tmp / "bin" / "proxmox-backup-client").write_text("#!/bin/sh\necho 'connection refused' >&2\nexit 1\n")
    r = await LocalExecutor(env).run(script(None, tmp, etc, proxmox=True))
    # The archive is still made, and the reason the push failed is in the log.
    assert r.rc == 0 and "connection refused" in r.stderr and len(list((tmp / "out").glob("*.tar.gz"))) == 1


async def test_ssh_host_run_reports_a_missing_tool_and_a_failed_script():
    class Fixed:
        def __init__(self, rc):
            self.rc = rc

        async def run(self, script, on_line=None, timeout=0):
            return ExecResult(rc=self.rc)

    got, cb = lines()
    with pytest.raises(RuntimeError, match="restic is not installed"):
        await SshHostBackup(Fixed(127), HostBackupConfig(type="restic", repository="/r")).run(cb)
    with pytest.raises(RuntimeError, match="exited with code 4"):
        await SshHostBackup(Fixed(4), None).run(cb)
    await SshHostBackup(Fixed(0), None).run(cb)


# ---- Proxmox ----

PVE_DATA = {
    "/nodes/pve/lxc": [{"vmid": "101"}, {"vmid": "199"}],
    "/nodes/pve/qemu": [{"vmid": 200}, {"vmid": 9000, "template": 1}],
    "/nodes/pve/storage": [
        {"storage": "local", "type": "dir", "active": 1},
        {"storage": "off", "type": "pbs", "active": 0},
        {"storage": "pbs", "type": "pbs", "active": 1},
    ],
    "/nodes/pve/tasks/UPID:1/status": {"status": "stopped", "exitstatus": "OK"},
}


def pve(handler_extra=None, cfg=None, ssh=None, **kw):
    posted = []

    def handler(req):
        path = req.url.path.removeprefix("/api2/json")
        if req.method == "POST":
            posted.append((path, dict(httpx.QueryParams(req.content.decode()))))
            return handler_extra(req) if handler_extra else httpx.Response(200, json={"data": "UPID:1"})
        if path.endswith("/log"):
            start = int(req.url.params["start"])
            return httpx.Response(200, json={"data": [{"n": 1, "t": "INFO: Finished"}][start:]})
        return httpx.Response(200, json={"data": PVE_DATA[path]})

    cfg = cfg or ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x", exclude=[199])
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pve:8006/api2/json")
    return PveHostBackup(client, "pve", cfg, ssh, poll=0, **kw), posted


async def test_proxmox_backs_up_the_guests_to_pbs_with_vzdump():
    run, posted = pve()
    got, cb = lines()
    await run.run(cb)
    ((path, body),) = posted
    assert path == "/nodes/pve/vzdump"
    # Excluded guests and templates are left out; the active PBS storage is chosen over "local".
    assert body == {"vmid": "101,200", "storage": "pbs", "mode": "snapshot", "compress": "zstd"}
    assert "INFO: Finished" in got and any("add ssh settings" in line for line in got)


async def test_proxmox_configured_storage_wins_and_guests_can_be_left_out():
    cfg = ProxmoxConfig(name="lab", api_url="https://pve:8006", token_id="t", token_secret="x", backup_storage="local")
    run, posted = pve(cfg=cfg)
    await run.run(lines()[1])
    assert posted[0][1]["storage"] == "local"
    cfg = cfg.model_copy(update={"backup_guests": False})
    run, posted = pve(cfg=cfg)
    await run.run(lines()[1])
    assert posted == []


async def test_proxmox_failed_vzdump_still_backs_up_the_config_then_fails():
    class Ssh:
        ran = False

        async def run(self, script, on_line=None, timeout=0):
            Ssh.ran = True
            return ExecResult(rc=0)

    run, _ = pve(ssh=Ssh())
    PVE_DATA["/nodes/pve/tasks/UPID:1/status"] = {"status": "stopped", "exitstatus": "job errors"}
    try:
        with pytest.raises(RuntimeError, match="job errors"):
            await run.run(lines()[1])
    finally:
        PVE_DATA["/nodes/pve/tasks/UPID:1/status"] = {"status": "stopped", "exitstatus": "OK"}
    assert Ssh.ran


async def test_proxmox_permission_error_says_what_to_do():
    run, _ = pve(lambda req: httpx.Response(403, text="Permission check failed (VM.Backup)"))
    with pytest.raises(RuntimeError, match="Connect on this machine again"):
        await run.run(lines()[1])


# ---- Incus ----

GB = 2**30


def incus(tmp_path, instances, cfg=None):
    calls = []

    def op(url):
        return httpx.Response(202, json={"operation": f"/1.0/operations/{len(calls)}"})

    def handler(req):
        path = req.url.path
        calls.append((req.method, path))
        if path.endswith("/wait"):
            return httpx.Response(200, json={"metadata": {"status": "Success"}})
        if req.method == "POST" or req.method == "DELETE":
            return op(path)
        if path == "/1.0":
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "config": {"core.https_address": ":8443"},
                        "environment": {"server_version": "6.0", "kernel": "x"},
                    }
                },
            )
        if path == "/1.0/projects":
            return httpx.Response(200, json={"metadata": [{"name": "default"}]})
        if path == "/1.0/profiles":
            return httpx.Response(200, json={"metadata": [{"name": "default"}]})
        if path == "/1.0/networks":
            return httpx.Response(403, text="nope")
        if path == "/1.0/storage-pools":
            return httpx.Response(200, json={"metadata": [{"name": "default", "driver": "dir"}]})
        if path == "/1.0/instances":
            return httpx.Response(200, json={"metadata": instances})
        if path.endswith("/backups"):
            return httpx.Response(
                200,
                json={
                    "metadata": [
                        f"/1.0/instances/web/backups/{n}"
                        for n in ("homelab_1", "homelab_2", "homelab_3", "homelab_4", "mine")
                    ]
                },
            )
        raise AssertionError(path)

    cfg = cfg or IncusConfig(name="inc", socket="/x")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    return IncusHostBackup(client, cfg, tmp_path / "dumps", op_timeout=1), calls


async def test_incus_saves_settings_and_exports_instances_keeping_the_newest(tmp_path):
    inst = [
        {"name": "web", "project": "default", "state": {"disk": {"root": {"usage": GB}}}},
        {"name": "huge", "project": "default", "state": {"disk": {"root": {"usage": 50 * GB}}}},
    ]
    run, calls = incus(tmp_path, inst)
    got, cb = lines()
    await run.run(cb)
    (dump,) = (tmp_path / "dumps").glob("incus-inc-*.json")
    data = json.loads(dump.read_text())
    assert data["server"]["config"] == {"core.https_address": ":8443"}
    assert "kernel" not in data["server"]["environment"]
    assert data["projects"]["default"]["profiles"] == [{"name": "default"}]
    assert "error" in data["projects"]["default"]["networks"]
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    exports = [c for c in calls if c == ("POST", "/1.0/instances/web/backups")]
    assert len(exports) == 1 and not any("huge" in p for _, p in calls)
    assert any("huge left out" in line for line in got)
    # 4 of ours listed, 3 kept: the oldest goes; the one not made here is never touched.
    deletes = [p for m, p in calls if m == "DELETE"]
    assert deletes == ["/1.0/instances/web/backups/homelab_1"]


async def test_incus_one_failed_instance_does_not_stop_the_others(tmp_path, monkeypatch):
    run, calls = incus(tmp_path, [{"name": "a"}, {"name": "web"}])
    orig = run.client._transport.handle_async_request

    async def flaky(req):
        if req.method == "POST" and req.url.path == "/1.0/instances/a/backups":
            return httpx.Response(500, text="disk full")
        return await orig(req)

    monkeypatch.setattr(run.client._transport, "handle_async_request", flaky)
    with pytest.raises(RuntimeError, match="could not export a"):
        await run.run(lines()[1])
    assert any(p == "/1.0/instances/web/backups" for m, p in calls if m == "GET")


async def test_incus_without_a_data_folder_still_exports(tmp_path):
    run, _ = incus(tmp_path, [{"name": "web"}])
    run.dump_dir = None
    with pytest.raises(RuntimeError, match="no state_file"):
        await run.run(lines()[1])


async def test_incus_exports_can_be_turned_off(tmp_path):
    run, calls = incus(tmp_path, [{"name": "web"}], IncusConfig(name="inc", socket="/x", backup_max_instance_gb=0))
    await run.run(lines()[1])
    assert not any(p.endswith("/backups") for _, p in calls)


# ---- through the job manager ----


class Runs:
    def __init__(self, fail=False):
        self.fail = fail

    async def run(self, on_line):
        on_line("stdout", "step one")
        if self.fail:
            raise RuntimeError("step two failed")


def manager(run, can_backup=True):
    inv = Inventory([])
    t = Target(id="h", name="h", platform="ssh", source="hosts", kind="host", can_backup=can_backup)
    inv.targets = {"h": (t, None)}
    inv.backup_runs = {"h": run} if run else {}
    return JobManager(inv)


async def finish(jobs):
    while any(j.status in ("queued", "running") for j in jobs.jobs.values()):
        await __import__("asyncio").sleep(0.01)


async def test_backup_job_runs_without_an_executor_and_logs_the_steps():
    jobs = manager(Runs())
    job = jobs.submit("h", "backup")
    await finish(jobs)
    assert job.status == "succeeded" and job.log == ["== Backing up h", "step one", "== Backup finished"]


async def test_backup_job_fails_with_the_reason():
    jobs = manager(Runs(fail=True))
    job = jobs.submit("h", "backup")
    await finish(jobs)
    assert job.status == "failed" and job.error == "step two failed"


async def test_backup_is_refused_where_there_is_none():
    with pytest.raises(ValueError, match="cannot be backed up"):
        manager(None).submit("h", "backup")
    with pytest.raises(ValueError, match="cannot be backed up"):
        manager(Runs(), can_backup=False).submit("h", "backup")


def test_module_names_only_its_own_backups_for_pruning():
    assert backup_runs.PREFIX == "homelab_" and backup_runs.FILE_PREFIX.startswith("homelab-config-")

import json
import os
import stat
import subprocess
import tarfile

import httpx
import pytest

from fleetdeck import backup_runs
from fleetdeck.backup_runs import DASHBOARD, IncusHostBackup, SshHostBackup, check_destination, config_script
from fleetdeck.config import HostBackupConfig, IncusConfig
from fleetdeck.inventory import Inventory
from fleetdeck.jobs import JobManager
from fleetdeck.models import ExecResult, Target
from fleetdeck.store import Store


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

    async def run_to_file(self, script, out, on_line=None, timeout=0):
        self.scripts.append(script)
        p = subprocess.run(["sh", "-c", script], capture_output=True, env={**os.environ, **(self.env or {})})
        out.write(p.stdout)
        for line in p.stderr.decode().splitlines():
            on_line and on_line("stderr", line)
        return p.returncode


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
    host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    for i in range(9):
        (out / f"fleetdeck-config-{host}-2020010{i + 1}_000000.tar.gz").write_text("x")
        os.utime(out / f"fleetdeck-config-{host}-2020010{i + 1}_000000.tar.gz", (1000 + i, 1000 + i))
    (out / "mine.txt").write_text("not ours")
    got, cb = lines()
    r = await LocalExecutor().run(script(None, tmp, etc), cb)
    assert r.rc == 0, got
    archives = sorted(out.glob("fleetdeck-config-*.tar.gz"))
    assert len(archives) == 7 and (out / "mine.txt").exists()
    new = next(a for a in archives if "_000000" not in a.name)
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    names = subprocess.run(["tar", "-tzf", new], capture_output=True, text=True).stdout
    assert "hosts" in names and "packages.txt" in names
    assert not any("Removing leading" in line for line in got)


async def test_pruning_leaves_other_hosts_archives_in_a_shared_folder(tree):
    tmp, etc = tree
    out = tmp / "out"
    out.mkdir()
    host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    others = [f"fleetdeck-config-nas-2020010{i}_000000.tar.gz" for i in range(1, 9)]
    others.append(f"fleetdeck-config-{host}-2-20200101_000000.tar.gz")  # a host named like ours plus a suffix
    for i, name in enumerate(others):
        (out / name).write_text("x")
        os.utime(out / name, (1000 + i, 1000 + i))
    got, cb = lines()
    r = await LocalExecutor().run(script(None, tmp, etc), cb)
    assert r.rc == 0, got
    assert all((out / n).exists() for n in others)
    assert len(list(out.glob("fleetdeck-config-*.tar.gz"))) == len(others) + 1


async def test_backup_folder_is_used_when_the_machine_has_one(tree):
    tmp, etc = tree
    dest = tmp / "nas"
    dest.mkdir()
    r = await LocalExecutor().run(script(HostBackupConfig(type="path", path=str(dest)), tmp, etc))
    assert r.rc == 0 and len(list(dest.glob("fleetdeck-config-*.tar.gz"))) == 1
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
    assert "BORG_RELOCATED_REPO_ACCESS_IS_OK=no" in b and "borg create --stats /mnt/b::fleetdeck-config-" in b


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
    # A named folder that is not on the machine says where it is looked up.
    with pytest.raises(RuntimeError, match="/app/backups is missing or not writable on the machine itself"):
        await SshHostBackup(Fixed(5), None).run(cb, "/app/backups")


# ---- Incus ----


def incus(tmp_path, cfg=None):
    calls = []

    def handler(req):
        path = req.url.path
        calls.append((req.method, path))
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
        raise AssertionError(path)

    cfg = cfg or IncusConfig(name="inc", socket="/x")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    return IncusHostBackup(client, cfg, tmp_path / "dumps"), calls


async def test_incus_saves_only_the_host_settings(tmp_path):
    run, calls = incus(tmp_path)
    await run.run(lines()[1])
    (dump,) = (tmp_path / "dumps").glob("incus-inc-*.json")
    data = json.loads(dump.read_text())
    assert data["server"]["config"] == {"core.https_address": ":8443"}
    assert "kernel" not in data["server"]["environment"]
    assert data["projects"]["default"]["profiles"] == [{"name": "default"}]
    assert "error" in data["projects"]["default"]["networks"]
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    # Never instances: nothing but settings is read, and nothing is written to Incus.
    assert not any("instances" in p for _, p in calls) and all(m == "GET" for m, _ in calls)


async def test_incus_keeps_the_newest_seven_settings_files(tmp_path):
    run, _ = incus(tmp_path)
    (tmp_path / "dumps").mkdir()
    for i in range(9):
        (tmp_path / "dumps" / f"incus-inc-2020010{i}_000000.json").write_text("{}")
    other = tmp_path / "dumps" / "incus-other-2020.json"
    other.write_text("{}")
    await run.run(lines()[1])
    assert len(list((tmp_path / "dumps").glob("incus-inc-*.json"))) == 7 and other.exists()


async def test_incus_backs_up_to_a_folder_the_user_names(tmp_path):
    run, _ = incus(tmp_path)
    share = tmp_path / "nas"
    share.mkdir()
    await run.run(lines()[1], str(share))
    assert len(list(share.glob("incus-inc-*.json"))) == 1 and not (tmp_path / "dumps").exists()
    with pytest.raises(RuntimeError, match="is the share mounted"):
        await run.run(lines()[1], str(tmp_path / "missing"))
    assert not (tmp_path / "missing").exists()


async def test_incus_without_a_data_folder_says_so(tmp_path):
    run, _ = incus(tmp_path)
    run.dump_dir = None
    with pytest.raises(RuntimeError, match="no state_file"):
        await run.run(lines()[1])


# ---- through the job manager ----


def _steps(job):
    """The job log without the note that the archive is not encrypted (no age key set in these tests)."""
    return [line for line in job.log if not line.startswith("! The archive is not encrypted")]


class Runs:
    def __init__(self, fail=False):
        self.fail = fail

    async def run(self, on_line, destination=None, dashboard_folder=None, age_recipient=None, password=None):
        self.folder = dashboard_folder
        on_line("stdout", f"step one {destination or ''}".strip())
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
    assert job.status == "succeeded" and _steps(job) == ["== Backing up h", "step one", "== Backup finished"]


async def test_backup_job_passes_the_folder_on():
    jobs = manager(Runs())
    job = jobs.submit("h", "backup", destination="/mnt/nas")
    await finish(jobs)
    assert _steps(job) == ["== Backing up h to /mnt/nas", "step one /mnt/nas", "== Backup finished"]


def test_backup_place_is_remembered_per_host_and_survives_a_restart(tmp_path):
    store = Store(tmp_path / "s.db")
    jobs = JobManager(Inventory([]), store=store)
    jobs.set_backup_place("a", "/mnt/a")
    jobs.set_backup_place("b", "/mnt/b")
    jobs.set_backup_place("b", None)
    assert JobManager(Inventory([]), store=Store(tmp_path / "s.db")).backup_places == {"a": "/mnt/a"}


def test_destination_must_be_a_plain_full_path():
    assert check_destination(" /mnt/nas/backups ") == "/mnt/nas/backups"
    for bad in ("backups", "/mnt/../etc", "/mnt/a\nb", "/" + "x" * 1030, ""):
        with pytest.raises(ValueError):
            check_destination(bad)


async def test_destination_script_refuses_a_missing_folder_and_quotes_it(tree):
    tmp, etc = tree
    r = await LocalExecutor().run(script(None, tmp, etc, destination=str(tmp / "nas")))
    assert r.rc == 5 and "is the share mounted" in r.stderr and not (tmp / "nas").exists()
    odd = tmp / "my $(touch pwned) share"
    odd.mkdir()
    r = await LocalExecutor().run(script(None, tmp, etc, destination=str(odd)))
    assert r.rc == 0 and len(list(odd.glob("fleetdeck-config-*.tar.gz"))) == 1 and not (tmp / "pwned").exists()
    assert not (tmp / "out").exists()
    # A folder asked for in this run also beats the PBS storage on a Proxmox node.
    env = pbs_env(tmp)
    got = await LocalExecutor(env).run(script(None, tmp, etc, proxmox=True, destination=str(odd)))
    assert got.rc == 0 and "repo=" not in got.stdout


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
    assert backup_runs.FILE_PREFIX.startswith("fleetdeck-config-")


# ---- save on the dashboard: the archive is streamed back over SSH ----


class FakeSSH(LocalExecutor):
    class cfg:  # noqa: N801 - stands in for SSHConfig
        host = "10.0.0.5"


async def run_stream(tmp, etc, folder, name="pi-4", ssh=None):
    run = SshHostBackup(ssh or FakeSSH(), None, name=name)
    got, cb = lines()
    old = backup_runs.DEFAULT_PATHS
    backup_runs.DEFAULT_PATHS = (str(etc),)
    try:
        await run.run(cb, DASHBOARD, folder)
    finally:
        backup_runs.DEFAULT_PATHS = old
    return got


async def test_save_on_dashboard_streams_a_valid_archive_into_the_machine_folder(tree):
    tmp, etc = tree
    folder = tmp / "backups"
    folder.mkdir()
    got = await run_stream(tmp, etc, folder, "my pi/4")
    files = list((folder / "fleetdeck" / "my_pi_4").glob("fleetdeck-config-*.tar.gz"))
    assert len(files) == 1 and f"wrote {files[0]}" in got[-1]
    assert not list((folder / "fleetdeck" / "my_pi_4").glob(".*.part")) and not (tmp / "out").exists()
    with tarfile.open(files[0]) as t:
        assert any(n.endswith("hosts") for n in t.getnames()) and any(n.endswith("packages.txt") for n in t.getnames())
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    # Only its own folder is added to the mount; whatever else is there is not touched.
    assert [p.name for p in folder.iterdir()] == ["fleetdeck"]


async def test_save_on_dashboard_keeps_the_newest_seven_and_only_ours(tree):
    tmp, etc = tree
    sub = tmp / "backups" / "fleetdeck" / "pi"
    sub.mkdir(parents=True)
    for i in range(9):
        (sub / f"fleetdeck-config-2020010{i}_000000.tar.gz").write_text("x")
    (sub / "mine.tar.gz").write_text("not ours")
    await run_stream(tmp, etc, tmp / "backups", "pi")
    assert len(list(sub.glob("fleetdeck-config-*.tar.gz"))) == 7 and (sub / "mine.tar.gz").exists()


async def test_save_on_dashboard_needs_the_folder_to_be_there_and_writable(tree):
    tmp, etc = tree
    with pytest.raises(RuntimeError, match="does not exist in the dashboard's container"):
        await run_stream(tmp, etc, tmp / "nas")
    assert not (tmp / "nas").exists()
    with pytest.raises(RuntimeError, match="no backup folder"):
        await run_stream(tmp, etc, None)
    if os.geteuid() != 0:
        ro = tmp / "ro"
        ro.mkdir(mode=0o500)
        with pytest.raises(RuntimeError, match="can't write"):
            await run_stream(tmp, etc, ro)


async def test_a_failed_stream_leaves_no_partial_file(tree):
    tmp, etc = tree
    folder = tmp / "backups"
    folder.mkdir()

    class Cut(FakeSSH):
        async def run_to_file(self, script, out, on_line=None, timeout=0):
            out.write(b"\x1f\x8b half an archive")
            return 4

    with pytest.raises(RuntimeError, match="exited with code 4"):
        await run_stream(tmp, etc, folder, ssh=Cut())
    assert list((folder / "fleetdeck" / "pi-4").iterdir()) == []

    class Truncated(FakeSSH):
        async def run_to_file(self, script, out, on_line=None, timeout=0):
            out.write(b"not gzip at all")
            return 0

    with pytest.raises(RuntimeError):
        await run_stream(tmp, etc, folder, ssh=Truncated())
    assert list((folder / "fleetdeck" / "pi-4").iterdir()) == []


def test_stream_script_writes_nothing_on_the_machine_and_keeps_stdout_clean(tree):
    tmp, etc = tree
    s = script(None, tmp, etc, stream=True)
    p = subprocess.run(["sh", "-c", s], capture_output=True)
    assert p.returncode == 0 and p.stdout[:2] == b"\x1f\x8b" and not (tmp / "out").exists()


async def test_incus_saves_settings_and_etc_into_the_machine_folder(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    inc, _ = incus(tmp_path, IncusConfig(name="skulls", socket="/x"))
    got, cb = lines()
    host = IncusHostBackup(inc.client, inc.cfg, tmp_path / "data", FakeSSH())
    old = backup_runs.DEFAULT_PATHS
    backup_runs.DEFAULT_PATHS = (str(tmp_path),)
    try:
        await host.run(cb, DASHBOARD, folder)
    finally:
        backup_runs.DEFAULT_PATHS = old
    assert len(list((folder / "fleetdeck" / "skulls").glob("incus-settings-*.json"))) == 1
    assert len(list((folder / "fleetdeck" / "skulls").glob("fleetdeck-config-*.tar.gz"))) == 1
    assert not (tmp_path / "data").exists()


def test_dashboard_is_a_valid_destination_and_the_job_gets_the_folder(tmp_path):
    assert check_destination(" dashboard ") == "dashboard"


async def test_backup_job_hands_the_dashboard_folder_to_the_run(tmp_path):
    runs = Runs()
    inv = Inventory([])
    t = Target(id="h", name="h", platform="ssh", source="hosts", kind="host", can_backup=True)
    inv.targets, inv.backup_runs = {"h": (t, None)}, {"h": runs}
    jobs = JobManager(inv, backup_folder=tmp_path)
    job = jobs.submit("h", "backup", destination="dashboard")
    await finish(jobs)
    assert job.status == "succeeded" and runs.folder == tmp_path and job.log[0] == f"== Backing up h to {tmp_path}/"


KEY = "age1" + "q" * 58


def _fake_age(tmp_path):
    """An `age` that marks its output, so the tests don't need the real program."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "age"
    exe.write_text('#!/bin/sh\nprintf "AGE-ENCRYPTED\n" > "$4"\ncat "$5" >> "$4"\n')
    exe.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def test_age_recipient_must_be_an_age_public_key():
    with pytest.raises(ValueError, match="age public key"):
        config_script(None, age_recipient="not-a-key")
    assert f"age -r {KEY}" in config_script(None, age_recipient=KEY)
    assert "age -r" not in config_script(None)
    from fleetdeck.config import AppConfig

    with pytest.raises(ValueError, match="age1"):
        AppConfig(backup_age_recipient="oops")


async def test_archive_on_the_machine_is_encrypted_and_pruned_by_name(tree):
    tmp, etc = tree
    out = tmp / "out"
    out.mkdir()
    got, cb = lines()
    r = await LocalExecutor(_fake_age(tmp)).run(script(None, tmp, etc, age_recipient=KEY), cb)
    assert r.rc == 0, got
    (archive,) = out.glob("fleetdeck-config-*.tar.gz.age")
    assert archive.read_bytes().startswith(b"AGE-ENCRYPTED\n")
    assert not list(out.glob("*.tar.gz"))  # no plain archive left next to it


async def test_no_age_on_the_machine_writes_nothing_unencrypted(tree):
    tmp, etc = tree
    out = tmp / "out"
    out.mkdir()
    if subprocess.run(["sh", "-c", "command -v age"], capture_output=True).returncode == 0:
        pytest.skip("age is installed here")
    r = await LocalExecutor().run(script(None, tmp, etc, age_recipient=KEY), lines()[1])
    assert r.rc == 127 and not list(out.glob("fleetdeck-config-*"))


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell")
def test_dashboard_side_encrypt_runs_age(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _fake_age(tmp_path)["PATH"])
    plain, out = tmp_path / "a.tar.gz", tmp_path / "a.tar.gz.age"
    plain.write_bytes(b"data")
    backup_runs._encrypt(plain, out, KEY)
    assert out.read_bytes() == b"AGE-ENCRYPTED\ndata"
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(RuntimeError, match="age is not installed"):
        backup_runs._encrypt(plain, out, KEY)


def _no_hostname(tmp_path):
    """A PATH where `hostname` and `uname` print nothing, like a machine without the hostname program."""
    bin_dir = tmp_path / "nohost"
    bin_dir.mkdir()
    for name in ("hostname", "uname"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 127\n")
        (bin_dir / name).chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell")
async def test_archive_without_a_hostname_is_named_after_the_machine(tree):
    tmp, etc = tree
    (tmp / "out").mkdir()
    got, cb = lines()
    r = await LocalExecutor(_no_hostname(tmp)).run(script(None, tmp, etc, name="pve 2"), cb)
    assert r.rc == 0, got
    (archive,) = (tmp / "out").glob("fleetdeck-config-*.tar.gz")
    assert archive.name.startswith("fleetdeck-config-pve_2-")


async def test_save_on_dashboard_puts_the_machine_in_the_file_name(tree):
    tmp, etc = tree
    folder = tmp / "backups"
    folder.mkdir()
    await run_stream(tmp, etc, folder, "my pi/4")
    (f,) = (folder / "fleetdeck" / "my_pi_4").glob("fleetdeck-config-*.tar.gz")
    assert backup_runs.STAMP_RE.fullmatch(f.name.removeprefix("fleetdeck-config-my_pi_4-").removesuffix(".tar.gz"))


async def test_pruning_goes_by_date_whatever_the_machine_is_called(tree):
    tmp, etc = tree
    sub = tmp / "backups" / "fleetdeck" / "10.0.0.5"
    sub.mkdir(parents=True)
    # Names from before the machine was in them sort after "10.0..." by name, but are older.
    for i in range(7):
        (sub / f"fleetdeck-config-2020010{i}_000000.tar.gz").write_text("x")
    await run_stream(tmp, etc, tmp / "backups", "10.0.0.5")
    names = sorted(p.name for p in sub.glob("fleetdeck-config-*.tar.gz"))
    assert len(names) == 7 and "fleetdeck-config-20200100_000000.tar.gz" not in names
    assert any(n.startswith("fleetdeck-config-10.0.0.5-") for n in names)


async def test_save_on_dashboard_with_a_password_writes_an_age_file_it_opens(tree):
    from test_agecrypt import decrypt

    tmp, etc = tree
    folder = tmp / "backups"
    folder.mkdir()
    run = SshHostBackup(FakeSSH(), None, name="pi")
    old, backup_runs.DEFAULT_PATHS = backup_runs.DEFAULT_PATHS, (str(etc),)
    old_n, backup_runs.agecrypt.WORK_FACTOR = backup_runs.agecrypt.WORK_FACTOR, 10
    try:
        # The password wins over a key on the dashboard: an age file can't have both.
        await run.run(lines()[1], DASHBOARD, folder, age_recipient=KEY, password="correct horse")
    finally:
        backup_runs.DEFAULT_PATHS, backup_runs.agecrypt.WORK_FACTOR = old, old_n
    sub = folder / "fleetdeck" / "pi"
    (f,) = sub.glob("fleetdeck-config-pi-*.tar.gz.age")
    assert [p.name for p in sub.iterdir()] == [f.name]  # no plain archive or temp file left
    out = tmp / "back.tar.gz"
    out.write_bytes(decrypt(f.read_bytes(), "correct horse"))
    with tarfile.open(out) as t:
        assert any(n.endswith("hosts") for n in t.getnames())


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell")
async def test_a_password_alone_writes_nothing_unencrypted_on_the_machine(tree):
    tmp, etc = tree
    out = tmp / "out"
    run = SshHostBackup(LocalExecutor(), None, name="pi")
    got, cb = lines()
    old, backup_runs.DEFAULT_PATHS = backup_runs.DEFAULT_PATHS, (str(etc),)
    try:
        with pytest.raises(RuntimeError, match="only encrypts backups saved on the dashboard"):
            await run.run(cb, str(tmp), password="correct horse")
    finally:
        backup_runs.DEFAULT_PATHS = old
    assert not list(tmp.glob("fleetdeck-config-*")) and not out.exists()


def test_with_a_key_too_the_machine_uses_the_key():
    s = config_script(None, age_recipient=KEY, refuse_plain=True)
    assert f"age -r {KEY}" in s and "exit 6" not in s
    assert "exit 6" in config_script(None, refuse_plain=True)
    # restic and borg keep their own encryption, so a password doesn't stop them.
    assert "exit 6" not in config_script(HostBackupConfig(type="restic", repository="/r"), refuse_plain=True)


def test_backup_password_is_checked():
    from fleetdeck.config import AppConfig

    with pytest.raises(ValueError, match="at least 8"):
        AppConfig(backup_password="short")
    assert AppConfig(backup_password="long enough").backup_password == "long enough"
    assert AppConfig().backup_password is None

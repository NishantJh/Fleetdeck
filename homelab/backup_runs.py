"""The "Back up" button on a host: makes the host's own backup, with defaults per kind of machine.

* **Proxmox node:** its guests with vzdump through the API (to a Proxmox Backup Server storage if there
  is one, else the first backup storage), then, over SSH, the node's config (``/etc``, which holds
  ``/etc/pve``) to the same Proxmox Backup Server, or to the backup folder if there is none.
* **Incus host:** the server, project, profile, network and storage settings as a JSON file in the
  dashboard's data folder, and an export of each instance made by Incus itself.
* **Any other machine over SSH:** an archive of ``/etc`` and a few more config folders, in the
  folder, restic or borg repository the machine's ``backup:`` entry names, else in
  ``/var/backups/homelab-manager`` on the machine.

Nothing here deletes anything but the older backups this dashboard made itself, found by their name.
Every step runs even if an earlier one failed; the run fails at the end if any step did.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from .config import HostBackupConfig, IncusConfig, ProxmoxConfig
from .executors import LineCallback, SSHExecutor
from .host_backups import NO_PROMPTS, _env

log = logging.getLogger(__name__)

# Names of what this dashboard makes, so only those are ever pruned.
PREFIX = "homelab_"
FILE_PREFIX = "homelab-config-"
LOCAL_DIR = "/var/backups/homelab-manager"
KEEP_FILES = 7
# A node's vzdump of all its guests can take hours.
TASK_TIMEOUT = 12 * 3600.0
TASK_POLL = 5.0


class BackupRun(Protocol):
    """Makes one host's backup, reporting progress with ``on_line``. Raises if any step failed."""

    async def run(self, on_line: LineCallback) -> None: ...


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


class _Steps:
    """Runs steps one after another, each isolated, and reports the failures together at the end."""

    def __init__(self, on_line: LineCallback):
        self.on_line = on_line
        self.failures: list[str] = []

    def say(self, text: str) -> None:
        self.on_line("stdout", text)

    def warn(self, text: str) -> None:
        self.on_line("stderr", text)

    async def step(self, title: str, coro_fn: Callable[[], object]) -> None:
        self.say(f"== {title}")
        try:
            await coro_fn()  # type: ignore[misc]
        except Exception as e:  # noqa: BLE001 - one failed step must not stop the others
            reason = str(e) or type(e).__name__
            self.failures.append(f"{title}: {reason}")
            self.warn(f"{title} failed: {reason}")

    def finish(self) -> None:
        if self.failures:
            raise RuntimeError("; ".join(self.failures))


# ---- config archive over SSH (every machine with SSH, and the Proxmox node) ----------------------


DEFAULT_PATHS = ("/etc", "/usr/local/etc", "/usr/local/bin", "/var/spool/cron")


def config_script(
    cfg: HostBackupConfig | None,
    *,
    proxmox: bool = False,
    paths: tuple[str, ...] = DEFAULT_PATHS,
    local_dir: str = LOCAL_DIR,
) -> str:
    """A POSIX sh script that backs up the machine's config where its ``backup:`` entry says.

    It only reads the machine's files and writes the archive. Where ``cfg`` is None: on a Proxmox node
    to its Proxmox Backup Server storage (the credentials are the ones Proxmox keeps for that storage),
    else to /var/backups/homelab-manager."""
    local_dir = shlex.quote(local_dir)
    extra = " ".join(shlex.quote(p) for p in (*paths, *(cfg.include if cfg else [])))
    head = f"""\
umask 077
STAMP=$(date -u +%Y%m%d_%H%M%S)
HOST=$(hostname)
W=$(mktemp -d) || exit 3
trap 'rm -rf "$W"' EXIT
# What goes in: the config folders that exist here.
set --
for p in {extra}; do
  [ -e "$p" ] && set -- "$@" "$p"
done
# What is installed, so a rebuilt machine can get the same packages.
{{ dpkg --get-selections || rpm -qa || apk info -v || pacman -Q; }} >"$W/packages.txt" 2>/dev/null
"""
    local = f"""\
archive() {{
  # $1: the folder to put it in; the rest is what to archive. tar exit 1 only means a file changed
  # while it was read.
  DEST="$1"; shift
  OUT="$DEST/{FILE_PREFIX}$HOST-$STAMP.tar.gz"
  mkdir -p "$DEST" && chmod 700 "$DEST" 2>/dev/null
  tar -czf "$OUT" "$@" "$W/packages.txt" 2>"$W/err"
  rc=$?
  grep -v 'Removing leading' "$W/err" >&2
  [ "$rc" -le 1 ] || {{ rm -f "$OUT"; echo "tar failed" >&2; exit 4; }}
  echo "wrote $OUT ($(wc -c <"$OUT") bytes)"
  # Keep the newest {KEEP_FILES} of the archives made here (by their name), nothing else.
  ls -1t "$DEST"/{FILE_PREFIX}*.tar.gz 2>/dev/null | tail -n +{KEEP_FILES + 1} | while read -r old; do
    rm -f -- "$old" && echo "removed old $old"
  done
}}
"""
    if cfg is not None and cfg.type == "restic":
        repo = f"-r {shlex.quote(cfg.repository)} " if cfg.repository else ""
        return (
            NO_PROMPTS + _env(cfg) + head
            + f'restic {repo}backup --tag homelab-manager "$@" "$W/packages.txt"\n'
        )  # fmt: skip
    if cfg is not None and cfg.type == "borg":
        repo = shlex.quote(cfg.repository) if cfg.repository else ""
        return (
            NO_PROMPTS + _env(cfg) + head
            + f'borg create --stats {repo}::{FILE_PREFIX}"$HOST"-"$STAMP" "$@" "$W/packages.txt"\n'
        )  # fmt: skip
    if cfg is not None and cfg.type == "path":
        # A folder the machine's own backups go to. A single backup file is not ours to put things next to.
        dest = shlex.quote(cfg.path)
        return (
            head + local
            + f'if [ -d {dest} ]; then archive {dest} "$@"; '
            + f'else echo "{cfg.path} is not a folder, so the archive goes to {local_dir}" >&2; '
            + f"archive {local_dir} \"$@\"; fi\n"
        )  # fmt: skip
    if proxmox:
        return head + local + _PBS_FUNCS + f'push_pbs "$@" || archive {local_dir} "$@"\n'
    return head + local + f'archive {local_dir} "$@"\n'


# Looks up a Proxmox Backup Server storage in storage.cfg and pushes /etc to it with the same
# credentials Proxmox itself uses (they are kept for root under /etc/pve/priv/storage). Returns
# non-zero without doing anything when there is no usable PBS storage, so the caller falls back.
_PBS_FUNCS = r"""
push_pbs() {
  command -v proxmox-backup-client >/dev/null 2>&1 || { echo "proxmox-backup-client is not installed here" >&2; return 1; }
  LINE=$(awk '
    function flush() { if (type == "pbs") print name "|" server "|" ds "|" usr "|" fp "|" port "|" ns "|" dis "|" content }
    /^[A-Za-z]+: / { flush(); type = $1; sub(":", "", type); name = $2; server = ds = usr = fp = port = ns = dis = content = ""; next }
    /^[ \t]+server / { server = $2 }
    /^[ \t]+datastore / { ds = $2 }
    /^[ \t]+username / { usr = $2 }
    /^[ \t]+fingerprint / { fp = $2 }
    /^[ \t]+port / { port = $2 }
    /^[ \t]+namespace / { ns = $2 }
    /^[ \t]+disable/ { dis = "1" }
    /^[ \t]+content / { content = $2 }
    END { flush() }
  ' "${PVE_STORAGE_CFG:-/etc/pve/storage.cfg}" 2>/dev/null | awk -F'|' '$8 == "" && ($9 == "" || $9 ~ /backup/) { print; exit }')
  [ -n "$LINE" ] || { echo "no Proxmox Backup Server storage is set up on this node" >&2; return 1; }
  IFS='|' read -r NAME SERVER DS USR FP PORT NS DIS CONTENT <<EOF
$LINE
EOF
  PRIV="${PVE_PRIV_DIR:-/etc/pve/priv/storage}"
  [ -r "$PRIV/$NAME.pw" ] || { echo "no saved password for the storage $NAME" >&2; return 1; }
  export PBS_REPOSITORY="$USR@$SERVER:${PORT:-8007}:$DS"
  PBS_PASSWORD=$(cat "$PRIV/$NAME.pw"); export PBS_PASSWORD
  [ -z "$FP" ] || export PBS_FINGERPRINT="$FP"
  set -- etc.pxar:/etc --include-dev /etc/pve --backup-type host --backup-id "$HOST"
  [ -z "$NS" ] || set -- "$@" --ns "$NS"
  # With client-side encryption on for the storage, the key Proxmox keeps for it is used as well.
  [ ! -r "$PRIV/$NAME.enc" ] || set -- "$@" --keyfile "$PRIV/$NAME.enc"
  echo "sending the node's config to the Proxmox Backup Server storage $NAME"
  proxmox-backup-client backup "$@" </dev/null
}
"""  # noqa: E501


async def _config_step(ssh: SSHExecutor, cfg: HostBackupConfig | None, steps: _Steps, *, proxmox: bool = False) -> None:
    res = await ssh.run(config_script(cfg, proxmox=proxmox), steps.on_line, timeout=3600)
    if res.rc == 127:
        what = cfg.type if cfg and cfg.type != "path" else "a command it needs"
        raise RuntimeError(f"{what} is not installed on this machine")
    if res.rc != 0:
        raise RuntimeError(f"exited with code {res.rc}")


class SshHostBackup:
    """A plain machine reached over SSH."""

    def __init__(self, ssh: SSHExecutor, cfg: HostBackupConfig | None):
        self.ssh, self.cfg = ssh, cfg

    async def run(self, on_line: LineCallback) -> None:
        steps = _Steps(on_line)
        await steps.step("Archiving the machine's config", lambda: _config_step(self.ssh, self.cfg, steps))
        steps.finish()


# ---- Proxmox ------------------------------------------------------------------------------------


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code == 403 or (r.status_code >= 400 and "permission" in r.text.lower()):
        raise RuntimeError(
            f"{what}: Proxmox says the API token may not do this (it needs VM.Backup and "
            "Datastore.AllocateSpace). Open Setup and press Connect on this machine again to update its permissions"
        )
    if r.status_code >= 400:
        raise RuntimeError(f"{what} failed ({r.status_code}): {r.text.strip()[:300]}")


class PveHostBackup:
    """A Proxmox node: its guests through the API, its config over SSH when the node has SSH settings."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        node: str,
        cfg: ProxmoxConfig,
        ssh: SSHExecutor | None,
        task_timeout: float = TASK_TIMEOUT,
        poll: float = TASK_POLL,
    ):
        self.client, self.node, self.cfg, self.ssh = client, node, cfg, ssh
        self.task_timeout, self.poll = task_timeout, poll

    async def _get(self, path: str, **params):
        r = await self.client.get(f"/nodes/{self.node}{path}", params=params or None)
        _check(r, path)
        return r.json()["data"]

    async def _vmids(self) -> list[int]:
        found: list[int] = []
        for kind in ("lxc", "qemu"):
            for g in await self._get(f"/{kind}"):
                vmid = int(g["vmid"])
                if vmid not in self.cfg.exclude and not g.get("template"):
                    found.append(vmid)
        return sorted(found)

    async def _storage(self) -> str:
        if self.cfg.backup_storage:
            return self.cfg.backup_storage
        storages = [s for s in await self._get("/storage", content="backup", enabled=1) if s.get("active", 1)]
        if not storages:
            raise RuntimeError("no active storage takes backups (add one under Datacenter > Storage in Proxmox)")
        # A Proxmox Backup Server deduplicates, and is meant for this.
        storages.sort(key=lambda s: s.get("type") != "pbs")
        return storages[0]["storage"]

    async def _guests(self, steps: _Steps) -> None:
        vmids = await self._vmids()
        if not vmids:
            steps.say("No guests on this node")
            return
        storage = await self._storage()
        steps.say(f"Backing up {len(vmids)} guest(s) ({', '.join(map(str, vmids))}) to the storage {storage}")
        r = await self.client.post(
            f"/nodes/{self.node}/vzdump",
            data={"vmid": ",".join(map(str, vmids)), "storage": storage, "mode": "snapshot", "compress": "zstd"},
        )
        _check(r, "vzdump")
        upid = r.json()["data"]
        deadline = time.monotonic() + self.task_timeout
        shown = 0
        while True:
            status = await self._get(f"/tasks/{upid}/status")
            # The task log, as far as it has got.
            lines = await self._get(f"/tasks/{upid}/log", start=shown, limit=500)
            for entry in lines:
                steps.say(entry.get("t", ""))
            shown += len(lines)
            if status.get("status") == "stopped":
                break
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"not finished after {self.task_timeout / 3600:.0f} h "
                    "(it keeps running in Proxmox; see its task log)"
                )
            await asyncio.sleep(self.poll)
        exit_status = str(status.get("exitstatus", ""))
        if exit_status.startswith("WARNINGS"):
            steps.warn(f"vzdump finished with {exit_status.lower()}")
        elif exit_status != "OK":
            raise RuntimeError(exit_status or "vzdump did not finish")

    async def run(self, on_line: LineCallback) -> None:
        steps = _Steps(on_line)
        if self.cfg.backup_guests:
            await steps.step("Backing up the guests with vzdump", lambda: self._guests(steps))
        else:
            steps.say("Guests are left out (backup_guests is off)")
        if self.ssh is None:
            steps.warn("The node's own config was not backed up: add ssh settings for this node to do that too")
        else:
            await steps.step(
                "Backing up the node's config",
                lambda: _config_step(self.ssh, None, steps, proxmox=True),  # type: ignore[arg-type]
            )
        steps.finish()


# ---- Incus --------------------------------------------------------------------------------------


class IncusHostBackup:
    """An Incus host through its REST API: its settings as a file, and an export of each instance."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cfg: IncusConfig,
        dump_dir: Path | None,
        ssh: SSHExecutor | None = None,
        op_timeout: int = 4 * 3600,
    ):
        self.client, self.cfg, self.dump_dir, self.ssh, self.op_timeout = client, cfg, dump_dir, ssh, op_timeout

    async def _get(self, path: str, **params):
        r = await self.client.get(path, params=params or None)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} failed ({r.status_code}): {r.text.strip()[:200]}")
        return r.json()["metadata"]

    async def _wait(self, r: httpx.Response, what: str) -> None:
        if r.status_code >= 400:
            raise RuntimeError(f"{what} failed ({r.status_code}): {r.text.strip()[:300]}")
        w = await self.client.get(
            f"{r.json()['operation']}/wait", params={"timeout": self.op_timeout}, timeout=self.op_timeout + 30
        )
        if w.status_code >= 400:
            raise RuntimeError(f"{what} failed ({w.status_code}): {w.text.strip()[:300]}")
        md = w.json()["metadata"]
        if md.get("status") != "Success":
            raise RuntimeError(f"{what} failed: {md.get('err') or md.get('status')}")

    async def settings(self) -> dict:
        """Everything `incus admin init --dump` would replay: server, projects, profiles, networks, pools."""
        server = await self._get("/1.0")
        out: dict = {
            "taken_at": datetime.now(UTC).isoformat(),
            "server": {"config": server.get("config"), "environment": {
                k: v for k, v in (server.get("environment") or {}).items()
                if k in ("server_name", "server_version", "storage", "storage_version")
            }},
            "storage_pools": await self._get("/1.0/storage-pools", recursion=1),
            "projects": {},
        }  # fmt: skip
        for p in await self._get("/1.0/projects", recursion=1):
            name = p["name"]
            entry: dict = {"project": p}
            # A project that doesn't have its own networks answers with an error; that's not a problem.
            for key, path in (("profiles", "/1.0/profiles"), ("networks", "/1.0/networks")):
                try:
                    entry[key] = await self._get(path, recursion=1, project=name)
                except RuntimeError as e:
                    entry[key] = {"error": str(e)}
            out["projects"][name] = entry
        return out

    async def _dump(self, steps: _Steps) -> None:
        if self.dump_dir is None:
            raise RuntimeError("no state_file is set, so there is no data folder to keep it in")
        data = await self.settings()
        self.dump_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.dump_dir / f"incus-{self.cfg.name}-{_stamp()}.json"
        path.touch(mode=0o600)
        path.write_text(json.dumps(data, indent=2, default=str))
        steps.say(f"wrote {path} ({path.stat().st_size} bytes)")
        old = sorted(self.dump_dir.glob(f"incus-{self.cfg.name}-*.json"))[:-KEEP_FILES]
        for f in old:
            f.unlink(missing_ok=True)
            steps.say(f"removed old {f.name}")

    async def _instances(self) -> list[dict]:
        r = await self.client.get("/1.0/instances", params={"recursion": 2, "all-projects": "true"})
        if r.status_code >= 400:
            raise RuntimeError(f"listing instances failed ({r.status_code}): {r.text.strip()[:200]}")
        return r.json()["metadata"]

    async def _export(self, steps: _Steps, inst: dict) -> None:
        name, project = inst["name"], inst.get("project", "default")
        label = name if project == "default" else f"{project}/{name}"
        params = {"project": project}
        used = sum(
            (d.get("usage") or 0) for d in ((inst.get("state") or {}).get("disk") or {}).values()
        )  # only known while it runs
        limit = self.cfg.backup_max_instance_gb * 2**30
        if used > limit:
            steps.warn(
                f"{label} left out: it uses {used / 2**30:.0f} GB, above backup_max_instance_gb "
                f"({self.cfg.backup_max_instance_gb}). Raise that setting to include it"
            )
            return
        backup = f"{PREFIX}{_stamp()}"
        steps.say(f"Exporting {label}")
        r = await self.client.post(
            f"/1.0/instances/{name}/backups",
            params=params,
            json={"name": backup, "instance_only": False, "optimized_storage": False, "compression_algorithm": "zstd"},
        )
        await self._wait(r, f"export of {label}")
        ours = sorted(
            n for u in await self._get(f"/1.0/instances/{name}/backups", **params)
            if (n := u.split("?")[0].rstrip("/").rsplit("/", 1)[-1]).startswith(PREFIX)
        )  # fmt: skip
        for old in ours[: max(len(ours) - self.cfg.backup_keep, 0)]:
            try:
                await self._wait(
                    await self.client.delete(f"/1.0/instances/{name}/backups/{old}", params=params),
                    f"deleting the old export {old}",
                )
                steps.say(f"removed old export {old} of {label}")
            except Exception as e:  # noqa: BLE001 - housekeeping only
                steps.warn(f"could not delete the old export {old} of {label}: {e}")

    async def _exports(self, steps: _Steps) -> None:
        if not self.cfg.backup_max_instance_gb:
            steps.say("Instance exports are off (backup_max_instance_gb is 0)")
            return
        instances = [
            i for i in await self._instances()
            if i["name"] not in self.cfg.exclude
            and f"{i.get('project', 'default')}/{i['name']}" not in self.cfg.exclude
        ]  # fmt: skip
        if not instances:
            steps.say("No instances")
        failed = []
        for inst in instances:
            try:
                await self._export(steps, inst)
            except Exception as e:  # noqa: BLE001 - go on with the other instances
                failed.append(inst["name"])
                steps.warn(f"{inst['name']} failed: {e or type(e).__name__}")
        if failed:
            raise RuntimeError(f"could not export {', '.join(failed)}")
        steps.say(
            "The exports are kept by Incus on the Incus host itself, so they don't survive losing it: "
            "copy them off with `incus export`, or back that host up too"
        )

    async def run(self, on_line: LineCallback) -> None:
        steps = _Steps(on_line)
        await steps.step("Saving the Incus settings", lambda: self._dump(steps))
        await steps.step("Exporting the instances", lambda: self._exports(steps))
        if self.ssh is not None:
            await steps.step(
                "Archiving the host's config",
                lambda: _config_step(self.ssh, self.cfg.ssh.backup if self.cfg.ssh else None, steps),  # type: ignore[arg-type]
            )
        steps.finish()

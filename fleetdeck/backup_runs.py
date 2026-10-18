"""The "Back up" button on a host: saves what is needed to set the host up again quickly.

Only the host's own settings, never its guests or instances (those are created separately):

* **Proxmox node:** its config (``/etc``, which holds ``/etc/pve``) over SSH, to the node's Proxmox
  Backup Server storage, or to an archive when there is none.
* **Incus host:** the server, project, profile, network and storage settings as a JSON file in the
  dashboard's data folder, plus ``/etc`` of the host when it has SSH settings.
* **Any other machine over SSH:** an archive of ``/etc`` and a few more config folders, in the
  folder, restic or borg repository the machine's ``backup:`` entry names, else in
  ``/var/backups/homelab-manager`` on the machine.

Each run can name a folder instead (a network share mounted on the machine; for Incus, mounted in
the dashboard's container). It must already exist, so a share that is not mounted is an error and
nothing lands on the wrong disk.

Or it can save **on the dashboard** (``DASHBOARD``): the archive is streamed back over the connection
the dashboard already has (SSH) into ``<backup folder>/fleetdeck/<machine>/``, so every machine ends up in one
folder mounted in the dashboard's container. Nothing is written on the machine, and it is the same
for a machine reached through a tunnel.

Nothing here deletes anything but older backups this dashboard made itself, found by their name.
Every step runs even if an earlier one failed; the run fails at the end if any step did.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from .config import HostBackupConfig, IncusConfig
from .executors import LineCallback, SSHExecutor
from .host_backups import NO_PROMPTS, _env

log = logging.getLogger(__name__)

# Names of what this dashboard makes, so only those are ever pruned.
FILE_PREFIX = "fleetdeck-config-"
LOCAL_DIR = "/var/backups/homelab-manager"
KEEP_FILES = 7
AGE_SUFFIX = ".age"
AGE_RECIPIENT = re.compile(r"^age1[a-z0-9]{50,}$")
STAMP_GLOB = "[0-9]" * 8 + "_" + "[0-9]" * 6  # matches the STAMP of the script, e.g. 20260101_030000
# The place to choose for "save on the dashboard", in a run's destination and a machine's saved place.
DASHBOARD = "dashboard"
# Everything saved on the dashboard goes in this folder inside the backup folder; nothing else there is touched.
APP_FOLDER = "fleetdeck"


class BackupRun(Protocol):
    """Makes one host's backup, reporting progress with ``on_line``. Raises if any step failed."""

    async def run(
        self, on_line: LineCallback, destination: str | None = None, dashboard_folder: Path | None = None
    ) -> None: ...


def check_destination(value: str) -> str:
    """A folder to back up to, as the user typed it, or DASHBOARD. Raises ValueError with a message to show."""
    value = value.strip()
    if value == DASHBOARD:
        return value
    if not value.startswith("/") or len(value) > 1024:
        raise ValueError("the backup folder must be a full path starting with /, e.g. /mnt/nas/backups")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or ".." in value.split("/"):
        raise ValueError("the backup folder can't contain control characters or '..'")
    return value


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def machine_folder(folder: Path | None, name: str) -> Path:
    """``<folder>/fleetdeck/<machine>/`` in the dashboard's backup folder, made if needed. The backup folder itself
    must be there already, so a share that is not mounted is an error, not a folder on the wrong disk."""
    if folder is None:
        raise RuntimeError("no backup folder on the dashboard is set: set backup_folder in the config")
    if not folder.is_dir():
        raise RuntimeError(f"{folder} does not exist in the dashboard's container (is the folder mounted?)")
    sub = folder / APP_FOLDER / (re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".") or "machine")
    try:
        sub.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe = sub / ".write-test"
        probe.touch()
        probe.unlink()
    except OSError as e:
        raise RuntimeError(
            f"the dashboard can't write to {sub} ({e.strerror or e}). The folder has to be writable by the "
            "user the dashboard runs as (see PUID/PGID)"
        ) from None
    return sub


def _prune(folder: Path, pattern: str, steps: _Steps) -> None:
    for f in sorted(folder.glob(pattern))[:-KEEP_FILES]:
        f.unlink(missing_ok=True)
        steps.say(f"removed old {f.name}")


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


_ARCHIVE_PLAIN = """\
  tar -czf "$OUT" "$@" "$W/packages.txt" 2>"$W/err"
  rc=$?
"""
# The plain archive only exists in the script's private temp folder (removed on exit), never next to the
# encrypted one. 127 is the exit code _config_step turns into "not installed".
_ARCHIVE_AGE = """\
  command -v age >/dev/null 2>&1 || {{ echo "age is not installed on this machine (apt install age)" >&2; exit 127; }}
  tar -czf "$W/archive.tgz" "$@" "$W/packages.txt" 2>"$W/err"
  rc=$?
  if [ "$rc" -le 1 ]; then
    age -r {key} -o "$OUT" "$W/archive.tgz" 2>>"$W/err" || rc=4
  fi
"""


def config_script(
    cfg: HostBackupConfig | None,
    *,
    proxmox: bool = False,
    paths: tuple[str, ...] | None = None,
    local_dir: str = LOCAL_DIR,
    destination: str | None = None,
    stream: bool = False,
    age_recipient: str | None = None,
) -> str:
    """A POSIX sh script that backs up the machine's config where its ``backup:`` entry says.

    It only reads the machine's files and writes the archive. Where ``cfg`` is None: on a Proxmox node
    to its Proxmox Backup Server storage (the credentials are the ones Proxmox keeps for that storage),
    else to /var/backups/homelab-manager. With ``destination``, to that folder instead, which must exist.
    With ``stream`` the tar.gz goes to stdout and nothing is written here (see SshHostBackup).
    With ``age_recipient`` the archives written on the machine are encrypted to that public key with `age`
    (it must be installed there; nothing is written unencrypted instead). Backups pushed to Proxmox Backup
    Server, restic or borg keep their own encryption."""
    if age_recipient is not None and not AGE_RECIPIENT.match(age_recipient):
        raise ValueError("backup_age_recipient is not an age public key (it starts with age1)")
    local_dir = shlex.quote(local_dir)
    extra = " ".join(shlex.quote(p) for p in (*(paths or DEFAULT_PATHS), *(cfg.include if cfg else [])))
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
  OUT="$DEST/{FILE_PREFIX}$HOST-$STAMP.tar.gz{AGE_SUFFIX if age_recipient else ""}"
  mkdir -p "$DEST" && chmod 700 "$DEST" 2>/dev/null
{_ARCHIVE_AGE.format(key=age_recipient) if age_recipient else _ARCHIVE_PLAIN}  grep -v 'Removing leading' "$W/err" >&2
  [ "$rc" -le 1 ] || {{ rm -f "$OUT"; echo "tar failed" >&2; exit 4; }}
  echo "wrote $OUT ($(wc -c <"$OUT") bytes)"
  # Keep the newest {KEEP_FILES} of this host's archives (by their name), nothing else: the folder may be
  # shared with other machines. The stamp is spelled out so that host "pve" does not match host "pve-2".
  ls -1t "$DEST"/{FILE_PREFIX}"$HOST"-{STAMP_GLOB}.tar.gz* 2>/dev/null |
    tail -n +{KEEP_FILES + 1} | while read -r old; do
    rm -f -- "$old" && echo "removed old $old"
  done
}}
"""
    if stream:
        return head + (
            'tar -czf - "$@" "$W/packages.txt" 2>"$W/err"\n'
            "rc=$?\n"
            "grep -v 'Removing leading' \"$W/err\" >&2\n"
            '[ "$rc" -le 1 ] || { echo "tar failed" >&2; exit 4; }\n'
        )
    if destination:
        # Asked for by the user for this run: a folder that must already exist (a share that is not
        # mounted is an error, not a reason to write to the mount point on the local disk).
        return (
            head + local
            + "D=" + shlex.quote(destination) + "\n"
            + '[ -d "$D" ] || { echo "$D does not exist on this machine (is the share mounted?)" >&2; exit 5; }\n'
            + '[ -w "$D" ] || { echo "$D is not writable" >&2; exit 5; }\n'
            + 'archive "$D" "$@"\n'
        )  # fmt: skip
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
            + f'else echo {shlex.quote(f"{cfg.path} is not a folder, so the archive goes to ") + local_dir} >&2; '
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


async def _config_step(
    ssh: SSHExecutor,
    cfg: HostBackupConfig | None,
    steps: _Steps,
    destination: str | None,
    *,
    proxmox: bool = False,
    age_recipient: str | None = None,
) -> None:
    script = config_script(cfg, proxmox=proxmox, destination=destination, age_recipient=age_recipient)
    res = await ssh.run(script, steps.on_line, timeout=3600)
    if res.rc == 127:
        what = cfg.type if cfg and cfg.type != "path" else "a command it needs (for encrypted backups: age)"
        raise RuntimeError(f"{what} is not installed on this machine")
    if res.rc == 5 and destination:
        raise RuntimeError(
            f"{destination} is missing or not writable on the machine itself. The folder is looked up on the machine "
            "the dashboard connects to (for Proxmox, the node), not inside the dashboard's container"
        )
    if res.rc != 0:
        raise RuntimeError(f"exited with code {res.rc}")


def _check_gzip(path: Path) -> None:
    """Reads the whole file through gzip, which checks its CRC, so a cut-off stream is never kept."""
    with gzip.open(path) as f:
        while f.read(1 << 20):
            pass


def _encrypt(plain: Path, out: Path, recipient: str) -> None:
    """Encrypt ``plain`` to the age public key with the `age` program, writing ``out``."""
    try:
        p = subprocess.run(["age", "-r", recipient, "-o", str(out), str(plain)], capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("age is not installed in the dashboard's container") from None
    if p.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"age failed: {p.stderr.strip()[:200]}")


async def _stream_step(
    ssh: SSHExecutor,
    cfg: HostBackupConfig | None,
    steps: _Steps,
    folder: Path | None,
    name: str,
    age_recipient: str | None = None,
) -> None:
    """The machine's config as a tar.gz streamed back over SSH into ``<folder>/<name>/``. It is written under a
    temporary name and renamed once complete, so a failed run leaves nothing that looks like a backup."""
    sub = machine_folder(folder, name)
    final = sub / f"{FILE_PREFIX}{_stamp()}.tar.gz{AGE_SUFFIX if age_recipient else ''}"
    part = sub / f".{final.name}.part"
    try:
        with open(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            rc = await ssh.run_to_file(config_script(cfg, stream=True), out, steps.on_line, timeout=3600)
            out.flush()
            os.fsync(out.fileno())
        if rc == 127:
            raise RuntimeError("a command it needs (tar or gzip) is not installed on this machine")
        if rc != 0:
            raise RuntimeError(f"exited with code {rc}")
        await asyncio.to_thread(_check_gzip, part)
        if age_recipient:
            sealed = sub / f".{final.name}.sealed"
            try:
                os.close(os.open(sealed, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
                await asyncio.to_thread(_encrypt, part, sealed, age_recipient)
                os.replace(sealed, final)
            finally:
                sealed.unlink(missing_ok=True)
            part.unlink(missing_ok=True)  # the plain archive never stays
        else:
            os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    steps.say(f"wrote {final} ({final.stat().st_size} bytes)")
    _prune(sub, f"{FILE_PREFIX}*.tar.gz*", steps)


class SshHostBackup:
    """A plain machine reached over SSH, or a Proxmox node (``proxmox=True``). ``name`` is the machine's
    folder under the dashboard's backup folder."""

    def __init__(self, ssh: SSHExecutor, cfg: HostBackupConfig | None, *, proxmox: bool = False, name: str = ""):
        self.ssh, self.cfg, self.proxmox, self.name = (
            ssh,
            cfg,
            proxmox,
            name or getattr(getattr(ssh, "cfg", None), "host", "machine"),
        )

    async def run(
        self,
        on_line: LineCallback,
        destination: str | None = None,
        dashboard_folder: Path | None = None,
        age_recipient: str | None = None,
    ) -> None:
        steps = _Steps(on_line)
        title = "Backing up the node's config" if self.proxmox else "Archiving the machine's config"
        if destination == DASHBOARD:
            await steps.step(
                title, lambda: _stream_step(self.ssh, self.cfg, steps, dashboard_folder, self.name, age_recipient)
            )
        else:
            await steps.step(
                title,
                lambda: _config_step(
                    self.ssh, self.cfg, steps, destination, proxmox=self.proxmox, age_recipient=age_recipient
                ),
            )
        steps.finish()


class IncusHostBackup:
    """An Incus host through its REST API: its settings as a file, and /etc over SSH if it has any."""

    def __init__(
        self, client: httpx.AsyncClient, cfg: IncusConfig, dump_dir: Path | None, ssh: SSHExecutor | None = None
    ):
        self.client, self.cfg, self.dump_dir, self.ssh = client, cfg, dump_dir, ssh

    async def _get(self, path: str, **params):
        r = await self.client.get(path, params=params or None)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} failed ({r.status_code}): {r.text.strip()[:200]}")
        return r.json()["metadata"]

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

    async def _dump(self, steps: _Steps, destination: str | None, dashboard_folder: Path | None) -> None:
        prefix = f"incus-{self.cfg.name}-"
        if destination == DASHBOARD:
            folder, prefix = machine_folder(dashboard_folder, self.cfg.name), "incus-settings-"
        else:
            folder = Path(destination) if destination else self.dump_dir
        if folder is None:
            raise RuntimeError("no state_file is set, so there is no data folder to keep it in")
        if destination and destination != DASHBOARD:
            # The folder of a share, which must be there already (see the module docstring).
            if not folder.is_dir():
                raise RuntimeError(f"{folder} does not exist in the dashboard's container (is the share mounted?)")
        else:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = await self.settings()
        path = folder / f"{prefix}{_stamp()}.json"
        path.touch(mode=0o600)
        path.write_text(json.dumps(data, indent=2, default=str))
        steps.say(f"wrote {path} ({path.stat().st_size} bytes)")
        _prune(folder, f"{prefix}*.json", steps)

    async def run(
        self,
        on_line: LineCallback,
        destination: str | None = None,
        dashboard_folder: Path | None = None,
        age_recipient: str | None = None,
    ) -> None:
        steps = _Steps(on_line)
        await steps.step("Saving the Incus settings", lambda: self._dump(steps, destination, dashboard_folder))
        if self.ssh is not None:
            ssh, cfg = self.ssh, self.cfg.ssh.backup if self.cfg.ssh else None
            # The host's own backup: entry is not used when the user named a place for this run.
            if destination == DASHBOARD:
                await steps.step(
                    "Archiving the host's config",
                    lambda: _stream_step(ssh, cfg, steps, dashboard_folder, self.cfg.name, age_recipient),
                )
            else:
                await steps.step(
                    "Archiving the host's config",
                    lambda: _config_step(ssh, cfg, steps, destination, age_recipient=age_recipient),
                )
        steps.finish()

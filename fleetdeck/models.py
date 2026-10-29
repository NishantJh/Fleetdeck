from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field

Platform = Literal["proxmox", "incus", "ssh", "demo"]
Kind = Literal["host", "lxc", "vm", "container"]


class BackupInfo(BaseModel):
    """Newest backup of one machine: Proxmox vzdump/PBS, Incus snapshots/exports, or a host's own check."""

    last: datetime | None = None
    storage: str | None = None  # where the newest backup is
    count: int = 0
    # No backup, or the newest is older than the configured maximum age.
    stale: bool = False
    # Whether a scheduled backup job covers this guest (None when unknown).
    in_job: bool | None = None
    # Verification state of the newest backup on PBS (None when not verified / not PBS).
    verified: bool | None = None
    # Why backups couldn't be read, e.g. missing permissions. With not_set_up: how to set it up.
    note: str | None = None
    # Nothing tells the dashboard where this machine's backups are.
    not_set_up: bool = False


class Target(BaseModel):
    """Something whose OS can be updated."""

    id: str
    name: str
    platform: Platform
    source: str  # name of the configured Proxmox/Incus/host entry
    kind: Kind
    parent: str | None = None  # host this guest runs on
    status: str = "running"
    updatable: bool = True
    # Can be checked but not upgraded from here (e.g. a Proxmox node without SSH settings).
    check_only: bool = False
    note: str | None = None
    backup: BackupInfo | None = None
    # Why this target must not be rebooted from here (e.g. the node that runs this dashboard).
    no_reboot: str | None = None
    # A Proxmox/Incus guest that can be snapshotted and rolled back.
    can_snapshot: bool = False
    # A host the "Back up" button works on (see backup_runs.py).
    can_backup: bool = False
    # Set monitor-only in the config: checked as usual, never updated from here (see JobManager.monitor_only).
    monitor_only: bool = False


class Package(BaseModel):
    name: str
    available: str
    current: str | None = None
    # Comes from a security repository (apt only: e.g. bookworm-security, jammy-security).
    security: bool = False


class CheckResult(BaseModel):
    package_manager: str | None = None
    os: str | None = None
    packages: list[Package] = Field(default_factory=list)
    reboot_required: bool | None = None
    checked_at: datetime
    error: str | None = None
    # Extra context to show next to the result, e.g. which repositories were used.
    notes: list[str] = Field(default_factory=list)
    # The dashboard's own container runs on this machine (see updates.self_script).
    runs_dashboard: bool = False
    # Installed by the Proxmox VE Helper-Scripts (community-scripts): the app's name, e.g. "plex".
    community_script: str | None = None


class ExecResult(BaseModel):
    rc: int
    stdout: str = ""
    stderr: str = ""


class StackService(BaseModel):
    """One service of a Docker Compose stack, as its container runs now."""

    name: str
    image: str
    status: str  # the container's state: running, exited, restarting...
    # A newer image for this tag is already on the machine (pulled by a check), but the container
    # was made from an older one.
    outdated: bool = False


class Stack(BaseModel):
    """A Docker Compose project found on a machine through its containers' compose labels."""

    name: str
    working_dir: str = ""
    config_files: list[str] = Field(default_factory=list)
    services: list[StackService] = Field(default_factory=list)
    # The stack that holds this dashboard's own container: never updated from here.
    is_dashboard: bool = False
    # The last pull of this stack's images failed (private registry, no network, a typo in a tag).
    error: str | None = None

    @property
    def running(self) -> list[StackService]:
        return [s for s in self.services if s.status in ("running", "restarting")]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def state(self) -> str:
        n = len(self.running)
        return "stopped" if n == 0 else "running" if n == len(self.services) else "partial"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def update_available(self) -> bool:
        return any(s.outdated for s in self.running)


class StacksResult(BaseModel):
    """What the last Docker check found on a machine."""

    # "yes", "none" (Docker is not installed) or "down" (Docker did not answer this account).
    docker: str = "yes"
    # "v2" (docker compose), "v1" (docker-compose) or "none".
    compose: str = "none"
    stacks: list[Stack] = Field(default_factory=list)
    checked_at: datetime
    error: str | None = None


JobAction = Literal["check", "upgrade", "reboot", "rollback", "backup", "stacks", "stack-update"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]


class Job(BaseModel):
    id: str
    target_id: str
    target_name: str
    action: JobAction
    # Upgrade only: reboot afterwards if the upgrade left a reboot pending.
    reboot_if_required: bool = False
    # Upgrade only: install just the packages from a security repository (see updates.SECURITY_UPGRADE_SCRIPT).
    security_only: bool = False
    # Upgrade: take a snapshot first (guests only). Upgrade/rollback: the snapshot taken / restored.
    snapshot_first: bool = False
    snapshot: str | None = None
    # Backup only: the folder the user asked for this time (None: the default place).
    destination: str | None = None
    # Stack update only: the Compose project to update, and whether to delete unused old images after.
    stack: str | None = None
    prune: bool = False
    status: JobStatus = "queued"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log: list[str] = Field(default_factory=list)
    error: str | None = None
    # While an upgrade runs detached on the target (see detached.py): its folder there, and how many
    # bytes of its log are already in ``log``.
    remote: str | None = None
    remote_offset: int = 0
    # True while ``remote`` is a helper-script app update, not the OS upgrade, so a resume picks the right step.
    app_update: bool = False
    # True once the helper-script app update has run, so a resume of the OS upgrade after it doesn't run it again.
    app_updated: bool = False
    # While waiting for a reboot to finish: the boot id from before it.
    reboot_from: str | None = None

    @property
    def resumable(self) -> bool:
        """Still running and can be picked up again after the dashboard restarts."""
        return self.status == "running" and (self.remote is not None or self.reboot_from is not None)

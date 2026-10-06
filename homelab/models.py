from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["proxmox", "incus", "ssh", "demo"]
Kind = Literal["host", "lxc", "vm", "container"]


class BackupInfo(BaseModel):
    """Proxmox backups (vzdump / Proxmox Backup Server) of one guest."""

    last: datetime | None = None
    storage: str | None = None  # where the newest backup is
    count: int = 0
    # No backup, or the newest is older than the configured maximum age.
    stale: bool = False
    # Whether a scheduled backup job covers this guest (None when unknown).
    in_job: bool | None = None
    # Verification state of the newest backup on PBS (None when not verified / not PBS).
    verified: bool | None = None
    # Why backups couldn't be read, e.g. missing permissions.
    note: str | None = None


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


class ExecResult(BaseModel):
    rc: int
    stdout: str = ""
    stderr: str = ""


JobAction = Literal["check", "upgrade", "reboot"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]


class Job(BaseModel):
    id: str
    target_id: str
    target_name: str
    action: JobAction
    # Upgrade only: reboot afterwards if the upgrade left a reboot pending.
    reboot_if_required: bool = False
    status: JobStatus = "queued"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log: list[str] = Field(default_factory=list)
    error: str | None = None

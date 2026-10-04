from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["proxmox", "incus", "ssh", "demo"]
Kind = Literal["host", "lxc", "vm", "container"]


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
    note: str | None = None


class Package(BaseModel):
    name: str
    available: str
    current: str | None = None


class CheckResult(BaseModel):
    package_manager: str | None = None
    os: str | None = None
    packages: list[Package] = Field(default_factory=list)
    reboot_required: bool | None = None
    checked_at: datetime
    error: str | None = None


class ExecResult(BaseModel):
    rc: int
    stdout: str = ""
    stderr: str = ""


JobAction = Literal["check", "upgrade"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]


class Job(BaseModel):
    id: str
    target_id: str
    target_name: str
    action: JobAction
    status: JobStatus = "queued"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log: list[str] = Field(default_factory=list)
    error: str | None = None

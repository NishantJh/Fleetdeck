"""Configuration loading.

The config is a YAML file (default ``config.yaml``, override with ``HOMELAB_CONFIG``).
``${VAR}`` references are expanded from the environment so secrets can stay out of
the file.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)


class SSHConfig(BaseModel):
    host: str
    port: int = 22
    user: str = "root"
    key_file: str | None = None
    password: str | None = None
    # Path to a known_hosts file. When unset, host keys are NOT verified.
    known_hosts: str | None = None
    # Wrap commands in `sudo -n` (for non-root users with passwordless sudo).
    sudo: bool = False


class ProxmoxConfig(BaseModel):
    name: str
    api_url: str  # e.g. https://192.168.1.10:8006
    token_id: str  # e.g. root@pam!homelab
    token_secret: str
    verify_ssl: bool | str = True  # bool or path to a CA bundle
    # SSH to the node: used to update the host itself and to run `pct exec` in LXCs.
    ssh: SSHConfig | None = None
    # Per-node SSH overrides for clusters, keyed by node name.
    node_ssh: dict[str, SSHConfig] = Field(default_factory=dict)
    # VMIDs to hide from the dashboard.
    exclude: list[int] = Field(default_factory=list)
    # VMIDs shown but never updated from the dashboard, e.g. the LXC/VM the dashboard runs in.
    # Upgrading it (Docker in particular) would restart the dashboard mid-upgrade.
    protect: list[int] = Field(default_factory=list)


class IncusConfig(BaseModel):
    name: str
    # Either a remote HTTPS URL with a trusted client certificate...
    url: str | None = None  # e.g. https://192.168.1.20:8443
    client_cert: str | None = None
    client_key: str | None = None
    verify_ssl: bool | str = True
    # ...or the local unix socket when the dashboard runs on the Incus host.
    socket: str | None = None  # e.g. /var/lib/incus/unix.socket
    # SSH to the Incus host, used to update the host itself.
    ssh: SSHConfig | None = None
    # Instance names ("project/name" or "name") to hide.
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _url_or_socket(self) -> IncusConfig:
        if not self.url and not self.socket:
            raise ValueError(f"incus '{self.name}': set either url or socket")
        return self


class HostConfig(SSHConfig):
    """A plain machine reached over SSH (e.g. a Raspberry Pi)."""

    name: str


class AuthConfig(BaseModel):
    username: str = Field("admin", min_length=1)
    password: str = Field(min_length=1)


_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
MIN_CHECK_INTERVAL = 300


def parse_duration(value: str | int) -> int:
    """``90``, ``"90s"``, ``"30m"``, ``"6h"`` or ``"1d"`` to seconds."""
    if isinstance(value, int):
        return value
    m = _DURATION.match(value.lower())
    if not m:
        raise ValueError(f"not a duration: {value!r} (use e.g. 30m, 6h, 1d)")
    return int(m[1]) * _UNITS[m[2]]


class ScheduleConfig(BaseModel):
    # Check every machine for updates this often (e.g. 6h, 1d). Unset: only when you click Check.
    check_every: int | None = None

    @field_validator("check_every", mode="before")
    @classmethod
    def _duration(cls, v):
        if v is None or v == "":
            return None
        seconds = parse_duration(v)
        if seconds < MIN_CHECK_INTERVAL:
            raise ValueError(f"check_every must be at least {MIN_CHECK_INTERVAL // 60} minutes")
        return seconds


class NotifyConfig(BaseModel):
    """Where to send a message when a scheduled check finds updates."""

    type: Literal["ntfy", "gotify", "discord", "telegram", "webhook"]
    name: str | None = None
    # ntfy: topic URL (https://ntfy.sh/my-topic); gotify: server URL; discord: webhook URL;
    # webhook: any URL that accepts a JSON POST. Not used for telegram.
    url: str | None = None
    # ntfy access token, Gotify application token, or Telegram bot token.
    token: str | None = None
    chat_id: str | None = None  # telegram

    @model_validator(mode="after")
    def _required(self) -> NotifyConfig:
        if self.type == "telegram":
            if not self.token or not self.chat_id:
                raise ValueError("telegram notifications need token and chat_id")
        elif not self.url:
            raise ValueError(f"{self.type} notifications need url")
        if self.type == "gotify" and not self.token:
            raise ValueError("gotify notifications need token")
        return self


class AppConfig(BaseModel):
    proxmox: list[ProxmoxConfig] = Field(default_factory=list)
    incus: list[IncusConfig] = Field(default_factory=list)
    hosts: list[HostConfig] = Field(default_factory=list)
    # Basic auth for the dashboard. Strongly recommended: the app holds root on your lab.
    auth: AuthConfig | None = None
    # How many targets may be checked/updated at the same time.
    max_parallel: int = Field(4, ge=1)
    # Adds fake targets so the UI can be tried without real infrastructure.
    demo: bool = False
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notify: list[NotifyConfig] = Field(default_factory=list)
    # The dashboard's address, linked from notifications (e.g. http://192.168.1.50:8080).
    public_url: str | None = None

    @model_validator(mode="after")
    def _unique_names(self) -> AppConfig:
        # Target ids and discovery errors are keyed by these names.
        names = [p.name for p in self.proxmox] + [i.name for i in self.incus]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"proxmox/incus names must be unique: {', '.join(dupes)}")
        hosts = [h.name for h in self.hosts]
        dupes = sorted({n for n in hosts if hosts.count(n) > 1})
        if dupes:
            raise ValueError(f"host names must be unique: {', '.join(dupes)}")
        return self


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value, missing: set[str]):
    """Expand ``${VAR}`` in parsed string values (never in raw YAML, so secrets can't break the syntax)."""
    if isinstance(value, str):

        def sub(m: re.Match) -> str:
            if m[1] not in os.environ:
                missing.add(m[1])
                return m[0]
            return os.environ[m[1]]

        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, missing) for k, v in value.items()}
    return value


def load_config(path: str | None = None) -> AppConfig:
    path = path or os.environ.get("HOMELAB_CONFIG", "config.yaml")
    p = Path(path)
    if not p.exists():
        log.warning("Config file %s not found; starting with no targets.", p)
        return AppConfig(demo=os.environ.get("HOMELAB_DEMO", "") == "1")
    missing: set[str] = set()
    data = _expand(yaml.safe_load(p.read_text()) or {}, missing)
    if missing:
        raise ValueError(f"{p}: environment variable(s) not set: {', '.join(sorted(missing))}")
    cfg = AppConfig.model_validate(data)
    if os.environ.get("HOMELAB_DEMO") == "1":
        cfg.demo = True
    for ssh in _ssh_configs(cfg):
        if not ssh.known_hosts:
            log.warning("SSH host key checking is off for %s; set known_hosts to enable it.", ssh.host)
    return cfg


def _ssh_configs(cfg: AppConfig) -> list[SSHConfig]:
    out: list[SSHConfig] = list(cfg.hosts)
    for p in cfg.proxmox:
        out += [p.ssh] if p.ssh else []
        out += p.node_ssh.values()
    out += [i.ssh for i in cfg.incus if i.ssh]
    return out

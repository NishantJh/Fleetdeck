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

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)


def normalize_fingerprint(value: str | None) -> str | None:
    """``AB:CD:...`` or ``abcd...`` (SHA-256, 64 hex digits) to lowercase hex without colons."""
    if value is None:
        return None
    fp = value.replace(":", "").replace(" ", "").lower()
    if len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        raise ValueError("tls_fingerprint must be a SHA-256 fingerprint (64 hex digits, colons optional)")
    return fp


class SSHConfig(BaseModel):
    host: str
    port: int = 22
    user: str = "root"
    key_file: str | None = None
    password: str | None = None
    # Path to a known_hosts file. Falls back to the top-level `known_hosts:`. With neither,
    # connections are refused unless insecure_skip_host_key_check is set.
    known_hosts: str | None = None
    # Accept any host key. Anyone on the network path can then pose as this machine.
    insecure_skip_host_key_check: bool = False
    # Wrap commands in `sudo -n` (for non-root users with passwordless sudo).
    sudo: bool = False


class ProxmoxConfig(BaseModel):
    name: str
    api_url: str  # e.g. https://192.168.1.10:8006
    token_id: str  # e.g. root@pam!homelab
    token_secret: str
    verify_ssl: bool | str = True  # bool or path to a CA bundle
    # SHA-256 fingerprint of the node's certificate (Node > System > Certificates, or
    # `openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256` on the node).
    # Pins that exact certificate; works with self-signed certs and IPs. Overrides verify_ssl.
    tls_fingerprint: str | None = None
    # SSH to the node: used to update the host itself and to run `pct exec` in LXCs.
    ssh: SSHConfig | None = None
    # Per-node SSH overrides for clusters, keyed by node name.
    node_ssh: dict[str, SSHConfig] = Field(default_factory=dict)
    # VMIDs to hide from the dashboard.
    exclude: list[int] = Field(default_factory=list)
    # VMIDs shown but never updated from the dashboard, e.g. the LXC/VM the dashboard runs in.
    # Upgrading it (Docker in particular) would restart the dashboard mid-upgrade.
    protect: list[int] = Field(default_factory=list)

    @field_validator("tls_fingerprint")
    @classmethod
    def _fingerprint(cls, v: str | None) -> str | None:
        return normalize_fingerprint(v)


class IncusConfig(BaseModel):
    name: str
    # Either a remote HTTPS URL with a trusted client certificate...
    url: str | None = None  # e.g. https://192.168.1.20:8443
    client_cert: str | None = None
    client_key: str | None = None
    verify_ssl: bool | str = True
    # SHA-256 fingerprint of the server certificate (`incus info | grep certificate_fingerprint`).
    # Pins that exact certificate. Overrides verify_ssl.
    tls_fingerprint: str | None = None
    # ...or the local unix socket when the dashboard runs on the Incus host.
    socket: str | None = None  # e.g. /var/lib/incus/unix.socket
    # SSH to the Incus host, used to update the host itself.
    ssh: SSHConfig | None = None
    # Instance names ("project/name" or "name") to hide.
    exclude: list[str] = Field(default_factory=list)

    @field_validator("tls_fingerprint")
    @classmethod
    def _fingerprint(cls, v: str | None) -> str | None:
        return normalize_fingerprint(v)

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
    # known_hosts file for every SSH connection that doesn't name its own.
    known_hosts: str | None = None

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

    @model_validator(mode="after")
    def _inherit_known_hosts(self) -> AppConfig:
        if self.known_hosts:
            for ssh in _ssh_configs(self):
                if not ssh.known_hosts and not ssh.insecure_skip_host_key_check:
                    ssh.known_hosts = self.known_hosts
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
        if ssh.insecure_skip_host_key_check:
            log.warning("SSH host key checking is off for %s (insecure_skip_host_key_check).", ssh.host)
        elif not ssh.known_hosts:
            log.warning(
                "No known_hosts for %s: connections to it will be refused. Set known_hosts (top-level or "
                "per host), or insecure_skip_host_key_check: true.",
                ssh.host,
            )
    for api in [*cfg.proxmox, *(i for i in cfg.incus if i.url)]:
        if api.verify_ssl is False and not api.tls_fingerprint:
            log.warning(
                "TLS certificate checking is off for %s: anyone on the network path can read its API "
                "credentials. Set tls_fingerprint (or verify_ssl to a CA file).",
                api.name,
            )
    return cfg


def _ssh_configs(cfg: AppConfig) -> list[SSHConfig]:
    out: list[SSHConfig] = list(cfg.hosts)
    for p in cfg.proxmox:
        out += [p.ssh] if p.ssh else []
        out += p.node_ssh.values()
    out += [i.ssh for i in cfg.incus if i.ssh]
    return out

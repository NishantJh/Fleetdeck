"""Configuration loading.

The config is a YAML file (default ``config.yaml``, override with ``FLEETDECK_CONFIG``).
``${VAR}`` references are expanded from the environment so secrets can stay out of
the file.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .envvars import env

log = logging.getLogger(__name__)


def normalize_fingerprint(value: str | None) -> str | None:
    """``AB:CD:...`` or ``abcd...`` (SHA-256, 64 hex digits) to lowercase hex without colons."""
    if value is None:
        return None
    fp = value.replace(":", "").replace(" ", "").lower()
    if len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        raise ValueError("tls_fingerprint must be a SHA-256 fingerprint (64 hex digits, colons optional)")
    return fp


# A machine's own update command (e.g. omv-upgrade) runs as root with sh -c; one line keeps what the confirm
# dialog shows the same as what runs.
MAX_UPDATE_COMMAND = 1000


def check_update_command(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("the update command is empty")
    if len(v) > MAX_UPDATE_COMMAND:
        raise ValueError(f"the update command is longer than {MAX_UPDATE_COMMAND} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("the update command must be one line, without control characters")
    return v


def check_update_commands(v: dict | None) -> dict[str, str]:
    """``{105: "omv-upgrade", "nas": "..."}`` (VMIDs come from YAML as numbers) to str keys, commands checked."""
    return {str(k): check_update_command(str(c)) for k, c in (v or {}).items()}


class HostBackupConfig(BaseModel):
    """Where this machine's backups are. Reading them never writes or locks anything; the dashboard's
    "Back up" button also uses this as the place to put the machine's config (see backup_runs.py)."""

    # path: the newest file under `path` (a folder rsync/tar/etc. write to, or one backup file).
    # restic / borg: the newest snapshot/archive in `repository`.
    type: Literal["path", "restic", "borg"]
    path: str | None = None
    # Repository as restic -r / borg take it. May be left out if env_file sets it
    # (RESTIC_REPOSITORY / BORG_REPO).
    repository: str | None = None
    # A file on the machine with the repository's settings as VAR=value lines, e.g. RESTIC_PASSWORD_FILE
    # or BORG_PASSPHRASE; read before restic/borg runs, so the password stays on the machine.
    env_file: str | None = None
    # Flag the backup once it's older than this many hours.
    max_age_hours: int = Field(48, ge=1)
    # Extra folders the dashboard's "Back up" button includes next to /etc (see backup_runs.py).
    include: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _where(self) -> HostBackupConfig:
        if self.type == "path" and not self.path:
            raise ValueError("backup type path needs path")
        if self.type != "path" and not (self.repository or self.env_file):
            raise ValueError(f"backup type {self.type} needs repository (or an env_file that sets it)")
        # A relative path would be read from the SSH user's home folder, and "-x" would reach find as
        # an option.
        for key in ("path", "env_file"):
            value = getattr(self, key)
            if value and not value.startswith("/"):
                raise ValueError(f"backup {key} must be a full path starting with /, not {value!r}")
        for value in self.include:
            if not value.startswith("/"):
                raise ValueError(f"backup include must be full paths starting with /, not {value!r}")
        return self


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
    # Where this machine's own backups are, for the "Last backup" column.
    backup: HostBackupConfig | None = None

    @property
    def host_key_problem(self) -> str | None:
        """Why connections to this machine would be refused, for the dashboard to show up front."""
        if self.known_hosts or self.insecure_skip_host_key_check:
            return None
        return f"SSH host key unknown: add {self.host} to known_hosts (ssh-keyscan, see README)"


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
    # Run this command instead of the built-in OS upgrade, keyed by VMID, guest name or node name,
    # e.g. {105: omv-upgrade}. Runs as root inside the guest (or on the node).
    update_commands: dict[str, str] = Field(default_factory=dict)
    # VMIDs shown but never updated from the dashboard, e.g. the LXC/VM the dashboard runs in.
    # Upgrading it (Docker in particular) would restart the dashboard mid-upgrade.
    protect: list[int] = Field(default_factory=list)
    # VMIDs that are only monitored: checked and reported, but never updated from the dashboard (e.g. an appliance
    # with its own updater). Unlike protect, reboots, snapshots and the node's own reboot stay allowed.
    monitor_only: list[int] = Field(default_factory=list)
    # Flag a guest whose newest backup is older than this many hours. 0 turns backup tracking off.
    # Reading backups needs Datastore.Audit, Datastore.AllocateSpace and VM.Backup.
    backup_max_age_hours: int = Field(48, ge=0)

    @field_validator("tls_fingerprint")
    @classmethod
    def _fingerprint(cls, v: str | None) -> str | None:
        return normalize_fingerprint(v)

    @field_validator("update_commands", mode="before")
    @classmethod
    def _update_commands(cls, v):
        return check_update_commands(v)


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
    # Instance names ("project/name" or "name") that are only monitored, never updated from the dashboard.
    monitor_only: list[str] = Field(default_factory=list)
    # Run this command instead of the built-in OS upgrade, keyed by instance name ("project/name" or "name")
    # or this entry's name for the host itself. Runs as root.
    update_commands: dict[str, str] = Field(default_factory=dict)
    # Flag an instance whose newest snapshot or export is older than this many hours. 0 turns it off.
    backup_max_age_hours: int = Field(48, ge=0)

    @field_validator("tls_fingerprint")
    @classmethod
    def _fingerprint(cls, v: str | None) -> str | None:
        return normalize_fingerprint(v)

    @field_validator("update_commands", mode="before")
    @classmethod
    def _update_commands(cls, v):
        return check_update_commands(v)

    @model_validator(mode="after")
    def _url_or_socket(self) -> IncusConfig:
        if not self.url and not self.socket:
            raise ValueError(f"incus '{self.name}': set either url or socket")
        return self


class TunnelConfig(BaseModel):
    """A WireGuard link the dashboard dials out to a machine it can't reach directly (see tunnels.py).

    The machine's SSH is then at 127.0.0.1:local_port, which is what host/port of the entry say."""

    endpoint: str  # the machine's public address
    port: int = Field(51821, ge=1, le=65535)  # its WireGuard UDP port
    target_port: int = Field(22, ge=1, le=65535)  # its sshd port
    private_key: str
    peer_public_key: str
    address: str  # the dashboard's tunnel IP
    peer_address: str  # the machine's tunnel IP
    local_port: int = Field(ge=1024, le=65535)


class HostConfig(SSHConfig):
    """A plain machine reached over SSH (e.g. a Raspberry Pi)."""

    name: str
    tunnel: TunnelConfig | None = None
    # Only monitor this machine: checked and reported, never updated from the dashboard.
    monitor_only: bool = False
    # Run this command instead of the built-in OS upgrade, e.g. omv-upgrade on OpenMediaVault. Runs as root.
    update_command: str | None = None

    @field_validator("update_command")
    @classmethod
    def _update_command(cls, v: str | None) -> str | None:
        return None if v is None else check_update_command(v)


def check_username(v: str) -> str:
    # Basic auth splits user and password at the first ":", so such a name could never log in.
    if ":" in v or any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("the username can't contain ':' or control characters")
    return v


class AuthConfig(BaseModel):
    username: str = Field("admin", min_length=1)
    # Either the password itself (e.g. ${FLEETDECK_PASSWORD}), or a hash of it as the setup page writes it.
    password: str | None = Field(None, min_length=1)
    password_hash: str | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        return check_username(v)

    @model_validator(mode="after")
    def _one_password(self) -> AuthConfig:
        if not self.password and not self.password_hash:
            raise ValueError("auth needs password (or password_hash)")
        return self


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
    # Include container checks in scheduled rounds. Native checks pull images; Dockhand uses registry metadata.
    check_docker: bool = False
    # After each check, install the updates from security repositories (apt only) on every machine that has
    # some: hosts first, then their containers and VMs. Never reboots. Off by default.
    auto_security_updates: bool = False

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


class ConsoleConfig(BaseModel):
    """Run commands on machines from the dashboard (see docs/CONSOLE.md). Off unless switched on here."""

    enabled: bool = False
    # The interactive terminal, on machines whose connection can give one. false keeps only Run command.
    terminal: bool = True
    # Client addresses allowed to use it, after trusted_proxies is applied: loopback, private networks and
    # 100.64.0.0/10 (Tailscale, Headscale, NetBird).
    allow_from: list[str] = Field(
        default_factory=lambda: [
            "127.0.0.0/8",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "fc00::/7",
            "100.64.0.0/10",
        ]
    )
    # Works over plain http:// from the allowed networks, under a warning; false blocks it there.
    allow_plain_http: bool = True
    unlock_minutes: int = Field(15, ge=1, le=60)
    keep_days: int = Field(90, ge=1)
    # Notify when a guarded command runs.
    notify: bool = True
    # Machine names or ids that never get it.
    exclude: list[str] = Field(default_factory=list)

    @field_validator("allow_from")
    @classmethod
    def _networks(cls, v: list[str]) -> list[str]:
        import ipaddress

        for n in v:
            try:
                ipaddress.ip_network(n, strict=False)
            except ValueError:
                raise ValueError(f"console.allow_from: {n!r} is not an address range such as 192.168.0.0/16") from None
        return v


class DockhandEnvironment(BaseModel):
    target_id: str = Field(min_length=1, max_length=512)
    environment_id: int = Field(ge=1)
    allow_updates: bool = False


class DockhandConfig(BaseModel):
    """Cached reports, optional checks, and explicitly allowed per-machine deployments."""

    url: str = Field(max_length=2048)
    token: str = Field(min_length=1, max_length=4096, repr=False)
    poll_seconds: int = Field(300, ge=60, le=86400)
    # Allow manual or scheduled registry checks; ordinary report polling remains read-only.
    allow_checks: bool = False
    environments: list[DockhandEnvironment] = Field(min_length=1, max_length=32)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError("Dockhand URL must not contain whitespace or control characters")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Dockhand URL must be HTTP(S), without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Dockhand URL must not contain a query or fragment")
        # Validate the port now, rather than failing later inside the status API.
        _ = parsed.port
        return value.rstrip("/")

    @field_validator("token")
    @classmethod
    def _token(cls, value: str) -> str:
        if any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("Dockhand token must be a printable token without whitespace")
        return value

    @field_validator("environments")
    @classmethod
    def _unique(cls, values: list[DockhandEnvironment]) -> list[DockhandEnvironment]:
        if len({v.target_id for v in values}) != len(values) or len({v.environment_id for v in values}) != len(values):
            raise ValueError("Each Dockhand environment and Fleetdeck target may be mapped only once")
        return values


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
    # Snapshot Proxmox/Incus guests before upgrading them (the default of the checkbox in the
    # Update dialog), and how many of the dashboard's own snapshots to keep per guest.
    snapshot_before_upgrade: bool = True
    snapshot_keep: int = Field(3, ge=1)
    # Update containers made by the Proxmox VE Helper-Scripts (community-scripts) with the app's own
    # update, as typing `update` in them would, instead of the OS upgrade; the OS upgrade only runs if
    # that fails. A plain OS upgrade can leave such an app broken. On by default; set false and the job only warns.
    # It runs the latest upstream script as root and can upgrade the app itself.
    community_scripts_update: bool = True
    # Independent of native Docker management. Configured target IDs always block native jobs.
    dockhand: DockhandConfig | None = None
    # An `age` public key (age1...). When set, the host config archives made by the "Back up" button are
    # encrypted to it (.tar.gz.age); only the matching private key, which the dashboard never has, opens them.
    backup_age_recipient: str | None = None

    @field_validator("backup_age_recipient")
    @classmethod
    def _age_key(cls, v: str | None) -> str | None:
        if v and not re.fullmatch(r"age1[a-z0-9]{50,}", v.strip()):
            raise ValueError("backup_age_recipient must be an age public key (it starts with age1)")
        return v.strip() if v else None

    # One password for the host config archives saved on the dashboard: they are written as age files
    # (.tar.gz.age) that `age -d` opens with it. Archives written on the machine can't use it (`age` only
    # takes a password from a terminal); set backup_age_recipient for those. Use ${VAR} to keep it out of the file.
    backup_password: str | None = None

    @field_validator("backup_password")
    @classmethod
    def _password(cls, v: str | None) -> str | None:
        if v and len(v) < 8:
            raise ValueError("backup_password must be at least 8 characters")
        return v or None

    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notify: list[NotifyConfig] = Field(default_factory=list)
    # The dashboard's address, linked from notifications (e.g. http://192.168.1.50:8080).
    public_url: str | None = None
    # SQLite file for check results and job history, so a restart doesn't lose them.
    # Unset keeps everything in memory. FLEETDECK_STATE overrides it (the Docker image sets /data/state.db).
    state_file: str | None = None
    # Folder, as the dashboard sees it, that the "Back up" button fills when a machine is set to save on the
    # dashboard: one subfolder per machine. FLEETDECK_BACKUP_FOLDER overrides it. Unset: `host-backups` next
    # to the state file (in the data volume). Mount a NAS or other folder there to keep them elsewhere.
    backup_folder: str | None = None
    # known_hosts file for every SSH connection that doesn't name its own.
    known_hosts: str | None = None
    # Running without `auth:` against real machines must be asked for explicitly.
    allow_no_auth: bool = False
    # Host names/IPs the dashboard is reached by (e.g. ["192.168.1.50", "homelab.lan"]). Requests
    # for any other Host header are refused, which stops DNS-rebinding attacks. Empty allows any.
    allowed_hosts: list[str] = Field(default_factory=list)
    # Reverse proxies whose X-Forwarded-For is believed (IPs or CIDRs). Without it, everyone behind a
    # proxy shares the proxy's address, so the login lockout would lock them all out together.
    trusted_proxies: list[str] = Field(default_factory=list)
    console: ConsoleConfig = Field(default_factory=ConsoleConfig)

    @property
    def dashboard_backup_folder(self) -> Path | None:
        """Where "save on the dashboard" backups go (see backup_folder), None with no state file either."""
        if self.backup_folder:
            return Path(self.backup_folder)
        return Path(self.state_file).parent / "host-backups" if self.state_file else None

    @property
    def has_real_targets(self) -> bool:
        return bool(self.proxmox or self.incus or self.hosts)

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
            name = m[1]
            if name not in os.environ:
                missing.add(name)
                return m[0]
            return os.environ[name]

        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, missing) for k, v in value.items()}
    return value


def unreadable(path: str | None) -> str | None:
    """A message if ``path`` exists but this process can't read it (e.g. the container's non-root uid)."""
    if not path or not os.path.exists(path) or os.access(path, os.R_OK):
        return None
    uid = os.getuid() if hasattr(os, "getuid") else "?"
    return f"can't read {path} as uid {uid}; on the Docker host run: sudo chown -R 10001:10001 config"


# Where configs lived before the setup page: a read-only volume of hand-written files.
LEGACY_CONFIG = "/config/config.yaml"


def config_path() -> str:
    """The config file to use: ``FLEETDECK_CONFIG``, else ``config.yaml``, else an older install's /config."""
    path = env("CONFIG", "config.yaml")
    if not os.path.exists(path) and os.path.exists(LEGACY_CONFIG):
        log.info("Using %s (from an older install; the setup page can't edit it).", LEGACY_CONFIG)
        return LEGACY_CONFIG
    return path


def validate_raw(data: dict | None, origin: str = "config") -> AppConfig:
    """Validate parsed YAML, expanding ``${VAR}`` references first."""
    missing: set[str] = set()
    data = _expand(data or {}, missing)
    if missing:
        raise ValueError(f"{origin}: environment variable(s) not set: {', '.join(sorted(missing))}")
    return AppConfig.model_validate(data)


def load_config(path: str | None = None) -> AppConfig:
    path = path or config_path()
    p = Path(path)
    if not p.exists():
        log.info("Config file %s not found yet; open /setup to create it.", p)
        return AppConfig(
            demo=env("DEMO", "") == "1", state_file=env("STATE") or None, backup_folder=env("BACKUP_FOLDER") or None
        )
    if problem := unreadable(str(p)):
        raise ValueError(problem)
    cfg = validate_raw(yaml.safe_load(p.read_text()), str(p))
    if env("DEMO") == "1":
        cfg.demo = True
    if env("CONSOLE") == "1":
        cfg.console.enabled = True
    if env("BACKUP_FOLDER"):
        cfg.backup_folder = env("BACKUP_FOLDER")
    if env("STATE"):
        cfg.state_file = env("STATE")
    elif cfg.state_file and not Path(cfg.state_file).is_absolute():
        # Relative to the config file, like the other paths people put next to it.
        cfg.state_file = str(p.parent / cfg.state_file)
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
    files = [f for ssh in _ssh_configs(cfg) for f in (ssh.key_file, ssh.known_hosts)]
    files += [f for i in cfg.incus for f in (i.client_cert, i.client_key)]
    for problem in dict.fromkeys(filter(None, map(unreadable, files))):
        log.error("%s", problem)
    return cfg


def _ssh_configs(cfg: AppConfig) -> list[SSHConfig]:
    out: list[SSHConfig] = list(cfg.hosts)
    for p in cfg.proxmox:
        out += [p.ssh] if p.ssh else []
        out += p.node_ssh.values()
    out += [i.ssh for i in cfg.incus if i.ssh]
    return out

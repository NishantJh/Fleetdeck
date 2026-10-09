"""The setup page's back end: the app's own credentials, per-machine bootstrap scripts, and
connecting to a machine for the first time.

Setup works the same for every machine. A short script, pasted on the machine as root, creates a
``homelab`` user that logs in with the app's own SSH key and has passwordless sudo. Everything else
is done by the app over that SSH connection: it pins the host key on first contact, and for Proxmox
creates a least-privilege API token and for Incus trusts the app's client certificate, pinning
both APIs' certificates. Every file the app writes lives next to its config, so the one data volume
is the whole install.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import logging
import os
import re
import shlex
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import asyncssh
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import BaseModel, Field, field_validator

from .config import AppConfig, SSHConfig, normalize_fingerprint, validate_raw
from .executors import CONNECT_TIMEOUT, SSHExecutor

log = logging.getLogger(__name__)

SSH_USER = "homelab"
KEY_COMMENT = "homelab-manager"
PVE_USER = "homelab@pve"
PVE_ROLE = "HomelabManager"
# Everything the dashboard uses (README, "What each platform needs"). Guest agent exec is
# VM.GuestAgent.Unrestricted on PVE 9 and VM.Monitor on PVE 8.
PVE_PRIVS = "VM.Audit Sys.Audit Sys.Modify Datastore.Audit VM.Snapshot VM.Snapshot.Rollback"
INCUS_PORT = 8443
HEADER = "# Written by Homelab Manager's setup page (/setup), where it can also be edited.\n"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")


def _address(value: str) -> str:
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value.strip("[]")))
    except ValueError:
        pass
    if not _HOST.match(value):
        raise ValueError("enter an IP address or a host name")
    return value


class Machine(BaseModel):
    kind: Literal["proxmox", "incus", "host"]
    name: str
    address: str
    port: int = Field(22, ge=1, le=65535)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("use letters, digits, dot, dash or underscore")
        return v

    @field_validator("address")
    @classmethod
    def _addr(cls, v: str) -> str:
        return _address(v)


def parse_allow_from(value: str) -> list[str]:
    """The addresses the dashboard connects from, for ``from=`` in authorized_keys."""
    out = []
    for part in re.split(r"[\s,]+", value.strip()):
        if not part:
            continue
        try:
            out.append(str(ipaddress.ip_network(part, strict=False)))
        except ValueError:
            raise ValueError(f"not an IP address or CIDR range: {part}") from None
    return out


@dataclass
class Paths:
    """Where the setup page keeps the config and the credentials it generates."""

    config: Path

    @property
    def keys(self) -> Path:
        return self.config.parent / "keys"

    @property
    def ssh_key(self) -> Path:
        return self.keys / "id_ed25519"

    @property
    def known_hosts(self) -> Path:
        return self.keys / "known_hosts"

    @property
    def incus_cert(self) -> Path:
        return self.keys / "incus-client.crt"

    @property
    def incus_key(self) -> Path:
        return self.keys / "incus-client.key"

    def writable(self) -> bool:
        d = self.config.parent
        if self.config.exists():
            return os.access(self.config, os.W_OK) and os.access(d, os.W_OK)
        return d.is_dir() and os.access(d, os.W_OK)


def _write_private(path: Path, data: bytes | str) -> None:
    """Write a file only this user can read, atomically."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


_identity_lock = threading.Lock()


def ensure_identity(paths: Paths) -> str:
    """Create the app's SSH key and Incus client certificate if missing; return the SSH public key."""
    with _identity_lock:
        return _ensure_identity(paths)


def _ensure_identity(paths: Paths) -> str:
    if not paths.ssh_key.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment=KEY_COMMENT)
        _write_private(paths.ssh_key, key.export_private_key())
        _write_private(paths.ssh_key.with_suffix(".pub"), key.export_public_key())
        log.info("Created the dashboard's SSH key %s", paths.ssh_key)
    if not paths.incus_cert.exists() or not paths.incus_key.exists():
        key = ec.generate_private_key(ec.SECP384R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, KEY_COMMENT)])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .sign(key, hashes.SHA384())
        )
        _write_private(
            paths.incus_key,
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ),
        )
        _write_private(paths.incus_cert, cert.public_bytes(serialization.Encoding.PEM))
    return asyncssh.read_private_key(str(paths.ssh_key)).export_public_key().decode().strip()


def incus_cert_fingerprint(paths: Paths) -> str:
    cert = x509.load_pem_x509_certificate(paths.incus_cert.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()


# --- the script pasted on each machine -------------------------------------------------------

_BOOTSTRAP = r"""# Homelab Manager: let the dashboard manage this machine ({name}).
# Creates the user {user}, which logs in only with the dashboard's key and has passwordless sudo.
set -eu
U={user}
KEY={key}
if ! command -v sudo >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq sudo
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q sudo
  elif command -v yum >/dev/null 2>&1; then yum install -y -q sudo
  elif command -v apk >/dev/null 2>&1; then apk add -q sudo
  elif command -v pacman >/dev/null 2>&1; then pacman -S --noconfirm sudo
  elif command -v zypper >/dev/null 2>&1; then zypper -q install -y sudo
  else echo "Install sudo first, then run this again." >&2; exit 1; fi
fi
# Never take over somebody's existing account of that name; one this script made has its sudoers file.
if id "$U" >/dev/null 2>&1 && [ ! -f /etc/sudoers.d/homelab-manager ]; then
  echo "A user called $U already exists and wasn't made by this script; not touching it." >&2; exit 1
fi
if ! id "$U" >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then useradd --create-home --shell /bin/sh --comment "Homelab Manager" "$U"
  else adduser -D -s /bin/sh -g "Homelab Manager" "$U"; fi
fi
# No password at all, but not "locked": sshd refuses key logins to locked accounts on some systems.
if command -v usermod >/dev/null 2>&1; then usermod -p '*' "$U"; else sed -i "s/^$U:!*:/$U:*:/" /etc/shadow; fi
H=$(getent passwd "$U" | cut -d: -f6)
G=$(id -gn "$U")
install -d -m 700 -o "$U" -g "$G" "$H/.ssh"
printf '%s\n' "$KEY" > "$H/.ssh/authorized_keys"
chown "$U:$G" "$H/.ssh/authorized_keys"
chmod 600 "$H/.ssh/authorized_keys"
mkdir -p /etc/sudoers.d
T=$(mktemp)
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$U" > "$T"
if command -v visudo >/dev/null 2>&1 && ! visudo -cqf "$T"; then echo "sudoers check failed" >&2; rm -f "$T"; exit 1; fi
chmod 440 "$T"
mv "$T" /etc/sudoers.d/homelab-manager
if command -v sshd >/dev/null 2>&1 && sshd -T 2>/dev/null | grep -qiE '^(allowusers|allowgroups) '; then
  echo
  echo "WARNING: sshd only lets some users log in (AllowUsers/AllowGroups in /etc/ssh/sshd_config)."
  echo "Add $U there and reload sshd, or the dashboard can't connect."
fi
echo
echo "Done. This machine's SSH host keys (compare with what the setup page shows after Connect):"
for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f" 2>/dev/null || true; done
"""


def bootstrap_script(machine: Machine, public_key: str, allow_from: list[str]) -> str:
    """The commands to paste on ``machine``, as root or as a user with sudo."""
    options = ["restrict"]
    if allow_from:
        options.append(f'from="{",".join(allow_from)}"')
    key_line = f"{','.join(options)} {public_key}"
    body = _BOOTSTRAP.format(name=machine.name, user=SSH_USER, key=shlex.quote(key_line))
    # One block that works pasted into any shell: root runs it directly, anyone else through sudo.
    return (
        "f=$(mktemp) && cat > \"$f\" <<'HOMELAB_SETUP'\n"
        f"{body}"
        "HOMELAB_SETUP\n"
        'if [ "$(id -u)" -eq 0 ]; then sh "$f"; else sudo sh "$f"; fi; r=$?; rm -f "$f"; [ "$r" -eq 0 ]\n'
    )


# --- connecting ------------------------------------------------------------------------------


class SetupError(Exception):
    pass


@dataclass
class ConnectResult:
    ok: bool
    log: list[str] = field(default_factory=list)
    fingerprint: str | None = None
    # Set when the machine's host key differs from the one pinned earlier.
    host_key_changed: bool = False
    # Set when the key isn't pinned yet: nothing was done, the user has to confirm the fingerprint.
    needs_confirm: bool = False


# The key type pinned: the one `ssh-keygen -lf` users most likely compare, then the rest.
HOST_KEY_ALGS = [
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
]


def _known_hosts_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _pinned_keys(paths: Paths, host: str, port: int) -> list[asyncssh.SSHKey]:
    if not paths.known_hosts.exists():
        return []
    name = _known_hosts_name(host, port)
    keys = []
    for line in paths.known_hosts.read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == name:
            try:
                keys.append(asyncssh.import_public_key(parts[1]))
            except (asyncssh.KeyImportError, ValueError):
                pass
    return keys


def forget_host_key(paths: Paths, host: str, port: int) -> None:
    if not paths.known_hosts.exists():
        return
    name = _known_hosts_name(host, port)
    lines = paths.known_hosts.read_text().splitlines()
    kept = [ln for ln in lines if ln.split(None, 1)[:1] != [name]]
    if len(kept) != len(lines):
        _write_private(paths.known_hosts, "\n".join(kept) + ("\n" if kept else ""))


def _pin_host_key(paths: Paths, host: str, port: int, key: asyncssh.SSHKey) -> None:
    name = _known_hosts_name(host, port)
    lines = paths.known_hosts.read_text().splitlines() if paths.known_hosts.exists() else []
    lines = [ln for ln in lines if ln.split(None, 1)[:1] != [name]]
    lines.append(f"{name} {key.export_public_key().decode().strip()}")
    _write_private(paths.known_hosts, "\n".join(lines) + "\n")


async def fetch_host_key(host: str, port: int) -> asyncssh.SSHKey:
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(host, port, server_host_key_algs=HOST_KEY_ALGS), CONNECT_TIMEOUT
        )
    except (OSError, TimeoutError, asyncssh.Error) as e:
        raise SetupError(
            f"can't reach SSH on {host}:{port} ({e or type(e).__name__}). Check the address and port, and that "
            "this machine's firewall lets the dashboard in."
        ) from None
    if key is None:
        raise SetupError(f"{host}:{port} sent no host key")
    return key


def ssh_config(machine: Machine, paths: Paths) -> SSHConfig:
    return SSHConfig(
        host=machine.address,
        port=machine.port,
        user=SSH_USER,
        key_file=str(paths.ssh_key),
        known_hosts=str(paths.known_hosts),
        sudo=True,
    )


async def _run(ssh: SSHExecutor, script: str, what: str) -> str:
    res = await ssh.run(script, timeout=120)
    if res.rc != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()[-3:]
        raise SetupError(f"{what} failed: {' / '.join(detail) or f'exit code {res.rc}'}")
    return res.stdout


def pve_token(machine_name: str) -> str:
    """One token per added node: nodes of one cluster share their users and tokens, so a shared
    token name would have each Connect revoke the token the previous node got."""
    return "hm-" + re.sub(r"[^A-Za-z0-9_-]", "-", machine_name)


def pve_script(token: str) -> str:
    return f"""set -e
pveum role add {PVE_ROLE} -privs VM.Audit >/dev/null 2>&1 || true
pveum role modify {PVE_ROLE} -privs "{PVE_PRIVS} VM.GuestAgent.Unrestricted" 2>/dev/null \\
  || pveum role modify {PVE_ROLE} -privs "{PVE_PRIVS} VM.Monitor"
pveum user add {PVE_USER} --comment "Homelab Manager" >/dev/null 2>&1 || true
pveum acl modify / --users {PVE_USER} --roles {PVE_ROLE}
# A token's secret is shown only once, so make a new one (revoking the old) each time.
pveum user token remove {PVE_USER} {token} >/dev/null 2>&1 || true
pveum user token add {PVE_USER} {token} --privsep 0 --comment "Homelab Manager" --output-format json
# The certificate the API serves: a custom/ACME one (pveproxy-ssl.pem) if uploaded, else PVE's own.
if [ -f /etc/pve/local/pveproxy-ssl.pem ]; then echo "__CUSTOM__=1"; f=/etc/pve/local/pveproxy-ssl.pem
else f=/etc/pve/local/pve-ssl.pem; fi
echo "__FP__=$(openssl x509 -in "$f" -noout -fingerprint -sha256)"
"""


def parse_pve(stdout: str) -> tuple[str, str, bool]:
    """(token secret, certificate fingerprint, whether it's a custom cert) from :func:`pve_script`'s output."""
    secret = fp = None
    custom = "__CUSTOM__=1" in stdout
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                secret = json.loads(line).get("value") or secret
            except ValueError:
                pass
        elif line.startswith("__FP__="):
            fp = line.rpartition("=")[2]
    if not secret:
        raise SetupError("Proxmox didn't return an API token (is this a Proxmox VE node?)")
    if not fp:
        raise SetupError("couldn't read the Proxmox certificate")
    return secret, normalize_fingerprint(fp) or "", custom


def incus_listen(address: str) -> str:
    """Where to open the Incus API: only on the address the dashboard uses, when that is an IP."""
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return f":{INCUS_PORT}"
    return f"{_url_host(address)}:{INCUS_PORT}"


def incus_script(cert_pem: str, cert_fp: str, listen: str = f":{INCUS_PORT}") -> str:
    return f"""set -e
command -v incus >/dev/null 2>&1 || {{ echo "incus is not installed here" >&2; exit 1; }}
addr=$(incus config get core.https_address)
if [ -z "$addr" ]; then incus config set core.https_address {listen}; addr="{listen}"; echo "__OPENED__=1"; fi
echo "__ADDR__=$addr"
if ! incus query /1.0/certificates/{cert_fp} >/dev/null 2>&1; then
  f=$(mktemp)
  cat > "$f" <<'EOF'
{cert_pem.strip()}
EOF
  incus config trust add-certificate "$f" --name {KEY_COMMENT} --restricted --projects default
  rm -f "$f"
fi
echo "__FP__=$(incus info | sed -n 's/^ *certificate_fingerprint: *//p' | head -n1)"
"""


def parse_incus(stdout: str) -> tuple[int, str]:
    """(API port, server certificate fingerprint) from :func:`incus_script`'s output."""
    port, fp = INCUS_PORT, None
    for line in stdout.splitlines():
        if line.startswith("__ADDR__="):
            tail = line.rpartition(":")[2]
            port = int(tail) if tail.isdigit() else INCUS_PORT
        elif line.startswith("__FP__="):
            fp = line.partition("=")[2].strip()
    if not fp:
        raise SetupError("couldn't read the Incus server certificate fingerprint")
    return port, normalize_fingerprint(fp) or ""


async def connect(
    machine: Machine, paths: Paths, trust_fingerprint: str | None = None
) -> tuple[ConnectResult, dict | None]:
    """Connect to a machine the bootstrap script has run on; return the result and its config entry.

    A host key that isn't pinned yet is only reported (``needs_confirm``): nothing is pinned or
    changed until the call is repeated with ``trust_fingerprint`` set to that key's fingerprint, after
    the user compared it with what the script printed on the machine.
    """
    res = ConnectResult(ok=False)
    try:
        key = await fetch_host_key(machine.address, machine.port)
        res.fingerprint = key.get_fingerprint("sha256")
        pinned = _pinned_keys(paths, machine.address, machine.port)
        same = any(k.public_data == key.public_data for k in pinned)
        if not same and trust_fingerprint != res.fingerprint:
            if pinned:
                res.host_key_changed = True
                raise SetupError(
                    f"the SSH host key of {machine.address} has changed since it was first seen (now "
                    f"{res.fingerprint}). If you reinstalled the machine, trust the new key; otherwise something "
                    "is impersonating it."
                )
            res.needs_confirm = True
            raise SetupError(
                f"{machine.address} presents SSH host key {res.fingerprint}. Check that it is one of the fingerprints "
                "the setup script printed on the machine, then confirm."
            )
        if not same:
            _pin_host_key(paths, machine.address, machine.port, key)
            res.log.append(f"Pinned SSH host key {res.fingerprint}")
        else:
            res.log.append(f"SSH host key matches the pinned one ({res.fingerprint})")

        ssh = SSHExecutor(ssh_config(machine, paths))
        try:
            who = (await ssh.run("id -un", timeout=60)).stdout.strip()
        except asyncssh.PermissionDenied:
            raise SetupError(
                f"{machine.address} refused the dashboard's key for user {SSH_USER}. Run the setup script on it "
                "first (and if sshd has AllowUsers, add homelab there)."
            ) from None
        except (OSError, asyncssh.Error) as e:
            raise SetupError(f"SSH to {machine.address} failed: {e}") from None
        if who != "root":
            raise SetupError(
                f"sudo didn't give root on {machine.address} (got {who or 'nothing'}). Re-run the script; if sudoers "
                "has `Defaults requiretty` (older RHEL/CentOS), remove it for homelab."
            )
        res.log.append(f"Logged in as {SSH_USER}, sudo works")

        entry: dict = {"name": machine.name}
        ssh_entry = {
            "host": machine.address,
            "port": machine.port,
            "user": SSH_USER,
            "key_file": str(paths.ssh_key),
            "sudo": True,
        }
        if machine.kind == "proxmox":
            token = pve_token(machine.name)
            secret, fp, custom = parse_pve(await _run(ssh, pve_script(token), "creating the Proxmox API token"))
            res.log.append(f"Created API token {PVE_USER}!{token} (role {PVE_ROLE}) and pinned the API certificate")
            if custom:
                res.log.append(
                    "Note: this node serves a custom (e.g. ACME) certificate, which is pinned exactly. After it "
                    "renews, press Reconnect here to pin the new one."
                )
            entry |= {
                "api_url": f"https://{_url_host(machine.address)}:8006",
                "token_id": f"{PVE_USER}!{token}",
                "token_secret": secret,
                "tls_fingerprint": fp,
                "ssh": ssh_entry,
            }
        elif machine.kind == "incus":
            pem = paths.incus_cert.read_text()
            listen = incus_listen(machine.address)
            out = await _run(ssh, incus_script(pem, incus_cert_fingerprint(paths), listen), "trusting Incus")
            port, fp = parse_incus(out)
            if "__OPENED__=1" in out:
                where = "on every interface" if listen.startswith(":") else f"on {listen} only"
                res.log.append(f"Turned on the Incus API {where}")
            res.log.append(f"Incus API on port {port} trusts the dashboard (project default); pinned its certificate")
            entry |= {
                "url": f"https://{_url_host(machine.address)}:{port}",
                "client_cert": str(paths.incus_cert),
                "client_key": str(paths.incus_key),
                "tls_fingerprint": fp,
                "ssh": ssh_entry,
            }
        else:
            entry |= ssh_entry
        res.ok = True
        return res, entry
    except SetupError as e:
        res.log.append(str(e))
        return res, None


def _url_host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


# --- the config file -------------------------------------------------------------------------

SECTION = {"proxmox": "proxmox", "incus": "incus", "host": "hosts"}


def read_raw(paths: Paths) -> dict:
    if not paths.config.exists():
        return {}
    try:
        data = yaml.safe_load(paths.config.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{paths.config} is not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{paths.config} must be a mapping of settings")
    return data


def write_raw(paths: Paths, data: dict) -> AppConfig:
    """Validate and save the config; raises ValueError (with pydantic's message) if it's invalid."""
    cfg = validate_raw(data, str(paths.config))
    text = HEADER + yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
    _write_private(paths.config, text)
    return cfg


def write_text(paths: Paths, text: str) -> AppConfig:
    """Save hand-edited YAML from the setup page, after checking it."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {e}") from None
    if data is not None and not isinstance(data, dict):
        raise ValueError("the config must be a mapping of settings")
    cfg = validate_raw(data, str(paths.config))
    if cfg.auth is None and not cfg.allow_no_auth:
        raise ValueError("keep the auth: section, or nobody could log in")
    _write_private(paths.config, text if text.endswith("\n") else text + "\n")
    return cfg


def new_config(paths: Paths, username: str, password_hash: str) -> dict:
    return {
        "auth": {"username": username, "password_hash": password_hash},
        "known_hosts": str(paths.known_hosts),
        "schedule": {"check_every": "6h"},
        "snapshot_before_upgrade": True,
        "proxmox": [],
        "incus": [],
        "hosts": [],
    }


def upsert(data: dict, kind: str, entry: dict) -> dict:
    """Add or replace the machine called ``entry['name']``, also removing that name from other sections."""
    for k, section in SECTION.items():
        items = [m for m in data.get(section) or [] if m.get("name") != entry["name"]]
        if k == kind:
            items.append(entry)
        data[section] = items
    return data


def remove(data: dict, name: str) -> dict | None:
    """Remove the machine called ``name``; return its entry (None if there was none)."""
    found = None
    for section in SECTION.values():
        items = data.get(section) or []
        found = found or next((m for m in items if m.get("name") == name), None)
        data[section] = [m for m in items if m.get("name") != name]
    return found


def ssh_endpoints(data: dict) -> set[tuple[str, int]]:
    """(host, port) of every SSH connection in a raw config."""
    out = set()
    for section in SECTION.values():
        for m in data.get(section) or []:
            ssh = m if section == "hosts" else m.get("ssh") or {}
            if ssh.get("host"):
                out.add((str(ssh["host"]), int(ssh.get("port") or 22)))
    return out


def machines(cfg: AppConfig) -> list[dict]:
    """The configured machines, as the setup page lists them."""
    from urllib.parse import urlsplit

    out = []
    for p in cfg.proxmox:
        addr = p.ssh.host if p.ssh else urlsplit(p.api_url).hostname
        out.append({"kind": "proxmox", "name": p.name, "address": addr, "port": p.ssh.port if p.ssh else 22})
    for i in cfg.incus:
        addr = i.ssh.host if i.ssh else (urlsplit(i.url).hostname if i.url else "local socket")
        out.append({"kind": "incus", "name": i.name, "address": addr, "port": i.ssh.port if i.ssh else 22})
    for h in cfg.hosts:
        out.append({"kind": "host", "name": h.name, "address": h.host, "port": h.port})
    return out

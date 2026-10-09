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
import base64
import contextlib
import datetime as dt
import ipaddress
import json
import logging
import os
import re
import shlex
import socket
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
from pydantic import BaseModel, Field, field_validator, model_validator

from . import tunnels
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
    if "%" in value:
        raise ValueError("link-local addresses with a zone (%eth0) don't work here; use another address")
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
    # Reach it through a WireGuard tunnel the dashboard dials out (see tunnels.py), on this UDP port.
    tunnel: bool = False
    udp_port: int = Field(tunnels.DEFAULT_UDP_PORT, ge=1, le=65535)

    @model_validator(mode="after")
    def _tunnel_hosts_only(self) -> Machine:
        if self.tunnel and self.kind != "host":
            raise ValueError("a tunnel is only offered for Linux hosts")
        return self

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

# Is a TCP/UDP port free on this machine? (`port_free tcp 8443`) Never take one something else uses.
PORT_FREE_SH = r"""port_free() {
  h=$(printf '%04X' "$2"); st=0A; [ "$1" = udp ] && st=07
  for f in /proc/net/$1 /proc/net/${1}6; do
    if [ -r "$f" ] && awk -v h="$h" -v st="$st" \
      'NR > 1 { n = split($2, a, ":"); if (a[n] == h && $4 == st) f = 1 } END { exit !f }' "$f"; then return 1; fi
  done
  return 0
}
"""


_CHECKS = r"""# Homelab Manager: let the dashboard manage this machine ({name}).
# It creates the user {user}, which logs in only with the dashboard's key and has passwordless sudo.
# First it checks everything and changes nothing; then it lists the changes and makes them.
set -eu
# `su root -c` (not a login shell) keeps the user's PATH, which lacks the sbin folders on Debian.
PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
U={user}
KEY={key}
SUDOERS=/etc/sudoers.d/homelab-manager
stop() {{ echo >&2; echo "Stopped; nothing was changed. $*" >&2; exit 1; }}
{port_free}# Installs package $1 only when that upgrades or removes nothing already installed.
apt_ok() {{
  out=$(apt-get -s install -y --no-install-recommends "$1" 2>/dev/null) || return 1
  printf '%s\n' "$out" | grep -q "^Inst $1 " || return 1
  if printf '%s\n' "$out" | grep -Eq '^(Inst [^ ]+ \[|Remv )'; then return 1; fi
}}
pkg_hint() {{
  for pm in "dnf install" "yum install" "apk add" "pacman -S" "zypper install" "apt-get install"; do
    if command -v "${{pm%% *}}" >/dev/null 2>&1; then echo "$pm $1"; return; fi
  done
  echo "your package manager"
}}

# --- checks ---
[ "$(id -u)" -eq 0 ] || stop "This needs root."
for nas in /etc/unraid-version /etc/synoinfo.conf; do
  if [ -f "$nas" ]; then echo "WARNING: this looks like Unraid or Synology, which reset /etc or don't read"
    echo "/etc/sudoers.d; the user may vanish on reboot or update. Managing it over SSH isn't supported."; fi
done
if [ -f /etc/version ] && grep -qi truenas /etc/version 2>/dev/null; then
  echo "WARNING: TrueNAS resets users and sudoers on upgrades; you may need to run this again after one."
fi
if [ "$(cat /proc/sys/crypto/fips_enabled 2>/dev/null || echo 0)" = 1 ]; then
  stop "FIPS mode is on, which refuses the dashboard's ed25519 key, so this machine can't be managed."
fi
# Never take over somebody's account of that name (local, or from LDAP/SSSD); ours has its sudoers file.
if id "$U" >/dev/null 2>&1; then
  [ -f "$SUDOERS" ] || stop "A user called $U already exists and wasn't made by this script."
  grep -q "^$U:" /etc/passwd || stop "The user $U comes from a directory service (LDAP/SSSD), not this machine."
fi
# What sshd allows for this user (Match blocks included), when it can tell.
S=""
if command -v sshd >/dev/null 2>&1; then
  S=$(sshd -T -C "user=$U,host=homelab-manager,addr={addr}" 2>/dev/null || sshd -T 2>/dev/null || true)
fi
v() {{ printf '%s\n' "$S" | sed -n "s/^$1 //p" | tr '\n' ' ' | sed 's/ $//'; }}
am=$(v authenticationmethods)
case " $am " in
  "  "|*" any "*|*" publickey "*) ;;
  *) stop "sshd wants more than a key to log in (AuthenticationMethods $am). Add these two lines at the
end of /etc/ssh/sshd_config, reload sshd, then run this again:
Match User $U
    AuthenticationMethods publickey" ;;
esac
akf=$(v authorizedkeysfile)
case " $akf " in
  "  "|*" .ssh/authorized_keys "*|*"/.ssh/authorized_keys "*) ;;
  *) stop "sshd reads keys only from $akf, not ~/.ssh/authorized_keys. Add .ssh/authorized_keys to
AuthorizedKeysFile in /etc/ssh/sshd_config (or use Match User $U), reload sshd, then run this again." ;;
esac
au=$(v allowusers)
if [ -n "$au" ]; then
  case " $au " in
    *" $U "*|*" $U@"*) ;;
    *) stop "sshd only lets some users log in (AllowUsers $au). Add $U to AllowUsers in
/etc/ssh/sshd_config, reload sshd, then run this again." ;;
  esac
fi
case " $(v denyusers) " in
  *" $U "*) stop "sshd's DenyUsers lists $U; take it out, reload sshd, then run this again." ;;
esac
ag=$(v allowgroups)
INSTALL=""
if ! command -v sudo >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1 && apt_ok sudo; then INSTALL="sudo"
  elif command -v apt-get >/dev/null 2>&1; then
    stop "sudo isn't installed, and apt can't add it on its own (out-of-date package lists, or it would
upgrade other packages). Run
  apt-get update && apt-get install sudo
(on Proxmox without a subscription, enable the no-subscription repository under Updates > Repositories
first), then run this again."
  else stop "sudo isn't installed. Install it ($(pkg_hint sudo)), then run this again."
  fi
elif ! command -v visudo >/dev/null 2>&1; then
  stop "sudo is installed but visudo isn't, so the sudo rule can't be checked."
elif ! grep -Eqs '^[[:space:]]*[#@]includedir[[:space:]]+/etc/sudoers\.d/?[[:space:]]*$' /etc/sudoers; then
  stop "sudo here doesn't read /etc/sudoers.d (no includedir line in /etc/sudoers), so it can't be set up."
fi
"""

_CHANGES = r"""
# --- changes ---
echo "Checks passed. Making these changes:"
if [ -n "$INSTALL" ]; then echo "  - install the package(s): $INSTALL"; fi
{plan}if id "$U" >/dev/null 2>&1; then echo "  - update the user $U (made by this script before)"
else echo "  - add the user $U (no password, logs in only with the dashboard's key)"; fi
echo "  - $SUDOERS: passwordless sudo for $U"
echo
if [ -n "$INSTALL" ]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends -o DPkg::Lock::Timeout=120 $INSTALL
fi
{changes}if ! id "$U" >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then useradd --create-home --shell /bin/sh --comment "Homelab Manager" "$U"
  else adduser -D -s /bin/sh -g "Homelab Manager" "$U"; fi
fi
# No password at all, but not "locked": sshd refuses key logins to locked accounts on some systems.
if command -v usermod >/dev/null 2>&1; then usermod -p '*' "$U"
else
  cp -p /etc/shadow /etc/shadow.homelab-bak && sed -i "s/^$U:!*:/$U:*:/" /etc/shadow && rm -f /etc/shadow.homelab-bak
fi
H=$(getent passwd "$U" | cut -d: -f6)
G=$(id -gn "$U")
install -d -m 700 -o "$U" -g "$G" "$H/.ssh"
printf '%s\n' "$KEY" > "$H/.ssh/authorized_keys"
chown "$U:$G" "$H/.ssh/authorized_keys"
chmod 600 "$H/.ssh/authorized_keys"
if command -v restorecon >/dev/null 2>&1; then restorecon -R "$H/.ssh" 2>/dev/null || true; fi
# The rule is written next to its final place under a dotted name (which sudo skips), checked, moved in,
# and the whole sudo configuration checked again; if that fails it is taken out again.
mkdir -p /etc/sudoers.d
T=$(mktemp /etc/sudoers.d/.homelab-manager.XXXXXX)
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$U" > "$T"
chmod 440 "$T"
if command -v visudo >/dev/null 2>&1 && ! visudo -cqf "$T"; then
  rm -f "$T"; echo "The sudoers rule didn't check out." >&2; exit 1
fi
OLD=""
if [ -f "$SUDOERS" ]; then OLD=$(mktemp /etc/sudoers.d/.homelab-manager-old.XXXXXX); cp -p "$SUDOERS" "$OLD"; fi
mv -f "$T" "$SUDOERS"
if command -v restorecon >/dev/null 2>&1; then restorecon "$SUDOERS" 2>/dev/null || true; fi
if command -v visudo >/dev/null 2>&1 && ! visudo -cq; then
  if [ -n "$OLD" ]; then mv -f "$OLD" "$SUDOERS"; else rm -f "$SUDOERS"; fi
  echo "sudo's configuration doesn't check out with the rule added, so it was taken out again." >&2; exit 1
fi
if [ -n "$OLD" ]; then rm -f "$OLD"; fi
if ! sudo -l -U "$U" 2>/dev/null | grep -q NOPASSWD; then
  echo "sudo doesn't give $U passwordless sudo (it ignores $SUDOERS here); the dashboard needs that." >&2; exit 1
fi
if [ -n "$ag" ]; then
  echo
  echo "WARNING: sshd only lets members of some groups log in (AllowGroups $ag)."
  echo "Add $U to one of them (usermod -aG <group> $U), or the dashboard can't connect."
fi
echo
echo "Done. This machine's SSH host keys (compare with what the setup page shows after Connect):"
for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f" 2>/dev/null || true; done
"""

# Runs the script as root from any login shell (sh, bash, zsh, fish, nushell, csh): the script travels as
# base64, and sh picks sudo, doas or su. No `!` (csh history), backslashes (fish) or newlines (csh).
_WRAPPER = (
    'sh -c \'S=$1; if [ "$(id -u)" = 0 ]; then printf %s "$S" | base64 -d | sh; '
    'elif command -v sudo >/dev/null 2>&1; then printf %s "$S" | base64 -d | sudo sh; '
    'elif command -v doas >/dev/null 2>&1; then printf %s "$S" | base64 -d | doas sh; '
    'else echo "Enter the root password:" >&2; su root -c "printf %s $S | base64 -d | sh"; fi\' homelab-setup '
)


def wrap(script: str) -> str:
    """One line to paste into a shell on the machine, as root or as a user with sudo, doas or su."""
    # The leading space keeps it out of the history in bash (HISTCONTROL=ignorespace/ignoreboth, Debian's
    # default) and zsh with histignorespace; it carries keys.
    return " " + _WRAPPER + base64.b64encode(script.encode()).decode() + "\n"


def bootstrap_source(machine: Machine, public_key: str, allow_from: list[str], tunnel: dict | None = None) -> str:
    """What the setup script runs on ``machine`` (as root). With ``tunnel`` (from :func:`tunnels.pending`) it
    also sets up the machine's end of the tunnel, and the key only works through it."""
    if tunnel is not None:
        allow_from = [f"10.99.{tunnel['index']}.2/32"]
    options = ["restrict"]
    if allow_from:
        options.append(f'from="{",".join(allow_from)}"')
    key_line = f"{','.join(options)} {public_key}"
    # The address sshd's Match blocks are checked against: one the dashboard will connect from.
    addr = allow_from[0].split("/")[0] if allow_from else "192.0.2.1"
    checks = _CHECKS.format(
        name=machine.name, user=SSH_USER, key=shlex.quote(key_line), addr=addr, port_free=PORT_FREE_SH
    )
    plan = changes = ""
    if tunnel is not None:
        checks += tunnels.vps_checks(tunnel)
        plan = tunnels.vps_plan(tunnel)
        changes = tunnels.vps_changes(tunnel, machine.port)
    return checks + _CHANGES.format(plan=plan, changes=changes)


def bootstrap_script(machine: Machine, public_key: str, allow_from: list[str], tunnel: dict | None = None) -> str:
    return wrap(bootstrap_source(machine, public_key, allow_from, tunnel))


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


def _pin_host_key(paths: Paths, host: str, port: int, *keys: asyncssh.SSHKey) -> None:
    name = _known_hosts_name(host, port)
    lines = paths.known_hosts.read_text().splitlines() if paths.known_hosts.exists() else []
    lines = [ln for ln in lines if ln.split(None, 1)[:1] != [name]]
    lines += [f"{name} {key.export_public_key().decode().strip()}" for key in keys]
    _write_private(paths.known_hosts, "\n".join(lines) + "\n")


async def fetch_host_key(host: str, port: int) -> asyncssh.SSHKey:
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(host, port, server_host_key_algs=HOST_KEY_ALGS), CONNECT_TIMEOUT
        )
    except socket.gaierror:
        hint = " (.local names usually don't resolve inside a container)" if host.endswith(".local") else ""
        raise SetupError(f"can't resolve {host} from the dashboard{hint}; enter its IP address instead") from None
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


def _pve_token_base(machine_name: str) -> str:
    return "hm-" + re.sub(r"[^A-Za-z0-9_-]", "-", machine_name)


def pve_token(machine_name: str, now: dt.datetime | None = None) -> str:
    """A new token name for an added node, unique each time. One per node: nodes of one cluster share
    their users and tokens, so they must not replace each other's."""
    stamp = (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%d%H%M%S")
    return f"{_pve_token_base(machine_name)}-{stamp}"


def stale_pve_tokens(machine_name: str, token_list_json: str, keep: set[str]) -> list[str]:
    """This node's earlier tokens (from `pveum user token list --output-format json`), except ``keep``."""
    mine = re.compile(re.escape(_pve_token_base(machine_name)) + r"(-\d{14})?")
    try:
        tokens = [t.get("tokenid", "") for t in json.loads(token_list_json or "[]") if isinstance(t, dict)]
    except ValueError:
        return []
    return [t for t in tokens if mine.fullmatch(t) and t not in keep]


def pve_script(token: str) -> str:
    return f"""set -e
pveum role add {PVE_ROLE} -privs VM.Audit >/dev/null 2>&1 || true
pveum role modify {PVE_ROLE} -privs "{PVE_PRIVS} VM.GuestAgent.Unrestricted" 2>/dev/null \\
  || pveum role modify {PVE_ROLE} -privs "{PVE_PRIVS} VM.Monitor"
pveum user add {PVE_USER} --comment "Homelab Manager" >/dev/null 2>&1 || true
pveum acl modify / --users {PVE_USER} --roles {PVE_ROLE}
# A token's secret is shown only once, so make a new one each time; the old one keeps working until
# the dashboard no longer uses it, and goes on the next Connect.
pveum user token add {PVE_USER} {token} --privsep 0 --comment "Homelab Manager" --output-format json
# The certificate the API serves: a custom/ACME one (pveproxy-ssl.pem) if uploaded, else PVE's own.
if [ -f /etc/pve/local/pveproxy-ssl.pem ]; then echo "__CUSTOM__=1"; f=/etc/pve/local/pveproxy-ssl.pem
else f=/etc/pve/local/pve-ssl.pem; fi
echo "__FP__=$(openssl x509 -in "$f" -noout -fingerprint -sha256)"
# Cluster members and their SSH host keys, which the cluster keeps in /etc/pve (read over this pinned
# connection, so they can be trusted without asking about each node).
echo "__CLUSTER__=$(pvesh get /cluster/status --output-format json 2>/dev/null | tr -d '\n')"
for d in /etc/pve/nodes/*/; do
  n=$(basename "$d")
  if [ -f "$d/ssh_known_hosts" ]; then sed "s/^/__NODEKEY__=$n /" "$d/ssh_known_hosts"; fi
done
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


def parse_cluster(stdout: str) -> list[tuple[str, str, list[asyncssh.SSHKey]]]:
    """(name, IP, host keys) of the other nodes of the cluster, from :func:`pve_script`'s output."""
    nodes: list = []
    keys: dict[str, list[asyncssh.SSHKey]] = {}
    for line in stdout.splitlines():
        if line.startswith("__CLUSTER__="):
            try:
                nodes = json.loads(line.partition("=")[2] or "[]")
            except ValueError:
                nodes = []
        elif line.startswith("__NODEKEY__="):
            parts = line.partition("=")[2].split()
            if len(parts) >= 4:  # node, host pattern, key type, key data
                try:
                    keys.setdefault(parts[0], []).append(asyncssh.import_public_key(f"{parts[2]} {parts[3]}"))
                except (asyncssh.KeyImportError, ValueError):
                    pass
    return [
        (n["name"], n["ip"], keys.get(n["name"], []))
        for n in nodes
        if isinstance(n, dict) and n.get("type") == "node" and not n.get("local") and n.get("name") and n.get("ip")
    ]


async def _remove_stale_tokens(ssh: SSHExecutor, machine_name: str, keep: set[str], res: ConnectResult) -> None:
    """Revoke this node's older tokens, but never one the running dashboard (or the new config) uses."""
    listed = await ssh.run(f"pveum user token list {PVE_USER} --output-format json", timeout=60)
    if listed.rc != 0:
        return
    for t in stale_pve_tokens(machine_name, listed.stdout, keep):
        if (await ssh.run(f"pveum user token remove {PVE_USER} {shlex.quote(t)}", timeout=60)).rc == 0:
            res.log.append(f"Revoked the old token {PVE_USER}!{t}")


async def _check_cluster_nodes(machine: Machine, paths: Paths, stdout: str, res: ConnectResult) -> None:
    """Pin the other cluster nodes' host keys and check the dashboard can log in to each: it reaches
    them at their cluster IP for `pct exec` and host upgrades."""
    for name, ip, keys in parse_cluster(stdout):
        if not keys:
            res.log.append(f"Cluster node {name} ({ip}): no SSH host key in /etc/pve; add it as its own machine")
            continue
        _pin_host_key(paths, ip, machine.port, *keys)
        node = machine.model_copy(update={"address": ip})
        try:
            ok = (await SSHExecutor(ssh_config(node, paths)).run("id -un", timeout=60)).stdout.strip() == "root"
        except (OSError, asyncssh.Error):
            ok = False
        state = (
            "reachable, sudo works" if ok else "can't log in as homelab yet; run the setup script there, then Reconnect"
        )
        res.log.append(f"Cluster node {name} ({ip}): {state}")


def incus_listen(address: str) -> str:
    """The host part to open the Incus API on: only the address the dashboard uses, when that is an IP."""
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return ""
    return _url_host(address)


def incus_script(cert_pem: str, cert_fp: str, listen_host: str = "") -> str:
    # LXD has the same API under the `lxc` command; only adding a trusted certificate is spelled differently.
    return f"""set -e
if command -v incus >/dev/null 2>&1; then C=incus; ADD="add-certificate"
elif command -v lxc >/dev/null 2>&1 && lxc query /1.0 >/dev/null 2>&1; then C=lxc; ADD="add"
else echo "neither Incus nor LXD is installed here" >&2; exit 1; fi
{PORT_FREE_SH}addr=$($C config get core.https_address)
opened=""
# If a later step fails, turn the API back off when this script turned it on: leave things as they were.
trap 'if [ $? -ne 0 ] && [ -n "$opened" ]; then $C config unset core.https_address; fi' EXIT
if [ -z "$addr" ]; then
  # 8443 is often taken (UniFi, other web UIs): use the next free port rather than fight over it.
  p={INCUS_PORT}
  while ! port_free tcp $p; do
    p=$((p + 1))
    if [ $p -gt {INCUS_PORT + 20} ]; then
      echo "no free TCP port between {INCUS_PORT} and {INCUS_PORT + 20}" >&2; exit 1
    fi
  done
  if [ $p -ne {INCUS_PORT} ]; then echo "__PORTNOTE__=port {INCUS_PORT} is already in use here, so the API uses $p"; fi
  addr="{listen_host}:$p"
  $C config set core.https_address "$addr"
  opened=1
  echo "__OPENED__=1"
fi
case "$addr" in
  127.*|localhost*|"[::1]"*|::1*)
    echo "The API only listens on $addr (core.https_address); set it to this machine's address." >&2; exit 1 ;;
esac
echo "__ADDR__=$addr"
# Trusted for every project that exists now; press Reconnect after adding one.
names=$($C query /1.0/projects | grep -o '"/1.0/projects/[^"]*"' | sed 's#^"/1.0/projects/##; s#"$##')
[ -n "$names" ] || names=default
echo "__PROJECTS__=$(printf '%s\\n' "$names" | paste -sd, -)"
if $C config trust show {cert_fp} >/dev/null 2>&1; then
  # Already trusted: only update its projects (never remove it, which would cut off the dashboard).
  json=$(printf '"%s"\\n' $names | paste -sd, -)
  $C query -X PATCH /1.0/certificates/{cert_fp} -d "{{\\"restricted\\": true, \\"projects\\": [$json]}}" >/dev/null
else
  f=$(mktemp)
  cat > "$f" <<'CERT'
{cert_pem.strip()}
CERT
  $C config trust $ADD "$f" --name {KEY_COMMENT} --restricted --projects "$(printf '%s\\n' "$names" | paste -sd, -)" \\
    || {{ rm -f "$f"; exit 1; }}
  rm -f "$f"
fi
echo "__FP__=$($C info | sed -n 's/^ *certificate_fingerprint: *//p' | head -n1)"
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
    machine: Machine, paths: Paths, trust_fingerprint: str | None = None, keep_tokens: frozenset[str] = frozenset()
) -> tuple[ConnectResult, dict | None]:
    """Connect to a machine the bootstrap script has run on; return the result and its config entry.

    A host key that isn't pinned yet is only reported (``needs_confirm``): nothing is pinned or
    changed until the call is repeated with ``trust_fingerprint`` set to that key's fingerprint, after
    the user compared it with what the script printed on the machine. ``keep_tokens`` are Proxmox token
    names still in use (by the running dashboard), which are not revoked.
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
                "first; it checks sshd's settings (AllowUsers, AuthenticationMethods, AuthorizedKeysFile) and says "
                "what to change. Failed logins count towards fail2ban or CrowdSec bans, so run the script before "
                "trying again."
            ) from None
        except (OSError, asyncssh.Error) as e:
            raise SetupError(f"SSH to {machine.address} failed: {e}") from None
        if who != "root":
            raise SetupError(
                f"sudo didn't give root on {machine.address} (got {who or 'nothing'}). Re-run the script; if sudoers "
                "has `Defaults requiretty` (older RHEL/CentOS), remove it for homelab."
            )
        res.log.append(f"Logged in as {SSH_USER}, sudo works")
        if await _shares_our_kernel(ssh):
            res.log.append(OWN_MACHINE[machine.kind])

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
            out = await _run(ssh, pve_script(token), "creating the Proxmox API token")
            secret, fp, custom = parse_pve(out)
            res.log.append(f"Created API token {PVE_USER}!{token} (role {PVE_ROLE}) and pinned the API certificate")
            await _remove_stale_tokens(ssh, machine.name, {token, *keep_tokens}, res)
            await _check_cluster_nodes(machine, paths, out, res)
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
                where = f"on {listen} only" if listen else "on every interface"
                res.log.append(f"Turned on the API {where}, port {port}")
            res.log += [ln.partition("=")[2] for ln in out.splitlines() if ln.startswith("__PORTNOTE__=")]
            projects = next((ln.partition("=")[2] for ln in out.splitlines() if ln.startswith("__PROJECTS__=")), "")
            res.log.append(
                f"API on port {port} trusts the dashboard for projects {projects or 'default'}; pinned its "
                "certificate. Press Reconnect after adding a project."
            )
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


OWN_MACHINE = {
    "proxmox": "WARNING: the dashboard runs on this node (in a container or VM sharing its kernel). Put the "
    "VMID of its LXC under protect: in the config editor, so an upgrade or reboot can't stop it mid-job.",
    "incus": "WARNING: the dashboard runs on this machine or in one of its containers. Put that container under "
    "protect: in the config editor, so an upgrade or reboot can't stop it mid-job.",
    "host": "WARNING: the dashboard runs on this machine. Upgrading Docker or rebooting it from the dashboard "
    "stops the dashboard mid-job; do those yourself.",
}


async def _shares_our_kernel(ssh: SSHExecutor) -> bool:
    """Whether the machine is the one the dashboard runs on: a container shares its host's kernel, and
    with it the boot ID."""
    try:
        ours = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        theirs = (await ssh.run("cat /proc/sys/kernel/random/boot_id", timeout=60)).stdout.strip()
    except (OSError, asyncssh.Error):
        return False
    return bool(ours) and ours == theirs


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


def uninstall_source(kind: str, entry: dict, incus_fp: str) -> str:
    """What undoes the setup on a machine: everything the setup script and Connect added for this machine."""
    parts = [
        f"""# Homelab Manager: undo the setup of {entry.get("name", "this machine")}.
set -u
U={SSH_USER}
SUDOERS=/etc/sudoers.d/homelab-manager
[ "$(id -u)" -eq 0 ] || {{ echo "This needs root." >&2; exit 1; }}
"""
    ]
    if kind == "proxmox":
        base = _pve_token_base(entry.get("name", ""))
        parts.append(
            f"""if command -v pveum >/dev/null 2>&1; then
  for t in $(pveum user token list {PVE_USER} --output-format json 2>/dev/null \\
      | grep -o '"tokenid" *: *"[^"]*"' | sed 's/.*"\\([^"]*\\)"$/\\1/' | grep -Ex '{base}(-[0-9]{{14}})?'); do
    pveum user token remove {PVE_USER} "$t" && echo "Revoked the API token {PVE_USER}!$t"
  done
  # The user and role are shared by every node of a cluster: they go with the last token.
  if [ "$(pveum user token list {PVE_USER} --output-format json 2>/dev/null | tr -d ' \\n')" = "[]" ]; then
    pveum user delete {PVE_USER} && pveum role delete {PVE_ROLE} && echo "Removed {PVE_USER} and the role {PVE_ROLE}"
  fi
fi
"""
        )
    elif kind == "incus":
        parts.append(
            f"""C=""
if command -v incus >/dev/null 2>&1; then C=incus; elif command -v lxc >/dev/null 2>&1; then C=lxc; fi
if [ -n "$C" ]; then
  $C config trust remove {incus_fp} 2>/dev/null && echo "Removed the dashboard's certificate from the trust list"
  echo "If nothing else uses the API ($($C config get core.https_address)), turn it off:"
  echo "  $C config unset core.https_address"
fi
"""
        )
    tunnel = entry.get("tunnel") or {}
    peer = str(tunnel.get("peer_address", ""))
    if peer.count(".") == 3 and peer.split(".")[2].isdigit():
        parts.append(tunnels.vps_uninstall(int(peer.split(".")[2]), tunnel["port"], tunnel.get("target_port", 22)))
    parts.append(
        """if [ -f "$SUDOERS" ]; then
  rm -f "$SUDOERS"
  if id "$U" >/dev/null 2>&1; then
    # Its SSH sessions from the dashboard first, or userdel refuses.
    pkill -u "$U" 2>/dev/null && sleep 1
    if command -v userdel >/dev/null 2>&1; then userdel -r "$U" 2>/dev/null || userdel "$U"
    else deluser --remove-home "$U" 2>/dev/null || deluser "$U"; fi
  fi
  if id "$U" >/dev/null 2>&1; then echo "Removed the sudo rule, but couldn't remove the user $U." >&2; exit 1; fi
  echo "Removed the user $U and its sudo rule."
else
  echo "There's no $U user made by Homelab Manager here."
fi
"""
    )
    return "".join(parts)


def find(data: dict, name: str) -> tuple[str, dict] | None:
    """(kind, entry) of the machine called ``name`` in a raw config."""
    for kind, section in SECTION.items():
        for m in data.get(section) or []:
            if m.get("name") == name:
                return kind, m
    return None


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
            for conn in [ssh, *(m.get("node_ssh") or {}).values()]:
                if isinstance(conn, dict) and conn.get("host"):
                    out.add((str(conn["host"]), int(conn.get("port") or 22)))
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
        if h.tunnel:
            t = h.tunnel
            out.append(
                {
                    "kind": "host",
                    "name": h.name,
                    "address": t.endpoint,
                    "port": t.target_port,
                    "tunnel": True,
                    "udp_port": t.port,
                }
            )
        else:
            out.append({"kind": "host", "name": h.name, "address": h.host, "port": h.port})
    return out


def used_tunnels(data: dict) -> set[int]:
    """Tunnel numbers taken by machines in a raw config."""
    out = set()
    for h in data.get("hosts") or []:
        addr = (h.get("tunnel") or {}).get("peer_address", "")
        if addr.count(".") == 3 and addr.split(".")[2].isdigit():
            out.add(int(addr.split(".")[2]))
    return out

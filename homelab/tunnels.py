"""WireGuard tunnels to machines the dashboard can't reach directly.

For a VPS that only accepts SSH from a few addresses, with the dashboard behind CGNAT (no fixed
public IP, no way in), the dashboard dials out: a small WireGuard link from the dashboard to the
VPS on its own UDP port, and SSH goes through it. The dashboard's side is wireproxy, a userspace
WireGuard client, so the container needs no extra privileges, devices or networking: each tunnel
is a local TCP port (127.0.0.1:22000+n) that leads to the VPS's sshd over the tunnel.

Tunnel n uses 10.99.n.0/30: the VPS is 10.99.n.1, the dashboard 10.99.n.2.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .config import TunnelConfig

log = logging.getLogger(__name__)

DEFAULT_UDP_PORT = 51821  # Pangolin (Gerbil) uses 51820
LOCAL_PORT_BASE = 22000
WIREPROXY = os.environ.get("WIREPROXY", "wireproxy")


def _keypair() -> tuple[str, str]:
    key = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    priv = key.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def tunnel_dir(keys: Path) -> Path:
    return keys / "tunnels"


def pending(keys: Path, name: str, endpoint: str, udp_port: int, target_port: int, used: set[int]) -> dict:
    """The tunnel for machine ``name``, created on first use and kept (same keys and subnet) after.

    It holds the VPS's private key too, because the setup script that configures the VPS is made
    from it (kept with the dashboard's other keys, removed with the machine). ``used`` are tunnel
    numbers already taken by connected machines.
    """
    from .onboarding import _write_private  # noqa: PLC0415 (avoids an import cycle)

    path = tunnel_dir(keys) / f"{name}.json"
    data = json.loads(path.read_text()) if path.exists() else None
    if data is None:
        taken = used | {t["index"] for t in _all_pending(keys)}
        index = next((i for i in range(1, 255) if i not in taken), None)
        if index is None:
            raise ValueError("no free tunnel subnet left in 10.99.0.0/16")
        priv, pub = _keypair()
        peer_priv, peer_pub = _keypair()
        data = {
            "index": index,
            "private_key": priv,
            "public_key": pub,
            "peer_private_key": peer_priv,
            "peer_public_key": peer_pub,
        }
    data |= {"endpoint": endpoint, "port": udp_port, "target_port": target_port}
    _write_private(path, json.dumps(data, indent=2))
    return data


def _all_pending(keys: Path) -> list[dict]:
    out = []
    for p in sorted(tunnel_dir(keys).glob("*.json")):
        with contextlib.suppress(ValueError, OSError):
            out.append(json.loads(p.read_text()))
    return out


def forget(keys: Path, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        (tunnel_dir(keys) / f"{name}.json").unlink()


def to_config(data: dict) -> TunnelConfig:
    """The dashboard's half, as stored in config.yaml (without the VPS's private key)."""
    n = data["index"]
    return TunnelConfig(
        endpoint=data["endpoint"],
        port=data["port"],
        target_port=data["target_port"],
        private_key=data["private_key"],
        peer_public_key=data["peer_public_key"],
        address=f"10.99.{n}.2",
        peer_address=f"10.99.{n}.1",
        local_port=LOCAL_PORT_BASE + n,
    )


def _endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def wireproxy_conf(t: TunnelConfig) -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {t.private_key}\n"
        f"Address = {t.address}/32\n\n"
        "[Peer]\n"
        f"PublicKey = {t.peer_public_key}\n"
        f"Endpoint = {_endpoint(t.endpoint, t.port)}\n"
        f"AllowedIPs = {t.peer_address}/32\n"
        # Keeps the NAT/CGNAT mapping open, so the link stays up with nothing listening at home.
        "PersistentKeepalive = 25\n\n"
        "[TCPClientTunnel]\n"
        f"BindAddress = 127.0.0.1:{t.local_port}\n"
        f"Target = {t.peer_address}:{t.target_port}\n"
    )


def vps_script(data: dict, ssh_port: int) -> str:
    """Shell commands for the VPS: its end of the tunnel, never on a UDP port or subnet in use."""
    n, port = data["index"], data["port"]
    iface = f"hm{n}"
    return f"""# --- WireGuard tunnel for the dashboard: UDP {port}, interface {iface}, 10.99.{n}.0/30 ---
WG_CONF=/etc/wireguard/{iface}.conf
if ! command -v wg-quick >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then apt-get update -qq || true; apt-get install -y -qq wireguard-tools
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q wireguard-tools
  elif command -v apk >/dev/null 2>&1; then apk add -q wireguard-tools
  elif command -v pacman >/dev/null 2>&1; then pacman -S --noconfirm wireguard-tools
  else echo "Install wireguard-tools first, then run this again." >&2; exit 1; fi
fi
ours=no; if [ -f "$WG_CONF" ] && grep -q "Homelab Manager" "$WG_CONF"; then ours=yes; fi
if [ -f "$WG_CONF" ] && [ "$ours" = no ]; then
  echo "$WG_CONF exists and isn't ours; not touching it." >&2; exit 1
fi
if [ "$ours" = yes ]; then wg-quick down {iface} >/dev/null 2>&1 || true; fi
if ! port_free udp {port}; then
  p={port}; while ! port_free udp $p; do p=$((p + 1)); done
  echo "UDP port {port} is already in use here (Pangolin uses 51820). Set the tunnel port to $p on the" >&2
  echo "setup page, then run the new script." >&2; exit 1
fi
if ip -o addr 2>/dev/null | grep -q " 10\\.99\\.{n}\\."; then
  echo "10.99.{n}.0/30 is already used on this machine; remove that address, then run this again." >&2; exit 1
fi
umask 077
mkdir -p /etc/wireguard
cat > "$WG_CONF" <<'WG'
# Homelab Manager: the dashboard's tunnel. Remove: wg-quick down {iface}; rm /etc/wireguard/{iface}.conf
[Interface]
Address = 10.99.{n}.1/30
ListenPort = {port}
PrivateKey = {data["peer_private_key"]}

[Peer]
PublicKey = {data["public_key"]}
AllowedIPs = 10.99.{n}.2/32
WG
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl enable --now wg-quick@{iface} >/dev/null
else
  wg-quick up {iface}
  echo "NOTE: no systemd; bring the tunnel up at boot yourself (wg-quick up {iface})."
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow {port}/udp comment "Homelab Manager tunnel" >/dev/null
  ufw allow in on {iface} to any port {ssh_port} proto tcp comment "Homelab Manager SSH" >/dev/null
  echo "ufw: allowed UDP {port} in, and SSH from the tunnel."
else
  echo "If a firewall runs here (nftables, firewalld, ...), allow UDP {port} in, and TCP {ssh_port} on {iface}."
fi
echo "Also allow UDP {port} in your VPS provider's firewall, if it has one."
# --- end of tunnel ---
"""


def local_port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class TunnelManager:
    """Keeps one wireproxy process per tunnel running (one per container, across in-process restarts)."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self._procs: dict[str, tuple[str, subprocess.Popen]] = {}
        self._lock = threading.Lock()

    def ensure(self, name: str, t: TunnelConfig, wait: float = 5.0) -> None:
        """Start (or restart, if its settings changed or it died) the tunnel; wait until it listens."""
        conf = wireproxy_conf(t)
        with self._lock:
            running = self._procs.get(name)
            if running and running[0] == conf and running[1].poll() is None:
                return
            if running:
                self._stop(name)
            if shutil.which(WIREPROXY) is None:
                raise RuntimeError("wireproxy is not installed (it is in the Docker image)")
            if not local_port_free(t.local_port):
                raise RuntimeError(f"local port {t.local_port} for the tunnel is in use")
            from .onboarding import _write_private  # noqa: PLC0415

            path = self.workdir / f"{name}.wireproxy.conf"
            _write_private(path, conf)
            proc = subprocess.Popen(  # noqa: S603 - fixed binary, our own config file
                [WIREPROXY, "--silent", "--config", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._procs[name] = (conf, proc)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode(errors="replace").strip() if proc.stderr else ""
                raise RuntimeError(f"tunnel {name} stopped: {err or f'exit code {proc.returncode}'}")
            if not local_port_free(t.local_port):
                return
            time.sleep(0.1)
        raise RuntimeError(f"tunnel {name} didn't start listening on 127.0.0.1:{t.local_port}")

    def sync(self, wanted: dict[str, TunnelConfig]) -> None:
        """Run exactly these tunnels."""
        with self._lock:
            for name in [n for n in self._procs if n not in wanted]:
                self._stop(name)
        for name, t in wanted.items():
            try:
                self.ensure(name, t)
            except RuntimeError as e:
                log.error("Tunnel for %s: %s", name, e)

    def _stop(self, name: str) -> None:
        _, proc = self._procs.pop(name)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self) -> None:
        with self._lock:
            for name in list(self._procs):
                self._stop(name)


_manager: TunnelManager | None = None


def manager(workdir: Path) -> TunnelManager:
    """The process-wide manager: tunnels outlive the in-process restarts the setup page triggers."""
    global _manager
    if _manager is None:
        _manager = TunnelManager(workdir)
    return _manager

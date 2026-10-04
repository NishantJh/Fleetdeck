# Homelab Manager

One dashboard for the whole homelab. The first feature is **OS updates**: see what's pending and
upgrade, from a single page, across:

| Platform | Host | Guests | How it reaches the guest |
| --- | --- | --- | --- |
| Proxmox VE | SSH to each node | LXCs | `pct exec` over the node's SSH |
| | | VMs | QEMU guest agent, via the Proxmox API |
| Incus | SSH | containers and VMs | Incus REST API `exec` (VMs need `incus-agent`) |
| Anything else (Raspberry Pi, etc.) | SSH | | |

Guests are discovered automatically from the Proxmox and Incus APIs, so new LXCs/VMs show up without
editing config. Each machine detects its own package manager: **apt** (Debian, Ubuntu, Proxmox,
Raspberry Pi OS), **dnf/yum**, **apk** (Alpine) and **pacman** (Arch).

* **Check** refreshes package lists and shows each pending package with old → new version, plus
  whether a reboot is needed.
* **Update** runs a full upgrade (`apt-get dist-upgrade`, `dnf upgrade`, `apk upgrade`,
  `pacman -Syu`), streams the output to the Activity panel, then re-checks. Nothing is rebooted.
* Select several rows and **Update selected** to do them in one go (`max_parallel` at a time,
  never two jobs on the same machine at once).

## Try it without any infrastructure

```bash
pip install -e ".[dev]"
HOMELAB_DEMO=1 python -m homelab   # http://localhost:8080
```

## Run it for real

```bash
mkdir config
cp config.example.yaml config/config.yaml   # fill in your hosts
cp ~/.ssh/id_ed25519 config/                # key authorised on the hosts
ssh-keyscan 192.168.1.10 192.168.1.20 192.168.1.30 > config/known_hosts
HOMELAB_PASSWORD=change-me PVE_TOKEN_SECRET=... docker compose up -d --build
```

### What each platform needs

* **Proxmox:** an API token (Datacenter → Permissions → API Tokens) with `VM.Audit`/`Sys.Audit` to
  list guests and guest-agent exec rights to update VMs (`VM.GuestAgent.Unrestricted` on PVE 9,
  `VM.Monitor` on PVE 8). VMs need the *QEMU Guest Agent* option on and `qemu-guest-agent`
  installed inside. SSH to the node is used for the host itself and for LXCs.
* **Incus:** expose the API (`incus config set core.https_address :8443`) and trust a client
  certificate (`incus config trust add-certificate client.crt`), or run the dashboard on the Incus
  host and point `socket:` at `/var/lib/incus/unix.socket`. SSH is used for the host itself.
* **Raspberry Pi / other hosts:** SSH. Non-root users need passwordless sudo and `sudo: true`.

### Security

This app can run commands as root on everything it manages. Keep it on your LAN or behind a VPN,
set `auth:` in the config, and set `known_hosts` so SSH host keys are verified.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

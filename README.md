# Homelab Manager

One dashboard for the whole homelab. The first feature is **OS updates**: see what's pending and
upgrade, from a single page, across:

| Platform | Host | Guests | How it reaches the guest |
| --- | --- | --- | --- |
| Proxmox VE | PVE API to check (uses PVE's own repositories), SSH to upgrade | LXCs | `pct exec` over the node's SSH |
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
* **Backups:** each Proxmox guest shows when it was last backed up (vzdump or Proxmox Backup
  Server), turns orange once that's older than `backup_max_age_hours` (48 by default), and is flagged
  *none*, *no job* (not in any scheduled backup job) or *verify failed*. This needs `Datastore.Audit`
  on the backup storages; without it the column shows `?` and says why. **Upgrading:** an existing
  API token most likely lacks it, so add the read-only `PVEAuditor` role on `/storage`
  (`pveum acl modify /storage --tokens 'root@pam!homelab' --roles PVEAuditor`, with your token id),
  or set `backup_max_age_hours: 0` to turn the column off.
* Select several rows and **Update selected** to do them in one go (`max_parallel` at a time,
  never two jobs on the same machine at once).

## Try it without any infrastructure

```bash
pip install -e ".[dev]"
HOMELAB_DEMO=1 python -m homelab   # http://localhost:8080
```

## Run it for real

Every push to `main` publishes `ghcr.io/nishantjh/homelab-manager:latest` (amd64 and arm64).

```bash
mkdir config
cp config.example.yaml config/config.yaml   # fill in your hosts
ssh-keygen -t ed25519 -f config/id_ed25519 -N "" -C homelab-manager   # see "Giving the dashboard its own SSH key"
ssh-keyscan 192.168.1.10 192.168.1.20 192.168.1.30 > config/known_hosts
HOMELAB_PASSWORD=change-me PVE_TOKEN_SECRET=... docker compose up -d
```

### With Arcane

1. Create a new project in Arcane and paste in `docker-compose.yml`.
2. Put `config.yaml`, the SSH key and `known_hosts` in a `config/` folder inside that project's
   directory on the Docker host, and set `HOMELAB_PASSWORD` and `PVE_TOKEN_SECRET` in the
   project's `.env`.
3. While the repo is private, the image is too: add `ghcr.io` as a registry in Arcane with your
   GitHub username and a token that has `read:packages`.
4. Add the VMID of the LXC that runs Docker/Arcane to `protect:` under your Proxmox entry. It still
   shows on the dashboard, but can't be updated from it, because upgrading Docker in that LXC would
   restart the dashboard halfway through. Update that one LXC from its own console.

### What each platform needs

* **Proxmox:** an API token (Datacenter → Permissions → API Tokens) with `VM.Audit`/`Sys.Audit` to
  list guests, `Datastore.Audit` on `/storage` to see backups, `Sys.Modify` on `/nodes` so the host check can refresh package lists, and
  guest-agent exec rights to update VMs (`VM.GuestAgent.Unrestricted` on PVE 9, `VM.Monitor` on
  PVE 8). VMs need the *QEMU Guest Agent* option on and `qemu-guest-agent` installed inside.
* **Proxmox host updates** are checked through PVE itself, the same as *Updates → Refresh* in the
  web UI, so they come from whatever is enabled under *Updates → Repositories*. The dashboard shows
  which repositories were used and warns if the Enterprise repo is on without a subscription.
  PVE's API has no non-interactive upgrade call (its Upgrade button opens a shell), so the upgrade
  runs `apt-get dist-upgrade` over SSH, against those same repositories. SSH is also used for LXCs.
* **Incus:** expose the API (`incus config set core.https_address :8443`) and trust a client
  certificate (`incus config trust add-certificate client.crt`), or run the dashboard on the Incus
  host and point `socket:` at `/var/lib/incus/unix.socket`. SSH is used for the host itself.
* **Raspberry Pi, VPS and other hosts:** SSH. Non-root users need passwordless sudo and `sudo: true`.

### Giving the dashboard its own SSH key

The dashboard runs in Docker, so it can't use the key on your own PC. Give it a key of its own;
that way you can revoke it without touching yours. Inside the `config/` folder on the Docker host:

```bash
ssh-keygen -t ed25519 -f id_ed25519 -N "" -C homelab-manager
ssh-copy-id -i id_ed25519.pub user@my-vps.example.com   # repeat for each machine
```

`ssh-copy-id` asks you to log in once (password, or a key this host already has). If only your
Windows PC can log in, copy `id_ed25519.pub` to the PC first, then run this in PowerShell:

```powershell
type id_ed25519.pub | ssh user@my-vps.example.com "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Then record each machine's host key, so the dashboard refuses to connect if it ever changes. Use
`-p <port>` if SSH isn't on port 22:

```bash
ssh-keyscan my-vps.example.com 192.168.1.10 >> known_hosts
ssh-keygen -lf known_hosts   # shows the fingerprints you just trusted
```

`ssh-keyscan` trusts whatever answers the first time it runs. For a machine on the internet, check
that the fingerprint matches the one shown by `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`
on the VPS itself, using your provider's web console.

For a VPS:
- Use a non-root user with passwordless sudo (`sudo: true`) and always set `known_hosts`.
- This key plus passwordless sudo is root on the VPS, so restrict where it can be used. Either
  reach the VPS only over Tailscale/WireGuard, or prefix its line in `~/.ssh/authorized_keys`
  with your home IP: `from="203.0.113.7" ssh-ed25519 AAAA... homelab-manager`.

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

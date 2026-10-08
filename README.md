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
  `pacman -Syu`), streams the output to the Activity panel, then re-checks. Nothing is rebooted unless you ask.
* Check results and the Activity history are saved to `data/state.db` (SQLite), so they survive a
  restart or an image update. A job that was running when the dashboard stopped shows as interrupted.
* **Reboot** reboots a machine, waits until it is back (its boot id changes), then re-checks it.
  Tick *Reboot afterwards if a reboot is needed* when updating to do both in one go. A Proxmox node
  that runs a `protect:`ed guest (the dashboard's own LXC) is never rebooted from here.
* **Scheduled checks:** set `schedule.check_every` (e.g. `6h`) and the dashboard checks everything on
  its own, then notifies you through ntfy, Gotify, Discord, Telegram or a JSON webhook when something
  new is pending (a new update, a new pending reboot, or a check that started failing). It never
  upgrades on its own. *Test notification* in the toolbar sends a test message.
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
chmod 600 config/id_ed25519 && mkdir -p data && sudo chown -R 10001:10001 config data   # the container runs as uid 10001
HOMELAB_PASSWORD=change-me PVE_TOKEN_SECRET=... docker compose up -d
```

The container runs as an unprivileged user (uid 10001) with a read-only filesystem and no Linux
capabilities, so the files in `config/` must be readable, and `data/` writable, by
that uid. Anything it can't read is logged at start-up with the `chown` to fix it, and shows on the
affected machine's check.

**Upgrading from an image that ran as root:** before pulling the new image, run
`mkdir -p data && sudo chown -R 10001:10001 config data` in the project folder, or the dashboard
can't read its config and won't start. To pin a version instead of `latest`, use a
release tag such as `ghcr.io/nishantjh/homelab-manager:0.2.0`.

### With Arcane

1. Create a new project in Arcane and paste in `docker-compose.yml`.
2. Put `config.yaml`, the SSH key and `known_hosts` in a `config/` folder inside that project's
   directory on the Docker host (check results and job history go in `data/` next to it), and set `HOMELAB_PASSWORD` and `PVE_TOKEN_SECRET` in the
   project's `.env`.
3. While the repo is private, the image is too: add `ghcr.io` as a registry in Arcane with your
   GitHub username and a token that has `read:packages`.
4. Add the VMID of the LXC that runs Docker/Arcane to `protect:` under your Proxmox entry. It still
   shows on the dashboard, but can't be updated from it, because upgrading Docker in that LXC would
   restart the dashboard halfway through. Update that one LXC from its own console.

### What each platform needs

* **Proxmox:** an API token (Datacenter → Permissions → API Tokens) with `VM.Audit`/`Sys.Audit` to
  list guests, `Sys.Modify` on `/nodes` so the host check can refresh package lists, and
  guest-agent exec rights to update VMs (`VM.GuestAgent.Unrestricted` on PVE 9, `VM.Monitor` on
  PVE 8). VMs need the *QEMU Guest Agent* option on and `qemu-guest-agent` installed inside.
* **Proxmox host updates** are checked through PVE itself, the same as *Updates → Refresh* in the
  web UI, so they come from whatever is enabled under *Updates → Repositories*. The dashboard shows
  which repositories were used and warns if the Enterprise repo is on without a subscription.
  PVE's API has no non-interactive upgrade call (its Upgrade button opens a shell), so the upgrade
  runs `apt-get dist-upgrade` over SSH, against those same repositories. SSH is also used for LXCs.
* **Incus:** expose the API (`incus config set core.https_address :8443`) and trust a client
  certificate restricted to the projects you use
  (`incus config trust add-certificate client.crt --restricted --projects default`), or run the
  dashboard on the Incus host and point `socket:` at `/var/lib/incus/unix.socket`. An unrestricted
  certificate is a full Incus admin, which is root on the Incus host. SSH is used for the host itself; if root login is disabled there, use a normal user with passwordless sudo and `sudo: true` in the `ssh:` block (the app runs `sudo -n sh -c ...`, so the sudoers rule must allow `sh`, e.g. `NOPASSWD: ALL`).
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

**Least privilege for the Proxmox token.** Instead of a token under `root@pam`, create a user that
has only what the dashboard needs:

```bash
pveum user add homelab@pve
pveum role add HomelabManager -privs "VM.Audit Sys.Audit Sys.Modify VM.GuestAgent.Unrestricted"  # PVE 8: VM.Monitor
pveum aclmod / -user homelab@pve -role HomelabManager
pveum user token add homelab@pve dashboard --privsep 0
```

The dashboard also has root SSH to the node, so this mainly limits the damage if the token alone
leaks. `Sys.Modify` is only needed for refreshing package lists through the API.

**Restrict the SSH key** on every machine, not just the VPS: in `~/.ssh/authorized_keys`, prefix the
dashboard's key with `from="<dashboard IP>",restrict` (`restrict` turns off port, agent and X11
forwarding and the PTY, none of which the dashboard uses). A stolen key then only works from the
dashboard's address.

**The Docker host:** keep the LXC that runs Docker unprivileged (`unprivileged: 1`, `nesting=1`), and
if it has several network interfaces, bind the port to the LAN one in the compose file.

This app can run commands as root on everything it manages. Keep it on your LAN or behind a VPN,
and set `auth:` in the config.

* **SSH host keys are always checked.** Put every machine's key in `config/known_hosts`
  (`ssh-keyscan`, see below) and set the top-level `known_hosts:`. A machine without one is refused
  with a message saying so; `insecure_skip_host_key_check: true` on that entry opts out.
* **Proxmox and Incus certificates:** set `tls_fingerprint` (shown in the example config) rather than
  `verify_ssl: false`. It pins the exact certificate, works with the self-signed ones both ship and
  with IP addresses, and the dashboard warns at start-up about any API with checking off.
* `auth:` is required once real machines are configured (the dashboard refuses to start without
  it, unless you set `allow_no_auth: true` because a proxy in front does the login). Repeated wrong
  passwords lock that IP out for 5 minutes.
* Basic auth sends the password with every request, so use HTTPS: either set `TLS_CERT` and
  `TLS_KEY` (paths inside the container, e.g. `/config/tls.crt`) to serve HTTPS directly, or put
  Caddy, Traefik or `tailscale serve` in front.
* Set `allowed_hosts` to the names/IPs you open the dashboard by, so other websites can't reach it
  through DNS rebinding.
* Behind a reverse proxy, set `trusted_proxies` to its address (or the Docker network's range), so
  the lockout applies to each visitor's IP rather than the proxy's. The proxy must pass the original
  `Host` header, because `allowed_hosts` and the cross-site check compare against it. Caddy and
  Traefik do this by default; with nginx add `proxy_set_header Host $host;`.
* Cross-site form posts are refused, and the page can't be framed by another site.

### Upgrading from a version without host key checks

SSH connections are now refused unless the machine's host key is known, so before updating the
image (or right after; affected machines show "SSH host key unknown" and can't be updated until
then):

```bash
# Every machine in config.yaml, and for a Proxmox cluster every node's cluster IP too.
ssh-keyscan 192.168.1.10 192.168.1.11 192.168.1.20 192.168.1.30 my-vps.example.com > config/known_hosts
ssh-keygen -lf config/known_hosts   # compare with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on each
```

Then add `known_hosts: /config/known_hosts` at the top of `config.yaml`. Optionally add
`tls_fingerprint` to each Proxmox/Incus entry (see the example config) so the API certificate is
pinned too.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

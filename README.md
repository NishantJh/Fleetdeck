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
sudo chown -R 10001:10001 config && chmod 600 config/id_ed25519   # the container runs as uid 10001
HOMELAB_PASSWORD=change-me PVE_TOKEN_SECRET=... docker compose up -d
```

The container runs as an unprivileged user (uid 10001) with a read-only filesystem and no Linux
capabilities, so the files in `config/` (and `data/`, if you keep state there) must be readable by
that uid. Anything it can't read is logged at start-up with the `chown` to fix it, and shows on the
affected machine's check.

**Upgrading from an image that ran as root:** before pulling the new image, run
`sudo chown -R 10001:10001 config` (and `data` if it exists) in the project folder, or the dashboard
can't read its config and won't start. To pin a version instead of `latest`, use a
release tag such as `ghcr.io/nishantjh/homelab-manager:0.2.0`.

### With Arcane

Run these on the Docker host (the machine or LXC that runs Arcane), in the folder Arcane created for
the project. It needs `ssh-keygen`, `ssh-copy-id` and `ssh-keyscan` (`apt install openssh-client`).

**1. Create the project.** Make a new project in Arcane and paste in `docker-compose.yml`. Then, in
the project's folder:

```bash
mkdir -p config data
cp config.example.yaml config/config.yaml   # or download it from this repo; fill it in in step 6
```

**2. Generate the dashboard's SSH key.** It gets a key of its own, so you can revoke it without
touching yours. No passphrase, because nobody is there to type it:

```bash
ssh-keygen -t ed25519 -f config/id_ed25519 -N "" -C homelab-manager
```

This creates `config/id_ed25519` (private, never leaves this folder) and `config/id_ed25519.pub`
(the public half you put on each machine).

**3. Install the public key on every machine the dashboard manages.** `ssh-copy-id` logs in once
with your password or existing key and appends the key to that user's `~/.ssh/authorized_keys`:

```bash
ssh-copy-id -i config/id_ed25519.pub root@192.168.1.10   # Proxmox node (once per cluster node)
ssh-copy-id -i config/id_ed25519.pub root@192.168.1.20   # Incus host
ssh-copy-id -i config/id_ed25519.pub pi@192.168.1.30     # Raspberry Pi (user with passwordless sudo)
ssh-copy-id -i config/id_ed25519.pub ubuntu@my-vps.example.com   # VPS
```

If a machine has password login turned off, open its console instead (on Proxmox:
*Node → Shell*), and append the line from `config/id_ed25519.pub` by hand:

```bash
echo 'ssh-ed25519 AAAA...your key... homelab-manager' >> ~/.ssh/authorized_keys
```

On the VPS, also restrict where the key can be used (see "For a VPS" below).

**4. Record each machine's host key.** The dashboard refuses to connect to a machine whose host key
isn't in `known_hosts`, so a machine pretending to be yours can't get your commands. List every
machine from step 3; for a Proxmox cluster, every node's cluster IP as well. Add `-p <port>` if SSH
isn't on port 22:

```bash
ssh-keyscan 192.168.1.10 192.168.1.11 192.168.1.20 192.168.1.30 my-vps.example.com > config/known_hosts
ssh-keygen -lf config/known_hosts   # the fingerprints you just trusted
```

For the VPS, compare its fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run
on the VPS itself through your provider's web console.

**5. Test the key** before the dashboard does. Each of these should print `ok` without asking for a
password or about an unknown host:

```bash
ssh -i config/id_ed25519 -o UserKnownHostsFile=config/known_hosts -o StrictHostKeyChecking=yes root@192.168.1.10 echo ok
```

**6. Point `config.yaml` at both files.** The container sees the project's `config/` folder as
`/config`, so use those paths, not the host's:

```yaml
known_hosts: /config/known_hosts   # top level: used by every SSH entry

proxmox:
  - name: pve
    # ...
    ssh:
      host: 192.168.1.10
      user: root
      key_file: /config/id_ed25519

hosts:
  - name: pi4
    host: 192.168.1.30
    user: pi
    key_file: /config/id_ed25519
    sudo: true
```

**7. Hand the files to the container's user.** The container runs as uid 10001, not root, so it
must own them. Run this again whenever you add or replace a file in `config/`:

```bash
chmod 600 config/id_ed25519
sudo chown -R 10001:10001 config data
```

**8. Finish the project settings.**
- Set `HOMELAB_PASSWORD` and `PVE_TOKEN_SECRET` in the project's `.env`.
- While the repo is private, the image is too: add `ghcr.io` as a registry in Arcane with your
  GitHub username and a token that has `read:packages`.
- Add the VMID of the LXC that runs Docker/Arcane to `protect:` under your Proxmox entry. It still
  shows on the dashboard, but can't be updated from it, because upgrading Docker in that LXC would
  restart the dashboard halfway through. Update that one LXC from its own console.

**9. Deploy** the project in Arcane, open the dashboard and press **Check all**. A machine that
says "SSH host key unknown" is missing from `known_hosts` (step 4); "can't read …" means step 7
needs running again.

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
  certificate is a full Incus admin, which is root on the Incus host. SSH is used for the host itself.
* **Raspberry Pi, VPS and other hosts:** SSH. Non-root users need passwordless sudo and `sudo: true`.

### Giving the dashboard its own SSH key

The Arcane steps above walk through this end to end; this section adds the Windows-only route and
the VPS hardening. The dashboard runs in Docker, so it can't use the key on your own PC. Give it a
key of its own; that way you can revoke it without touching yours. Inside the `config/` folder on
the Docker host:

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

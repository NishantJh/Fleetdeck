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
  whether a reboot is needed. On apt machines, updates from a security repository
  (`bookworm-security`, `noble-security`, …) get a red *security* tag and are listed first
  (a Proxmox host needs its `ssh:` settings for this, as the PVE API doesn't say).
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
* **Backups:** each Proxmox guest shows when it was last backed up (vzdump or Proxmox Backup
  Server), turns orange once that's older than `backup_max_age_hours` (48 by default), and is flagged
  *none*, *no job* (not in any scheduled backup job) or *verify failed*. This needs `Datastore.Audit`
  on the backup storages; without it the column shows `?` and says why. **Upgrading:** an existing
  API token most likely lacks it, so add the read-only `PVEAuditor` role on `/storage`
  (`pveum acl modify /storage --tokens 'root@pam!homelab' --roles PVEAuditor`, with your token id),
  or set `backup_max_age_hours: 0` to turn the column off.
  Incus instances show their newest snapshot or export (the dashboard's own pre-upgrade snapshots
  don't count). Plain hosts such as a Raspberry Pi show *not set up* until you add a `backup:` entry:
  the newest file in a folder (`type: path`), or the newest snapshot in a restic or borg repository
  (read with `--no-lock` / `--bypass-lock`, so it never gets in a running backup's way). See
  `config.example.yaml`.
* **Snapshot first:** the Update dialog can snapshot Proxmox LXCs/VMs and Incus instances before
  upgrading (on by default, `snapshot_before_upgrade`). A guest whose storage can't snapshot at all
  (a raw disk on dir/NFS storage) is upgraded without one and the log says so; any other snapshot
  failure stops the upgrade. The job's log then has a **Roll back to snapshot** button, which also
  works when the guest no longer boots or its agent stopped answering. The dashboard keeps its last
  `snapshot_keep` (3) snapshots per guest, named `homelab_<date>_<time>`, and never touches
  snapshots it didn't make. The button only covers the job's own snapshot while it is still kept
  and the job is still in Activity; roll back to anything older in Proxmox or Incus.
* Select several rows and **Update selected** to do them in one go (`max_parallel` at a time,
  never two jobs on the same machine at once).

## Try it without any infrastructure

```bash
pip install -e ".[dev]"
HOMELAB_DEMO=1 python -m homelab   # http://localhost:8080
```

## Install

Every push to `main` publishes `ghcr.io/nishantjh/homelab-manager:latest` (amd64 and arm64).
Installing takes one compose file and a web page:

1. Run [`docker-compose.yml`](docker-compose.yml): `docker compose up -d` in a folder holding it,
   or paste it into whatever manages your containers (Portainer, Dockge, Arcane, Komodo, ...) as a
   new stack and deploy. Plain Docker works too:
   `docker run -d --name homelab-manager -p 8080:8080 -v homelab-data:/data --restart unless-stopped ghcr.io/nishantjh/homelab-manager:latest`.
   Nothing else is needed: no `.env` file, no config file and no `chown`. If port 8080 is already
   used on that host, Docker refuses to start it ("port is already allocated"); set
   `HOMELAB_PORT=8081` (or any free port) in the stack's environment and deploy again.
2. The container's log prints a one-time link (`docker logs homelab-manager`, or the logs view
   of your container manager):
   `http://<this machine>:8080/setup#token=...`. Open it and create the admin login.
   The link is only printed until the login exists. After that the log says
   `Setup is done: open http://<this machine>:8080 and log in`, and the dashboard's Setup button
   takes you back to the setup page.
3. Add each machine: pick its type (Proxmox VE, Incus, or a Linux host such as a Raspberry Pi or
   a VPS), give it a name, and enter its IP address and SSH port.
4. The page shows a short command for each machine, which downloads its script from the dashboard
   (`curl ... /install/<link> && sh ...`). Run it on that machine in any shell (bash, zsh, fish,
   csh, ...), as root or as a user who can use sudo, doas or `su`; it asks for the password it
   needs. The link works for an hour and needs no login, so don't share it. If the machine can't
   reach the dashboard (a VPS, or any machine with a tunnel), paste the whole script as one line
   instead. *What it runs* on the page shows the script in full.
5. Press **Connect**. The dashboard does the rest itself, then press **Apply**.

**What the script does** on the machine: first it checks, changing nothing, and stops with what
to do if anything is in the way: a `homelab` user it didn't create (or one from LDAP/SSSD), sudo
missing or not reading `/etc/sudoers.d`, or an sshd setting that would refuse the dashboard
(`AllowUsers`, `DenyUsers`, `AuthenticationMethods` asking for a second factor, an
`AuthorizedKeysFile` elsewhere). FIPS mode isn't supported (it refuses ed25519 keys). Then it lists
the changes and makes them: a user `homelab` with no password, which logs in only with the
dashboard's own SSH key, and passwordless sudo for it (checked with `visudo`, alone and with the
rest of sudo's config, and taken out again if that fails). The key line carries `restrict` and, if
you fill in *Only accept the dashboard's key from these addresses*, a `from=` list, so a stolen key
can't be used from anywhere else. If sudo isn't installed, it installs it only with apt and only
when that changes no other package; otherwise it tells you the command to run. It never touches
`sshd_config`, the firewall (except for a tunnel, below) or other services. Finally it prints the
machine's SSH host key fingerprints. Running it twice is harmless.

**What Connect does:**

* The first time, it only shows the machine's SSH host key fingerprint. Compare it with what the
  script printed and press *It matches, connect*; nothing is pinned or changed on the machine
  before that. The key is pinned from then on, and if it ever changes the dashboard refuses to
  connect until you confirm the new one.
* It checks that it can log in and that sudo works.
* **Proxmox:** it creates a `homelab@pve` user, a `HomelabManager` role with only what the
  dashboard uses (PVE 8 and 9 alike), and an API token of its own for each node you add, and pins
  the API's TLS certificate. *Reconnect* makes a new token; the old one is revoked on a later
  Connect, once the running dashboard no longer uses it. If the node serves a custom (e.g. ACME) certificate, that exact one is
  pinned, so press *Reconnect* after it renews.
* **Incus or LXD:** it turns on the HTTPS API if it's off, on port 8443 or, if something
  already uses that (UniFi often does), the next free port, listening only on the address
  you entered when that is an IP (on every interface for a host name), trusts the dashboard's own client
  certificate for the projects that exist (press *Reconnect* after adding one), and pins the server
  certificate. If that fails, it turns the API off again when it was the one that turned it on.
* If the machine is the one the dashboard runs on (or its Proxmox node), it says so: put the
  dashboard's container under `protect:`, so an upgrade or reboot can't stop it mid-job.

The same page (the **Setup** button on the dashboard) adds and removes machines later. Removing
one shows a script that undoes the setup on it: the `homelab` user and its sudo rule, its Proxmox
token (and the `homelab@pve` user and role with the last one), the Incus trust, the tunnel. It also
has an editor for `config.yaml` (schedule, notifications, `protect:`, everything in
[`config.example.yaml`](config.example.yaml)), which checks the YAML before saving it.

**Machines that only accept SSH from some addresses**, like a hardened VPS, or **a home network
behind CGNAT** (no fixed public IP to allow): tick *via tunnel* on the machine's card (Linux hosts).
The dashboard then dials out a WireGuard tunnel to the machine and connects over it, so the
machine never needs to reach your home, and SSH stays closed to the internet. The script sets up
the machine's end (interface `hm<n>`, subnet `10.99.<n>.0/30`, UDP port 51821 by default, since
Pangolin uses 51820); if that port is taken it stops and tells you the next free one to enter
instead. Allow that UDP port in your VPS provider's firewall too. The dashboard's end runs inside
the container (wireproxy, no extra privileges or devices), and its keys, including the ones for the
machine's end, are kept in `/data/keys/tunnels`. Removing the machine removes them; on the machine,
`wg-quick down hm<n>` and deleting `/etc/wireguard/hm<n>.conf` removes its end. Otherwise allow
the address the dashboard connects from (for a VPS, your home's public IP), or reach it over
Tailscale and enter that address instead. The dashboard always connects out to
your machines (there's no agent), so each one has to be reachable from it. Connecting before the
script has run counts as failed logins, which fail2ban or CrowdSec may ban the dashboard for.

**Addresses:** use IP addresses or names your DNS server resolves; `.local` (mDNS) names usually
don't resolve inside a container. Docker's default network has no IPv6, so an IPv6-only machine
needs IPv6 enabled in Docker.

**Proxmox cluster:** add one node and run the script on every node. Connect reads the other
nodes and their host keys from the cluster, pins them, and lists any node that still needs the
script (the dashboard reaches each node at its cluster IP, for `pct exec` and host upgrades).
Press *Reconnect* once you've run it there. Put the VMID of the LXC that runs the dashboard under
`protect:` (in the config editor), so upgrading Docker inside it can't restart the dashboard
halfway through.

**The page looks unstyled or out of date after an upgrade?** Your browser is holding an old copy.
Do a hard reload: `Ctrl+Shift+R` (Windows, Linux), `Cmd+Shift+R` (Mac, Chrome and Firefox) or
`Cmd+Option+R` (Mac, Safari).

### The data folder

Everything the dashboard keeps is in `/data` (the `homelab-data` volume, or a folder if you mount
one there instead): `config.yaml`, its SSH key, the pinned host keys, the Incus client certificate
and the check history. Back it up and you have backed up the whole install. Every file in it is
readable only by the dashboard; edit the config on the setup page. Nothing else on the host (the
compose file, an `.env`) is touched, so your container manager can still edit those.

**Backups from the dashboard:** the **Backup** button downloads one file with the config, the
login, every key and token, and the job history, encrypted with a passphrase you choose
(AES-256-GCM, key from scrypt). Keep it off this server; without the passphrase nobody, including
you, can open it. To restore, pick the file in the same dialog: it first shows what is in it, then
replaces the current files and restarts once you confirm. What was there before is kept as an
encrypted file in `/data/backups/`, with the same passphrase. Key files the backup doesn't have
are left in place. Files outside `/data` (say a key file the config points at elsewhere) aren't
in it.

The container starts as root only long enough to sort out ownership, then switches user and keeps
no Linux capabilities (that is what the `cap_add` lines in the compose file are for):

* A folder you mounted and own (say uid 1000) stays yours: the app runs as that user, so you can
  still read and back it up.
* A folder owned by root (one Docker created for you) or the named volume is given to uid 10001.
* `PUID`/`PGID` pick the user yourself. With rootless Docker or Podman, set `PUID=0`: root in
  the container is your own user there.

**Not supported:** NAS systems that rebuild `/etc` (Unraid, TrueNAS on upgrade) or ignore
`/etc/sudoers.d` (Synology) lose the `homelab` user; the script warns about them. The image is
built for amd64 and arm64, so the dashboard itself needs a 64-bit OS (a Pi on 32-bit Raspberry Pi
OS can still be managed).

While the repo is private, the image is too: log in to `ghcr.io` (`docker login ghcr.io`, or
add it as a registry in your container manager) with your GitHub username and a token that has
`read:packages`. To pin a version instead of `latest`, use a release tag such as
`ghcr.io/nishantjh/homelab-manager:0.2.0`.

### Upgrading from the `config/` folder install

An install that mounts `./config:/config:ro` keeps working unchanged: the dashboard uses
`/config/config.yaml` when `/data` has no config, and the setup page shows it as read-only. To
switch, remove the `config` volume and `user:` from the compose file, deploy, and set the
machines up again from the setup page.

The compose file now uses a named volume (`homelab-data`). If yours mounts `./data:/data`, keep
that line instead, or the check history in it is left behind.

Removing a machine on the setup page leaves the machine itself as it is: delete the `homelab`
user and `/etc/sudoers.d/homelab-manager` there, and on Proxmox the `homelab@pve` token (or on
Incus the `homelab-manager` trust entry), if you no longer want them.

## Configuring by hand

The setup page writes an ordinary `config.yaml`, and everything below can also be set by hand
(see [`config.example.yaml`](config.example.yaml); `${VARS}` in it are read from the environment).

### What each platform needs

* **Proxmox:** an API token (Datacenter → Permissions → API Tokens) with `VM.Audit`/`Sys.Audit` to
  list guests, `Datastore.Audit` on `/storage` to see backups,
  `VM.Snapshot` and `VM.Snapshot.Rollback` for snapshots, `Sys.Modify` on `/nodes` so the host check can refresh package lists, and
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
pveum role add HomelabManager -privs "VM.Audit Sys.Audit Sys.Modify VM.GuestAgent.Unrestricted Datastore.Audit VM.Snapshot VM.Snapshot.Rollback"  # PVE 8: VM.Monitor
pveum aclmod / -user homelab@pve -role HomelabManager
pveum user token add homelab@pve dashboard --privsep 0
```

The dashboard also has root SSH to the node, so this mainly limits the damage if the token alone
leaks. `Sys.Modify` is only needed for refreshing package lists through the API, `Datastore.Audit` for the
backup column and `VM.Snapshot`/`VM.Snapshot.Rollback` for snapshots before upgrades.

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
* `auth:` is required. Without it, nothing but the setup page is served, and that page only
  works through the one-time link in the logs until it has created a login (`allow_no_auth: true`
  skips this when a proxy in front does the login). The setup page stores only a scrypt hash of
  the password (`password_hash`). Repeated wrong passwords lock that IP out for 5 minutes.
* Basic auth sends the password with every request, so use HTTPS: either set `TLS_CERT` and
  `TLS_KEY` (paths inside the container, e.g. `/data/tls.crt`) to serve HTTPS directly, or put
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

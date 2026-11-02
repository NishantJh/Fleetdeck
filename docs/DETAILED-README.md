# Fleetdeck

> **Built with help from Claude Code, tested on real homelab hardware.** It has been run on a Proxmox VE host with Proxmox Backup Server, an Incus host, a Raspberry Pi 4 and an
> Ubuntu VPS. CI runs the linter and the test suite on every change, and the owner approves each
> merge. It has **not** had an independent security audit, so read [Safety](#safety) before you point
> it at machines you care about.

One dashboard for the whole homelab: see what's pending and update it, keep Docker Compose stacks
current, back up each machine's setup, and get notified when something needs attention. It works across:

| Platform | Host | Guests | How it reaches the guest |
| --- | --- | --- | --- |
| Proxmox VE | PVE API to check (uses PVE's own repositories), SSH to upgrade | LXCs | `pct exec` over the node's SSH |
| | | VMs | QEMU guest agent, via the Proxmox API |
| Incus | SSH | containers and VMs | Incus REST API `exec` (VMs need `incus-agent`) |
| Anything else (Raspberry Pi, VPS, etc.) | SSH | | |

Guests are discovered automatically from the Proxmox and Incus APIs, so new LXCs/VMs show up without
editing config. Each machine detects its own package manager: **apt** (Debian, Ubuntu, Proxmox,
Raspberry Pi OS), **dnf/yum**, **apk** (Alpine) and **pacman** (Arch).

* **Check** refreshes package lists and shows each pending package with old → new version, plus
  whether a reboot is needed. On apt machines, updates from a security repository
  (`bookworm-security`, `noble-security`, …) get a red *security* tag and are listed first
  (a Proxmox host needs its `ssh:` settings for this, as the PVE API doesn't say).
* **Update** runs a full upgrade (`apt-get dist-upgrade` on Proxmox and Raspberry Pi OS, `apt-get upgrade --with-new-pkgs`
  (never removes a package) elsewhere, `dnf upgrade`, `apk upgrade`, `pacman -Syu`), streams the output to the Activity panel, then re-checks. Nothing is rebooted unless you ask.
  * **Bottom-up order:** host, then its LXCs/VMs, then the Docker stacks inside them. While anything below a
    machine is updating, its own Update and Reboot buttons, and those of every level above it, are disabled
    with the reason, and the dashboard refuses those jobs too. Checks, backups and image checks are never blocked.
  * **Community-scripts apps:** a container made by a Proxmox VE Helper-Script (community-scripts) is updated by its
    app's own update, the same as typing `update` inside it (in a pseudo-terminal).
    The regular OS upgrade then runs right after it (also if the app's own update failed), because many of these containers
    have more installed than the helper script put there.
    On by default. Turn it off with `community_scripts_update: false` (the dashboard then only warns and you run `update` yourself).
  * **A machine's own update command:** for appliances with their own updater, such as `omv-upgrade` on
    OpenMediaVault, Update can run that command (as root, with `sh -c`) instead of the built-in upgrade. Set it in the
    config (`update_command:` on a host, `update_commands:` by VMID, guest or node name under `proxmox:`, by instance
    name under `incus:`) or with a machine's **Update command…** button, which asks for the dashboard password again,
    only works from the networks in `console.allow_from`, writes an audit row and sends a notification. One set in the
    config can't be changed on the dashboard. The Update dialog shows the command. It runs like a normal update (snapshot
    first, live log, survives a dashboard restart, re-check after), but the helper-script app update is skipped, and if
    the command fails the job fails: the built-in upgrade is not tried instead. Scheduled security updates leave these
    machines alone (the command may install more than security fixes) and the notification says so. A monitor-only
    machine is never updated, command or not.
* Check results and the Activity history are saved to `data/state.db` (SQLite), so they survive a
  restart or an image update. A job that was running when the dashboard stopped shows as interrupted.
* **Reboot** reboots a machine, waits until it is back (its boot id changes), then re-checks it.
  Tick *Reboot afterwards if a reboot is needed* when updating to do both in one go. A Proxmox node
  that runs a `protect:`ed guest (the dashboard's own LXC) is never rebooted from here.
* **Scheduled checks:** set `schedule.check_every` (e.g. `6h`) and the dashboard checks everything on
  its own, then notifies you through ntfy, Gotify, Discord, Telegram or a JSON webhook when something
  new is pending (a new update, a new pending reboot, or a check that started failing). It never
  upgrades on its own. Add `check_docker: true` under `schedule` to look for newer Docker images too;
  that pulls the images on every machine with Docker each round, so it is off by default. *Test notification* in the toolbar sends a test message.
* **Backups:** each Proxmox guest shows when it was last backed up (vzdump or Proxmox Backup
  Server), turns orange once that's older than `backup_max_age_hours` (48 by default), and is flagged
  *none*, *no job* (not in any scheduled backup job) or *verify failed*. This needs `Datastore.Audit`,
  `Datastore.AllocateSpace` and `VM.Backup` (Proxmox lists a guest's backups only to a user that could
  make them); without them the column shows `?` and says why. Press *Connect* on that machine to update
  its role, or set `backup_max_age_hours: 0` to turn the column off.
  Incus instances show their newest snapshot or export (the dashboard's own pre-upgrade snapshots
  don't count). Every other machine (Proxmox nodes, the Incus host, a Raspberry Pi, a VPS) shows
  *not set up* until you add a `backup:` entry to its SSH settings (under `ssh:` for Proxmox and Incus):
  the newest file in a folder (`type: path`), or the newest snapshot in a restic or borg repository
  (read with `--no-lock` / `--bypass-lock`, so it never gets in a running backup's way). See
  `config.example.yaml`.
* **Back up a host:** the **Back up** button on a host row saves the machine's settings, so you can set it
  up again quickly. It never backs up guests or instances (create those separately), and nothing on the
  machine is stopped or changed. Each run has a live log. The dialog asks where the archive goes, with three
  choices:
  * **The default place** (what you get if you change nothing):
    * **Proxmox node:** its Proxmox Backup Server storage, with the credentials and encryption key Proxmox
      already keeps for it. With no PBS, or if the push fails, a local archive in
      `/var/backups/homelab-manager` **on the node**.
    * **Raspberry Pi, VPS or other SSH machine:** an archive in the folder or restic/borg repository of its
      `backup:` entry; without one, `/var/backups/homelab-manager` **on that machine**.
    * **Incus host:** a settings file in `host-backups/` in the data volume, plus `/etc` of the host when it
      has SSH settings.
  * **On the dashboard** (recommended for one shared place for every machine): the archive is sent back over
    the connection the dashboard already uses (SSH, through a tunnel too) and saved as
    `<backup folder>/fleetdeck/<machine name>/fleetdeck-config-<machine name>-<date>_<time>.tar.gz` (the Incus host also gets
    `incus-settings-<date>_<time>.json` there). Nothing is written on the machine and nothing needs mounting
    there. The dashboard only ever writes inside its `fleetdeck/` folder, and changes nothing else in the
    mount, including owners and permissions. Each file is written under a temporary name and renamed when
    complete, so a failed run leaves no half archive.
    **The backup folder** is `host-backups/` in the data volume unless you set `backup_folder:` in the
    config (or `FLEETDECK_BACKUP_FOLDER`). To keep them on a NAS or any other folder, mount it into the
    container and point the setting at it. With Docker Compose (full path on the Docker host, here
    `/apps/backups`):

    ```yaml
    services:
      fleetdeck:
        environment:
          FLEETDECK_BACKUP_FOLDER: /backups
        volumes:
          - /apps/backups:/backups
    ```

    The folder must exist (mount it first) and be writable by the user the dashboard runs as (uid 10001
    by default; `PUID`/`PGID` only if you really need another). The dashboard never changes the owner of a
    mounted folder. At start it tries to write a small test file there and, if that fails, the log says so
    and names the uid: allow that user on the NAS, or map all users to one that can write (a share owned by
    `nobody` usually already does). A missing or unwritable folder is an error when you back up, never a
    silent write to the container's disk.
  * **A folder or network share on the machine:** a full path on that machine (for Proxmox, on the node),
    with any share already mounted there. For Incus it is a path inside the dashboard's container. A folder
    that does not exist is an error.

  **What a Proxmox node backup contains:** all of `/etc` on the node, which includes `/etc/pve` (the
  cluster filesystem: `storage.cfg`, every guest's config in `qemu-server/` and `lxc/`, users, ACLs and
  API tokens, firewall, backup jobs, certificates, and `priv/` with the storage passwords and keys),
  plus network (`/etc/network/interfaces`, `hosts`, `hostname`), `fstab`, apt sources, `vzdump.conf`,
  modprobe/GRUB settings and the node's SSH host keys. The archive form also has `/usr/local/etc`,
  `/usr/local/bin`, `/var/spool/cron` and the installed package list; the PBS form is `/etc` only.
  It does **not** contain guests or their disks, `/root`, `/home`, `/var/lib/vz`, ZFS pool layout or
  anything else outside those folders. It holds secrets (`/etc/shadow`, SSH host keys, on Proxmox
  `/etc/pve/priv`). Set `backup_age_recipient` to an [age](https://github.com/FiloSottile/age) public key (`age-keygen` makes the pair; keep the private key in your
  password manager) and archives are written encrypted as `.tar.gz.age`, with no passphrase to type. Restore with
  `age -d -i key.txt file.tar.gz.age | tar xz`. A machine that writes the archive itself needs `age` installed.
  Or set one `backup_password` (8 characters or more; `${BACKUP_PASSWORD}` reads it from the environment) and
  archives saved on the dashboard are written as `.tar.gz.age` files locked with it. Restore with
  `age -d file.tar.gz.age | tar xz`, which asks for the password. `age` can only take a password from a terminal,
  so it can't lock an archive written on the machine itself: with only a password set, those backups stop with an
  error rather than write an unencrypted archive. Save those machines on the dashboard, or set
  `backup_age_recipient` as well (then the machine uses the key and the dashboard uses the password).
  Without either setting the archive is not encrypted, so keep it where you keep other secrets:
  turn on client-side encryption for the PBS storage, or use a folder only you can read (archives are
  written readable by root only). To rebuild a node: reinstall Proxmox, restore `/etc/network`, then
  `/etc/pve` (or re-add storage and restore the guests from their own backups).

  The newest 7 archives (or settings files) per machine are kept in each folder; older ones this dashboard
  made there are deleted, and nothing else. *Always back up this machine here* remembers the choice for
  that machine (each machine has its own); leave it unticked to choose every time. Backup is a manual
  button: schedule real backups on the hosts, and let *Last backup* watch them.
* **Snapshot first:** the Update dialog can snapshot Proxmox LXCs/VMs and Incus instances before
  upgrading (on by default, `snapshot_before_upgrade`). A guest whose storage can't snapshot at all
  (a raw disk on dir/NFS storage) is upgraded without one and the log says so; any other snapshot
  failure stops the upgrade. The job's log then has a **Roll back to snapshot** button, which also
  works when the guest no longer boots or its agent stopped answering. The dashboard keeps its last
  `snapshot_keep` (3) snapshots per guest, named `fleetdeck_<date>_<time>`, and never touches
  snapshots it didn't make. The button only covers the job's own snapshot while it is still kept
  and the job is still in Activity; roll back to anything older in Proxmox or Incus.
* **Docker Compose stacks:** the **Docker** button on any machine (host, VM or LXC) lists the Compose
  stacks running on it, found through the labels Compose puts on its containers, so no paths to set up.
  *Check for new images* downloads newer images for every running stack (nothing is restarted, but a later `docker compose up` you run
  yourself on that machine will use them) and marks the stacks that are behind; **Update** on a stack pulls and runs
  `docker compose up -d --no-deps` for the services running now (their stopped `depends_on` services are not started).
  A service you stopped stays stopped, a stopped stack is not started, and a failed pull
  leaves the stack as it was. Tick *remove old images after an update* to also run `docker image prune`
  (all unused, untagged images on the machine, not only this stack's). Like OS upgrades, it keeps running on the machine if the dashboard
  restarts, and while it runs the machine and its host are not updated or rebooted. The stack that runs
  the dashboard itself is shown but not updated from here (it would stop halfway); update it with
  `docker compose pull && docker compose up -d`. It needs nothing new on machines you already set up: the
  dashboard's sudo rule already allows `docker`. Docker must be reachable as root (rootless Docker for a
  normal user isn't supported).
* Select several rows and **Update selected** to do them in one go (`max_parallel` at a time,
  never two jobs on the same machine at once).

![The dashboard (demo data)](screenshots/dashboard-light.png#gh-light-mode-only)
![The dashboard (demo data)](screenshots/dashboard-dark.png#gh-dark-mode-only)

On a phone, each machine becomes a card with its own Update and Docker buttons:

![The dashboard on a phone (demo data)](screenshots/dashboard-phone-light.png#gh-light-mode-only)
![The dashboard on a phone (demo data)](screenshots/dashboard-phone-dark.png#gh-dark-mode-only)

The first-run setup page creates the admin login:

![The setup page](screenshots/setup-light.png#gh-light-mode-only)
![The setup page](screenshots/setup-dark.png#gh-dark-mode-only)

Then add each machine: fill in its details, paste the generated script on it, press Connect:

![Adding a machine](screenshots/add-machines-light.png#gh-light-mode-only)
![Adding a machine](screenshots/add-machines-dark.png#gh-dark-mode-only)

Every check and upgrade streams its output to the Activity panel:

![The Activity panel](screenshots/activity-light.png#gh-light-mode-only)
![The Activity panel](screenshots/activity-dark.png#gh-dark-mode-only)

## Safety

Read this before you add machines. The dashboard can change things on them, and some of those
changes are hard to undo.

* It runs commands as root on each machine you add, through its own SSH key for a `homelab` user
  with passwordless sudo. Give that key a `from=` address (see [Install](#install)) so it only works
  from the dashboard. The setup script changes only its own `homelab` user and sudo rule, and never
  touches your own SSH keys.
* It upgrades only when you press *Update*, and reboots only when you ask. A Proxmox node that runs a
  guest you put under `protect:` is never rebooted from here.
* A machine set to *Monitor only* (its button on the dashboard, or `monitor_only:` in the config) is
  checked and reported as usual, but no update is ever installed on it from here: not by *Update*,
  *Update selected*, a Docker stack update or automatic security updates. Its reboot, backups and
  snapshots still work, and so does its host's reboot.
* The setup script never changes `sshd_config` or the firewall, except the UDP port of a tunnel you
  chose. Removing a machine on the setup page gives you a script that undoes its setup.
* Keep the dashboard on your LAN or behind a VPN, with a login (`auth:`, set by the setup page). If you
  reach it across a network, use HTTPS.
* Report security problems privately, as described in [SECURITY.md](../SECURITY.md).

## Try it without any infrastructure

```bash
pip install -e ".[dev]"
FLEETDECK_DEMO=1 python -m fleetdeck   # http://localhost:8080
```

## Install

Installing takes one compose file and a web page:

1. Run [`docker-compose.yml`](../docker-compose.yml): `docker compose up -d` in a folder holding it,
   or paste it into whatever manages your containers (Portainer, Dockge, Arcane, Komodo, ...) as a
   new stack and deploy. Plain Docker works too:
   `docker run -d --name fleetdeck -p 8080:8080 -v fleetdeck-data:/data --restart unless-stopped ghcr.io/nishantjh/fleetdeck:latest`.
   Nothing else is needed: no `.env` file, no config file and no `chown`. If port 8080 is already
   used on that host, Docker refuses to start it ("port is already allocated"); set
   `FLEETDECK_PORT=8081` (or any free port) in the stack's environment and deploy again.
2. Open `http://<this machine>:8080` in a browser. Until the admin login exists it goes straight
   to the setup page, which asks for the **setup code** from the container's log (the logs view of
   your container manager, or `docker logs fleetdeck 2>&1 | grep "Setup code"`). The code
   proves you run the server, so nobody else on your network can claim the dashboard; it changes
   each time the container starts. The log also prints a link with the code already in it
   (`http://<this machine>:8080/setup#token=...`). Enter the code and create the admin login.
   The code is only printed until the login exists. After that the log says
   `Setup is done: open http://<this machine>:8080 and log in`, and the dashboard's **+ Add machines**
   button takes you back to the setup page.
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
  Add the machine by IP to keep the API off your other networks: with a host name it listens on every
  interface (the setup log says so), protected by the client certificate (mutual TLS) and a pinned server certificate.
* If the machine is the one the dashboard runs on (or its Proxmox node), it says so: put the
  dashboard's container under `protect:`, so an upgrade or reboot can't stop it mid-job.

The same page (the **+ Add machines** button on the dashboard) adds and removes machines later. Removing
one shows a script that undoes the setup on it: the `homelab` user and its sudo rule, its Proxmox
token (and the `homelab@pve` user and role with the last one), the Incus trust, the tunnel. It also
has an editor for `config.yaml` (schedule, notifications, `protect:`, everything in
[`config.example.yaml`](../config.example.yaml)), which checks the YAML before saving it.

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

Everything the dashboard keeps is in `/data` (the `fleetdeck-data` volume, or a folder if you mount
one there instead): `config.yaml`, its SSH key, the pinned host keys, the Incus client certificate
and the check history. Back it up and you have backed up the whole install. Every file in it is
readable only by the dashboard; edit the config on the setup page. Nothing else on the host (the
compose file, an `.env`) is touched, so your container manager can still edit those.

**Backups from the dashboard:** the **Backup** button downloads one file with the config, the
login, every key and token, and the job history, encrypted with a passphrase you choose
(AES-256-GCM, key from scrypt). Keep it off this server; without the passphrase nobody, including
you, can open it. To restore, pick the file in the same dialog: it first shows what is in it, then
replaces the current files and restarts once you confirm. On a fresh install, open the dashboard, enter the setup code from the logs and use **Or restore from a backup** instead of creating a login: it brings back the login, the machines and the keys, so the machines need nothing run again. What was there before is kept as an
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

The image is public, so `docker pull` needs no login. To pin a version instead of `latest`, use a release tag such as
`ghcr.io/nishantjh/fleetdeck:1.2.3` (see [Install](#install)).

## Settings

Two files shape the container, and each setting goes in one of them:

* **`docker-compose.yml`** (or the stack editor of your container manager) decides what the container
  gets: the image version, the ports, the folders it can see and the environment variables it is given.
* **`.env`** is a file named `.env` in the same folder as `docker-compose.yml`. Compose reads it to fill in
  `${NAME}` placeholders *inside the compose file*. A value in `.env` does **not** reach the container by
  itself. Pass it on with an `environment:` line (`NAME: ${NAME}`), or with `env_file: .env` to pass the whole
  file. In Portainer, Dockge, Arcane and similar tools, the stack's "environment" box plays the part of `.env`.

After any change to the compose file or `.env`, run `docker compose up -d` again (or deploy the stack again).
A restart alone does not reload either file.

### Settings in docker-compose.yml

**Version (`image:`).** `ghcr.io/nishantjh/fleetdeck:1` takes every 1.x release when you pull. `:1.2` takes only
patch fixes, and `:1.2.3` stays on exactly that release. `:latest` follows every change to `main`, released or not.
To update: `docker compose pull && docker compose up -d`.

**Port (`FLEETDECK_PORT`).** The port on the host, 8080 by default. The compose file already maps it:

```yaml
ports:
  - "${FLEETDECK_PORT:-8080}:8080"
environment:
  FLEETDECK_PORT: ${FLEETDECK_PORT:-8080}   # so the setup link in the log shows the right port
```

To change it, put this in `.env`:

```
FLEETDECK_PORT=8081
```

To listen on one network interface only, edit the `ports:` line, for example
`"192.168.1.50:${FLEETDECK_PORT:-8080}:8080"`. The container always listens on 8080 inside, so leave that part alone.

**Data folder (`volumes:`).** The default `fleetdeck-data` named volume holds everything the dashboard keeps:
config, keys, pinned host keys and history. To keep it in a folder you can see, replace the volume line with
`- ./data:/data`. Back up that folder and you have backed up the whole install.

**Owner of the data folder (`PUID` / `PGID`).** At start, the container runs as the owner of `/data`. Owner
means uid 10001 for a named volume or a folder Docker created, and whoever owns a folder you made yourself.
Set `PUID` and `PGID` to pick the user yourself. Be aware that the app then changes the owner of everything
in `/data` to that user. Example, for a folder owned by uid 1000:

```yaml
environment:
  PUID: "1000"
  PGID: "1000"
```

With rootless Docker or Podman, use `PUID: "0"`, which keeps root in the container (your own user on the host).

**Backup folder (`FLEETDECK_BACKUP_FOLDER`).** Where "Back up" > "On the dashboard" saves each machine's
archive. Mount the folder, then name its path inside the container. The complete steps, including the
folder's permissions, are under **Back up a host** in the Backups part of the features list above. The short form:

```yaml
environment:
  FLEETDECK_BACKUP_FOLDER: /backups
volumes:
  - /apps/backups:/backups     # the folder on this Docker host, then the path in the container
```

**HTTPS (`TLS_CERT` / `TLS_KEY`).** The dashboard serves HTTPS itself when it has a certificate. The two
names have no `FLEETDECK_` prefix. Put the certificate and key in the data folder, then give their paths
inside the container. The port stays the same and now speaks HTTPS:

```yaml
environment:
  TLS_CERT: /data/tls.crt
  TLS_KEY: /data/tls.key
```

If a reverse proxy in front already does HTTPS, skip this and see [Put it behind HTTPS](#put-it-behind-https).

**Run command (`FLEETDECK_CONSOLE`).** Off by default. Setting `FLEETDECK_CONSOLE: "1"` turns it on even if
`config.yaml` does not, and it needs a login (`auth:`). Turning it on in `config.yaml` instead is
`console: { enabled: true }`. Everything about it, including who can reach it, is in [docs/CONSOLE.md](CONSOLE.md).

**Demo (`FLEETDECK_DEMO`).** `FLEETDECK_DEMO: "1"` shows fake demo machines so you can look around. Remove
it once you add real machines.

**Set by the image, leave alone.** The image already sets `FLEETDECK_DATA=/data`,
`FLEETDECK_CONFIG=/data/config.yaml`, `FLEETDECK_STATE=/data/state.db` and `PORT=8080`. Changing them can
break the paths the dashboard expects.

### Values used in config.yaml

Settings such as the login, machines and notifications live in `config.yaml`. The setup page writes that
file, and its editor (**+ Add machines** > config editor) changes it. A copy with every option described is in
[`config.example.yaml`](../config.example.yaml). Some of the common ones:

* `max_parallel`: how many machines are checked or updated at once.
* `schedule.check_every`: how often to check, for example `6h`.
* `public_url`: the link notifications use to point back at the dashboard.
* `allowed_hosts` and `trusted_proxies`: the names you open it by, and the reverse proxy in front of it, if any.
* `snapshot_before_upgrade` and `snapshot_keep`: snapshots taken before upgrades.
* `backup_age_recipient`: encrypts host backups to your age public key.

A value written as `${NAME}` in `config.yaml` is read from the container's environment. It has to be passed in
with an `environment:` line, as above. The `.env` file next to the compose file is the usual place to keep it:

```yaml
# docker-compose.yml
environment:
  NTFY_TOKEN: ${NTFY_TOKEN}
```

```
# .env
NTFY_TOKEN=your-token-here
```

If a `${NAME}` is not set, the dashboard does not start, and the log names the missing variable. Common
ones are `PVE_TOKEN_SECRET` (a Proxmox API token), `NTFY_TOKEN`, `GOTIFY_APP_TOKEN`, `DISCORD_WEBHOOK_URL`,
`TELEGRAM_BOT_TOKEN` and `BACKUP_PASSWORD`. The names are your choice; `config.example.yaml` shows the form.
A login made on the setup page is stored as a hash, so you do not need `FLEETDECK_PASSWORD` for it. Only a
hand-written `password: ${FLEETDECK_PASSWORD}` in `config.yaml` needs that variable.

## Configuring by hand

The setup page writes an ordinary `config.yaml`, and everything below can also be set by hand
(see [`config.example.yaml`](../config.example.yaml); `${VARS}` in it are read from the environment).

### What each platform needs

* **Proxmox:** an API token (Datacenter → Permissions → API Tokens) with `VM.Audit`/`Sys.Audit` to
  list guests, `Datastore.Audit`, `Datastore.AllocateSpace` and `VM.Backup` to see backups,
  `VM.Snapshot` and `VM.Snapshot.Rollback` for snapshots, `Sys.Modify` on `/nodes` so the host check can refresh package lists, and
  guest-agent exec rights to update VMs (`VM.GuestAgent.Unrestricted` on PVE 9, `VM.Monitor` on
  PVE 8). VMs need the *QEMU Guest Agent* option on and `qemu-guest-agent` installed inside.
  Connect creates a `homelab@pve` user with a `HomelabManager` role holding these privileges.
* **Proxmox host updates** are checked through PVE itself, the same as *Updates → Refresh* in the
  web UI, so they come from whatever is enabled under *Updates → Repositories*. The dashboard shows
  which repositories were used and warns if the Enterprise repo is on without a subscription.
  PVE's API has no non-interactive upgrade call (its Upgrade button opens a shell), so the upgrade
  runs `apt-get dist-upgrade` over SSH, against those same repositories. SSH is also used for LXCs.
* **Incus:** expose the HTTPS API and trust a client certificate restricted to the projects you use
  (`incus config set core.https_address :8443`, then `incus config trust add-certificate client.crt --restricted --projects default`),
  or run the dashboard on the Incus host and point `socket:` at `/var/lib/incus/unix.socket`. An unrestricted
  certificate is a full Incus admin, which is root on the Incus host. Connect does this for you.
* **Raspberry Pi, VPS and other hosts:** SSH. Non-root users need passwordless sudo and `sudo: true`.

### Security

* **SSH host keys are always checked.** Every machine's key goes in `known_hosts:` (the setup page pins them
  for you). A machine without one is refused with a message saying so; `insecure_skip_host_key_check: true`
  on that entry opts out.
* **Proxmox and Incus certificates:** set `tls_fingerprint` (shown in the example config) rather than
  `verify_ssl: false`. It pins the exact certificate, works with the self-signed ones both ship and
  with IP addresses, and the dashboard warns at start-up about any API with checking off.
* **Restrict the SSH key** on every machine: prefix the dashboard's key line in `~/.ssh/authorized_keys`
  with `from="<dashboard IP>",restrict` (`restrict` turns off port, agent and X11 forwarding and the PTY,
  none of which the dashboard uses). A stolen key then only works from the dashboard's address. On a VPS,
  use a non-root user with passwordless sudo (`sudo: true`), since that key plus sudo is root on the VPS.
* **Least privilege for the Proxmox token:** use the `homelab@pve` user and `HomelabManager` role that
  Connect makes, rather than a token under `root@pam`. The dashboard also has root SSH to the node, so this
  mainly limits the damage if the token alone leaks.
  It is not read-only: with `Sys.Modify` and `VM.GuestAgent.Unrestricted` (and no privilege separation)
  it can run commands as root inside any VM with the guest agent and change node settings.
* **The Docker host:** keep the LXC that runs Docker unprivileged (`unprivileged: 1`, `nesting=1`), and
  if it has several network interfaces, bind the port to the LAN one in the compose file.
* This app can run commands as root on everything it manages. Keep it on your LAN or behind a VPN,
  and set `auth:` in the config.
* `auth:` is required. Without it, nothing but the setup page is served, and that page only
  works through the one-time link in the logs until it has created a login (`allow_no_auth: true`
  skips this when a proxy in front does the login). The setup page stores only a scrypt hash of
  the password (`password_hash`). Repeated wrong passwords lock that IP out for 5 minutes.
* The browser signs in on the login page and gets a session cookie (HttpOnly, SameSite=Lax, Secure over
  HTTPS), for the browser session or 30 days with "Keep me signed in". Changing the username or password
  ends every session; so does Sign out, in that browser. Scripts can still send the login with each
  request (HTTP Basic, e.g. `curl -u admin:...`).
* The password and the session cookie cross the network, so use HTTPS: either set `TLS_CERT` and
  `TLS_KEY` (paths inside the container, e.g. `/data/tls.crt`) to serve HTTPS directly, or put
  Caddy, Traefik or `tailscale serve` in front.
* The page shows a warning when it is opened over plain HTTP from anything but localhost.
* Set `allowed_hosts` to the names/IPs you open the dashboard by, so other websites can't reach it
  through DNS rebinding.
* Behind a reverse proxy, set `trusted_proxies` to its address (or the Docker network's range), so
  the lockout applies to each visitor's IP rather than the proxy's. The proxy must pass the original
  `Host` header, because `allowed_hosts` and the cross-site check compare against it. Caddy and
  Traefik do this by default; with nginx add `proxy_set_header Host $host;`.
* Cross-site form posts are refused, and the page can't be framed by another site.

### Put it behind HTTPS

Over plain HTTP the password and the session cookie cross your network in clear text, and they are
effectively root on every machine the dashboard manages. Most people already run a reverse proxy; put
Fleetdeck behind it. With [Caddy](https://caddyserver.com) (a certificate is made for you):

```
fleetdeck.example.com {
    reverse_proxy fleetdeck:8080
}
```

Then, in the config, set `trusted_proxies` to the proxy's address (so the login lockout counts each visitor,
not the proxy) and `allowed_hosts` to `fleetdeck.example.com`, and drop the `ports:` mapping from
`docker-compose.yml` so only the proxy can reach the container. Traefik, Nginx Proxy Manager, Pangolin and
`tailscale serve` work the same way. Without a proxy, `TLS_CERT` and `TLS_KEY` serve HTTPS from the
container directly.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
```

See [docs/PLAN.md](PLAN.md) for the roadmap.

## Support

Fleetdeck is free. If it saves you time, you can support it on [Ko-fi](https://ko-fi.com/nishant19836) or [PayPal](https://paypal.me/nishantjha1659).

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).

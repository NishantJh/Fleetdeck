# Install and setup

The first-run setup page creates the admin login:

![The setup page](screenshots/setup-light.png#gh-light-mode-only)
![The setup page](screenshots/setup-dark.png#gh-dark-mode-only)

Then add each machine: fill in its details, paste the generated script on it, press Connect:

![Adding a machine](screenshots/add-machines-light.png#gh-light-mode-only)
![Adding a machine](screenshots/add-machines-dark.png#gh-dark-mode-only)

Every check and upgrade streams its output to the Activity panel:

![The Activity panel](screenshots/activity-light.png#gh-light-mode-only)
![The Activity panel](screenshots/activity-dark.png#gh-dark-mode-only)

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
  Add the machine by IP to keep the API off your other networks: with a host name it listens on every
  interface (the setup log says so), protected by the client certificate (mutual TLS) and a pinned server certificate.
* If the machine is the one the dashboard runs on (or its Proxmox node), it says so: put the
  dashboard's container under `protect:`, so an upgrade or reboot can't stop it mid-job.

The same page (the **Setup** button on the dashboard) adds and removes machines later. Removing
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
`ghcr.io/nishantjh/fleetdeck:1.2.3` (see [Install](#install-and-setup)).

### Upgrading from an older install

* **Config folder:** an install that mounts `./config:/config:ro` keeps working unchanged: the dashboard
  uses `/config/config.yaml` when `/data` has no config, and the setup page shows it as read-only. To switch,
  remove the `config` volume and `user:` from the compose file, deploy, and set the machines up again from
  the setup page.
* **Data volume:** the compose file uses the named volume `fleetdeck-data`. If you ran the older
  `homelab-data` volume, copy it before you deploy (Docker can't rename a volume). With the stack stopped:

  ```
  docker volume create STACK_fleetdeck-data
  docker run --rm -v STACK_homelab-data:/from:ro -v STACK_fleetdeck-data:/to alpine sh -c 'cp -a /from/. /to/'
  ```

  `STACK` is your stack's name (`docker volume ls | grep homelab-data` shows the exact name). The old volume
  is left untouched; remove it once the new one works. If you had `./data:/data` in the compose file, keep it.
* **Port:** `HOMELAB_PORT` is no longer read; set `FLEETDECK_PORT` instead.

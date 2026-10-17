# Features

It works across:

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
  * **Community-scripts apps:** a container made by a Proxmox VE Helper-Script (community-scripts) also runs
    its app's own update after the OS upgrade, the same as typing `update` inside it, in a pseudo-terminal.
    Turn it off with `community_scripts_update: false` (the dashboard then only warns and you run `update` yourself).
* Check results and the Activity history are saved to `data/state.db` (SQLite), so they survive a
  restart or an image update. A job that was running when the dashboard stopped shows as interrupted.
* **Reboot** reboots a machine, waits until it is back (its boot id changes), then re-checks it.
  Tick *Reboot afterwards if a reboot is needed* when updating to do both in one go. A Proxmox node
  that runs a `protect:`ed guest (the dashboard's own LXC) is never rebooted from here.
* **Scheduled checks:** set `schedule.check_every` (e.g. `6h`) and the dashboard checks everything on
  its own, then notifies you through ntfy, Gotify, Discord, Telegram or a JSON webhook when something
  new is pending (a new update, a new pending reboot, or a check that started failing). It never
  upgrades on its own. *Test notification* in the toolbar sends a test message.

* **Snapshot first:** the Update dialog can snapshot Proxmox LXCs/VMs and Incus instances before
  upgrading (on by default, `snapshot_before_upgrade`). A guest whose storage can't snapshot at all
  (a raw disk on dir/NFS storage) is upgraded without one and the log says so; any other snapshot
  failure stops the upgrade. The job's log then has a **Roll back to snapshot** button, which also
  works when the guest no longer boots or its agent stopped answering. The dashboard keeps its last
  `snapshot_keep` (3) snapshots per guest, named `homelab_<date>_<time>`, and never touches
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

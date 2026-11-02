# Plan

Goal: a single self-hosted dashboard for managing a homelab. OS updates come first.

## Architecture

* **Backend:** Python + FastAPI, async throughout. One process, one container.
* **Frontend:** a static page (vanilla JS, no build step) served by the backend. Easy to replace
  with a framework later if the UI grows.
* **Providers** discover targets (Proxmox nodes/LXCs/VMs, Incus host/instances, SSH hosts).
* **Executors** run one POSIX `sh` script on a target, whatever the transport: SSH, `pct exec`,
  QEMU guest agent, Incus exec. Adding a platform means adding a provider and maybe an executor.
* **Jobs** run checks/upgrades in the background with a global concurrency limit and one job per
  target at a time. The UI polls for status and log lines.

## Milestones

### 1. OS updates (this PR)
- [x] Discover Proxmox, Incus and SSH targets
- [x] Proxmox host checked through the PVE API, using the repositories configured in PVE
- [x] Check pending updates (apt, dnf/yum, apk, pacman) and reboot-required
- [x] Upgrade one or many targets, with live log
- [x] Basic auth, Docker image on GHCR (amd64/arm64), Arcane-ready compose, demo mode, CI

### 2. Make updates safe and hands-off
- [x] Persist check results and job history (SQLite) so restarts don't lose state
- [x] Snapshot before upgrade (Proxmox snapshot, Incus snapshot), with rollback button
- [x] Reboot action, and "reboot if required" option after upgrade
- [x] Scheduled checks plus notifications (ntfy, Gotify, Discord, Telegram, webhook) when updates are pending
- [ ] Per-target holds (skip kernel / Proxmox major upgrades)
- [x] A machine's own update command (e.g. `omv-upgrade`) instead of the built-in upgrade, from the config or the dashboard (password again, logged)
- [ ] Optional SSH for VMs that don't run the guest agent
- [x] Run upgrades detached on the target (`systemd-run`, or `setsid nohup`) so they survive a dashboard restart; the job picks them up again on start. The dashboard's own machine can now be updated and rebooted from here

### 3. Toward the "ultimate" dashboard
- [ ] Live resource view (CPU/RAM/disk) for nodes and guests
- [ ] Start/stop/restart guests
- [ ] Run commands on any machine or Docker container: a Run command box first (done, off by default), then an interactive terminal (SSH hosts, Proxmox LXCs and Docker containers on them; [spec](CONSOLE.md))
- [x] Docker/Compose stacks on any machine or guest: find them, check for newer images, update one stack (pull + `up -d`, optional image prune)
- [ ] Docker: schedule image checks, notify, update all stacks at once, per-container actions
- [x] Backup status: last backup per guest (vzdump/PBS), stale, not in a job, failed verification
- [x] Last backup for Incus (snapshots/exports) and plain hosts (folder, restic, borg)
- [x] "Back up" button on hosts: the host's own settings (Proxmox node config to PBS, Incus settings, SSH hosts' /etc), to the default place or a chosen folder/share, remembered per host
- [ ] Set a host's backup location from the setup page
- [ ] Notify when a backup goes stale
- [x] Login page with session cookies instead of the browser's Basic auth popup
- [ ] Single sign-on (OIDC)

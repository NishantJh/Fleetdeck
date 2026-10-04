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
- [ ] Persist check results and job history (SQLite) so restarts don't lose state
- [x] Snapshot before upgrade (Proxmox snapshot, Incus snapshot), with rollback button
- [ ] Reboot action, and "reboot if required" option after upgrade
- [ ] Scheduled checks plus notifications (ntfy, Discord, email) when updates are pending
- [ ] Per-target holds (skip kernel / Proxmox major upgrades)
- [ ] Optional SSH for VMs that don't run the guest agent
- [ ] Run upgrades detached on the target (e.g. `systemd-run`) so they survive a dashboard restart; then `protect:` is no longer needed for the dashboard's own LXC

### 3. Toward the "ultimate" dashboard
- [ ] Live resource view (CPU/RAM/disk) for nodes and guests
- [ ] Start/stop/restart guests
- [ ] Docker/Compose stacks inside guests: image update checks
- [ ] Backup status (Proxmox Backup Server)
- [ ] Proper login (OIDC) instead of basic auth

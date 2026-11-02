# Fleetdeck

One dashboard to manage a homelab: OS updates across Proxmox, Incus and plain hosts, plus backups,
snapshots, notifications and setup for each machine. Python 3.11+ (CI and the image use 3.12),
FastAPI, a static HTML/JS front end, SQLite for state.

## Map

Entry points
- `fleetdeck/__main__.py`: starts uvicorn on `PORT` (default 8080; `HOST` defaults to 0.0.0.0). `TLS_CERT`/`TLS_KEY` serve HTTPS directly. Data folder from `FLEETDECK_DATA`.
- `fleetdeck/app.py`: FastAPI app factory, routes for the dashboard, lifespan (scheduler, providers, store).
- `fleetdeck/setup_routes.py`: HTTP API for setup and structured per-machine Docker provider configuration.
- `fleetdeck/docker_management.py`: secret-aware Dockhand connection/mapping edits; no network requests.

Machines and how they are reached
- `fleetdeck/providers/__init__.py`: the provider protocol. A provider finds targets and returns an executor for each.
- `fleetdeck/providers/proxmox.py`: Proxmox nodes, their LXCs and VMs (VMs via the QEMU guest agent).
- `fleetdeck/providers/incus.py`: Incus instances and VMs.
- `fleetdeck/providers/ssh_hosts.py`: plain machines over SSH (Debian/Ubuntu hosts, Raspberry Pi, VPS).
- `fleetdeck/providers/demo.py`: fake targets for demo mode.
- `fleetdeck/executors.py`: runs one POSIX `sh` script as root on a target. Transports: SSH, `pct exec`,
  QEMU guest agent, Incus exec. Output lines stream live over SSH.
- `fleetdeck/inventory.py`: builds the machine list and its status from providers.
- `fleetdeck/tls.py`: pins the Proxmox and Incus API certificates by SHA-256 fingerprint.
- `fleetdeck/tunnels.py`: WireGuard tunnels (wireproxy) to machines the dashboard can't reach directly.

Updates and jobs
- `fleetdeck/updates.py`: the package-manager script every target runs, and the parsers for its output.
- `fleetdeck/jobs.py`: runs upgrade jobs and keeps their history.
- `fleetdeck/detached.py`: runs long scripts under `systemd-run` (or `setsid nohup`) so they survive a dashboard restart.
  Logs and exit codes go to files on the target.
- `fleetdeck/snapshots.py`: Proxmox and Incus guest snapshots before an upgrade. Only prunes snapshots whose names
  start with its prefix.
- `fleetdeck/scheduler.py`: checks every machine on a timer and sends a notification when something new needs attention.
- `fleetdeck/report.py`: plain-text report of what is failing, for help requests.

Backups
- `fleetdeck/backup.py`: the dashboard's own backup (`.hlmbackup`): config, keys and history, AES-256-GCM with a scrypt
  key from a passphrase. Written encrypted, downloaded by the browser, never stored on the server.
- `fleetdeck/backup_runs.py`: the "Back up" button on a host. Backs up host settings only, never guests.
  Proxmox: `/etc` (includes `/etc/pve`) over SSH, to Proxmox Backup Server storage or a local archive.
- `fleetdeck/agecrypt.py`: writes age passphrase files (`backup_password`), since the `age` program only takes a passphrase
  from a terminal. `age -d` opens them.
- `fleetdeck/host_backups.py`: reads the newest backup of a plain machine from a folder or a restic/borg repo.
  Read-only: restic runs with `--no-lock`, borg with `--bypass-lock`.

Setup and security
- `fleetdeck/onboarding.py`: the setup page's back end. Generates the per-machine bootstrap script, creates the app's
  SSH key, connects to a machine the first time. Holds the on-machine markers (see Rules).
- `fleetdeck/security.py`: auth with brute-force throttling, CSRF, security headers.
- `fleetdeck/config.py`: YAML config (`FLEETDECK_CONFIG`, default `config.yaml`), `${VAR}` expansion, the models
  for each machine type and tunnel. `config.example.yaml` documents the options.
- `fleetdeck/envvars.py`: `env(name)` reads `FLEETDECK_<name>`.
- `fleetdeck/store.py`: SQLite for check results, job history and hidden machines.
- `fleetdeck/console.py`, `console_routes.py`: the off-by-default Run command box (unlock, network gate, danger guard, audit log). Spec: `docs/CONSOLE.md`.
- `fleetdeck/notify.py`: ntfy, Gotify, Discord, Telegram, generic JSON webhook.
- `fleetdeck/models.py`: pydantic models for API payloads and results.

Front end (served from `fleetdeck/static/`, packaged via `pyproject.toml`)
- `index.html`, `app.js`, `style.css`: the dashboard.
- `setup.html`, `setup.js`: the setup page.
- `console.js`: the Run command dialog. `terminal.html`/`.js`/`.css`: the terminal window, served at `/terminal` with its
  own CSP (xterm.js sets style elements). `xterm.js`, `xterm.css`, `xterm-addon-fit.js` are vendored from npm
  (@xterm/xterm 6.0.0, @xterm/addon-fit 0.11.0, MIT, licence in `xterm-LICENSE.txt`); no CDN.
- `theme.js`: light/dark theme.

Other
- `tests/`: one `test_<module>.py` per module. Pytest with `asyncio_mode = "auto"`.
- `Dockerfile`: python:3.12-slim plus a pinned wireproxy build. Runs as uid 10001 (PUID/PGID to change). Data in `/data`.
- `docker-compose.yml`: the example stack. `docs/PLAN.md`: roadmap. `docs/screenshots/`: README images (demo data only).
- `.github/workflows/ci.yml`: ruff and pytest on every PR from this repo (pull requests from forks are skipped: jobs run on the owner's self-hosted runner). `image.yml`: builds and pushes the image. `ghcr-cleanup.yml`: cleans up old image versions on main.

## Run, test, lint

```bash
pip install -e ".[dev]"      # install dev deps first; without them ~100 tests fail for missing packages
pytest -q
ruff check . && ruff format --check .
```

Demo mode (fake machines, no infrastructure needed):

```bash
FLEETDECK_DEMO=1 python -m fleetdeck    # http://localhost:8080
```

Ruff: line length 120, rules E, F, W, I, UP, B. Run `ruff format .` before committing.

## Images and releases

- `main` pushes build `ghcr.io/nishantjh/fleetdeck:latest` and `:sha-…`.
- A git tag `v*` publishes the semver tags. Use three-part versions such as `v1.2.3`, which give `:1.2.3`, `:1.2`, `:1`.
- The old `homelab-manager` image name also gets tags as an alias.

## Rules

- Never break a machine. Setup, updates and backups run as root on machines the owner uses. Test on a demo or a
  non-critical target before changing a script that runs there.
- Keep these on-machine markers recognised, because machines set up earlier still have them:
  - SSH user `homelab` (`SSH_USER`), key comment `homelab-manager` (`KEY_COMMENT`).
  - Proxmox user `homelab@pve` and role `HomelabManager` (`PVE_USER`, `PVE_ROLE`).
  - `/etc/sudoers.d/homelab-manager`.
  - WireGuard and SSH comments that start with `homelab-manager`.
  - Backup and state paths under `/var/backups/homelab-manager` and `/var/lib/homelab-manager`.
  Renaming any of them needs a migration that accepts both names.
- Env vars are all `FLEETDECK_*`; the old `HOMELAB_*` names are no longer read. `FLEETDECK_PORT` is the host port that
  `docker-compose.yml` maps to the container's `PORT` (8080).
- Screenshots in `docs/screenshots/` and README images use demo data only. Never put real hostnames, IPs or names in them.
- Never copy, print or overwrite the app's SSH keys or the owner's keys. Keys stay on the server's `/data`.
- Nothing merges to `main` without the owner's explicit go-ahead.
- Keep changes small and tested. Add or update the matching `tests/test_<module>.py`.
- Do not commit `config.yaml`, state databases, backups or anything under `/data`.

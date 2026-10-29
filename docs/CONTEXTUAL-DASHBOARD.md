# OS-focused dashboard preview

This fork changes the dashboard, not the OS upgrade commands or snapshot behavior.

## What changes

- Three compact summary cards: machines needing updates, machines with security updates,
  and machines requiring a reboot. Labels use the correct singular/plural form, and the
  overview shows when the next automatic check will run.
- A short toolbar: **Check updates**, with less frequent tasks in a compact icon-led menu.
  The menu separates inventory tasks from backup and notification tools, and marks the
  optional hidden-machine view without changing the menu label.
- Single-line rows keep machine/state, type, OS, backup age, **Security**, **Package**,
  **Phased**, **Update**, **Reboot**, **Checked** and **More** in separate, left-aligned columns.
  Stable column proportions reserve flexible room for machine and OS names without letting
  the compact status/action columns drift apart.
- Apt packages deferred by Ubuntu/Debian phasing are detected with read-only simulations.
  Packages that apt lists separately as not upgrading, but which become installable when only
  the phase policy changes, are included in the green **Phased** count and identified as
  **Waiting** in the detail table. They are excluded from actionable counts and update jobs.
- **Update**, **Check** and **Retry** share the Update column and appear only when relevant;
  otherwise it shows a green **Up to date** state. A stopped guest shows a neutral
  **Not running** state in that column. A reboot
  requirement becomes a distinct gold **Reboot** button in its own column when the action is
  allowed, or a non-clickable **Required** status when configuration blocks it. Empty cells
  do not show placeholder dashes.
- Per-machine **More** menus use icons and concise labels for checks, configuration backups,
  hiding and any optional command-console or Docker actions that are actually available.
- All machines move backup notes into one neutral information popover beside the checked time;
  Proxmox nodes also include repository context there. Backup-note icons are removed from the
  backup column, while real backup timestamps and clear states such as **Not set up** and
  **Not scheduled** remain visible.
- Selecting machines reveals the bulk-update bar. Updates and reboots retain their confirmation dialogs.
- Platform/host groups and guest rows have a deterministic order, including after refreshes.
  Hosts use **Node** for Proxmox. A centered 2 px tree line uses compact guest indentation,
  rounded branches and an exact final endpoint to keep the hierarchy clear without wasting name
  space. Online and offline dots use matching green or red halos.
- The Fleetdeck logo links home, and dashboard/setup/login pages have a favicon.

Existing protections, snapshot defaults, authentication and CSRF protections remain in place.
This does **not** add an OMV-specific updater or an independent monitor-only OS policy.

## Docker stays with your existing manager

Docker management is off by default in this fork, including for existing configurations:

```yaml
docker_enabled: false
```

With this setting there is no Containers tab, Docker summary or Docker column/menu item.
Manual and bulk Docker check/deployment requests are rejected by the server. Scheduled image
checks are skipped even if an old config has `schedule.check_docker: true`. OS checks do not
perform the new optional Docker capability discovery.

This does not change the existing dashboard self-detection used to identify its host, or the
separately enabled command console. It also cannot stop a remote detached operation already
running when the dashboard restarts: finish existing Docker jobs before switching configuration.
Disabled Docker deployments are not resumed by the dashboard.

To use the optional Docker view later, explicitly set `docker_enabled: true` in `config.yaml`
and restart. Then OS checks discover Docker using `command -v docker` without pulling images.
The separate Containers view shows only known Docker hosts. Per-host settings allow:

- **Managed:** checks and deployments through Fleetdeck.
- **Monitor only:** image checks allowed, stack deployments blocked.
- **External manager:** an optional Dockhand/other HTTP(S) link; Fleetdeck Docker jobs blocked.
- **Disabled:** Fleetdeck Docker jobs blocked for that machine.

These per-host choices are stored in the state database and enforced for individual, bulk,
scheduled, queued and resumed jobs. Changing settings waits for active Docker jobs to finish.
Failed stack checks display **unknown / check failed**, not a successful up-to-date status.

**Important:** Fleetdeck's existing image check downloads images (`docker compose pull`);
it is not a read-only registry lookup. The UI asks for confirmation and says this explicitly.
Scheduled image checks additionally require `schedule.check_docker: true`. Neither an ordinary
dashboard refresh nor opening the Containers view pulls images or restarts containers.

## Try it without connecting to the homelab

The ordinary `docker-compose.yml` still points to the upstream published image. That image
does not include this branch's changes. Use a source build for this preview:

```sh
git clone --branch feat/contextual-update-dashboard https://github.com/avhove/Updates.git
cd Updates
docker compose -f docker-compose.preview.yml up -d --build
```

Open `http://localhost:8081` on the Docker machine. From another computer, use an SSH tunnel:

```sh
ssh -N -L 8081:127.0.0.1:8081 your-user@your-docker-host
```

Then open `http://localhost:8081` on that computer. This demo is bound to loopback because it
has no login. It uses fake machines and a separate `fleetdeck-preview_preview-data` volume.
Do not mount production config, keys, data or the Docker socket into it; do not add real
machines to the demo. Stop it without deleting its volume:

```sh
docker compose -f docker-compose.preview.yml down
```

After reviewing the demo, back up the live Fleetdeck data before deploying any source-built
image against it. Keep the current image available for rollback. No live deployment is done
by the preview instructions. The inherited GitHub workflows require the upstream author's
self-hosted runner; pushing this branch alone does not publish a new container image.

## Development checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest -q
pnpm install --frozen-lockfile
pnpm test
```

Use Python 3.12 and Linux for the full Python suite; some existing integration tests depend
on Linux `/proc`, GNU tools and permission to run local system commands. The Node tests
use an offline DOM with synthetic API responses; they do not open a browser or contact hosts.
Node dependencies are development-only and are not included in the runtime image.

### Preview verification

In the restricted development environment: 398 Python tests passed, 6 were skipped, and 16 documented
platform/sandbox-dependent tests were deselected after a full run reproduced those same failures.
All 8 offline dashboard tests, JavaScript syntax checks and Ruff checks passed. Browser visual
verification used only the built-in simulated machines; a Docker image build was not completed in
this environment. No real homelab machines were contacted or updated during development.

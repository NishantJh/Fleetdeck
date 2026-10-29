# OS-focused dashboard preview

This fork changes the dashboard, not the OS upgrade commands or snapshot implementation.

## What changes

- Four compact summary cards: machines needing updates, machines with security updates,
  machines requiring a reboot, and machines needing recheck. Updates and Security include the
  last successful results for offline/failed machines; those machines can also count toward recheck.
  Reboot uses current successful results only. Unchecked machines count toward recheck but do not
  imply known updates. Phased-only packages and hidden machines stay outside update/security totals.
  Labels use the correct singular/plural form, and the
  overview shows when the next automatic check will run.
  Clicking a card filters the machine list; clicking it again clears the filter. Parent nodes
  remain visible for context, with selection and the main action disabled when they do not match.
  KPI totals continue to describe the full inventory. Selections outside a filter are cleared.
- A short toolbar: **Check updates**, with less frequent tasks in a compact icon-led menu.
  The menu separates inventory tasks from backup and notification tools, and marks the
  optional hidden-machine view without changing the menu label.
- Single-line rows combine machine/state, type and a compact OS label in one flexible cell;
  the full OS name remains available on the label and in machine information. Backup age and
  job coverage retain separate columns; Last backup is capped at a compact 120 px width.
  The table has one caption row. Security, Package, Phased and the icon-only Reboot indicator
  stay together with centered compact cells. Status and Actions are separate and left-aligned.
- Updates available uses a purple package icon and regular-weight label; the filled Update
  action retains its download icon. Status is 150 px wide. Reboot
  stays gold, Up to date stays green with regular weight, failed checks stay red with regular weight, and unavailable/unchecked states
  use neutral icons. Full Security, Package and Phased cells open the package list, including
  empty cells when the row has known packages. They share a subtle hover highlight and native
  keyboard-accessible buttons; selection, Reboot and other actions remain independent.
  All groups share the same columns and fixed compact widths, including backup columns when any
  visible group has backup reporting. Machine names take priority over OS text when space is tight.
- Dashboard and machine menus share item spacing, text, icon sizing and hover treatment.
  Machines and Settings include an icon on the Dashboard button, Back up & restore, and Test
  notifications when notification targets are configured. Shared handlers open the encrypted-backup
  dialog directly on the current page and preserve passphrase checks, preview and explicit restore
  confirmation. Loading these pages never sends notifications or starts a backup.
- Machines and Settings have separate URLs and separate entries in More. `/machines` contains
  connection security (permanently expanded above the connection cards), scripts and connection
  management. `/settings` contains the full YAML editor, with validation, read-only handling and
  the existing separate apply/restart step. `/setup` retains the first-run setup-code and admin
  creation flow, and remains a compatible machine-management entry after setup. All pages and APIs
  retain the existing authentication and request protections; connection scripts are unchanged.
- Absolute dates in information popovers and dashboard/setup restore previews use
  **yyyy-mm-dd HH:mm:ss**, in the browser's local time zone with a 24-hour clock.
  Relative ages such as **2 h ago** remain unchanged.
- Apt packages deferred by Ubuntu/Debian phasing are detected with read-only simulations.
  Packages that apt lists separately as not upgrading, but which become installable when only
  the phase policy changes, are included in the green **Phased** count and identified as
  **Waiting** in the detail table. They are excluded from actionable counts and update jobs.
- Status shows **Up to date**, **Updates available**, **Check failed**, **Not running** or **Offline**.
  Actions shows one next action: **Update**, **Check**, **Retry**, or **Reboot**. When updates and a
  reboot are both needed, Update takes priority; manual reboot remains in More and the existing
  update confirmation can request reboot afterwards. A current reboot requirement uses a small
  gold box in the Reboot column, including when configuration prevents rebooting. Empty cells
  do not show placeholder dashes or an ambiguous Unknown reboot value.
- Failed checks preserve the last successful package/reboot snapshot separately in persistent
  state. Offline or failed machines show muted last-known package counts, expandable with a
  **Last known · age** notice. These results are included in update/security KPIs and their filters,
  but are excluded from update selection and actionable buttons; a stale
  reboot requirement is explained in Info rather than presented as current. Info also includes
  the latest error, attempt time and last successful check time. A successful check clears the snapshot.
  Historical results lost before this version cannot be recovered.
- Per-machine **More** menus use icons and concise labels for checks, configuration backups,
  hiding and any optional command-console or Docker actions that are actually available.
- Backup age and job coverage use separate compact columns: a neutral calendar icon appears only
  when a backup job covers the machine. A muted dash means no backup was found or backup
  information could not be read; Info explains which. Proxmox node configuration-backup cells stay blank.
  A job may exist even when Last backup is a dash. Ages use hours, days and weeks without changing job coverage.
  Exact dates, storage, counts, verification, job coverage and backup notes live in one neutral
  information popover in a dedicated compact **Info** column; Proxmox nodes also include repository context there.
- Selecting machines reveals the bulk-update bar. Updates and reboots retain their confirmation dialogs.
  The update dialog has a compact machine summary and aligned options. Snapshot first and Reboot
  if required both start unchecked every time; users explicitly opt in for each operation. This
  changes the manual confirmation default, not stored configuration or server-side snapshot behavior.
  Each nonzero update type in the confirmation has its own icon, including mixed Security,
  Package and Phased counts. Phased packages stay green and marked as waiting. Machine restrictions
  and helper-script notes remain visible beneath the type summary.
- Platform/host groups and guest rows have a deterministic order, including after refreshes.
  Hosts use **Node** for Proxmox. A centered 2 px tree line uses compact guest indentation,
  rounded branches and an exact final endpoint to keep the hierarchy clear without wasting name
  space. Online and offline dots use matching green or red halos.
- The Fleetdeck logo links home, and dashboard/setup/login pages have a favicon.
- Narrow screens render dedicated machine cards using the same update, backup and action logic
  as the desktop table. Type and OS sit below the name, status at the top right, and check time
  directly below status. Nonzero Security, Package and Phased counts are icon-only; the boxed
  reboot indicator aligns right. Update-type and reboot icons are 18 px; the reboot box is 32 px.
  Info follows the check time in a consistent location on every card. Backup age and job coverage
  stay together. Cards without an action omit the empty action footer and place More on the backup
  or a compact bottom-right utility row. Where an action exists, the footer has the action left and More right, with no divider.
  Mobile actions share a 96 × 36 px size, with expanded touch areas. Spacing around the icon row
  accounts for the visible label and badge edges, and status text is slightly larger at 13 px.
  Mobile cards have no selection controls; bulk selection remains available on desktop. Switching
  to mobile clears desktop selection so hidden bulk operations cannot remain pending. The toolbar
  places the controls on a separate row below the logo, shared across Dashboard, Machines and Settings. Icon controls are 44 px squares with even gaps; menus stay inside the viewport. Info aligns with the right edge of the card. KPIs use two columns with extra inset for their numbers. Expanded package details retain a local
  horizontal scroller. Menus and confirmation dialogs stay within the mobile viewport.
  Layout switching and interactions were checked in the local browser with simulated machines at
  320, 390 and 430 px widths, and the desktop table at 1200 px. Native iPhone Safari still needs
  a device check.

Existing protections, authentication and CSRF protections remain in place.
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

For the latest page split, shared tools and confirmation fixes: all 25 offline UI tests pass. They cover KPI
filters/reset, parent context, cross-group column structure, selection pruning, historical
counts, single/bulk update dialogs, cancellation, unchecked defaults and explicit option payloads.
Ruff and diff checks pass. The setup/security Python suite passes 55 tests and skips 4; three existing
shell tests were deselected after reproducing permission failures for local system commands in this
environment. The browser check used simulated machines for separate page navigation and mixed-type
confirmation icons. These checks do not contact homelab machines.
The 20 focused Python backup tests also pass after sharing the existing front-end backup handler.

For the status/actions and last-known-results revision: all 10 offline dashboard tests pass,
including failed/offline history, update/reboot priority, confirmation dialogs, selection,
backup-job coverage, column spans, escaped content and Docker-disabled behavior. The focused
Python suite passes 121 tests, skips 1 and deselects 2 after reproducing their existing macOS
incompatibilities (GNU `script` options and Linux `/proc` boot information). Ruff checks pass.
Browser access to the loopback demo was declined for this revision, so its rendered appearance
was not verified in a browser. No homelab machines were contacted.

In the restricted development environment: 398 Python tests passed, 6 were skipped, and 16 documented
platform/sandbox-dependent tests were deselected after a full run reproduced those same failures.
All 8 offline dashboard tests, JavaScript syntax checks and Ruff checks passed. Browser visual
verification used only the built-in simulated machines; a Docker image build was not completed in
this environment. No real homelab machines were contacted or updated during development.

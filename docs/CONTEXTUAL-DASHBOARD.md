# Container dashboard and Dockhand integration

This extends the existing OS dashboard and native Docker Compose workflow. Dockhand is
optional; installing Fleetdeck does not require Dockhand, Node or a JavaScript build step.

## Dashboard

- **All** shows an OS row and, where applicable, a container row for each machine.
- **Machines** shows OS information and package details.
- **Containers** shows container hosts and their container details.

OS and container rows have independent status, update counters, timestamps and actions.
Machine identity and backup context are shared in All. Opening details never starts a check
or installs updates. On phones, the same information appears in separate card sections.
Both detail panels have matching text headings and a top close button. Closing OS details
leaves container details open, and vice versa, and returns keyboard focus to the status control.
Container-only phone cards follow the OS card layout; empty counters and missing backup
placeholders do not reserve rows. Container detail actions fit their contents, with check times
beside their labels and failed attempts clearly distinguished.
Theme and Sign out are in the top More menu on narrow screens; Sign out requires authentication.

OS counters filter security, regular and phased package details. Container counters filter
image updates, review findings, coverage exceptions and failed checks. Clicking a status
opens the full list. Zero image-update counts and empty coverage counters are hidden.
The overview counts hosts with container image updates separately from machines with OS updates.

## Per-machine providers

Open **Docker management** from the machine's More menu. The selected machine is already
known; no Fleetdeck target ID needs to be entered in that dialog.

| Mode | Checks | Installation |
| --- | --- | --- |
| Managed by Fleetdeck | Original native Docker Compose image checks | Confirmed stack updates |
| Fleetdeck monitor only | Native Compose checks | Disabled for containers |
| Managed by Dockhand | Read saved reports; optional fresh registry checks | Disabled unless enabled for this mapping |
| Other external manager | Link only; no update-status integration | Open the configured manager |
| Disabled | No native Docker checks | No native container updates |

Machine-level monitor-only restrictions still apply independently. Provider selection does
not itself start a check or install an update. Native Docker remains the default policy;
there is no global `docker_enabled` option.

Ordinary OS checks also discover whether the Docker executable is installed, without calling
the daemon or downloading images. Native image comparison remains an explicit or scheduled
Docker operation. It covers Compose services, not standalone containers or newer version tags.
This general scope limitation is explained in details, without an empty coverage icon/tab. A completed,
successful native scan with no stacks shows **No stacks found**, a muted slashed-layers icon
and its scan time, without an update counter. Missing scans remain Not checked; stale or failed
scans retain their own status. An empty Compose scan does not establish that no standalone
containers exist.

Dockhand-mapped machines cannot run native Docker checks or stack updates. Other external and
Disabled modes also block native Docker jobs on the server, not just in the interface.

## Dockhand configuration

Use the per-machine dialog, or configure one shared Dockhand connection with explicit mappings:

```yaml
dockhand:
  url: https://dockhand.example
  token: ${FLEETDECK_DOCKHAND_TOKEN}
  poll_seconds: 300
  allow_checks: false
  environments:
    - target_id: your-exact-fleetdeck-target-id
      environment_id: 1
      allow_updates: false
```

For manual YAML configuration, obtain the exact target ID from Fleetdeck's authenticated
`/api/targets` response and the environment ID from Dockhand. Names and addresses are not used
to guess mappings. The dialog supplies the target ID automatically. Each target and environment
can be mapped only once; the current model supports one connection with up to 32 mappings.

URL, token, polling interval and fresh-check permission are shared by mapped machines.
Environment ID and installation permission belong to the individual mapping. The dialog shows
that distinction. Save connection/mapping changes, then Apply or restart Fleetdeck to activate
them. Native-only policy changes apply immediately. Configuration changes are refused while
conflicting jobs are active, and stale configuration revisions cannot overwrite newer edits.

A blank token field retains the saved token. Changing the URL requires entering a token again,
so the previous credential is not silently forwarded to another server. Tokens are never returned
by the provider settings/status APIs. The existing authenticated YAML editor still has access
to the full configuration, as it does for other server credentials.

TLS verification remains enabled. The Dockhand client does not follow redirects or inherit
proxy settings from the process environment. Use a dedicated token with the permissions needed
for the enabled operations. Existing authentication, CSRF and setup protections remain in place.

## Refreshing, checking and scheduling

| Action | Behaviour |
| --- | --- |
| Dashboard polling | Reads saved Dockhand reports at the configured polling interval |
| Refresh inventory | Rediscovers machines and reads saved reports immediately; starts no OS or registry check |
| Check OS updates | Checks OS packages only |
| Check container updates | Checks eligible native hosts and permitted Dockhand mappings |
| Check all | Checks OS packages and permitted container providers |
| Machine Update | Installs OS packages only |
| Container Update | Opens a separate confirmation for the selected provider |

When Check all includes eligible native Docker hosts, a dialog offers **Cancel**, **Check OS only**
and **Check OS & containers** before any work starts. Native checks pull Compose images, using
bandwidth/disk space, but do not restart services. Dockhand checks registry digests and metadata
without downloading image layers. With no eligible native hosts, Check all runs directly.
The separate container-only action retains download confirmation when native hosts participate.

The original scheduling flag is retained:

```yaml
schedule:
  check_every: 6h
  check_docker: false
```

- `check_docker: false`: Fleetdeck scheduled rounds check OS packages only.
- `check_docker: true`: scheduled rounds additionally request eligible container checks.
- Dockhand fresh checks also require `dockhand.allow_checks: true`, whether manual or scheduled.
- Dockhand's own schedule is never edited. Reading its reports does not request another check.
- Checks never install container updates. Fleetdeck's OS notification report is not extended
  to send Dockhand update alerts.

The collapsed **Current options and how to change them** section shows the running permissions,
provider, intervals and scheduled-check setting, with links to Docker management and Settings.

## Results and coverage

Details use a flat table sorted naturally by stack, then container/service name. Containers
without Compose metadata are labelled Standalone. Dockhand can report these; native checks
remain Compose-only. Runtime dots beside names describe running/stopped state independently
of image-update status.

| Finding | Presentation |
| --- | --- |
| Applicable image change | Image update reported; eligible updates can be selected for installation |
| Confirmed current image | Up to date |
| Newer version tag | Separate review line; changing the configured tag remains manual |
| Pinned digest or held update | Separate review finding, with cooldown where reported |
| Local image | No registry comparison available |
| Policy exclusion | Excluded by policy |
| New/replaced or unmatched container | Check result unavailable |
| Registry/authentication/rate-limit failure | Failed check; known findings may remain labelled last-known |

Findings can overlap: an image update and a newer-tag advisory may apply to the same container.
Review information is only shown when the provider supplies it; native checks do not discover
new version tags. Missing pending-update records do not prove a successful check.

Coverage exceptions remain visible in counters and per-container details. They do not become
an overall host status: if no updates, reviews, active checks or errors remain, the summary
says Up to date. Individual local, excluded and unknown rows retain their actual meanings.
If the last coverage exception clears while its filter is open, details return to All.

**Synced** means reports were fetched. **Last checked** means a matched per-container result or
labelled failed attempt. **Updated** means a confirmed deployment, not a registry-check timestamp.
Saved results are scoped to the connection, mapping, container ID and image, and persist in
Fleetdeck's state storage. Replacements cannot inherit old results by name alone. When state
storage is unavailable, results last only until restart. Failed reads leave saved results stale.

## Installing updates through Dockhand

Installation is off by default, enabled per mapping with `allow_updates: true` or **Install
container updates** in Docker management. The token must permit container recreation in Dockhand.
Enabling installation does not install anything or change a schedule.

The host's container row offers Update when eligible image updates exist. Individual rows also
offer Update; the details header does not repeat the host action. Opening the confirmation reads
fresh inventory/reports and lists selected containers and exclusions. Running containers are
selected by default; stopped containers can be explicitly selected and remain stopped. Dockhand
may also recreate dependent containers that share a selected container's network namespace.

Only confirmed image updates checked within 24 hours qualify. Local/unmatched/failed results,
version advisories, pinned digests, held updates and manager/dashboard self-updates are excluded.
The server revalidates mapping, policy, identity, image, state, labels and conflicting machine/host
jobs before sending the mapped batch-update request. The generic job endpoint cannot bypass
this confirmation route. No tag changes, image pruning or stack-file edits are requested.

Deployments use the existing job history, machine lock and concurrency limit. Old selected
results are invalidated before transmission. Failed, skipped, warning or malformed responses
are not reported as a fully successful batch. Uncertain requests are not retried or replayed
after restart; inspect Dockhand before retrying. Raw provider error payloads are not logged.

After Dockhand confirms a successful pull/recreation, Fleetdeck verifies the returned replacement
ID, name and image against fresh inventory. That container is Up to date as of the update, with
an Updated timestamp. No additional registry check is requested. The next ordinary scheduled or
manual check reassesses it; explicit later findings supersede the deployment confirmation.
Updates performed directly in Dockhand need a matching API result for the replacement identity.

## Proxmox guest boot

Eligible stopped Proxmox VMs and LXCs have a confirmed **Boot up** action. It uses the configured
Proxmox API client, requires the appropriate audit/power permissions and respects protected guests
and conflicting host jobs. It does not need a working guest executor. The job confirms the Proxmox
task and running state; uncertain starts are not automatically retried. After a start is accepted,
the status shows a spinner and **Booting up…** while queued or running. On completion, refreshed
inventory supplies the current machine state; booting does not imply a successful OS update check. No force or skiplock option
is sent. This is independent of container provider selection and installs no updates.

## Development and verification

Use the existing Python workflow, matching CI:

```sh
pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest -q
```

Use Python 3.12 on Linux for the complete suite; existing integration tests rely on Linux tools
and `/proc`. Added Python tests cover provider policies, saved findings/history, authentication,
configuration changes, Dockhand updates and Proxmox boot using mocks or fake executors.

For UI review, use the existing demo mode or the simulated Docker example:

```sh
FLEETDECK_DEMO=1 python -m fleetdeck
# Optional simulated Dockhand mapping; never contacts the example manager:
FLEETDECK_CONFIG=config.docker-demo.yaml python -m fleetdeck
```

Use separate demo state/configuration. Do not supply production credentials or add real machines
to the demo. No Node/pnpm files or JavaScript test harness are required in this contribution;
local UI checks are kept separately in the fork's development tooling. A real browser is still
needed to assess layout and touch behaviour. Dockhand compatibility depends on its inventory,
pending-update, scheduled-history, check and batch-update API response formats; this work has
been verified with mocks and simulated machines, not against the user's homelab.

For a manual UI review using fake machines:

1. Run Check all to populate OS results, then compare All, Machines and Containers.
2. With a native Docker host discovered, open Check all and verify Cancel starts nothing,
   OS only excludes containers, and OS & containers includes the configured providers.
3. Open status and individual counters; verify their detail filters and independent timestamps.
4. Open Docker management and inspect check/install permissions; cancelling saves nothing.
5. Open an eligible Dockhand Update confirmation; inspect selection/exclusions, then cancel.
6. At phone width, compare All, Machines and Containers; check names, status/check times,
   backup spacing, content-sized actions and theme/logout placement in the top More menu.
7. Close OS and container details independently with the top button and verify focus returns
   to the corresponding status. Check both native and external-provider headers.

Use mocked Proxmox API tests for guest boot; do not point the demo at a real Proxmox host.

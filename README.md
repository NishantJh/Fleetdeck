# Fleetdeck

> **Built with help from Claude Code, tested on real homelab hardware.** It has been run on a Proxmox VE host
> with Proxmox Backup Server, an Incus host, a Raspberry Pi 4 and an Ubuntu VPS. CI runs the linter and the
> test suite on every change, and the owner approves each merge. It has **not** had an independent security
> audit, so read [Safety](docs/DETAILED-README.md#safety) before you point it at machines you care about.

A self-hosted dashboard to update Proxmox, Incus, LXC, Docker and Linux servers from one place. Built for
homelabs: it covers Proxmox VE hosts, LXCs and VMs, Incus, Docker Compose stacks, and plain Debian or Ubuntu
machines such as a Raspberry Pi or VPS. See what's pending and update it, back up each machine's setup, and
get notified when something needs attention.

![The dashboard (demo data)](docs/screenshots/dashboard-light.png#gh-light-mode-only)
![The dashboard (demo data)](docs/screenshots/dashboard-dark.png#gh-dark-mode-only)

* Checks and updates Proxmox VE (hosts, LXCs and VMs), Incus, and plain Linux machines such as a Raspberry Pi or VPS.
* Shows pending packages and reboots, updates on demand, and streams each job's output to the Activity panel.
* Takes snapshots before upgrades and updates Docker Compose stacks.
* Checks on a schedule and sends notifications through ntfy, Gotify, Discord, Telegram or a JSON webhook.
* Shows when each machine was last backed up, and can back up the settings of a host.

## Container providers and dashboard

The dashboard has All, Machines and Containers views, with independent OS/container status,
check times and actions. Native Docker Compose checks and stack updates remain available;
optional Dockhand integration adds saved reports, fresh checks and confirmed container updates.
Choose the provider per machine, including monitor-only, another external manager or disabled.
Stopped Proxmox VMs and LXCs also have a confirmed Boot up action where permitted.

See [Container dashboard and Dockhand integration](docs/CONTEXTUAL-DASHBOARD.md) for configuration,
check schedules, update permissions, result meanings and demo instructions.

## Quick start

1. Run [`docker-compose.yml`](docker-compose.yml) with `docker compose up -d`, or deploy it as a stack in your container manager.
2. Open `http://<this machine>:8080`. The setup page asks for the setup code from the container's log, then creates the admin login.
3. Add each machine, paste its generated script on it, and press Connect.

## Settings

Most installs change nothing here. These are set in the `environment:` part of [`docker-compose.yml`](docker-compose.yml),
or in a `.env` file next to it. A `.env` value only reaches the container once the compose file passes it on
(details in the [guide](docs/DETAILED-README.md#settings)).

* **`FLEETDECK_PORT`**: the host port, 8080 by default. If 8080 is taken, put `FLEETDECK_PORT=8081` in `.env`.
* **Image tag**: `image: ghcr.io/nishantjh/fleetdeck:1` takes every 1.x update. Use `:1.2.3` to pin one release.
* **`PUID` / `PGID`**: the user the app runs as, if the data folder has the wrong owner. Example: `PUID: "1000"`.
* **`FLEETDECK_BACKUP_FOLDER`**: where "Back up" saves archives, with that folder mounted into the container.
* **`TLS_CERT` / `TLS_KEY`**: serve HTTPS directly, using certificate files in `/data`.
* **`FLEETDECK_CONSOLE`** (`"1"` turns on Run command, see below) and **`FLEETDECK_DEMO`** (`"1"` shows demo machines): both off by default.

Everything else, such as the login, machines and notifications, is set on the setup page, which writes `config.yaml`.
After changing the compose file, run `docker compose up -d` again. A plain restart does not reload it.

## Run commands from the dashboard

Run command is off by default. To turn it on, add this under the Fleetdeck service's `environment:` in
[`docker-compose.yml`](docker-compose.yml), then run `docker compose up -d`:

```yaml
environment:
  FLEETDECK_CONSOLE: "1"
```

Then it appears in the dashboard:

* Each running machine's row gets a **Run command…** button. Stopped and offline machines don't show it.
* **Open terminal** is inside the Run command dialog. It appears only on SSH hosts, Proxmox nodes and Proxmox LXCs.
  Proxmox VMs get Run command only.

It needs a dashboard login (set on the setup page). Without one, the console stays off and the log says so. It
only answers from private networks and Tailscale, and asks for your password again each time you unlock it. Commands
run as root, so turn it on only if you need it. The full rules are in [docs/CONSOLE.md](docs/CONSOLE.md).

## Full documentation

The complete guide, with features, backups, install steps, configuration, security, HTTPS and development notes, is in **[docs/DETAILED-README.md](docs/DETAILED-README.md)**.

## Try it without any infrastructure

```bash
pip install -e ".[dev]"
FLEETDECK_DEMO=1 python -m fleetdeck   # http://localhost:8080
```

## Support

Fleetdeck is free. If it saves you time, you can support it on [Ko-fi](https://ko-fi.com/nishant19836) or [PayPal](https://paypal.me/nishantjha1659).

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).

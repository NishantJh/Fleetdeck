# Fleetdeck

> **Built with help from Claude Code, tested on real homelab hardware.** It has been run on a Proxmox VE host
> with Proxmox Backup Server, an Incus host, a Raspberry Pi 4 and an Ubuntu VPS. CI runs the linter and the
> test suite on every change, and the owner approves each merge. It has **not** had an independent security
> audit, so read [Safety](docs/security.md#safety) before you point it at machines you care about.

One dashboard for the whole homelab: see what's pending and update it, keep Docker Compose stacks
current, back up each machine's setup, and get notified when something needs attention.

![The dashboard (demo data)](docs/screenshots/dashboard-light.png#gh-light-mode-only)
![The dashboard (demo data)](docs/screenshots/dashboard-dark.png#gh-dark-mode-only)

* Checks and updates Proxmox VE (hosts, LXCs and VMs), Incus, and plain Linux machines such as a Raspberry Pi or VPS.
* Shows pending packages and reboots, updates on demand, and streams each job's output to the Activity panel.
* Takes snapshots before upgrades and updates Docker Compose stacks.
* Checks on a schedule and sends notifications through ntfy, Gotify, Discord, Telegram or a JSON webhook.
* Shows when each machine was last backed up, and can back up the settings of a host.

Details are in [Features](docs/features.md) and [Backups](docs/backups.md).

## Quick start

1. Run [`docker-compose.yml`](docker-compose.yml) with `docker compose up -d`, or deploy it as a stack in your container manager.
2. Open `http://<this machine>:8080`. The setup page asks for the setup code from the container's log, then creates the admin login.
3. Add each machine, paste its generated script on it, and press Connect.

The full steps are in [Install and setup](docs/install.md).

## Documentation

* [Features](docs/features.md): what each machine type gets, and how updates, checks and stacks work
* [Backups](docs/backups.md): host backups, where archives go, encryption, and the dashboard's own backup
* [Install and setup](docs/install.md): setup steps, what the script and Connect do, tunnels, the data folder, upgrades
* [Configuration](docs/configuration.md): setting up by hand, and what each platform needs
* [Security](docs/security.md): safety notes, security settings, and putting Fleetdeck behind HTTPS
* [Development](docs/development.md): running the tests and linter

## Try it without any infrastructure

```bash
pip install -e ".[dev]"
FLEETDECK_DEMO=1 python -m fleetdeck   # http://localhost:8080
```

## Support

Fleetdeck is free. If it saves you time, you can support it on [Ko-fi](https://ko-fi.com/nishant19836) or [PayPal](https://paypal.me/nishantjha1659).

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).

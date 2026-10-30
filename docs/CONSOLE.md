# Spec: run commands on any machine

Status: draft for review. No code yet.

Type a command, or open a terminal, on any machine Fleetdeck manages: a Proxmox or Incus host, a plain SSH host,
an LXC or Incus container, a VM, or a Docker container on any of them.

## Recommendation in short

1. **Build it in two steps.** First a **Run command** box (one command, its output, its exit code) on every kind of
   machine. Then an **interactive terminal** on the machines whose transport can give a real terminal.
2. **Off by default, and only switched on from the server.** `console.enabled: true` in `config.yaml` or
   `FLEETDECK_CONSOLE=1`. There is no switch in the web page, so a stolen dashboard password alone can't turn it on.
3. **It needs a login and a private network.** It refuses to start without `auth`, and only answers private and
   loopback addresses. Over plain HTTP it still works, under a red warning that the password and every command cross
   the network unencrypted; `console.allow_plain_http: false` blocks it there.
4. **Unlock with the password again.** Opening it asks for the password, which gives a short-lived unlock (15 minutes
   idle, 1 hour at most) held only in the page's memory.
5. **Commands run as root, and the page says so.** That is what every transport already gives. There is no pretend
   "safe user".
6. **A seatbelt, not a wall.** Commands that match a short list of destructive patterns need the machine's name typed
   to confirm. This stops slips, not attackers; the gates above are the security.
7. **Everything is written down twice.** An audit log in the dashboard that the page can't edit or delete, and a
   syslog line on the machine itself. A notification goes out when a terminal opens or a guarded command runs.

## What exists today

Fleetdeck already runs scripts as root on every machine, through `fleetdeck/executors.py`:

| Machine | Transport | Live output | Can give a terminal (PTY) |
| --- | --- | --- | --- |
| Proxmox node, Incus host, plain host | SSH (`SSHExecutor`), `sudo -n` | yes | yes (asyncssh PTY) |
| Proxmox LXC | `pct exec` over the node's SSH (`PctExecutor`) | yes | yes (`pct enter` over an SSH PTY) |
| Proxmox VM | QEMU guest agent API (`PveAgentExecutor`) | no, at the end | no |
| Incus container or VM | Incus REST exec (`IncusExecutor`) | no, at the end | yes (exec with `interactive: true`, websockets) |
| Docker container | not yet; `stacks.py` runs `docker` on the machine above | as its machine | as its machine, with `docker exec -it` |

So the feature adds no new reach into the lab: the server can already do all of this. What it adds is a path from a
browser to a root shell. The design is about guarding that path.

## Threats this must handle

| Threat | Answer |
| --- | --- |
| Someone has the dashboard password | Off unless enabled on the server; private networks only by default; audit log and notifications show use. |
| An unattended, logged-in browser | Password again to unlock; unlock lives in page memory, ends after 15 idle minutes and on reload. |
| Password sniffed on plain HTTP | Over plain HTTP the password and the session cookie travel in the clear; the console warns on every open over plain HTTP, and `allow_plain_http: false` blocks it. |
| Another site drives the browser (CSRF) | Existing `cross_site` check covers POSTs. WebSockets are GET, so the terminal checks `Origin` itself. |
| Command text breaks out of quoting | The command is one argument to `sh -c`, quoted with `shlex.quote`, as every script is today. Container names are checked against the list the machine returned and a name pattern. |
| Output attacks the page (HTML, escape codes) | One-off output is shown as text (`textContent`), with control characters other than tab and newline stripped. The terminal renders through xterm.js, which handles escape codes. |
| A slip: `rm -rf /` on the wrong machine | The pattern guard plus typing the machine's name. |
| The dashboard breaks itself | Its own container is not offered; commands on the machine it runs on carry a warning. |

## UX

### Run command (step 1)

- Each machine's menu gets **Run command…**. A machine that runs Docker also lists its containers under it, each with
  the same entry.
- The dialog has: the machine (and container) name, a line that says **"Runs as root on <machine>"**, a multi-line
  command box, a timeout (default 60 s, up to 1 h), **Run**, and an output pane.
- Output streams line by line where the transport streams (SSH, `pct exec`); otherwise it shows when the command ends,
  with "running…" until then. The exit code and duration show at the end. **Stop** ends it.
- The output pane keeps the last 1 MB; a **Copy** button copies it.
- The last 20 commands (from the audit log, this browser's user) are offered in a history dropdown. Nothing is stored
  in the browser.
- One machine at a time in step 1. Running on several machines at once is useful (`df -h` everywhere) but multiplies
  mistakes; it is left for later, with a typed count to confirm.

### Terminal (step 2)

- **Open terminal** next to Run command, on machines that can give one (see the table). On a Proxmox VM the entry
  is missing with a note: "the guest agent can't give a terminal; Run command works". When the VM SSH option on the
  roadmap lands, VMs reached over SSH get a terminal too.
- It opens in a panel over the dashboard using xterm.js, vendored into `fleetdeck/static/` (no CDN, so the CSP stays
  `'self'`). Check before building that xterm.js works under the current CSP; it may inject a `<style>` element, which
  `default-src 'self'` blocks.
- Header: machine name, "root", how long it has been open, **Close**. Resizing the panel resizes the remote terminal.
- At most 2 terminals open at once; one closes after 15 minutes without keystrokes or output, and every one closes
  after 4 hours.

## Turning it on

```yaml
console:
  enabled: false          # FLEETDECK_CONSOLE=1 also turns it on
  terminal: true          # the interactive terminal, once built; false keeps only Run command
  allow_from:             # client addresses allowed to use it (after trusted_proxies)
    - 127.0.0.0/8
    - ::1/128
    - 10.0.0.0/8
    - 172.16.0.0/12
    - 192.168.0.0/16
    - fc00::/7
    - 100.64.0.0/10       # Tailscale, Headscale, NetBird
  allow_plain_http: true  # works over http:// from the allowed networks, with a warning; false blocks it
  unlock_minutes: 15
  keep_days: 90           # audit log retention
  notify: true            # notify when a terminal opens or a guarded command runs
  exclude: []             # machine names or ids that never get it
```

- **VPN.** A WireGuard VPN that hands out private addresses (10.x, 192.168.x) is covered as it is. Tailscale, Headscale
  and NetBird use 100.64.0.0/10, which is in the default list. Any other VPN range is added to `allow_from`.
- **Behind a reverse proxy or tunnel** (Caddy, Pangolin, Cloudflare Tunnel), every request arrives from the proxy's
  own address, which is usually private, so internet visitors would look private too. The client address therefore
  comes from `X-Forwarded-For` only when the proxy is listed in `trusted_proxies`. When a request arrives from a
  private address but carries `X-Forwarded-For` from a proxy that isn't trusted, the console refuses it and says
  to add the proxy to `trusted_proxies`.
- Enabled only from the config file or the environment, both of which need access to the server. The setup page shows
  whether it is on and how to turn it on, but has no switch.
- With `enabled: true` but no `auth`, the dashboard logs a warning and leaves it off.
- In first-run (setup) mode it is off.
- In demo mode it works against the fake machines with canned output, so it can be tried and screenshotted.

## Security details

### Secure connection

- A request counts as secure when the dashboard serves HTTPS itself (`TLS_CERT`), when it comes through a trusted proxy
  that says `X-Forwarded-Proto: https` (uvicorn already honours `trusted_proxies`), or when it comes from loopback.
- On plain HTTP from any other address the dialog and the terminal show a red warning: "This connection isn't
  encrypted. Your password and every command and its output can be read on the network. Put the dashboard behind
  HTTPS (see docs, Caddy)." The audit log marks such runs as "http". This builds on the existing plain-HTTP banner.
- With `allow_plain_http: false` the API answers 403 there instead, and the page shows the same text in place of the
  dialog.

### Unlock (re-auth)

- `POST /api/console/unlock {password}` checks the password with the existing scrypt code. A wrong one counts in
  `LoginThrottle` like a failed login.
- It returns a random 32-byte token, kept in server memory (lost on restart), tied to the client address, with the idle
  and absolute expiry above. The page keeps it in a JS variable only, never in `localStorage` or a cookie.
- Every console call sends it in `X-Console-Token`; the terminal sends it as the first websocket message, so it never
  sits in a URL or an access log.
- **Lock** in the dialog drops it at once.
- Later, when OIDC login lands, unlock can ask for a fresh OIDC login or a TOTP code instead.

### Privilege

- Commands run as root, through the same paths and the same `sudo -n` rule as updates. No machine needs a script re-run.
- Inside a Docker container they run as the container's default user, with a **Run as root in the container** tick box
  (`docker exec -u 0`).
- A "run as user" choice (`su -s /bin/sh user -c`) is easy to add later; it is not a security boundary, since the
  dashboard is root, so it isn't in the first step.

### Dangerous command guard

Before running, the server checks the command (lower-cased, whitespace squeezed) against a short list:

- wiping or formatting: `rm -rf /`, `rm -rf /*`, `rm -rf ~`, `mkfs`, `wipefs`, `dd … of=/dev/`, `> /dev/sd…`,
  `shred /dev/`, `zpool destroy`, `zfs destroy`, `lvremove`, `vgremove`, `pvremove`, `sgdisk -Z`;
- power: `reboot`, `shutdown`, `poweroff`, `halt`, `init 0`, `init 6`, `systemctl reboot|poweroff|halt`;
- guests: `pct destroy`, `qm destroy`, `incus delete`, `docker rm -f`, `docker system prune`, `docker volume rm`;
- locking yourself out: `iptables -F`, `nft flush`, `ufw enable`, stopping or disabling `ssh`/`sshd`, `passwd -l`,
  touching `/etc/sudoers.d/homelab-manager` or the `homelab` user's `authorized_keys`;
- fork bombs: `:(){`.

A match shows what matched and asks for the machine's name to be typed. The confirmed command is run and logged as
"guarded". The page says plainly that this is a safety net for slips. It can't stop a determined user (`eval`, base64,
a script file), and it doesn't look inside a terminal session, where keystrokes are not parsed.

On top of that:
- a command on a Proxmox node or the machine the dashboard runs on gets a yellow note "this is <host>; guests and the
  dashboard depend on it";
- the dashboard's own Docker container is not offered (it would let the page print the app's SSH keys, and stopping
  it stops the dashboard). `stacks.own_container_id` already finds it.

### Limits

- Every one-off command runs under `timeout -k 5 <seconds>` on the target (coreutils and busybox both have it), so
  Stop and timeouts end it even where closing the connection doesn't (guest agent, Incus).
- At most 4 one-off commands at a time across the dashboard, and 1 per machine.
- A command can run while an update job is running on the same machine, with a note that `apt`/`dpkg` may be locked.
- Command text is at most 16 KB.

### Exposure over HTTP

New routes, all behind the existing auth guard and CSRF check, and all answering 404 when the console is off:

| Route | What |
| --- | --- |
| `GET /api/console` | on/off, terminal on/off, whether this request is allowed (secure, network), unlocked or not |
| `POST /api/console/unlock` · `POST /api/console/lock` | unlock and lock |
| `GET /api/targets/{id}/containers` | Docker containers on a machine (`docker ps -a`, not only Compose ones) |
| `POST /api/console/run` | `{target, container?, command, timeout, confirm?}`; returns a run id |
| `GET /api/console/runs/{id}` | output from an offset, exit code when done (the page polls, like jobs) |
| `POST /api/console/runs/{id}/stop` | stop it |
| `GET /api/console/audit` | the audit log, newest first |
| `WS /api/console/terminal` | step 2: first message `{token, target, container?, cols, rows}`, then raw I/O and resize messages |

The websocket handler checks `Origin` matches `Host`, the token, the network and the secure connection itself, because
the HTTP middleware's CSRF check doesn't cover a GET upgrade.

## Audit log

- A new SQLite table `console_log` in `store.py`: time, user, client address, user agent, machine id and name,
  container, kind (`run`, `terminal`, `unlock`, `unlock-failed`; also `update-command` and `update-command-failed`
  for changes to a machine's update command, written even when the console is off), command text, guarded yes/no, plain HTTP yes/no,
  exit code, duration,
  and for one-off runs the last 4 KB of output.
- For terminal sessions it records open and close with the duration, not the keystrokes: they would hold passwords
  typed into other programs. A `console.record_terminal: true` option can save full transcripts later if wanted.
- The page shows it under Activity and can't edit or delete it. Old rows go after `keep_days`.
- On the machine, each run first calls `logger -t fleetdeck-console "<user>@<ip>: <first 200 chars of the command>"`
  (or the terminal open), so the machine's own journal has a record the dashboard can't erase.
- It is part of `state.db`, so the encrypted dashboard backup carries it.

## How it is built on the existing code

- `fleetdeck/console.py`: settings, the unlock tokens, the guard patterns, the run registry and the audit writes.
- `fleetdeck/console_routes.py`: the routes above, added in `app.py` only when enabled.
- `fleetdeck/executors.py`: one-off runs use `run`/`run_command` as they are. Docker wraps the command as
  `docker exec [-u 0] <id> sh -c <quoted>`, run through the machine's own executor. Step 2 adds an `open_terminal`
  method to `SSHExecutor` (PTY), `PctExecutor` (`pct enter <vmid>` over an SSH PTY) and `IncusExecutor`
  (interactive exec over websockets with the pinned client certificate; this needs a websocket client library).
- `fleetdeck/store.py`: the `console_log` table.
- `fleetdeck/config.py` and `config.example.yaml`: the `console` block.
- `fleetdeck/static/`: the Run command dialog in `app.js`, xterm.js and the terminal panel in step 2.
- Tests: `tests/test_console.py` (guard patterns, unlock expiry and throttling, network and HTTPS gating, audit rows,
  quoting of commands and container names), `tests/test_console_routes.py` (404 when off, 403 on HTTP, websocket
  `Origin` check), plus demo-mode runs. Try every transport on a non-critical machine before release.

## Not in this spec

- Running one command on several machines at once.
- File upload and download.
- Saved commands or snippets.
- A terminal for Proxmox VMs without SSH (Proxmox's own serial console proxy).
- Per-user permissions: the dashboard has one login today.

## Decisions (Nishant, 2026-10-19)

1. Networks: private and loopback only by default, plus 100.64.0.0/10 so a VPN (Tailscale and the like) works for debugging.
2. Plain HTTP on the LAN: allowed, with a warning.
3. Notifications when a terminal opens or a guarded command runs: on by default.

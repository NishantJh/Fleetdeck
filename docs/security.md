# Security

## Safety

Read this before you add machines. The dashboard can change things on them, and some of those
changes are hard to undo.

* It runs commands as root on each machine you add, through its own SSH key for a `homelab` user
  with passwordless sudo. Give that key a `from=` address (see [Install](install.md)) so it only works
  from the dashboard. The setup script changes only its own `homelab` user and sudo rule, and never
  touches your own SSH keys.
* It upgrades only when you press *Update*, and reboots only when you ask. A Proxmox node that runs a
  guest you put under `protect:` is never rebooted from here.
* The setup script never changes `sshd_config` or the firewall, except the UDP port of a tunnel you
  chose. Removing a machine on the setup page gives you a script that undoes its setup.
* Keep the dashboard on your LAN or behind a VPN, with a login (`auth:`, set by the setup page). If you
  reach it across a network, use HTTPS.
* Report security problems privately, as described in [SECURITY.md](../SECURITY.md).

## Security settings


* **SSH host keys are always checked.** Every machine's key goes in `known_hosts:` (the setup page pins them
  for you). A machine without one is refused with a message saying so; `insecure_skip_host_key_check: true`
  on that entry opts out.
* **Proxmox and Incus certificates:** set `tls_fingerprint` (shown in the example config) rather than
  `verify_ssl: false`. It pins the exact certificate, works with the self-signed ones both ship and
  with IP addresses, and the dashboard warns at start-up about any API with checking off.
* **Restrict the SSH key** on every machine: prefix the dashboard's key line in `~/.ssh/authorized_keys`
  with `from="<dashboard IP>",restrict` (`restrict` turns off port, agent and X11 forwarding and the PTY,
  none of which the dashboard uses). A stolen key then only works from the dashboard's address. On a VPS,
  use a non-root user with passwordless sudo (`sudo: true`), since that key plus sudo is root on the VPS.
* **Least privilege for the Proxmox token:** use the `homelab@pve` user and `HomelabManager` role that
  Connect makes, rather than a token under `root@pam`. The dashboard also has root SSH to the node, so this
  mainly limits the damage if the token alone leaks.
  It is not read-only: with `Sys.Modify` and `VM.GuestAgent.Unrestricted` (and no privilege separation)
  it can run commands as root inside any VM with the guest agent and change node settings.
* **The Docker host:** keep the LXC that runs Docker unprivileged (`unprivileged: 1`, `nesting=1`), and
  if it has several network interfaces, bind the port to the LAN one in the compose file.
* This app can run commands as root on everything it manages. Keep it on your LAN or behind a VPN,
  and set `auth:` in the config.
* `auth:` is required. Without it, nothing but the setup page is served, and that page only
  works through the one-time link in the logs until it has created a login (`allow_no_auth: true`
  skips this when a proxy in front does the login). The setup page stores only a scrypt hash of
  the password (`password_hash`). Repeated wrong passwords lock that IP out for 5 minutes.
* Basic auth sends the password with every request, so use HTTPS: either set `TLS_CERT` and
  `TLS_KEY` (paths inside the container, e.g. `/data/tls.crt`) to serve HTTPS directly, or put
  Caddy, Traefik or `tailscale serve` in front.
* The page shows a warning when it is opened over plain HTTP from anything but localhost.
* Set `allowed_hosts` to the names/IPs you open the dashboard by, so other websites can't reach it
  through DNS rebinding.
* Behind a reverse proxy, set `trusted_proxies` to its address (or the Docker network's range), so
  the lockout applies to each visitor's IP rather than the proxy's. The proxy must pass the original
  `Host` header, because `allowed_hosts` and the cross-site check compare against it. Caddy and
  Traefik do this by default; with nginx add `proxy_set_header Host $host;`.
* Cross-site form posts are refused, and the page can't be framed by another site.

## Put it behind HTTPS

The login is HTTP Basic, so over plain HTTP the password crosses your network in clear text, and it is
effectively root on every machine the dashboard manages. Most people already run a reverse proxy; put
Fleetdeck behind it. With [Caddy](https://caddyserver.com) (a certificate is made for you):

```
fleetdeck.example.com {
    reverse_proxy fleetdeck:8080
}
```

Then, in the config, set `trusted_proxies` to the proxy's address (so the login lockout counts each visitor,
not the proxy) and `allowed_hosts` to `fleetdeck.example.com`, and drop the `ports:` mapping from
`docker-compose.yml` so only the proxy can reach the container. Traefik, Nginx Proxy Manager, Pangolin and
`tailscale serve` work the same way. Without a proxy, `TLS_CERT` and `TLS_KEY` serve HTTPS from the
container directly.

# Security Policy

## Supported versions

Only the latest release (the newest `v*` tag, published as `ghcr.io/nishantjh/fleetdeck:latest`
and its numbered tags) and `main` get security fixes. Older tags do not.

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately instead:

1. Go to the repository's **Security** tab and choose **Report a vulnerability**, or open
   <https://github.com/NishantJh/Homelab-Manager/security/advisories/new>.
2. Include the image tag or commit, the steps to reproduce, and what an attacker could do.

The maintainer aims to reply within 7 days and will keep you updated until a fix is released.
Credit is given in the release notes unless you ask otherwise.

## Scope

In scope: the dashboard itself, its web login and setup pages, the setup scripts it generates, SSH and
TLS handling (host keys, certificate pinning), backup encryption, and anything that lets someone
without a login run commands or read keys and tokens.

Out of scope:

* Running the dashboard open to the internet without a login, or without HTTPS when it is reached
  across a network. The README's [Safety](README.md#safety) and Security sections describe the setup
  that is expected.
* Weaknesses in Proxmox, Incus, Docker, or other software the dashboard talks to. Report those to
  their own projects.
* A machine the dashboard manages that was already compromised before it was added.

## Hardening

The README's [Security](README.md#security) section lists the settings that matter most: `auth:`,
`allowed_hosts`, `trusted_proxies`, `known_hosts`, `tls_fingerprint`, and restricting the dashboard's
SSH key with `from=` and `restrict`.

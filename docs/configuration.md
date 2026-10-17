# Configuration

The setup page writes an ordinary `config.yaml`, and everything below can also be set by hand
(see [`config.example.yaml`](../config.example.yaml); `${VARS}` in it are read from the environment).

### What each platform needs

* **Proxmox:** an API token (Datacenter → Permissions → API Tokens) with `VM.Audit`/`Sys.Audit` to
  list guests, `Datastore.Audit`, `Datastore.AllocateSpace` and `VM.Backup` to see backups,
  `VM.Snapshot` and `VM.Snapshot.Rollback` for snapshots, `Sys.Modify` on `/nodes` so the host check can refresh package lists, and
  guest-agent exec rights to update VMs (`VM.GuestAgent.Unrestricted` on PVE 9, `VM.Monitor` on
  PVE 8). VMs need the *QEMU Guest Agent* option on and `qemu-guest-agent` installed inside.
  Connect creates a `homelab@pve` user with a `HomelabManager` role holding these privileges.
* **Proxmox host updates** are checked through PVE itself, the same as *Updates → Refresh* in the
  web UI, so they come from whatever is enabled under *Updates → Repositories*. The dashboard shows
  which repositories were used and warns if the Enterprise repo is on without a subscription.
  PVE's API has no non-interactive upgrade call (its Upgrade button opens a shell), so the upgrade
  runs `apt-get dist-upgrade` over SSH, against those same repositories. SSH is also used for LXCs.
* **Incus:** expose the HTTPS API and trust a client certificate restricted to the projects you use
  (`incus config set core.https_address :8443`, then `incus config trust add-certificate client.crt --restricted --projects default`),
  or run the dashboard on the Incus host and point `socket:` at `/var/lib/incus/unix.socket`. An unrestricted
  certificate is a full Incus admin, which is root on the Incus host. Connect does this for you.
* **Raspberry Pi, VPS and other hosts:** SSH. Non-root users need passwordless sudo and `sudo: true`.


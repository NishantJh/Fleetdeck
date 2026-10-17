# Backups

* **Backups:** each Proxmox guest shows when it was last backed up (vzdump or Proxmox Backup
  Server), turns orange once that's older than `backup_max_age_hours` (48 by default), and is flagged
  *none*, *no job* (not in any scheduled backup job) or *verify failed*. This needs `Datastore.Audit`,
  `Datastore.AllocateSpace` and `VM.Backup` (Proxmox lists a guest's backups only to a user that could
  make them); without them the column shows `?` and says why. A token made by an older version lacks
  the last two, so every guest shows *none*: press *Connect* on that machine again (it updates the role),
  or set `backup_max_age_hours: 0` to turn the column off.
  Incus instances show their newest snapshot or export (the dashboard's own pre-upgrade snapshots
  don't count). Every other machine (Proxmox nodes, the Incus host, a Raspberry Pi, a VPS) shows
  *not set up* until you add a `backup:` entry to its SSH settings (under `ssh:` for Proxmox and Incus):
  the newest file in a folder (`type: path`), or the newest snapshot in a restic or borg repository
  (read with `--no-lock` / `--bypass-lock`, so it never gets in a running backup's way). See
  `config.example.yaml`.
* **Back up a host:** the **Back up** button on a host row saves the machine's settings, so you can set it
  up again quickly. It never backs up guests or instances (create those separately), and nothing on the
  machine is stopped or changed. Each run has a live log. The dialog asks where the archive goes, with three
  choices:
  * **The default place** (what you get if you change nothing):
    * **Proxmox node:** its Proxmox Backup Server storage, with the credentials and encryption key Proxmox
      already keeps for it. With no PBS, or if the push fails, a local archive in
      `/var/backups/homelab-manager` **on the node**.
    * **Raspberry Pi, VPS or other SSH machine:** an archive in the folder or restic/borg repository of its
      `backup:` entry; without one, `/var/backups/homelab-manager` **on that machine**.
    * **Incus host:** a settings file in `host-backups/` in the data volume, plus `/etc` of the host when it
      has SSH settings.
  * **On the dashboard** (recommended for one shared place for every machine): the archive is sent back over
    the connection the dashboard already uses (SSH, through a tunnel too) and saved as
    `<backup folder>/fleetdeck/<machine name>/homelab-config-<date>_<time>.tar.gz` (the Incus host also gets
    `incus-settings-<date>_<time>.json` there). Nothing is written on the machine and nothing needs mounting
    there. The dashboard only ever writes inside its `fleetdeck/` folder, and changes nothing else in the
    mount, including owners and permissions. Each file is written under a temporary name and renamed when
    complete, so a failed run leaves no half archive.
    **The backup folder** is `host-backups/` in the data volume unless you set `backup_folder:` in the
    config (or `FLEETDECK_BACKUP_FOLDER`). To keep them on a NAS or any other folder, mount it into the
    container and point the setting at it. With Docker Compose (full path on the Docker host, here
    `/apps/backups`):

    ```yaml
    services:
      fleetdeck:
        environment:
          FLEETDECK_BACKUP_FOLDER: /backups
        volumes:
          - /apps/backups:/backups
    ```

    The folder must exist (mount it first) and be writable by the user the dashboard runs as (uid 10001
    by default; `PUID`/`PGID` only if you really need another). The dashboard never changes the owner of a
    mounted folder. At start it tries to write a small test file there and, if that fails, the log says so
    and names the uid: allow that user on the NAS, or map all users to one that can write (a share owned by
    `nobody` usually already does). A missing or unwritable folder is an error when you back up, never a
    silent write to the container's disk.
  * **A folder or network share on the machine:** a full path on that machine (for Proxmox, on the node),
    with any share already mounted there. For Incus it is a path inside the dashboard's container. A folder
    that does not exist is an error.

  **What a Proxmox node backup contains:** all of `/etc` on the node, which includes `/etc/pve` (the
  cluster filesystem: `storage.cfg`, every guest's config in `qemu-server/` and `lxc/`, users, ACLs and
  API tokens, firewall, backup jobs, certificates, and `priv/` with the storage passwords and keys),
  plus network (`/etc/network/interfaces`, `hosts`, `hostname`), `fstab`, apt sources, `vzdump.conf`,
  modprobe/GRUB settings and the node's SSH host keys. The archive form also has `/usr/local/etc`,
  `/usr/local/bin`, `/var/spool/cron` and the installed package list; the PBS form is `/etc` only.
  It does **not** contain guests or their disks, `/root`, `/home`, `/var/lib/vz`, ZFS pool layout or
  anything else outside those folders. It holds secrets (`/etc/shadow`, SSH host keys, on Proxmox
  `/etc/pve/priv`). Set `backup_age_recipient` to an [age](https://github.com/FiloSottile/age) public key (`age-keygen` makes the pair; keep the private key in your
  password manager) and archives are written encrypted as `.tar.gz.age`, with no passphrase to type. Restore with
  `age -d -i key.txt file.tar.gz.age | tar xz`. A machine that writes the archive itself needs `age` installed.
  Without the setting the archive is not encrypted, so keep it where you keep other secrets:
  turn on client-side encryption for the PBS storage, or use a folder only you can read (archives are
  written readable by root only). To rebuild a node: reinstall Proxmox, restore `/etc/network`, then
  `/etc/pve` (or re-add storage and restore the guests from their own backups).

  The newest 7 archives (or settings files) per machine are kept in each folder; older ones this dashboard
  made there are deleted, and nothing else. *Always back up this machine here* remembers the choice for
  that machine (each machine has its own); leave it unticked to choose every time. Backup is a manual
  button: schedule real backups on the hosts, and let *Last backup* watch them.

**Backups from the dashboard:** the **Backup** button downloads one file with the config, the
login, every key and token, and the job history, encrypted with a passphrase you choose
(AES-256-GCM, key from scrypt). Keep it off this server; without the passphrase nobody, including
you, can open it. To restore, pick the file in the same dialog: it first shows what is in it, then
replaces the current files and restarts once you confirm. On a fresh install, open the dashboard, enter the setup code from the logs and use **Or restore from a backup** instead of creating a login: it brings back the login, the machines and the keys, so the machines need nothing run again. What was there before is kept as an
encrypted file in `/data/backups/`, with the same passphrase. Key files the backup doesn't have
are left in place. Files outside `/data` (say a key file the config points at elsewhere) aren't
in it.

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from ..backup_runs import BackupRun, SshHostBackup
from ..config import ProxmoxConfig, SSHConfig
from ..executors import DEFAULT_TIMEOUT, LineCallback, PctExecutor, PveAgentExecutor, SSHExecutor
from ..host_backups import HostBackups
from ..models import BackupInfo, CheckResult, ExecResult, Package, Target
from ..snapshots import PveSnapshots
from ..tls import PinnedTransport
from ..updates import REBOOT_SCRIPT, parse_apt, parse_reboot
from . import Discovered

log = logging.getLogger(__name__)
# The PVE API doesn't say which suite an update comes from; apt does, after the API's refresh.
APT_LIST_SCRIPT = "LC_ALL=C apt list --upgradable 2>/dev/null"
PROTECTED_NOTE = "protected: runs this dashboard, update it from its own console"
# Listing backup storages (PBS in particular) is slow, so it is done less often than discovery.
BACKUP_TTL = 300.0
NO_BACKUP_STORAGE = "the API token can't see any backup storage (it needs Datastore.Audit on /storage)"
# PVE lists a backup only to a user who could have made it: Datastore.AllocateSpace on the storage and
# VM.Backup on the guest. Without them a storage reads fine but looks empty.
BACKUP_PRIVS = ("Datastore.AllocateSpace", "VM.Backup")
NO_BACKUP_PRIVS = (
    "Proxmox hides backups from the API token: it needs Datastore.AllocateSpace and VM.Backup. "
    "Open Setup and press Connect on this machine again to update its permissions"
)


@dataclass
class BackupIndex:
    """Backups found on all storages, by VMID."""

    latest: dict[int, tuple[datetime, str, bool | None]] = field(default_factory=dict)
    counts: dict[int, int] = field(default_factory=dict)
    not_in_job: set[int] | None = None  # None: unknown
    note: str | None = None


class PveNodeExecutor:
    """The Proxmox host itself.

    Checks go through the PVE API, exactly like Updates > Refresh in the web UI: PVE refreshes
    package lists from the repositories configured under Updates > Repositories and reports
    what is upgradable. The API has no non-interactive upgrade call (the UI opens a shell for
    that), so the upgrade itself is `apt-get dist-upgrade` over SSH, against the same repositories.
    """

    def __init__(self, client: httpx.AsyncClient, node: str, ssh: SSHExecutor | None):
        self.client = client
        self.node = node
        self.ssh = ssh

    @property
    def detached(self) -> bool:
        return self.ssh is not None

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        if self.ssh is None:
            raise RuntimeError("upgrading the Proxmox host needs ssh settings for this node")
        return await self.ssh.run(script, on_line, timeout)

    async def _get(self, path: str, **params):
        r = await self.client.get(f"/nodes/{self.node}{path}", params=params or None)
        r.raise_for_status()
        return r.json()["data"]

    async def _wait_task(self, upid: str, timeout: float = 600) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = await self._get(f"/tasks/{upid}/status")
            if status.get("status") == "stopped":
                return status
            if time.monotonic() > deadline:
                raise TimeoutError("PVE package refresh task did not finish")
            await asyncio.sleep(1)

    async def check(self, on_line: LineCallback | None = None) -> CheckResult:
        def say(line: str) -> None:
            if on_line:
                on_line("stdout", line)

        notes: list[str] = []
        version = await self._get("/version")
        os_name = f"Proxmox VE {version.get('version', '?')}"

        repos = await self._get("/apt/repositories")
        enabled = [r["name"] for r in repos.get("standard-repos", []) if r.get("status") == 1]
        notes.append("Repositories (from PVE): " + (", ".join(enabled) or "no standard Proxmox repository enabled"))
        for err in repos.get("errors", []):
            notes.append(f"Repository error in {err.get('path')}: {err.get('error')}")
        if any("Enterprise" in name for name in enabled):
            sub = await self._get("/subscription")
            if str(sub.get("status", "")).lower() != "active":
                notes.append(
                    "An Enterprise repository is enabled without an active subscription, so refreshing it "
                    "fails. Switch to No-Subscription under Updates > Repositories in PVE."
                )
        for note in notes:
            say(f"== {note}")

        say("== Refreshing package lists through the Proxmox API")
        r = await self.client.post(f"/nodes/{self.node}/apt/update")
        if r.status_code == 403:
            raise RuntimeError("the API token needs Sys.Modify on /nodes to refresh package lists")
        r.raise_for_status()
        upid = r.json()["data"]
        status = await self._wait_task(upid)
        for entry in await self._get(f"/tasks/{upid}/log", limit=1000):
            say(entry.get("t", ""))
        exit_status = status.get("exitstatus", "")
        error = None if exit_status == "OK" or exit_status.startswith("WARNINGS") else f"refresh failed: {exit_status}"

        packages = [
            Package(name=p["Package"], current=p.get("OldVersion"), available=p["Version"])
            for p in await self._get("/apt/update")
        ]

        # The API doesn't say whether a reboot is pending (e.g. a newer kernel is installed).
        reboot = None
        if self.ssh is not None:
            try:
                reboot = parse_reboot((await self.ssh.run(REBOOT_SCRIPT, timeout=60)).stdout)
            except Exception as e:  # informational only; don't fail the check
                say(f"! could not check for a pending reboot: {e}")
            try:
                res = await self.ssh.run(APT_LIST_SCRIPT, timeout=60)
                security = {p.name for p in parse_apt(res.stdout.splitlines()) if p.security}
                packages = [p.model_copy(update={"security": p.name in security}) for p in packages]
            except Exception as e:  # noqa: BLE001 - informational only
                say(f"! could not tell which updates are security updates: {e}")
        return CheckResult(
            package_manager="apt (via PVE)",
            os=os_name,
            packages=packages,
            reboot_required=reboot,
            checked_at=datetime.now(UTC),
            error=error,
            notes=notes,
        )


class ProxmoxProvider:
    """Discovers nodes, LXCs and VMs through the Proxmox VE API.

    * Node (host): checked through the PVE API, upgraded over SSH (see PveNodeExecutor).
    * LXC: `pct exec` over the node's SSH connection.
    * VM: QEMU guest agent exec through the API.
    """

    def __init__(self, cfg: ProxmoxConfig):
        self.cfg = cfg
        self.name = cfg.name
        self._backups: BackupIndex | None = None
        self._backups_at = 0.0
        self.snapshots: dict[str, PveSnapshots] = {}
        self.backup_runs: dict[str, BackupRun] = {}
        self.host_backups = HostBackups()
        self.client = httpx.AsyncClient(
            base_url=cfg.api_url.rstrip("/") + "/api2/json",
            headers={"Authorization": f"PVEAPIToken={cfg.token_id}={cfg.token_secret}"},
            verify=cfg.verify_ssl,
            transport=PinnedTransport(cfg.api_url, cfg.tls_fingerprint) if cfg.tls_fingerprint else None,
            timeout=30,
        )

    def _node_ssh(self, node: str, node_ips: dict[str, str]) -> SSHConfig | None:
        if node in self.cfg.node_ssh:
            return self.cfg.node_ssh[node]
        if self.cfg.ssh is None:
            return None
        # `pct exec` only works on the node that runs the container, so in a cluster reuse the
        # shared ssh settings but point them at each node's own (cluster) address. The node the
        # user's ssh.host already names keeps it; set node_ssh if cluster IPs aren't reachable.
        if len(node_ips) > 1 and node in node_ips and self.cfg.ssh.host not in (node, node_ips[node]):
            return self.cfg.ssh.model_copy(update={"host": node_ips[node]})
        return self.cfg.ssh

    async def _node_ips(self) -> dict[str, str]:
        try:
            status = await self._get("/cluster/status")
        except httpx.HTTPError:
            return {}
        return {e["name"]: e["ip"] for e in status if e.get("type") == "node" and e.get("ip")}

    async def _get(self, path: str):
        r = await self.client.get(path)
        r.raise_for_status()
        return r.json()["data"]

    async def _vm_blocker(self, node: str, vmid: int) -> str | None:
        """Why a running VM can't be updated through the guest agent, or None if it can."""
        try:
            conf = await self._get(f"/nodes/{node}/qemu/{vmid}/config")
        except httpx.HTTPError as e:
            # One unreadable VM shouldn't hide the rest of the node.
            return f"could not read VM config: {e}"
        if str(conf.get("ostype", "")).startswith("w"):
            return "Windows guests are not supported yet"
        if not str(conf.get("agent", "0")).startswith(("1", "enabled=1")):
            return "QEMU guest agent is not enabled"
        return None

    async def _backup_index(self, nodes: list[str]) -> BackupIndex:
        if self._backups is not None and time.monotonic() - self._backups_at < BACKUP_TTL:
            return self._backups
        index = BackupIndex()
        errors: list[str] = []
        # (node, storage, shared) to read. Shared storages (PBS, NFS) list the same backups from
        # every node, so they're read once.
        to_read: list[tuple[str, str, bool]] = []
        queued: set[str] = set()
        inactive: set[str] = set()
        for node in nodes:
            try:
                r = await self.client.get(f"/nodes/{node}/storage", params={"content": "backup", "enabled": 1})
                r.raise_for_status()
                storages = r.json()["data"]
            except httpx.HTTPError as e:
                errors.append(f"{node}: {e}")
                continue
            for st in storages:
                name, shared = st["storage"], bool(st.get("shared"))
                if shared and name in queued:
                    continue
                if not st.get("active", 1):
                    if shared:  # it may be mounted on another node only; try that one
                        inactive.add(name)
                    else:
                        errors.append(f"{name} on {node}: storage inactive")
                    continue
                queued.add(name)
                to_read.append((node, name, shared))
        errors += [f"{name}: storage inactive" for name in sorted(inactive - queued)]

        async def content(node: str, name: str) -> list[dict]:
            r = await self.client.get(f"/nodes/{node}/storage/{name}/content", params={"content": "backup"})
            r.raise_for_status()
            return r.json()["data"]

        # A PBS datastore with many snapshots can take seconds to list; read them all at once.
        results = await asyncio.gather(*(content(n, s) for n, s, _ in to_read), return_exceptions=True)
        read_any = False
        seen_volids: set[tuple[str | None, str]] = set()
        for (node, name, shared), res in zip(to_read, results, strict=True):
            if isinstance(res, BaseException):
                errors.append(f"{name}: {res}")
                continue
            read_any = True
            for item in res:
                # The same volid on two nodes' local storages is two different backups.
                key = (None if shared else node, item.get("volid"))
                if key in seen_volids or not item.get("vmid") or not item.get("ctime"):
                    continue
                seen_volids.add(key)
                vmid = int(item["vmid"])
                when = datetime.fromtimestamp(int(item["ctime"]), UTC)
                state = (item.get("verification") or {}).get("state")
                verified = {"ok": True, "failed": False}.get(state)
                index.counts[vmid] = index.counts.get(vmid, 0) + 1
                if vmid not in index.latest or when > index.latest[vmid][0]:
                    index.latest[vmid] = (when, name, verified)
        if not read_any:
            index.note = "; ".join(errors) or NO_BACKUP_STORAGE
        elif errors:
            index.note = "some storages could not be read: " + "; ".join(errors)
        elif not index.latest and await self._lacks_backup_privs():
            index.note = NO_BACKUP_PRIVS
        try:
            missing = await self._get("/cluster/backup-info/not-backed-up")
            index.not_in_job = {int(g["vmid"]) for g in missing}
        except httpx.HTTPError as e:
            log.warning("%s: could not tell which guests are in no backup job: %s", self.name, e)
        # Don't remember a complete failure for the whole TTL; the next refresh tries again.
        if read_any or not errors:
            self._backups, self._backups_at = index, time.monotonic()
        return index

    async def _lacks_backup_privs(self) -> bool:
        """Whether the token is missing a privilege PVE wants before it lists backups."""
        try:
            perms = await self._get("/access/permissions")
        except httpx.HTTPError:
            return False
        held = {priv for privs in (perms or {}).values() for priv, ok in privs.items() if ok}
        return not set(BACKUP_PRIVS) <= held

    def _update_command(self, vmid: int, name: str | None) -> str | None:
        """The guest's own update command from the config, by VMID or by name."""
        cmds = self.cfg.update_commands
        return cmds.get(str(vmid)) or (cmds.get(name) if name else None)

    def _backup_for(self, vmid: int, index: BackupIndex | None) -> BackupInfo | None:
        if index is None:
            return None
        in_job = None if index.not_in_job is None else vmid not in index.not_in_job
        if vmid not in index.latest:
            # Without a readable storage, "no backup" would be a guess.
            return BackupInfo(stale=index.note is None, in_job=in_job, note=index.note)
        when, storage, verified = index.latest[vmid]
        stale = datetime.now(UTC) - when > timedelta(hours=self.cfg.backup_max_age_hours)
        return BackupInfo(
            last=when, storage=storage, count=index.counts[vmid], stale=stale, in_job=in_job, verified=verified,
            note=index.note,
        )  # fmt: skip

    async def discover(self) -> Discovered:
        found: Discovered = []
        snapshots: dict[str, PveSnapshots] = {}
        backup_runs: dict[str, BackupRun] = {}
        node_ips = await self._node_ips()
        nodes = await self._get("/nodes")
        backups = None
        if self.cfg.backup_max_age_hours:
            online = [n["node"] for n in nodes if n.get("status") == "online"]
            try:
                backups = await self._backup_index(online)
            except Exception as e:  # noqa: BLE001 - backups are extra; never hide the guests
                backups = BackupIndex(note=f"could not read backups: {e}")
        # Each node's own backup check runs over its SSH, all nodes at once.
        node_backups = await asyncio.gather(
            *(
                self.host_backups.for_machine(
                    n["node"], self._node_ssh(n["node"], node_ips), n.get("status") == "online"
                )
                for n in nodes
            )
        )
        for node, node_backup in zip(nodes, node_backups, strict=True):
            nname = node["node"]
            online = node.get("status") == "online"
            ssh_cfg = self._node_ssh(nname, node_ips)
            # Without a verifiable host key the node is used as if it had no SSH settings, and says why.
            ssh_problem = ssh_cfg.host_key_problem if ssh_cfg else None
            ssh = SSHExecutor(ssh_cfg) if ssh_cfg and not ssh_problem else None
            prefix = f"proxmox:{self.name}:{nname}"
            host = Target(
                id=prefix,
                name=nname,
                platform="proxmox",
                source=self.name,
                update_command=self.cfg.update_commands.get(nname),
                kind="host",
                status=node.get("status", "unknown"),
                updatable=online,
                check_only=ssh is None,
                note=(None if ssh else ssh_problem or "check only: add ssh settings to upgrade this node")
                if online
                else "offline",
                backup=node_backup,
                can_backup=online and ssh is not None,
            )
            found.append((host, PveNodeExecutor(self.client, nname, ssh)))
            if not online:
                continue
            if ssh is not None:
                backup_runs[prefix] = SshHostBackup(ssh, ssh_cfg.backup, proxmox=True, name=nname)
            # Rebooting the node would take down the protected guest, i.e. this dashboard.
            protected_here: list[str] = []

            for ct in await self._get(f"/nodes/{nname}/lxc"):
                vmid = int(ct["vmid"])
                if vmid in self.cfg.exclude:
                    continue
                running = ct.get("status") == "running"
                updatable = running and ssh is not None
                note = (None if ssh else ssh_problem or "needs node ssh for pct exec") if running else "not running"
                if vmid in self.cfg.protect:
                    updatable, note = False, PROTECTED_NOTE
                    if running:
                        protected_here.append(ct.get("name") or f"ct{vmid}")
                t = Target(
                    id=f"{prefix}:lxc:{vmid}",
                    name=ct.get("name") or f"ct{vmid}",
                    platform="proxmox",
                    source=self.name,
                    update_command=self._update_command(vmid, ct.get("name")),
                    kind="lxc",
                    parent=nname,
                    status=ct.get("status", "unknown"),
                    updatable=updatable,
                    note=note,
                    backup=self._backup_for(vmid, backups),
                    can_snapshot=vmid not in self.cfg.protect,
                    monitor_only=vmid in self.cfg.monitor_only,
                )
                if t.can_snapshot:
                    snapshots[t.id] = PveSnapshots(self.client, nname, "lxc", vmid)
                found.append((t, PctExecutor(ssh, vmid) if ssh else None))  # type: ignore[arg-type]

            for vm in await self._get(f"/nodes/{nname}/qemu"):
                vmid = int(vm["vmid"])
                if vmid in self.cfg.exclude or vm.get("template"):
                    continue
                running = vm.get("status") == "running"
                note = None if running else "not running"
                updatable = running
                if vmid in self.cfg.protect:
                    updatable, note = False, PROTECTED_NOTE
                    if running:
                        protected_here.append(vm.get("name") or f"vm{vmid}")
                elif running:
                    note = await self._vm_blocker(nname, vmid)
                    updatable = note is None
                t = Target(
                    id=f"{prefix}:vm:{vmid}",
                    name=vm.get("name") or f"vm{vmid}",
                    platform="proxmox",
                    source=self.name,
                    update_command=self._update_command(vmid, vm.get("name")),
                    kind="vm",
                    parent=nname,
                    status=vm.get("status", "unknown"),
                    updatable=updatable,
                    note=note,
                    backup=self._backup_for(vmid, backups),
                    can_snapshot=vmid not in self.cfg.protect,
                    monitor_only=vmid in self.cfg.monitor_only,
                )
                if t.can_snapshot:
                    snapshots[t.id] = PveSnapshots(self.client, nname, "qemu", vmid)
                found.append((t, PveAgentExecutor(self.client, nname, vmid)))
            if protected_here:
                host.no_reboot = f"runs protected guest {', '.join(protected_here)}; reboot it from the Proxmox UI"
        self.snapshots = snapshots
        self.backup_runs = backup_runs
        return found

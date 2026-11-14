"""Services-only sources: machines whose containers are listed in the Services tab and nothing else.

A source is never a Target: it is not in the machine list, never checked for updates, snapshotted, backed up,
notified about or reachable from any job. It is read with one fixed read-only listing (``docker inspect`` over SSH,
or ``GET`` on the Docker Engine / Portainer API) and the result is only kept in memory. Links are never fetched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

import httpx

from .config import ServiceSource
from .executors import SSHExecutor
from .models import NetworkAddress, PortMapping, SourceStatus, StackService, StacksResult, Target
from .stacks import _parse_exposed, _parse_labels, _parse_ports
from .urls import safe_host, safe_url

log = logging.getLogger("fleetdeck.sources")

MAX_CONTAINERS = 500
MIN_INTERVAL = 20.0  # seconds between scans of the same source: the button can't hammer a NAS

# Fixed, read-only. Prints the networks' drivers, then the inspect JSON of every container.
SSH_SCRIPT = r"""
command -v docker >/dev/null 2>&1 || { echo "__DOCKER__=none"; exit 0; }
ids=$(docker ps -aq 2>/dev/null) || { echo "__DOCKER__=down"; exit 0; }
echo "__DOCKER__=yes"
docker network ls --format '__ND__ {{.Name}} {{.Driver}}' 2>/dev/null
echo "__INSPECT__"
if [ -n "$ids" ]; then docker inspect $ids; else echo "[]"; fi
"""

_UNRAID_TOKEN = re.compile(r"\[(IP|PORT:(\d{1,5}))\]")


def _unraid_url(template: str, address: str, ports: list[PortMapping]) -> str | None:
    """Unraid's ``net.unraid.docker.webui`` label: ``http://[IP]:[PORT:8080]/``. [PORT:n] is n's published port."""
    mapped = {p.container_port: p.host_port for p in ports if p.protocol == "tcp"}

    def fill(match: re.Match) -> str:
        if match.group(1) == "IP":
            return address
        port = int(match.group(2))
        return str(mapped.get(port, port))

    return safe_url(_UNRAID_TOKEN.sub(fill, template.strip()[:512]))


def _finish(item: dict, labels: dict[str, str], ports: list[PortMapping], address: str) -> dict:
    proxy, urls, ui, category = _parse_labels({k: v for k, v in labels.items() if isinstance(v, str)})
    webui = labels.get("net.unraid.docker.webui")
    if isinstance(webui, str) and (url := _unraid_url(webui, address, ports)) and url not in urls:
        urls.insert(0, url)
    return {"proxy": proxy, "explicit_urls": urls[:5], "ui": ui, "category": category, **item}


def parse_inspect(stdout: str, address: str) -> StacksResult:
    """``SSH_SCRIPT`` output to a scan result. Anything unexpected is skipped, never raised."""
    docker, drivers, body = "yes", {}, ""
    head, sep, body = stdout.partition("__INSPECT__")
    for line in head.splitlines():
        if line.startswith("__DOCKER__="):
            docker = line.split("=", 1)[1].strip()
        elif line.startswith("__ND__ "):
            parts = line.split(None, 2)
            if len(parts) == 3:
                drivers[parts[1]] = parts[2].strip()[:32]
    error = None
    if docker == "none":
        error = "Docker is not installed on this machine"
    elif docker == "down":
        error = "Docker did not answer this account. Is it running?"
    try:
        data = json.loads(body) if sep else []
    except ValueError:
        data, error = [], error or "could not read Docker's answer"
    services: list[StackService] = []
    for c in (data if isinstance(data, list) else [])[:MAX_CONTAINERS]:
        if not isinstance(c, dict):
            continue
        cfg_, state = c.get("Config") or {}, c.get("State") or {}
        net = c.get("NetworkSettings") or {}
        labels = cfg_.get("Labels") or {}
        ports = _parse_ports(json.dumps(net.get("Ports") or {}))
        networks = [
            NetworkAddress(name=str(n)[:128], ip=str((v or {}).get("IPAddress") or "")[:64], driver=drivers.get(n, ""))
            for n, v in list((net.get("Networks") or {}).items())[:20]
        ]
        cid = str(c.get("Id") or "")[:64]
        name = str(c.get("Name") or "").lstrip("/")[:128] or cid[:12]
        extra = _finish(
            {
                "container_id": cid,
                "container_name": name,
                "network_mode": str((c.get("HostConfig") or {}).get("NetworkMode") or "")[:64],
                "ports": ports,
                "exposed_ports": _parse_exposed(json.dumps(cfg_.get("ExposedPorts") or {})),
                "networks": networks,
            },
            labels if isinstance(labels, dict) else {},
            ports,
            address,
        )
        services.append(
            StackService(
                name=name, image=str(cfg_.get("Image") or "")[:512], status=str(state.get("Status") or ""), **extra
            )
        )
    return StacksResult(standalone=services, checked_at=datetime.now(UTC), error=error)


def parse_api(containers: list, networks: list, address: str) -> StacksResult:
    """``GET /containers/json?all=1`` (+ ``/networks``) to a scan result."""
    drivers = {str(n.get("Name")): str(n.get("Driver") or "")[:32] for n in networks if isinstance(n, dict)}
    services: list[StackService] = []
    for c in containers[:MAX_CONTAINERS]:
        if not isinstance(c, dict):
            continue
        names = c.get("Names") or []
        cid = str(c.get("Id") or "")[:64]
        name = (str(names[0]) if names else "").lstrip("/")[:128] or cid[:12]
        ports = [
            PortMapping(
                host_ip=str(p.get("IP") or "")[:64],
                host_port=int(p["PublicPort"]),
                container_port=int(p.get("PrivatePort") or 0),
                protocol=str(p.get("Type") or "tcp")[:8],
            )
            for p in (c.get("Ports") or [])[:50]
            if isinstance(p, dict)
            and isinstance(p.get("PublicPort"), int)
            and 0 < int(p.get("PrivatePort") or 0) < 65536
        ]
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        networks_ = [
            NetworkAddress(name=str(n)[:128], ip=str((v or {}).get("IPAddress") or "")[:64], driver=drivers.get(n, ""))
            for n, v in list(nets.items())[:20]
        ]
        labels = c.get("Labels") or {}
        extra = _finish(
            {
                "container_id": cid,
                "container_name": name,
                "network_mode": str((c.get("HostConfig") or {}).get("NetworkMode") or "")[:64],
                "ports": ports,
                "networks": networks_,
            },
            labels if isinstance(labels, dict) else {},
            ports,
            address,
        )
        services.append(
            StackService(name=name, image=str(c.get("Image") or "")[:512], status=str(c.get("State") or ""), **extra)
        )
    return StacksResult(standalone=services, checked_at=datetime.now(UTC))


def source_address(source: ServiceSource) -> str | None:
    host = source.address or (source.ssh.host if source.ssh else None)
    if not host and source.url:
        host = source.url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    return safe_host(host or "") or None


def source_target(source: ServiceSource) -> Target:
    """A stand-in used only to build cards. Not in the inventory, so no job or check can ever address it."""
    return Target(
        id=f"src:{source.name}",
        name=source.name,
        platform="ssh",
        source=source.name,
        kind="host",
        address=source_address(source),
        updatable=False,
        check_only=True,
    )


async def _scan_api(source: ServiceSource, address: str) -> StacksResult:
    base = (source.url or "").rstrip("/")
    prefix = f"/api/endpoints/{source.portainer_endpoint}/docker" if source.portainer_endpoint is not None else ""
    headers = {}
    if source.token:
        headers = (
            {"X-API-Key": source.token}
            if source.portainer_endpoint is not None
            else {"Authorization": f"Bearer {source.token}"}
        )
    async with httpx.AsyncClient(base_url=base, headers=headers, verify=source.verify_ssl, timeout=15) as client:
        containers = await client.get(f"{prefix}/containers/json", params={"all": "1"})
        containers.raise_for_status()
        nets: list = []
        try:
            r = await client.get(f"{prefix}/networks")
            nets = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            pass  # drivers only decide whether a macvlan address is shown
    return parse_api(containers.json(), nets if isinstance(nets, list) else [], address)


async def scan_source(source: ServiceSource) -> StacksResult:
    address = source_address(source) or ""
    if source.type == "ssh":
        assert source.ssh is not None
        res = await SSHExecutor(source.ssh).run(SSH_SCRIPT, timeout=60)
        result = parse_inspect(res.stdout, address)
        if res.rc != 0 and not result.error:
            result.error = f"exit code {res.rc}: {(res.stderr or res.stdout).strip()[-200:]}"
        return result
    return await _scan_api(source, address)


class SourceCache:
    """The last scan of each services-only source, in memory. Scans only happen when asked."""

    def __init__(self, sources: list[ServiceSource]):
        self.sources = {s.name: s for s in sources}
        self.results: dict[str, StacksResult] = {}
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def targets(self) -> dict[str, Target]:
        return {f"src:{n}": source_target(s) for n, s in self.sources.items()}

    @property
    def stacks(self) -> dict[str, StacksResult]:
        return {f"src:{n}": r for n, r in self.results.items()}

    def status_models(self) -> list[SourceStatus]:
        out = []
        for name in self.sources:
            r = self.results.get(name)
            out.append(
                SourceStatus(
                    name=name,
                    scanned=r is not None,
                    error=r.error if r else None,
                    checked_at=r.checked_at if r else None,
                )
            )
        return out

    fake = False  # demo data: never connect

    async def scan(self, force: bool = False) -> None:
        if self.fake:
            return
        loop = asyncio.get_running_loop()
        async with self._lock:

            async def one(name: str, source: ServiceSource) -> None:
                if not force and loop.time() - self._last.get(name, -1e9) < MIN_INTERVAL:
                    return
                self._last[name] = loop.time()
                try:
                    self.results[name] = await scan_source(source)
                except Exception as exc:  # a NAS being off must not break the tab
                    log.warning("services source %s: %s", name, exc)
                    old = self.results.get(name)
                    self.results[name] = StacksResult(
                        standalone=old.standalone if old else [],
                        checked_at=datetime.now(UTC),
                        error=(str(exc) or type(exc).__name__)[:300],
                    )

            await asyncio.gather(*(one(n, s) for n, s in self.sources.items()))


def demo_source(cache: SourceCache) -> None:
    """A fake NAS for demo mode: fixed data, no connection."""
    from .config import ServiceSource, SSHConfig

    source = ServiceSource(name="nas", type="ssh", address="nas.lan", ssh=SSHConfig(host="nas.lan"))
    cache.sources[source.name] = source
    cache.fake = True
    now = datetime.now(UTC)

    def svc(name, image, port, category="", state="running"):
        ports = [PortMapping(host_port=port, container_port=port)]
        return StackService(name=name, image=image, status=state, ports=ports, category=category)

    cache.results[source.name] = StacksResult(
        standalone=[
            svc("jellyfin", "jellyfin/jellyfin:latest", 8096),
            svc("sonarr", "lscr.io/linuxserver/sonarr:latest", 8989),
            svc("radarr", "lscr.io/linuxserver/radarr:latest", 7878),
            svc("syncthing", "syncthing/syncthing:latest", 8384),
            svc("immich", "ghcr.io/immich-app/immich-server:release", 2283),
            svc("photoprism", "photoprism/photoprism:latest", 2342, state="exited"),
        ],
        checked_at=now,
    )

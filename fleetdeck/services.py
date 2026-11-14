"""The Services tab: turn Docker scan results into safe links to each service's own UI.

Read-only. Labels and proxy routes are untrusted; this module validates URLs and never fetches one.
Only host-published ports, host networking and macvlan/ipvlan addresses become direct links; bridge and
overlay container IPs are never shown.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime
from hashlib import sha1

from .config import ServicesConfig, StaticServiceLink
from .icons import icon_slug
from .models import (
    CheckResult,
    ProxyRoute,
    ServiceEntry,
    ServiceLink,
    ServicesView,
    SourceStatus,
    StackService,
    StacksResult,
    Target,
)
from .urls import safe_host, safe_url

CATEGORY_ORDER = [
    "Media",
    "Home automation",
    "Network",
    "Storage & backup",
    "Monitoring",
    "Security & identity",
    "Development",
    "Productivity",
    "Tools",
    "Virtualization",
    "Other",
    "Support",
]

# Image slug (registry and tag removed) -> category. Substring matching keeps linuxserver/ and -alpine variants.
IMAGE_CATEGORIES = {
    "plex": "Media",
    "jellyfin": "Media",
    "emby": "Media",
    "sonarr": "Media",
    "radarr": "Media",
    "lidarr": "Media",
    "readarr": "Media",
    "prowlarr": "Media",
    "bazarr": "Media",
    "overseerr": "Media",
    "tautulli": "Media",
    "tdarr": "Media",
    "immich": "Media",
    "photoprism": "Media",
    "home-assistant": "Home automation",
    "homeassistant": "Home automation",
    "hass": "Home automation",
    "zigbee2mqtt": "Home automation",
    "z2m": "Home automation",
    "node-red": "Home automation",
    "esphome": "Home automation",
    "mosquitto": "Home automation",
    "pihole": "Network",
    "adguard": "Network",
    "unifi": "Network",
    "nginx-proxy-manager": "Network",
    "npm": "Network",
    "traefik": "Network",
    "caddy": "Network",
    "wireguard": "Network",
    "syncthing": "Storage & backup",
    "minio": "Storage & backup",
    "duplicati": "Storage & backup",
    "proxmox-backup": "Storage & backup",
    "pbs": "Storage & backup",
    "nextcloud": "Productivity",
    "paperless": "Productivity",
    "vaultwarden": "Productivity",
    "mealie": "Productivity",
    "gitea": "Development",
    "gitlab": "Development",
    "jenkins": "Development",
    "drone": "Development",
    "forgejo": "Development",
    "grafana": "Monitoring",
    "uptime-kuma": "Monitoring",
    "glances": "Monitoring",
    "netdata": "Monitoring",
    "prometheus": "Monitoring",
    "authentik": "Security & identity",
    "keycloak": "Security & identity",
    "authelia": "Security & identity",
    "portainer": "Tools",
    "dockge": "Tools",
    "arcane": "Tools",
    "proxmox": "Virtualization",
}

# Images that are infrastructure, not a user-facing app. A proxy route or fleetdeck.ui=true overrides this.
SUPPORT = re.compile(
    r"(?:^|[-_/])("
    r"postgres|postgresql|mysql|mariadb|mongo|mongodb|redis|valkey|memcached|"
    r"rabbitmq|kafka|nats|elasticsearch|opensearch|clickhouse|influxdb|cassandra|"
    r"etcd|consul|pgbouncer|"
    r"prometheus|node-exporter|cadvisor|loki|tempo|alloy|telegraf|fluent-bit|"
    r"watchtower|diun|certbot|docker-socket-proxy|cloudflared|gluetun|garage|"
    r"tailscale|tailscaled|gerbil|derper|headscale|autoheal|ofelia|acme|lego|"
    r"dnsmasq|unbound|otel-collector|otelcol|vector|portainer-agent|"
    r"traefik|caddy|haproxy|nginx-proxy"
    r")(?:$|[-_/])",
    re.IGNORECASE,
)

# A name that says this is a UI overrides the support-container denylist (e.g. caddy-ui).
UI_NAME = re.compile(r"(?:^|[-_])(ui|webui|web-ui|dashboard)(?:$|[-_])", re.IGNORECASE)

UI_PORTS = {
    80,
    81,
    443,
    3000,
    3001,
    5000,
    8000,
    8006,
    8080,
    8081,
    8096,
    8123,
    8443,
    9000,
    9443,
    2283,
}


# Helper-script (community-scripts) apps that run straight on a container, not in Docker: default web port and
# scheme. Only used for machines whose last check named the app.
NATIVE_APPS = {
    "plex": (32400, "http", "/web"),
    "jellyfin": (8096, "http", ""),
    "emby": (8096, "http", ""),
    "sonarr": (8989, "http", ""),
    "radarr": (7878, "http", ""),
    "lidarr": (8686, "http", ""),
    "readarr": (8787, "http", ""),
    "prowlarr": (9696, "http", ""),
    "bazarr": (6767, "http", ""),
    "overseerr": (5055, "http", ""),
    "tautulli": (8181, "http", ""),
    "homeassistant": (8123, "http", ""),
    "home-assistant": (8123, "http", ""),
    "pihole": (80, "http", "/admin"),
    "adguard": (3000, "http", ""),
    "nginxproxymanager": (81, "http", ""),
    "uptimekuma": (3001, "http", ""),
    "grafana": (3000, "http", ""),
    "vaultwarden": (8000, "http", ""),
    "paperless-ngx": (8000, "http", ""),
    "mealie": (9000, "http", ""),
    "syncthing": (8384, "http", ""),
    "nextcloud": (80, "http", ""),
    "gitea": (3000, "http", ""),
    "forgejo": (3000, "http", ""),
    "zigbee2mqtt": (8080, "http", ""),
    "nodered": (1880, "http", ""),
    "node-red": (1880, "http", ""),
    "esphome": (6052, "http", ""),
    "wallos": (80, "http", ""),
    "homepage": (3000, "http", ""),
    "netdata": (19999, "http", ""),
    "portainer": (9443, "https", ""),
}


def native_entries(
    targets: dict[str, Target], checks: dict[str, CheckResult], cfg: ServicesConfig, hidden: set[str]
) -> list[ServiceEntry]:
    """Cards for apps that run directly on a machine (helper-script containers), from the last OS check."""
    out: list[ServiceEntry] = []
    for target_id, target in targets.items():
        check = checks.get(target_id)
        app = (check.community_script or "").lower() if check else ""
        spec = NATIVE_APPS.get(app) or NATIVE_APPS.get(app.replace("-", "").replace("_", ""))
        address = _machine_address(target, cfg)
        if not spec or not address:
            continue
        port, scheme, path = spec
        link = _link(_authority_url(scheme, address, port) + path, "port", f"port {port}")
        if link is None:
            continue
        key = f"{target_id}|app|{app}"[:512]
        out.append(
            ServiceEntry(
                id=key,
                target_id=target_id,
                machine=target.name,
                name=app.replace("-", " ").title()[:128],
                image="installed on the machine (not Docker)",
                icon=icon_slug(name=app),
                state=target.status[:32],
                category=_category_for(app) or "Other",
                links=[link],
                ui=True,
                hidden=key in hidden,
                updated_at=check.checked_at if check else None,
            )
        )
    return out


def image_slug(image: str) -> str:
    """The lower-case app name from an image reference, without registry, tag or digest."""
    name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0].lower()


def _category_for(slug: str) -> str:
    for key, category in IMAGE_CATEGORIES.items():
        if key in slug:
            return category
    return ""


def _has_ui_port(service: StackService) -> bool:
    ports = {p.container_port for p in service.ports if p.protocol == "tcp"}
    ports.update(service.exposed_ports)
    ports.update(p.host_port for p in service.ports if p.host_port)
    return bool(ports & UI_PORTS)


def classify(service: StackService) -> tuple[bool, str]:
    """Whether this looks like a UI, and its category. Explicit labels always win."""
    slug = image_slug(service.image)
    category = service.category or _category_for(slug)
    if service.ui is False:
        return False, category or "Support"
    if service.ui is True:
        return True, category or "Tools"
    if service.proxy or service.explicit_urls:
        return True, category or "Other"
    if UI_NAME.search(service.name) or UI_NAME.search(service.container_name):
        return True, category or "Tools"
    if SUPPORT.search(slug) or SUPPORT.search(service.name) or SUPPORT.search(service.container_name):
        return False, category or "Support"
    if category:
        return True, category
    if _has_ui_port(service):
        return True, "Other"
    return False, "Support"


def _authority(host: str, port: int | None = None) -> str:
    host = host.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host if port is None else f"{host}:{port}"


def _link(url: str, kind: str, label: str) -> ServiceLink | None:
    url = safe_url(url)
    return ServiceLink(url=url, kind=kind, label=label) if url else None


def service_links(service: StackService, address: str | None) -> list[ServiceLink]:
    """Safe links for one service, best source first. Never a bridge/overlay container IP."""
    out: list[ServiceLink] = []
    for url in service.explicit_urls:
        if (link := _link(url, "explicit", "Configured")) and all(x.url != link.url for x in out):
            out.append(link)
    for route in service.proxy:
        scheme = "https" if route.tls else "http"
        if link := _link(f"{scheme}://{route.host}{route.path}", route.kind, f"via {route.kind.title()}"):
            if all(x.url != link.url for x in out):
                out.append(link)
    if address:
        address = safe_host(address) or address
        for port in service.ports:
            if port.protocol != "tcp" or not port.host_port:
                continue
            host = port.host_ip.strip()
            if host in ("127.0.0.1", "::1", "[::1]", "localhost"):
                continue  # published only on the container host: not reachable from the browser
            if not host or host in ("0.0.0.0", "::", "[::]"):
                host = address
            scheme = "https" if port.host_port in (443, 8443) else "http"
            if link := _link(_authority_url(scheme, host, port.host_port), "port", f"port {port.host_port}"):
                if all(x.url != link.url for x in out):
                    out.append(link)
        if service.network_mode == "host":
            for port in service.exposed_ports:
                scheme = "https" if port in (443, 8443) else "http"
                if link := _link(_authority_url(scheme, address, port), "host", f"host port {port}"):
                    if all(x.url != link.url for x in out):
                        out.append(link)
    for network in service.networks:
        if network.driver not in ("macvlan", "ipvlan") or not network.ip:
            continue
        port = (service.exposed_ports or [80])[0]
        scheme = "https" if port in (443, 8443) else "http"
        if link := _link(_authority_url(scheme, network.ip, port), "macvlan", "container IP"):
            if all(x.url != link.url for x in out):
                out.append(link)
    return out[:5]


def _authority_url(scheme: str, host: str, port: int) -> str:
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    return f"{scheme}://{_authority(host, None if default else port)}"


def _address_value(value: str | None) -> str | None:
    if not value:
        return None
    if host := safe_host(value):
        return host
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.is_loopback or address.is_link_local or address.is_unspecified:
        return None
    return str(address)


def _machine_address(target: Target, cfg: ServicesConfig, host_address: str = "") -> str | None:
    for key in (target.id, target.name):
        if value := cfg.addresses.get(key):
            if host := _address_value(value):
                return host
    if host := _address_value(target.address):
        return host
    return _address_value(host_address)


def _entry(
    target: Target,
    stack: str | None,
    service: StackService,
    address: str | None,
    cfg: ServicesConfig,
    hidden: set[str],
    at: datetime | None,
) -> ServiceEntry | None:
    if service.status not in ("running", "restarting") and not cfg.show_stopped:
        return None
    ui, category = classify(service)
    key = f"{target.id}|{stack or 'standalone'}|{service.name}"[:512]
    is_hidden = key in hidden
    links = service_links(service, address) if (ui or is_hidden) else []
    return ServiceEntry(
        id=key,
        target_id=target.id,
        machine=target.name,
        stack=stack,
        name=service.name[:128],
        image=service.image[:512],
        state=service.status[:32],
        category=category if ui else service.category or "Support",
        links=links,
        ui=ui,
        hidden=is_hidden,
        updated_at=at,
        link_only=target.id.startswith("src:"),
        update=(service.outdated or service.registry == "update") and not target.id.startswith("src:"),
        icon=icon_slug(service.image, service.name),
    )


def _split_upstream(value: str) -> tuple[str, int] | None:
    value = value.strip()
    if value.startswith(("unix/", "unix:")):
        return None
    value = value.split("://", 1)[-1].rstrip("/")
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        return None
    port_number = int(port)
    if not 1 <= port_number <= 65535:
        return None
    return host.strip().strip("[]"), port_number


def _routes_for(
    service: StackService,
    routes: list[ProxyRoute],
    address: str | None,
    *,
    foreign: bool = False,
) -> list[ProxyRoute]:
    """Match Caddy routes to this service by container IP/name or by host port. Read-only.

    ``foreign`` routes were read on another machine's proxy: only the machine address + published port counts."""
    if not routes:
        return []
    ips = {network.ip for network in service.networks if network.ip}
    container_ports = {p.container_port for p in service.ports} | set(service.exposed_ports)
    host_ports = {p.host_port for p in service.ports if p.host_port}
    names = {service.name, service.container_name} - {""}
    out: list[ProxyRoute] = []
    for route in routes:
        upstream = _split_upstream(route.upstream)
        if upstream is None:
            continue
        host, port = upstream
        by_container = not foreign and (host in ips or host in names)
        local_names = () if foreign else ("localhost", "127.0.0.1", "host.docker.internal")
        by_host = address is not None and host in (address, *local_names)
        if (by_container and (not container_ports or port in container_ports)) or (by_host and port in host_ports):
            if not any(r.host == route.host and r.path == route.path for r in [*service.proxy, *out]):
                out.append(route)
    return out[:10]


def build_services(
    targets: dict[str, Target],
    stacks: dict[str, StacksResult],
    cfg: ServicesConfig,
    hidden: set[str],
    *,
    now: datetime | None = None,
    added: dict[str, StaticServiceLink] | None = None,
    checks: dict[str, CheckResult] | None = None,
    sources: list[SourceStatus] | None = None,
) -> ServicesView:
    """The catalogue from cached scan results plus configured manual links."""
    now = now or datetime.now(UTC)
    if not cfg.enabled:
        return ServicesView(generated_at=now, enabled=False)
    entries: list[ServiceEntry] = []
    latest: datetime | None = None
    pooled = [(tid, route) for tid, res in stacks.items() for route in res.routes]

    def routes_of(target_id: str, service: StackService, result: StacksResult, address: str | None):
        found = _routes_for(service, result.routes, address)
        other = [route for tid, route in pooled if tid != target_id]
        return [*found, *_routes_for(service, other, address, foreign=True)]

    for target_id, target in targets.items():
        result = stacks.get(target_id)
        if result is None:
            continue
        latest = result.checked_at if latest is None or result.checked_at > latest else latest
        address = _machine_address(target, cfg, result.host_address)
        for stack in result.stacks:
            for service in stack.services:
                extra = routes_of(target_id, service, result, address)
                if extra:
                    service = service.model_copy(update={"proxy": [*service.proxy, *extra]})
                if (entry := _entry(target, stack.name, service, address, cfg, hidden, result.checked_at)) is not None:
                    entries.append(entry)
        for service in result.standalone:
            extra = routes_of(target_id, service, result, address)
            if extra:
                service = service.model_copy(update={"proxy": [*service.proxy, *extra]})
            if (entry := _entry(target, None, service, address, cfg, hidden, result.checked_at)) is not None:
                entries.append(entry)
    entries.extend(native_entries(targets, checks or {}, cfg, hidden))
    manual = [(None, link) for link in cfg.links] + [(lid, link) for lid, link in (added or {}).items()]
    for link_id, link in manual:
        url = safe_url(link.url)
        if url is None:
            continue
        key = (
            f"manual|{link_id}"
            if link_id
            else f"static|{_slug(link.name)}|{sha1(url.encode(), usedforsecurity=False).hexdigest()[:8]}"
        )[:512]
        entries.append(
            ServiceEntry(
                id=key,
                target_id="",
                machine=link.machine,
                stack=None,
                name=link.name[:128],
                category=link.category or "Other",
                links=[ServiceLink(url=url, kind="static", label="Configured")],
                ui=True,
                hidden=key in hidden,
                updated_at=None,
                link_only=True,
                manual=link_id is not None,
                icon=icon_slug(name=link.name),
            )
        )
    order = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    entries.sort(key=lambda e: (order.get(e.category, len(order)), e.name.lower(), e.machine.lower()))
    return ServicesView(
        generated_at=now,
        services=entries[:2000],
        hidden=sum(1 for e in entries if e.hidden),
        checked_at=latest,
        sources=sources or [],
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "link"

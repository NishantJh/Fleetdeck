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
from urllib.parse import urlsplit

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
    targets: dict[str, Target],
    checks: dict[str, CheckResult],
    cfg: ServicesConfig,
    hidden: set[str],
    stacks: dict[str, StacksResult] | None = None,
) -> list[ServiceEntry]:
    """Cards for apps that run directly on a machine (helper-script containers), from the last OS check."""
    out: list[ServiceEntry] = []
    stacks = stacks or {}
    for target_id, target in targets.items():
        check = checks.get(target_id)
        app = (check.community_script or "").lower() if check else ""
        spec = NATIVE_APPS.get(app) or NATIVE_APPS.get(app.replace("-", "").replace("_", ""))
        result = stacks.get(target_id)
        addresses = _machine_addresses(
            target, cfg, result.host_address if result else "", result.host_addresses if result else None
        )
        address = addresses[0] if addresses else None
        if not spec or not address:
            continue
        port, scheme, path = spec
        keys = (
            f"{target_id}|app|{app}",
            app,
            app.replace("-", "").replace("_", ""),
            f"{target.name}|{app}",
        )
        routes: list[ProxyRoute] = []
        if result is not None:
            # A helper-script app listens on the machine itself: match a proxy upstream like host networking.
            listening = StackService(name=app, image="", status="running", exposed_ports=[port], network_mode="host")
            other = [route for tid, res in stacks.items() if tid != target_id for route in res.routes]
            routes = [
                *_routes_for(listening, result.routes, addresses),
                *_routes_for(listening, other, addresses, foreign=True),
            ]
        links: list[ServiceLink] = []
        for url in _override_urls(cfg, keys):
            if (link := _link(url, "explicit", "Configured")) and all(x.url != link.url for x in links):
                links.append(link)
        links.extend(_proxy_links(routes, links))
        if link := _link(_authority_url(scheme, address, port) + path, "port", f"port {port}"):
            if all(x.url != link.url for x in links):
                links.append(link)
        if not links:
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
                description=str(_lookup(cfg.descriptions, keys) or "")[:160],
                links=links[:5],
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


def _host_key(value: str) -> str:
    """The host part of a name, ``host:port`` or URL, normalised for comparing with a proxy upstream."""
    value = value.strip()
    if not value:
        return ""
    bare = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        return str(ipaddress.ip_address(bare))
    except ValueError:
        pass
    try:
        host = urlsplit(value if "://" in value else f"//{value}").hostname
    except ValueError:
        return ""
    return (host or "").strip().rstrip(".").lower()


def _is_local_host(host: str) -> bool:
    """Whether a link's host is likely reachable on the local network (not a public domain)."""
    host = host.strip().lower().rstrip(".")
    if not host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return address.is_private or address.is_link_local or address.is_loopback
    if "." not in host:
        return True
    return host.endswith(
        (".lan", ".local", ".localdomain", ".home.arpa", ".internal", ".intranet", ".home", ".corp", ".lab")
    )


def _is_local_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return True
    return _is_local_host(host)


def _proxy_links(routes: list[ProxyRoute], known: list[ServiceLink] | None = None) -> list[ServiceLink]:
    """Links for a service's proxy routes, local (LAN) URLs before public ones. Best source first."""
    links: list[ServiceLink] = []
    for route in routes:
        scheme = "https" if route.tls else "http"
        if link := _link(f"{scheme}://{route.host}{route.path}", route.kind, f"via {route.kind.title()}"):
            if all(x.url != link.url for x in [*(known or []), *links]):
                links.append(link)
    links.sort(key=lambda link: 0 if _is_local_url(link.url) else 1)
    return links


def _link(url: str, kind: str, label: str) -> ServiceLink | None:
    url = safe_url(url)
    return ServiceLink(url=url, kind=kind, label=label) if url else None


def _lookup(mapping: dict, keys: tuple[str, ...]):
    """The first value for a card id, container or service name (case-insensitive on the key)."""
    for key in keys:
        if not key:
            continue
        if (value := mapping.get(key)) is not None:
            return value
        if (value := mapping.get(key.lower())) is not None:
            return value
    return None


def _service_keys(target: Target, stack: str | None, service: StackService) -> tuple[str, ...]:
    """The names a services.urls / services.descriptions entry may use for this card."""
    return (
        f"{target.id}|{stack or 'standalone'}|{service.name}",
        service.container_name,
        service.name,
        f"{target.name}|{service.name}",
    )


def _override_urls(cfg: ServicesConfig, keys: tuple[str, ...]) -> list[str]:
    return [url for url in _lookup(cfg.urls, keys) or [] if safe_url(url)]


def service_links(service: StackService, address: str | None, overrides: list[str] | None = None) -> list[ServiceLink]:
    """Safe links for one service, best source first. Never a bridge/overlay container IP."""
    out: list[ServiceLink] = []
    for url in overrides or ():
        if (link := _link(url, "explicit", "Configured")) and all(x.url != link.url for x in out):
            out.append(link)
    for url in service.explicit_urls:
        if (link := _link(url, "explicit", "Configured")) and all(x.url != link.url for x in out):
            out.append(link)
    out.extend(_proxy_links(service.proxy, out))
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


def _machine_addresses(
    target: Target, cfg: ServicesConfig, host_address: str = "", host_addresses: list[str] | None = None
) -> list[str]:
    """Every name and address this machine answers to, preferred link address first, for matching proxy routes."""
    out: list[str] = []
    values = [cfg.addresses.get(key) for key in (target.id, target.name)]
    values += [target.address, host_address, *(host_addresses or [])]
    for value in values:
        if value and (host := _address_value(value)) and host not in out:
            out.append(host)
    return out


def _entry(
    target: Target,
    stack: str | None,
    service: StackService,
    address: str | None,
    cfg: ServicesConfig,
    hidden: set[str],
    at: datetime | None,
    categories: dict[str, str] | None = None,
) -> ServiceEntry | None:
    if service.status not in ("running", "restarting") and not cfg.show_stopped:
        return None
    ui, category = classify(service)
    keys = _service_keys(target, stack, service)
    key = f"{target.id}|{stack or 'standalone'}|{service.name}"[:512]
    is_hidden = key in hidden
    if chosen := (categories or {}).get(key):
        # The group picker is authoritative: moving a support container to a user group makes it a normal
        # service (with its links), and moving a service to Support makes it obey "show support containers".
        category, ui = chosen, chosen != "Support"
    elif not ui:
        category = service.category or "Support"
    links = service_links(service, address, _override_urls(cfg, keys)) if (ui or is_hidden or chosen) else []
    return ServiceEntry(
        id=key,
        target_id=target.id,
        machine=target.name,
        stack=stack,
        name=(service.display_name or service.name)[:128],
        image=service.image[:512],
        state=service.status[:32],
        category=category,
        description=(str(_lookup(cfg.descriptions, keys) or "") or service.description)[:160],
        links=links,
        ui=ui,
        hidden=is_hidden,
        updated_at=at,
        link_only=target.id.startswith("src:"),
        update=(service.outdated or service.registry == "update") and not target.id.startswith("src:"),
        icon=service.icon or icon_slug(service.image, service.name),
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
    addresses: str | list[str] | None,
    *,
    foreign: bool = False,
) -> list[ProxyRoute]:
    """Match proxy routes to this service by container IP/name or by a machine address and port. Read-only.

    ``addresses`` are every name/address the machine answers to (see ``_machine_addresses``), so a proxy
    that forwards to the machine's IP, its hostname or a secondary interface still matches. ``foreign``
    routes were read on another machine's proxy: only this machine's address + a port it listens on counts."""
    if not routes:
        return []
    if isinstance(addresses, str):
        addresses = [addresses]
    keys = {key for value in addresses or () if (key := _host_key(value))}
    if not foreign:
        keys |= {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
    ips = {key for network in service.networks if network.ip and (key := _host_key(network.ip))}
    container_ports = {p.container_port for p in service.ports} | set(service.exposed_ports)
    host_ports = {p.host_port for p in service.ports if p.host_port}
    # A port published on one specific interface: the proxy may forward to that address, not the default one.
    published_on = {key for p in service.ports if p.host_port and (key := _host_key(p.host_ip))}
    # A host-network container listens on the machine itself, so its container ports are host ports too.
    listen_ports = host_ports | (container_ports if service.network_mode == "host" else set())
    names = {name.strip().lower() for name in (service.name, service.container_name) if name}
    out: list[ProxyRoute] = []
    for route in routes:
        upstream = _split_upstream(route.upstream)
        if upstream is None:
            continue
        host, port = upstream
        key = _host_key(host)
        # A route to this container's own IP is for this container, whatever port it names.
        by_ip = not foreign and key in ips
        by_name = not foreign and key in names and (not container_ports or port in container_ports)
        by_host = bool(key) and (key in keys or key in published_on) and port in listen_ports
        if by_ip or by_name or by_host:
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
    categories: dict[str, str] | None = None,
) -> ServicesView:
    """The catalogue from cached scan results plus configured manual links.

    ``categories`` moves individual cards to a group the user chose on the Services tab. It is kept in the
    dashboard's database, so it survives rescans and restarts without editing labels or config.yaml."""
    now = now or datetime.now(UTC)
    if not cfg.enabled:
        return ServicesView(generated_at=now, enabled=False)
    entries: list[ServiceEntry] = []
    latest: datetime | None = None
    pooled = [(tid, route) for tid, res in stacks.items() for route in res.routes]

    def routes_of(target_id: str, service: StackService, result: StacksResult, addresses: list[str]):
        # Routes read on this machine's own proxy first (local links), then ones read on another machine's proxy.
        found = _routes_for(service, result.routes, addresses)
        other = [route for tid, route in pooled if tid != target_id]
        return [*found, *_routes_for(service, other, addresses, foreign=True)]

    for target_id, target in targets.items():
        result = stacks.get(target_id)
        if result is None:
            continue
        latest = result.checked_at if latest is None or result.checked_at > latest else latest
        addresses = _machine_addresses(target, cfg, result.host_address, result.host_addresses)
        address = addresses[0] if addresses else None
        for stack in result.stacks:
            for service in stack.services:
                extra = routes_of(target_id, service, result, addresses)
                if extra:
                    service = service.model_copy(update={"proxy": [*service.proxy, *extra]})
                if (
                    entry := _entry(target, stack.name, service, address, cfg, hidden, result.checked_at, categories)
                ) is not None:
                    entries.append(entry)
        for service in result.standalone:
            extra = routes_of(target_id, service, result, addresses)
            if extra:
                service = service.model_copy(update={"proxy": [*service.proxy, *extra]})
            if (
                entry := _entry(target, None, service, address, cfg, hidden, result.checked_at, categories)
            ) is not None:
                entries.append(entry)
    entries.extend(native_entries(targets, checks or {}, cfg, hidden, stacks))
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
    if categories:
        for entry in entries:
            if chosen := categories.get(entry.id):
                entry.category = chosen
                entry.ui = chosen != "Support"
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

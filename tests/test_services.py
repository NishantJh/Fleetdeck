from datetime import UTC, datetime

import pytest

from fleetdeck.config import ServicesConfig, StaticServiceLink
from fleetdeck.models import (
    NetworkAddress,
    PortMapping,
    ProxyRoute,
    Stack,
    StackService,
    StacksResult,
    Target,
)
from fleetdeck.services import build_services, classify, service_links
from fleetdeck.urls import safe_host, safe_url

NOW = datetime.now(UTC)


def service(**changes):
    base = dict(name="app", image="example/app:latest", status="running")
    return StackService(**{**base, **changes})


def target(name="pi", address="pi.lan"):
    return Target(id=f"ssh:{name}", name=name, platform="ssh", source="hosts", kind="host", address=address)


def stacks(service_obj, standalone=None):
    return StacksResult(
        stacks=[Stack(name="stack", services=[service_obj])],
        standalone=standalone or [],
        checked_at=NOW,
    )


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "http://user:pass@example.com",
        "http://example.com:99999",
        "http://",
        "http://exa mple.com",
    ],
)
def test_bad_urls_are_rejected(value):
    assert safe_url(value) is None


def test_good_urls_and_hosts():
    assert safe_url("https://pihole.lan/admin?x=1") == "https://pihole.lan/admin?x=1"
    assert safe_host("app.lan:8443") == "app.lan:8443"
    assert safe_host("[::1]:8080") == "[::1]:8080"
    assert safe_host("*.lan") is None
    assert safe_host("app.lan/path") is None


def test_classify_hides_support_images_and_shows_known_apps():
    assert classify(service(image="postgres:16")) == (False, "Support")
    assert classify(service(image="lscr.io/linuxserver/jellyfin:latest")) == (True, "Media")
    assert classify(service(image="postgres:16", ui=True))[0] is True
    assert classify(service(image="postgres:16", proxy=[ProxyRoute(host="db.lan")]))[0] is True
    assert classify(service(image="unknown/api:1", ports=[PortMapping(host_port=8080, container_port=80)]))[0] is True
    assert classify(service(image="unknown/api:1")) == (False, "Support")


def test_proxy_link_prefers_https_and_path():
    links = service_links(
        service(proxy=[ProxyRoute(kind="traefik", host="app.lan", path="/ui", tls=True)]),
        "pi.lan",
    )
    assert links[0].url == "https://app.lan/ui" and links[0].kind == "traefik"


def test_published_port_uses_machine_address_but_not_loopback():
    ports = [
        PortMapping(host_port=8080, container_port=80),
        PortMapping(host_ip="127.0.0.1", host_port=9090, container_port=90),
    ]
    links = service_links(service(ports=ports), "pi.lan")
    assert [link.url for link in links] == ["http://pi.lan:8080"]
    assert links[0].kind == "port"


def test_bridge_container_ip_is_never_a_link():
    links = service_links(
        service(
            networks=[NetworkAddress(name="bridge", ip="172.18.0.2", driver="bridge")],
            ports=[PortMapping(host_port=8080, container_port=80)],
        ),
        None,
    )
    assert links == []


def test_macvlan_container_ip_is_a_link():
    links = service_links(
        service(
            networks=[NetworkAddress(name="lan", ip="192.168.1.50", driver="macvlan")],
            exposed_ports=[80],
        ),
        None,
    )
    assert [link.url for link in links] == ["http://192.168.1.50"]


def test_host_network_uses_exposed_ports():
    links = service_links(service(network_mode="host", exposed_ports=[8443]), "pi.lan")
    assert [link.url for link in links] == ["https://pi.lan:8443"]


def test_explicit_url_comes_first():
    links = service_links(
        service(explicit_urls=["https://manual.lan"], proxy=[ProxyRoute(host="caddy.lan")]),
        "pi.lan",
    )
    assert [link.kind for link in links] == ["explicit", "caddy"]


def test_catalogue_hides_stopped_by_default_and_marks_hidden():
    stopped = service(name="stopped", status="exited")
    config = ServicesConfig()
    result = build_services({"ssh:pi": target()}, {"ssh:pi": stacks(stopped)}, config, set())
    assert result.services == []

    config = ServicesConfig(show_stopped=True)
    result = build_services({"ssh:pi": target()}, {"ssh:pi": stacks(stopped)}, config, set())
    assert [entry.name for entry in result.services] == ["stopped"]

    key = result.services[0].id
    result = build_services({"ssh:pi": target()}, {"ssh:pi": stacks(stopped)}, config, {key})
    assert result.services[0].hidden and result.hidden == 1


def test_catalogue_category_order_and_support_toggle_data():
    jelly = service(name="jelly", image="jellyfin:latest")
    db = service(name="db", image="postgres:16")
    result = build_services({"ssh:pi": target()}, {"ssh:pi": stacks(jelly, standalone=[db])}, ServicesConfig(), set())
    assert [(entry.name, entry.ui) for entry in result.services] == [("jelly", True), ("db", False)]
    assert result.services[0].category == "Media"
    assert result.services[1].category == "Support"


def test_static_links_and_addresses():
    config = ServicesConfig(
        addresses={"ssh:pi": "pi.lan"},
        links=[StaticServiceLink(name="Proxmox", url="https://pve.lan:8006", category="Virtualization")],
    )
    result = build_services({"ssh:pi": target(address=None)}, {"ssh:pi": stacks(service())}, config, set())
    assert result.services[0].links[0].kind == "static"
    assert result.services[0].machine == ""
    assert any(entry.name == "app" for entry in result.services)


def test_disabled_catalogue_is_empty():
    result = build_services({}, {}, ServicesConfig(enabled=False), set())
    assert not result.enabled and result.services == []


def test_caddy_route_matches_container_ip_and_becomes_https_link():
    app = service(
        image="unknown/app:1",
        networks=[NetworkAddress(name="bridge", ip="172.18.0.5", driver="bridge")],
        ports=[PortMapping(host_port=8080, container_port=80)],
    )
    result = StacksResult(
        stacks=[Stack(name="stack", services=[app])],
        routes=[ProxyRoute(host="app.lan", tls=True, upstream="172.18.0.5:80")],
        checked_at=NOW,
    )
    services = build_services({"ssh:pi": target()}, {"ssh:pi": result}, ServicesConfig(), set()).services
    assert services[0].links[0].url == "https://app.lan/"


def test_caddy_route_matches_published_host_port():
    app = service(
        image="unknown/app:1",
        ports=[PortMapping(host_ip="0.0.0.0", host_port=8080, container_port=80)],
    )
    result = StacksResult(
        stacks=[Stack(name="stack", services=[app])],
        routes=[ProxyRoute(host="app.lan", tls=True, upstream="pi.lan:8080")],
        checked_at=NOW,
    )
    services = build_services({"ssh:pi": target()}, {"ssh:pi": result}, ServicesConfig(), set()).services
    assert services[0].links[0].url == "https://app.lan/"


def test_caddy_route_for_another_service_is_not_attached():
    app = service(
        image="unknown/app:1",
        networks=[NetworkAddress(name="bridge", ip="172.18.0.5", driver="bridge")],
        exposed_ports=[80],
    )
    result = StacksResult(
        stacks=[Stack(name="stack", services=[app])],
        routes=[ProxyRoute(host="other.lan", tls=True, upstream="172.18.0.9:80")],
        checked_at=NOW,
    )
    services = build_services({"ssh:pi": target()}, {"ssh:pi": result}, ServicesConfig(), set()).services
    assert services[0].links == []


def test_infrastructure_sidecars_are_support_containers():
    assert classify(service(image="tailscale/tailscale:latest", name="tailscale"))[0] is False
    assert classify(service(image="ghcr.io/tailscale/gerbil:latest", name="gerbil"))[0] is False
    assert classify(service(image="alpine", name="tailscaled"))[0] is False


def test_scan_host_address_is_used_for_port_links():
    app = service(ports=[PortMapping(host_port=8080, container_port=80)])
    target_without_address = Target(id="ssh:pi", name="pi", platform="ssh", source="hosts", kind="host", address=None)
    result = StacksResult(
        stacks=[Stack(name="stack", services=[app])],
        host_address="192.168.1.50",
        checked_at=NOW,
    )
    services = build_services({"ssh:pi": target_without_address}, {"ssh:pi": result}, ServicesConfig(), set()).services
    assert services[0].links[0].url == "http://192.168.1.50:8080"


def test_no_address_means_no_guessed_port_link():
    app = service(ports=[PortMapping(host_port=8080, container_port=80)])
    target_without_address = Target(id="ssh:pi", name="pi", platform="ssh", source="hosts", kind="host", address=None)
    result = StacksResult(
        stacks=[Stack(name="stack", services=[app])],
        checked_at=NOW,
    )
    services = build_services({"ssh:pi": target_without_address}, {"ssh:pi": result}, ServicesConfig(), set()).services
    assert services[0].links == []


def test_configured_address_wins_over_the_scan_address():
    app = service(ports=[PortMapping(host_port=8080, container_port=80)])
    result = StacksResult(
        stacks=[Stack(name="stack", services=[app])],
        host_address="192.168.1.50",
        checked_at=NOW,
    )
    config = ServicesConfig(addresses={"ssh:pi": "pi.lan"})
    services = build_services({"ssh:pi": target(address=None)}, {"ssh:pi": result}, config, set()).services
    assert services[0].links[0].url == "http://pi.lan:8080"


def test_a_ui_suffix_overrides_the_support_image_denylist():
    assert classify(service(image="caddy:2", name="caddy-ui"))[0] is True
    assert classify(service(image="caddy:2", name="caddy"))[0] is False
    explicit_off = service(image="caddy:2", name="caddy-ui", ui=False)
    assert classify(explicit_off)[0] is False


def test_services_tab_keeps_form_and_blocks_autofill():
    from pathlib import Path

    js = (Path(__file__).parent.parent / "fleetdeck" / "static" / "services.js").read_text()
    assert "data-form-slot" in js  # Add link form survives polling re-renders
    assert 'type="search"' in js and "readonly" in js and 'autocomplete="off"' in js


def test_route_read_on_another_machines_caddy_links_this_machines_service():
    app = service(
        image="unknown/app:1",
        ports=[PortMapping(host_ip="0.0.0.0", host_port=8080, container_port=80)],
    )
    here = StacksResult(stacks=[Stack(name="stack", services=[app])], checked_at=NOW)
    proxy = StacksResult(routes=[ProxyRoute(host="app.lan", tls=True, upstream="pi.lan:8080")], checked_at=NOW)
    targets = {"ssh:pi": target(), "ssh:proxy": target()}
    services = build_services(targets, {"ssh:pi": here, "ssh:proxy": proxy}, ServicesConfig(), set()).services
    assert services[0].links[0].url == "https://app.lan/"

import subprocess
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fleetdeck.config import ServicesConfig
from fleetdeck.models import PortMapping, ProxyRoute, Stack, StackService, StacksResult, Target
from fleetdeck.proxy_readers import (
    PROXY_SCAN_SH,
    parse_nginx,
    parse_traefik_api,
    parse_traefik_file,
    routes_from_lines,
)
from fleetdeck.services import build_services

NGINX = """
upstream backend {
  server 10.0.0.9:3000;
}
server {
  listen 443 ssl;
  server_name app.lan *.wild.lan ~^re$ _;
  location / {
    proxy_pass http://backend;
  }
  location /api/ {
    proxy_pass http://10.0.0.5:9000/;
  }
  location ~ ^/x {
    proxy_pass http://10.0.0.6:1;
  }
}
server {
  listen 80;
  server_name plain.lan;
  location / {
    proxy_pass http://$host:80;
  }
}
"""

NPM = """
server {
  set $forward_scheme http;
  set $server         "192.168.1.20";
  set $port           8080;
  listen 80;
  listen 443 ssl;
  server_name npm.example.com other.example.com;
  location / {
    include conf.d/include/proxy.conf;
  }
}
"""

NOW = datetime.now(UTC)


def service(**changes):
    return StackService(**{"name": "app", "image": "example/app:latest", "status": "running", **changes})


def target(name, address):
    return Target(id=f"ssh:{name}", name=name, platform="ssh", source="hosts", kind="host", address=address)


def lines(text):
    return [ln for ln in text.splitlines() if ln.strip()]


def test_parse_nginx_servers_locations_and_upstreams():
    routes = parse_nginx(lines(NGINX))
    got = {(r.host, r.path, r.upstream, r.tls) for r in routes}
    assert got == {
        ("app.lan", "/", "10.0.0.9:3000", True),
        ("app.lan", "/api/", "10.0.0.5:9000", True),
    }
    assert {r.kind for r in routes} == {"nginx"}


def test_parse_nginx_proxy_manager_variables():
    routes = parse_nginx(lines(NPM))
    assert sorted(r.host for r in routes) == ["npm.example.com", "other.example.com"]
    assert all(r.upstream == "192.168.1.20:8080" and r.tls for r in routes)


TRAEFIK_YAML = """
http:
  routers:
    app:
      rule: Host(`app.lan`) && PathPrefix(`/x`)
      service: appsvc
      entryPoints:
        - websecure
      tls: {}
    plain:
      rule: Host(`plain.lan`)
      service: plainsvc@file
  middlewares:
    auth:
      basicAuth:
        users:
  services:
    appsvc:
      loadBalancer:
        servers:
          - url: "https://10.0.0.4:8443"
    plainsvc:
      loadBalancer:
        servers:
          - url: http://10.0.0.5
"""

TRAEFIK_TOML = """
[http.routers.t]
rule = "Host(`t.lan`)"
service = "tsvc"
[http.routers.t.tls]
[http.services.tsvc.loadBalancer]
[[http.services.tsvc.loadBalancer.servers]]
url = "http://10.0.0.7:81"
"""


def test_parse_traefik_file_yaml_and_toml():
    yaml_routes = {(r.host, r.path, r.upstream, r.tls) for r in parse_traefik_file(lines(TRAEFIK_YAML))}
    assert yaml_routes == {
        ("app.lan", "/x", "10.0.0.4:8443", True),
        ("plain.lan", "/", "10.0.0.5:80", False),
    }
    toml = parse_traefik_file(lines(TRAEFIK_TOML))
    assert [(r.host, r.upstream, r.tls) for r in toml] == [("t.lan", "10.0.0.7:81", True)]


def test_parse_traefik_api_includes_http_provider_and_skips_internal():
    routers = (
        '[{"name":"3-router@http","rule":"Host(`p.example.com`)","service":"3-service@http",'
        '"entryPoints":["websecure"],"provider":"http"},'
        '{"name":"api@internal","rule":"PathPrefix(`/api`)","service":"api@internal","provider":"internal"}]'
    )
    services = '[{"name":"3-service@http","loadBalancer":{"servers":[{"url":"http://100.89.1.2:3000"}]}}]'
    routes = parse_traefik_api(routers, services)
    assert [(r.host, r.upstream, r.tls) for r in routes] == [("p.example.com", "100.89.1.2:3000", True)]
    assert parse_traefik_api("nope", "[]") == []


def test_routes_from_lines_groups_tags():
    groups = {
        "nx:host:1": lines(NGINX),
        "ta:abc:r": ['[{"name":"r@docker","rule":"Host(`a.lan`)","service":"s@docker"}]'],
        "ta:abc:s": ['[{"name":"s@docker","loadBalancer":{"servers":[{"url":"http://172.18.0.2:80"}]}}]'],
    }
    hosts = {r.host for r in routes_from_lines(groups)}
    assert hosts == {"app.lan", "a.lan"}


def test_shell_filters_keep_routes_and_drop_secrets(tmp_path):
    script = PROXY_SCAN_SH.split("proxy_scan() {")[0] + (
        "US=$(printf '\\037')\n"
        'head -c 100000 "$1" | px_traefik | px_emit tf:t:1\n'
        'head -c 100000 "$2" | px_nginx | px_emit nx:t:2\n'
    )
    ytxt = TRAEFIK_YAML.replace(
        "users:",
        'users:\n          - "admin:$apr1$secrethash"\n        password: hunter2\n'
        "    other:\n      rule: Host(`x.lan`)\n      service: http://user:pw@10.0.0.1:80",
    )
    ntxt = NGINX + "\nserver {\n  auth_basic_user_file /etc/secret;\n  add_header X-Token sekret;\n}\n"
    (tmp_path / "t.yml").write_text(ytxt)
    (tmp_path / "n.conf").write_text(ntxt)
    out = subprocess.run(
        ["sh", "-c", script, "sh", str(tmp_path / "t.yml"), str(tmp_path / "n.conf")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "secrethash" not in out and "hunter2" not in out and "sekret" not in out and "pw@" not in out
    groups: dict[str, list[str]] = {}
    for line in out.splitlines():
        _, tag, text = line.split("\x1f", 2)
        groups.setdefault(tag, []).append(text)
    got = {(r.host, r.path, r.upstream) for r in routes_from_lines(groups)}
    assert ("app.lan", "/x", "10.0.0.4:8443") in got
    assert ("app.lan", "/api/", "10.0.0.5:9000") in got


def test_routes_match_services_across_machines_by_address_and_port():
    app = service(image="unknown/app:1", ports=[PortMapping(host_ip="0.0.0.0", host_port=8080, container_port=80)])
    proxy_machine = StacksResult(
        routes=[
            ProxyRoute(kind="nginx", host="app.example.com", upstream="192.168.1.20:8080"),
            ProxyRoute(kind="nginx", host="wrong-port.example.com", upstream="192.168.1.20:9999"),
            ProxyRoute(kind="nginx", host="local-name.example.com", upstream="app:80"),
        ],
        checked_at=NOW,
    )
    app_machine = StacksResult(stacks=[Stack(name="s", services=[app])], host_address="192.168.1.20", checked_at=NOW)
    targets = {"ssh:proxy": target("proxy", "192.168.1.5"), "ssh:app": target("app", "192.168.1.20")}
    view = build_services(
        targets, {"ssh:proxy": proxy_machine, "ssh:app": app_machine}, ServicesConfig(), set()
    ).services
    hosts = sorted({urlsplit(link.url).hostname for entry in view for link in entry.links})
    assert [h for h in hosts if h != "192.168.1.20"] == ["app.example.com"]

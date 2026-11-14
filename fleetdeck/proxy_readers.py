"""Reverse-proxy routes read from config on the machine: nginx server blocks, Nginx Proxy Manager,
the Traefik file provider and the Traefik API (which also lists Pangolin's routes).

Everything here is read-only and size-capped. The shell half (``PROXY_SCAN_SH``) runs inside the stacks
scan and prints only whitelisted lines (server names, locations, upstreams, router rules); passwords,
headers, middleware settings and keys never leave the machine. The Python half turns those lines into
``ProxyRoute`` objects whose ``upstream`` is matched to services by ``services._routes_for``.
Nothing is ever written to a proxy.
"""

from __future__ import annotations

import json
import re

from .models import ProxyRoute
from .urls import safe_host, safe_path

# Tag prefixes of ``__PF__`` lines: nx = nginx files/containers, npm = Nginx Proxy Manager,
# tf = Traefik file provider, ta = Traefik API (r = routers, s = services).
TAG = re.compile(r"^(nx|npm|tf|ta):[A-Za-z0-9_.:-]{1,80}$")
MAX_LINES = 3000
MAX_JSON = 300_000

PROXY_SCAN_SH = r"""
px_nginx() {
  awk '
    { sub(/^[ \t]+/, "") }
    /^#/ || $0 == "" { next }
    /^}/ { print "}"; next }
    /\{[ \t]*$/ { if ($1 == "server" || $1 == "location" || $1 == "upstream") print; else print "block {"; next }
    $1 == "server_name" || $1 == "proxy_pass" || $1 == "listen" || $1 == "server" { print; next }
    $1 == "set" && $2 ~ /^\$(server|port|forward_scheme)$/ { print; next }
  ' | head -n 3000 | cut -c1-400
}
px_traefik() {
  awk '
    /^[ \t]*#/ || /^[ \t]*$/ { next }
    /:\/\/[^\/ \t"]*@/ { next }
    /^[ \t]*\[\[?[A-Za-z0-9_.@-]+\]\]?[ \t]*$/ { print; next }
    /^[ \t]*(- +)?"?[A-Za-z0-9_.@-]+"?[ \t]*:[ \t]*(#.*)?$/ { print; next }
    /^[ \t]*(- +)?"?(rule|service|url|certResolver|tls|entryPoints)"?[ \t]*[:=][ \t]*[^ \t]/ { print; next }
    /^[ \t]*- +"?[A-Za-z0-9_@.-]+"?[ \t]*$/ { print; next }
  ' | head -n 2000 | cut -c1-400
}
px_emit() {
  while IFS= read -r l; do printf '__PF__%s%s%s%s\n' "$US" "$1" "$US" "$l"; done
}
px_tpath() {
  { if [ -d "$2" ]; then
      find "$2" -maxdepth 2 -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.toml' \) 2>/dev/null | head -n 20
    elif [ -f "$2" ]; then printf '%s\n' "$2"; fi; } | while IFS= read -r f; do
    case "$f" in *.yml|*.yaml|*.toml) ;; *) continue ;; esac
    t=$(printf %s "$f" | cksum | cut -d' ' -f1)
    head -c 100000 "$f" 2>/dev/null | px_traefik | px_emit "tf:$1:$t"
  done
}
proxy_scan() {
  # Plain nginx on the machine itself.
  for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
    [ -f "$f" ] || continue
    t=$(printf %s "$f" | cksum | cut -d' ' -f1)
    head -c 100000 "$f" 2>/dev/null | px_nginx | px_emit "nx:host:$t"
  done
  px_tpath host /etc/traefik
  command -v docker >/dev/null 2>&1 || return 0
  docker ps -a --format '{{.ID}} {{.Image}}' 2>/dev/null | head -n 200 | while read -r cid image; do
    case "$image" in
      *nginx-proxy-manager*|*jc21*)
        docker exec "$cid" sh -c 'cat /data/nginx/proxy_host/*.conf 2>/dev/null' 2>/dev/null \
          | head -c 400000 | px_nginx | px_emit "npm:$cid" ;;
      *nginx*)
        docker exec "$cid" sh -c 'cat /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/* 2>/dev/null' 2>/dev/null \
          | head -c 200000 | px_nginx | px_emit "nx:$cid" ;;
      *traefik*)
        for what in routers services; do
          { docker exec "$cid" wget -qO- -T 5 "http://127.0.0.1:8080/api/http/$what" 2>/dev/null \
              | head -c 300000 | tr -d '\n\r'; echo; } | px_emit "ta:$cid:${what%${what#?}}"
        done
        docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$cid" 2>/dev/null | head -n 10 \
          | while IFS= read -r src; do [ -n "$src" ] && px_tpath "$cid" "$src"; done ;;
    esac
  done
}
trap proxy_scan EXIT
"""


# ---- nginx -------------------------------------------------------------------------------------------


def _clean(token: str) -> str:
    return token.strip().strip("\"'")


def _upstream_of(url: str, upstreams: dict[str, str]) -> str:
    """``host:port`` from a proxy_pass target, or '' when it is a socket, a variable or unusable."""
    if url.startswith("unix:") or "$" in url:
        return ""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return ""
    target = rest.split("/", 1)[0]
    if target in upstreams:
        return upstreams[target]
    host, colon, port = target.rpartition(":")
    if not colon:
        return f"{target}:{443 if scheme == 'https' else 80}"
    return f"{host}:{port}" if port.isdigit() else ""


def parse_nginx(lines: list[str]) -> list[ProxyRoute]:
    """Host -> upstream routes from nginx server blocks (also Nginx Proxy Manager's generated files)."""
    stack: list[dict] = []
    upstreams: dict[str, str] = {}
    pending: list[tuple[list[str], bool, list[tuple[str, str]]]] = []
    for raw in lines[:MAX_LINES]:
        line = raw.strip().rstrip(";").strip()
        if not line:
            continue
        tokens = line.split()
        if line.endswith("{"):
            kind = tokens[0]
            if kind == "upstream" and len(tokens) >= 3:
                stack.append({"t": "upstream", "name": tokens[1]})
            elif kind == "server" and len(tokens) == 2:
                stack.append({"t": "server", "names": [], "tls": False, "vars": {}, "pass": [], "has_root": False})
            elif kind == "location":
                path = None if len(tokens) >= 4 and tokens[1] in ("~", "~*", "=") else tokens[-2]
                stack.append({"t": "location", "path": path})
            else:
                stack.append({"t": "block"})
            continue
        if line.startswith("}"):
            if stack:
                done = stack.pop()
                if done["t"] == "server":
                    pending.append(_finish_server(done))
            continue
        server = next((c for c in reversed(stack) if c["t"] == "server"), None)
        if stack and stack[-1]["t"] == "upstream" and tokens[0] == "server" and len(tokens) >= 2:
            upstreams.setdefault(stack[-1]["name"], tokens[1])
            continue
        if server is None:
            continue
        if tokens[0] == "server_name":
            server["names"].extend(_clean(t) for t in tokens[1:])
        elif tokens[0] == "listen":
            if "ssl" in tokens or any(t.rsplit(":", 1)[-1] == "443" for t in tokens[1:2]):
                server["tls"] = True
        elif tokens[0] == "set" and len(tokens) >= 3:
            server["vars"][tokens[1].lstrip("$")] = _clean(" ".join(tokens[2:]))
        elif tokens[0] == "proxy_pass" and len(tokens) >= 2:
            loc = next((c for c in reversed(stack) if c["t"] == "location"), None)
            path = loc["path"] if loc else "/"
            if path is not None:
                server["pass"].append((path, tokens[1]))
    routes: list[ProxyRoute] = []
    seen: set[tuple[str, str]] = set()
    for names, tls, passes in pending:
        for path, url in passes:
            upstream = _upstream_of(url, upstreams)
            if not upstream or not (path := safe_path(path)):
                continue
            for name in names:
                if len(routes) >= 200:
                    return routes
                if (name, path) not in seen:
                    seen.add((name, path))
                    routes.append(ProxyRoute(kind="nginx", host=name, path=path, tls=tls, upstream=upstream[:256]))
    return routes


def _finish_server(server: dict) -> tuple[list[str], bool, list[tuple[str, str]]]:
    names = []
    for name in server["names"]:
        if name and not name.startswith(("~", "*", ".", "$", "_")) and (host := safe_host(name)):
            names.append(host)
    passes: list[tuple[str, str]] = list(server["pass"])
    vars_ = server["vars"]
    # Nginx Proxy Manager: the target is set as variables and proxied from an include file.
    if "server" in vars_ and "port" in vars_ and not any(p == "/" for p, _ in passes):
        scheme = vars_.get("forward_scheme", "http")
        passes.append(("/", f"{scheme}://{vars_['server']}:{vars_['port']}"))
    return names[:10], bool(server["tls"]), passes[:10]


# ---- Traefik -----------------------------------------------------------------------------------------

_YAML_KV = re.compile(r"^(?P<ind>\s*)(?P<dash>-\s+)?\"?(?P<key>[A-Za-z0-9_.@-]+)\"?\s*:\s*(?P<val>.*)$")
_YAML_ITEM = re.compile(r"^(?P<ind>\s*)-\s+\"?(?P<val>[A-Za-z0-9_@.-]+)\"?\s*$")
_TOML_HEAD = re.compile(r"^\s*\[\[?(?P<path>[A-Za-z0-9_.@-]+)\]\]?\s*$")
_TOML_KV = re.compile(r"^\s*\"?(?P<key>[A-Za-z0-9_.@-]+)\"?\s*=\s*(?P<val>.+)$")


def _unquote(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value.strip())
    return value.strip().strip("\"'")


def _leaves(lines: list[str]) -> list[tuple[tuple[str, ...], str]]:
    """(path, value) pairs from the filtered YAML or TOML lines of one Traefik file."""
    out: list[tuple[tuple[str, ...], str]] = []
    stack: list[tuple[int, str]] = []
    section: list[str] = []
    for raw in lines[:MAX_LINES]:
        if m := _TOML_HEAD.match(raw):
            section = m["path"].split(".")
            if section[-1] == "tls":  # an empty [http.routers.x.tls] table still means TLS
                out.append((tuple(section), "{}"))
            continue
        if section and (m := _TOML_KV.match(raw)):
            out.append(((*section, *m["key"].split(".")), _unquote(m["val"])))
            continue
        if m := _YAML_KV.match(raw):
            indent = len(m["ind"]) + len(m["dash"] or "")
            while stack and stack[-1][0] >= indent:
                stack.pop()
            value = _unquote(m["val"])
            if value:
                out.append((tuple(k for _, k in stack) + (m["key"],), value))
            else:
                stack.append((indent, m["key"]))
        elif m := _YAML_ITEM.match(raw):
            while stack and stack[-1][0] >= len(m["ind"]) + 2:
                stack.pop()
            if stack:
                out.append((tuple(k for _, k in stack), m["val"]))
    return out


def _server_url_upstream(url: str) -> str:
    scheme, sep, rest = url.partition("://")
    if not sep:
        return ""
    target = rest.split("/", 1)[0]
    if ":" not in target:
        target += ":443" if scheme == "https" else ":80"
    return target


def _routes_from(
    routers: dict[str, dict], servers: dict[str, str], hosts_of, path_of, default_service: bool = False
) -> list[ProxyRoute]:
    routes: list[ProxyRoute] = []
    for router in routers.values():
        service = str(router.get("service", "")).split("@", 1)[0]
        upstream = servers.get(service, "")
        rule = str(router.get("rule", ""))
        if not upstream or not rule:
            continue
        path = path_of(rule)
        for host in hosts_of(rule)[:10]:
            if len(routes) >= 200:
                return routes
            routes.append(
                ProxyRoute(kind="traefik", host=host, path=path, tls=bool(router.get("tls")), upstream=upstream[:256])
            )
    return routes


def parse_traefik_file(lines: list[str]) -> list[ProxyRoute]:
    """Routes from a Traefik dynamic file (YAML or TOML): router rules joined to their load-balancer URLs."""
    from .stacks import _hosts_from_rule, _path_from_rule

    routers: dict[str, dict] = {}
    servers: dict[str, str] = {}
    for path, value in _leaves(lines):
        if len(path) < 4 or path[0] != "http":
            continue
        if path[1] == "routers":
            router = routers.setdefault(path[2], {})
            prop = path[3]
            if prop in ("rule", "service"):
                router[prop] = value
            elif prop == "tls" and value.lower() != "false":
                router["tls"] = True
            elif prop == "entryPoints" and "websecure" in value.lower():
                router["tls"] = True
        elif path[1] == "services" and path[3:5] == ("loadBalancer", "servers") and path[-1] == "url":
            if upstream := _server_url_upstream(value):
                servers.setdefault(path[2], upstream)
    return _routes_from(routers, servers, _hosts_from_rule, _path_from_rule)


def parse_traefik_api(routers_json: str, services_json: str) -> list[ProxyRoute]:
    """Routes from Traefik's own API: every provider (file, docker, http), so also Pangolin's resources."""
    from .stacks import _hosts_from_rule, _path_from_rule

    try:
        raw_routers = json.loads(routers_json or "[]")
        raw_services = json.loads(services_json or "[]")
    except ValueError:
        return []
    if not isinstance(raw_routers, list) or not isinstance(raw_services, list):
        return []
    servers: dict[str, str] = {}
    for item in raw_services[:500]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        balancer = item.get("loadBalancer")
        first = balancer.get("servers") if isinstance(balancer, dict) else None
        if isinstance(first, list) and first and isinstance(first[0], dict):
            if upstream := _server_url_upstream(str(first[0].get("url", ""))):
                servers[item["name"].split("@", 1)[0]] = upstream
    routers: dict[str, dict] = {}
    for item in raw_routers[:500]:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("provider") != "internal":
            entry = [str(e).lower() for e in item.get("entryPoints") or [] if isinstance(e, str)]
            routers[item["name"]] = {
                "rule": item.get("rule", ""),
                "service": item.get("service", ""),
                "tls": bool(item.get("tls")) or "websecure" in entry,
            }
    return _routes_from(routers, servers, _hosts_from_rule, _path_from_rule)


def routes_from_lines(groups: dict[str, list[str]]) -> list[ProxyRoute]:
    """All routes from the ``__PF__`` lines of a scan, grouped by their tag."""
    routes: list[ProxyRoute] = []
    api: dict[str, dict[str, str]] = {}
    for tag, lines in groups.items():
        kind, _, rest = tag.partition(":")
        if kind in ("nx", "npm"):
            routes.extend(parse_nginx(lines))
        elif kind == "tf":
            routes.extend(parse_traefik_file(lines))
        elif kind == "ta":
            cid, _, what = rest.rpartition(":")
            api.setdefault(cid, {})[what] = "".join(lines)[:MAX_JSON]
    for parts in api.values():
        routes.extend(parse_traefik_api(parts.get("r", ""), parts.get("s", "")))
    return routes

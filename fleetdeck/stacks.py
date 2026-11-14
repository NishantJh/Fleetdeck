"""Docker Compose stacks on a machine: finding them, checking for newer images, updating them.

Stacks are found through the labels Compose puts on every container it makes
(``com.docker.compose.project`` and friends), so no paths have to be configured and it works for
stacks started by hand, by Arcane or by anything else that uses Compose.

* **Scan** (read-only): lists the stacks and, for each service, whether the image its container was
  made from is still the one its tag points at on the machine.
* **Check** = pull every stack's images (nothing is restarted), then scan: containers that run an older
  image than the freshly pulled one are "update available".
* **Update** one stack: pull, ``up -d`` for the services that are running now (a service somebody
  stopped stays stopped), optionally delete unused (dangling) images.

Every script runs as root through the usual executors (SSH, ``pct exec``, guest agent, Incus exec);
the dashboard's sudo rule already gives root, so machines set up earlier need no change.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path

from .models import ExecResult, NetworkAddress, PortMapping, ProxyRoute, Stack, StackService, StacksResult
from .updates import _CONTAINER_ID
from .urls import safe_host, safe_path, safe_url

US = "\x1f"  # separates fields; never in a name, path or image reference

# Compose project names: lower-case letters, digits, dashes and underscores (older versions allowed
# more). Checked because the name is put into a shell command.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PATH = re.compile(r"^/[^\x00-\x1f\x7f]*$")

SCAN_SCRIPT = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
export LC_ALL=C
US=$(printf '\037')
if ! command -v docker >/dev/null 2>&1; then echo "__DOCKER__=none"; exit 0; fi
if ! docker_version=$(docker info --format '{{.ServerVersion}}' 2>/dev/null); then
  echo "__DOCKER__=down"
  docker info 2>&1 | tail -n 3 | sed 's/^/! /'
  exit 0
fi
echo "__DOCKER__=yes"
printf '__DOCKER_VERSION__=%s\n' "$docker_version"
if docker compose version >/dev/null 2>&1; then echo "__COMPOSE__=v2"
elif command -v docker-compose >/dev/null 2>&1; then echo "__COMPOSE__=v1"
else echo "__COMPOSE__=none"; fi
ids=$(docker ps -a -q 2>/dev/null)
[ -n "$ids" ] || exit 0
L='com.docker.compose'
lab() { printf '{{with index .Config.Labels "%s"}}{{.}}{{end}}' "$1"; }
F="__C__${US}$(lab $L.project)${US}$(lab $L.service)"
F="$F${US}$(lab $L.project.working_dir)${US}$(lab $L.project.config_files)"
F="$F${US}{{.Config.Image}}${US}{{.Image}}${US}{{.State.Status}}${US}{{.Id}}"
F="$F${US}{{.HostConfig.NetworkMode}}${US}{{.Name}}"
docker inspect --format "$F" $ids 2>/dev/null
# Ports, declared ports and networks: the services catalogue uses published host ports and macvlan IPs only.
docker inspect --format "__P__${US}{{.Id}}${US}{{json .NetworkSettings.Ports}}" $ids 2>/dev/null
docker inspect --format "__E__${US}{{.Id}}${US}{{json .Config.ExposedPorts}}" $ids 2>/dev/null
NETF='__N__${US}{{.Id}}${US}{{.HostConfig.NetworkMode}}${US}'
NETF="$NETF{{range $n, $v := .NetworkSettings.Networks}}{{$n}}={{$v.IPAddress}};{{end}}"
docker inspect --format "$NETF" $ids 2>/dev/null
# Only the labels the catalogue may use. Secrets in middleware/auth/header labels never leave the machine.
LBL='{{range $k, $v := .Config.Labels}}{{$k}}{{"\x1f"}}{{$v}}{{"\n"}}{{end}}'
for id in $ids; do
  docker inspect --format "$LBL" "$id" 2>/dev/null | head -n 200 | cut -c1-600 | while IFS="$US" read -r k v; do
    case "$k" in
      caddy|caddy_*|caddy.*|traefik.http.routers.*|VIRTUAL_HOST|VIRTUAL_PORT|VIRTUAL_PROTO|fleetdeck.url|fleetdeck.ui|fleetdeck.category)
        printf '__L__%s%s%s%s%s%s%s\n' "$US" "$id" "$US" "$k" "$US" "$v" ;;
    esac
  done
done
for img in $(docker inspect --format '{{.Config.Image}}' $ids 2>/dev/null | sort -u); do
  id=$(docker image inspect --format '{{.Id}}' "$img" 2>/dev/null) || continue
  # Every sha256 in the tag's JSON: with the containerd image store the tag is a multi-platform index
  # while containers report the ID of their platform's manifest, which is listed in there.
  known=$(docker image inspect "$img" 2>/dev/null | grep -oE 'sha256:[0-9a-f]{64}' | sort -u | tr '
' ',')
  plat=$(docker image inspect --format '{{.Os}}/{{.Architecture}}{{with .Variant}}/{{.}}{{end}}' "$img" 2>/dev/null)
  echo "__I__${US}${img}${US}${id}${US}${known%,}${US}${plat}"
  echo "__R__${US}${img}${US}$(docker image inspect --format '{{join .RepoDigests ","}}' "$img" 2>/dev/null)"
done
# The driver decides whether a container IP is a LAN address (macvlan/ipvlan) or only internal (bridge/overlay).
docker inspect --format '{{range $n, $v := .NetworkSettings.Networks}}{{$n}}{{"\n"}}{{end}}' $ids 2>/dev/null \
  | sort -u | head -n 50 | while read -r net; do
  [ -n "$net" ] || continue
  driver=$(docker network inspect --format '{{.Driver}}' "$net" 2>/dev/null) || continue
  printf '__ND__%s%s%s%s%s\n' "$US" "$net" "$US" "$driver"
done
# Caddy: read the Caddyfile and keep only site lines, braces and reverse_proxy directives. Read-only.
for id in $ids; do
  image=$(docker inspect --format '{{.Config.Image}}' "$id" 2>/dev/null)
  case "$image" in
    *caddy*)
      docker exec "$id" sh -c 'cat /etc/caddy/Caddyfile 2>/dev/null || cat /config/Caddyfile 2>/dev/null' 2>/dev/null \
        | head -c 200000 | head -n 2000 | while IFS= read -r line; do
          case "$line" in
            *reverse_proxy*|*"{"*|*"}"*) printf '__CF__%s%s%s\n' "$US" "$id" "$line" ;;
          esac
        done ;;
  esac
done
exit 0
"""


def own_container_id(mountinfo: str) -> str | None:
    """The ID of the container this dashboard runs in (it is in the path of the bind-mounted /etc/hostname)."""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[4] == "/etc/hostname" and (m := _CONTAINER_ID.search(fields[3])):
            return m[0]
    return None


def _own_id() -> str | None:
    try:
        return own_container_id(Path("/proc/self/mountinfo").read_text())
    except OSError:
        return None


def _json_object(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _caddy_site_hosts(head: str) -> tuple[list[str], bool]:
    """Hosts and the TLS default from a Caddyfile site line (before its opening brace)."""
    hosts: list[str] = []
    tls = True
    for raw in re.split(r"[,\s]+", head.strip()):
        raw = raw.strip()
        if not raw or raw.startswith(("(", "@", ":")):
            continue
        scheme = ""
        if "://" in raw:
            scheme, _, raw = raw.partition("://")
        if scheme == "http" or raw.endswith(":80"):
            tls = False
        if host := safe_host(raw):
            hosts.append(host)
    return hosts[:10], tls


def _parse_caddyfile(lines: list[str]) -> list[ProxyRoute]:
    """Concrete host -> upstream routes from simple Caddyfile site blocks. Best effort, read-only.

    Handles the common ``site { reverse_proxy upstream }`` form (including nested handle/route blocks)
    and ignores snippets, wildcards, placeholders and non-reverse_proxy handlers."""
    routes: list[ProxyRoute] = []
    stack: list[tuple[list[str], bool, int]] = []
    depth = 0
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith("{"):
            hosts, tls = _caddy_site_hosts(line[:-1]) if depth == 0 else ([], True)
            stack.append((hosts, tls, depth))
            depth += 1
            continue
        if line.startswith("}"):
            depth = max(0, depth - 1)
            while stack and stack[-1][2] >= depth:
                stack.pop()
            continue
        if not line.startswith("reverse_proxy"):
            continue
        hosts, tls = next(((h, t) for h, t, _ in reversed(stack) if h), ([], True))
        if not hosts:
            continue
        upstream = ""
        for token in shlex.split(line)[1:]:
            if token in ("{", "}") or token.startswith(("/", "@")):
                continue
            upstream = token
            break
        if not upstream or "{" in upstream:
            continue
        upstream = upstream.split("://", 1)[-1].rstrip("/")
        if upstream.startswith("unix/"):
            continue
        for host in hosts:
            if len(routes) >= 200:
                return routes
            routes.append(ProxyRoute(host=host, tls=tls, upstream=upstream[:256]))
    return routes


def _parse_ports(raw: str) -> list[PortMapping]:
    """Published ports from ``docker inspect``'s NetworkSettings.Ports JSON."""
    data = _json_object(raw)
    if data is None:
        return []
    out: list[PortMapping] = []
    for key, values in list(data.items())[:50]:
        if not isinstance(key, str) or "/" not in key:
            continue
        port, _, protocol = key.partition("/")
        if not port.isdigit() or not 1 <= int(port) <= 65535 or not isinstance(values, list):
            continue
        for item in values[:10]:
            if not isinstance(item, dict):
                continue
            host_port = item.get("HostPort")
            if not isinstance(host_port, str) or not host_port.isdigit() or not 1 <= int(host_port) <= 65535:
                continue
            host_ip = item.get("HostIp") if isinstance(item.get("HostIp"), str) else ""
            out.append(
                PortMapping(
                    host_ip=host_ip[:64],
                    host_port=int(host_port),
                    container_port=int(port),
                    protocol=(protocol or "tcp")[:8],
                )
            )
    return out[:50]


def _parse_exposed(raw: str) -> list[int]:
    """Ports from ``docker inspect``'s Config.ExposedPorts JSON (the only port info for host networking)."""
    data = _json_object(raw)
    if data is None:
        return []
    out: list[int] = []
    for key in list(data)[:50]:
        if not isinstance(key, str) or "/" not in key:
            continue
        port = key.partition("/")[0]
        if port.isdigit() and 1 <= int(port) <= 65535 and int(port) not in out:
            out.append(int(port))
    return out[:20]


def _parse_networks(raw: str) -> list[NetworkAddress]:
    out: list[NetworkAddress] = []
    for item in raw.split(";"):
        name, sep, ip = item.partition("=")
        name, ip = name.strip()[:128], ip.strip()[:64]
        if sep and name:
            out.append(NetworkAddress(name=name, ip=ip))
    return out[:20]


def _tri_bool(value: str) -> bool | None:
    value = value.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _hosts_from_rule(rule: str) -> list[str]:
    """Concrete hosts from a Traefik rule's ``Host(...)`` matchers. HostRegexp is deliberately ignored."""
    out: list[str] = []
    for match in re.finditer(r"\bHost(?:Header)?\s*\(([^)]*)\)", rule):
        args = match.group(1)
        found = re.findall(r"[`\"']([^`\"']+)[`\"']", args) or [x.strip() for x in args.split(",")]
        for raw in found:
            host = safe_host(raw)
            if host and host not in out:
                out.append(host)
    return out


def _path_from_rule(rule: str) -> str:
    match = re.search(r"\bPathPrefix\s*\(\s*[`\"']([^`\"']+)[`\"']", rule)
    return safe_path(match.group(1)) or "/" if match else "/"


def _parse_labels(labels: dict[str, str]) -> tuple[list[ProxyRoute], list[str], bool | None, str]:
    """Proxy routes, explicit URLs, the ui override and the category from a filtered label set.

    Only the keys captured by SCAN_SCRIPT reach here, and middleware/auth/header labels are ignored
    even if they share a prefix. Values are capped again here."""
    proxy: list[ProxyRoute] = []
    urls: list[str] = []
    ui: bool | None = None
    category = ""
    traefik: dict[str, dict[str, str]] = {}
    nginx_tls = False

    def add(kind: str, host: str, path: str = "/", tls: bool = True) -> None:
        if len(proxy) >= 10 or not (host := safe_host(host)):
            return
        path = safe_path(path) or "/"
        if not any(r.kind == kind and r.host == host and r.path == path for r in proxy):
            proxy.append(ProxyRoute(kind=kind, host=host, path=path, tls=tls))

    for key, value in labels.items():
        key, value = key[:128], value[:512]
        if key == "fleetdeck.ui":
            ui = _tri_bool(value)
        elif key == "fleetdeck.category":
            clean = value.strip()
            if re.fullmatch(r"[A-Za-z0-9 &_-]{1,40}", clean):
                category = clean
        elif key == "fleetdeck.url":
            for part in re.split(r"[\s,;]+", value):
                url = safe_url(part)
                if url and url not in urls and len(urls) < 5:
                    urls.append(url)
        elif key == "caddy" or re.fullmatch(r"caddy_[0-9]+", key):
            for host in re.split(r"[\s,]+", value):
                add("caddy", host)
        elif key.startswith("traefik.http.routers."):
            router, _, prop = key.removeprefix("traefik.http.routers.").partition(".")
            if router and prop in ("rule", "entrypoints", "tls", "tls.certresolver"):
                traefik.setdefault(router, {})[prop] = value
        elif key == "VIRTUAL_HOST":
            for host in value.split(","):
                add("nginx", host, tls=False)
        elif key == "VIRTUAL_PROTO":
            nginx_tls = value.strip().lower() in ("https", "443")

    if nginx_tls:
        proxy = [r.model_copy(update={"tls": True}) if r.kind == "nginx" else r for r in proxy]
    for props in list(traefik.values())[:10]:
        hosts = _hosts_from_rule(props.get("rule", ""))
        path = _path_from_rule(props.get("rule", ""))
        tls = (
            props.get("tls", "").lower() in ("true", "1")
            or "tls.certresolver" in props
            or "websecure" in props.get("entrypoints", "").lower()
        )
        for host in hosts[:10]:
            add("traefik", host, path, tls)
    return proxy, urls, ui, category


def parse_scan(res: ExecResult, own_id: str | None = None) -> StacksResult:
    """The stacks a scan found. ``own_id``: the dashboard's own container, whose stack is marked."""
    docker, compose, docker_version = "yes", "none", None
    local: dict[str, str] = {}  # image reference -> ID of what that tag points at now
    known: dict[str, set[str]] = {}  # image reference -> every digest that belongs to that tag's image
    digests: dict[str, list[str]] = {}  # image reference -> its registry digests
    platforms: dict[str, str] = {}  # image reference -> os/arch[/variant] of the local image
    rows: list[list[str]] = []
    ports: dict[str, list[PortMapping]] = {}
    exposed: dict[str, list[int]] = {}
    networks: dict[str, list[NetworkAddress]] = {}
    drivers: dict[str, str] = {}
    labels: dict[str, dict[str, str]] = {}
    caddyfile: dict[str, list[str]] = {}
    for line in res.stdout.splitlines():
        if line.startswith("__DOCKER__="):
            docker = line.split("=", 1)[1]
        elif line.startswith("__DOCKER_VERSION__="):
            value = line.split("=", 1)[1]
            if re.fullmatch(r"[0-9][A-Za-z0-9.+~_-]{0,63}", value):
                docker_version = value
        elif line.startswith("__COMPOSE__="):
            compose = line.split("=", 1)[1]
        elif line.startswith("__I__" + US):
            parts = line.split(US)
            if len(parts) in (3, 4, 5):
                local[parts[1]] = parts[2]
                known[parts[1]] = {x for x in (parts[3] if len(parts) > 3 else "").split(",") if x}
                if len(parts) == 5 and re.fullmatch(r"[a-z0-9]{1,16}/[a-z0-9_]{1,16}(/[a-z0-9]{1,8})?", parts[4]):
                    platforms[parts[1]] = parts[4]
        elif line.startswith("__R__" + US):
            parts = line.split(US)
            if len(parts) == 3:
                digests[parts[1]] = [x for x in parts[2].split(",") if x]
        elif line.startswith("__P__" + US):
            parts = line.split(US, 2)
            if len(parts) == 3:
                ports[parts[1]] = _parse_ports(parts[2])
        elif line.startswith("__E__" + US):
            parts = line.split(US, 2)
            if len(parts) == 3:
                exposed[parts[1]] = _parse_exposed(parts[2])
        elif line.startswith("__N__" + US):
            parts = line.split(US, 3)
            if len(parts) == 4:
                networks[parts[1]] = _parse_networks(parts[3])
        elif line.startswith("__ND__" + US):
            parts = line.split(US, 2)
            if len(parts) == 3 and _NAME.match(parts[1]):
                drivers[parts[1]] = parts[2][:32]
        elif line.startswith("__L__" + US):
            parts = line.split(US, 3)
            if len(parts) == 4 and _NAME.match(parts[1]):
                labels.setdefault(parts[1], {})[parts[2]] = parts[3]
        elif line.startswith("__CF__" + US):
            parts = line.split(US, 2)
            if len(parts) == 3 and len(caddyfile.get(parts[1], [])) < 2000:
                caddyfile.setdefault(parts[1], []).append(parts[2][:600])
        elif line.startswith("__C__" + US):
            parts = line.split(US)
            if len(parts) in (9, 11):
                rows.append(parts[1:])
    for items in networks.values():
        for item in items:
            item.driver = drivers.get(item.name, "")

    stacks: dict[str, Stack] = {}
    standalone: list[StackService] = []
    for row in rows:
        project, service, wd, files, image, image_id, status, cid = row[:8]
        mode = row[8] if len(row) > 8 else ""
        container_name = (row[9] if len(row) > 9 else "").lstrip("/")[:128]
        proxy, urls, ui, category = _parse_labels(labels.get(cid, {}))
        now = local.get(image, "")
        # Same ID (classic store), or the container's ID is one of the tag's own manifests (containerd
        # store). Unknown tag: don't guess. A wrong "up to date" is better than a permanent false alarm.
        outdated = bool(now) and now != image_id and image_id not in known.get(image, ())
        fallback = container_name if _NAME.match(container_name) else cid[:12]
        name = service if _NAME.match(service) else fallback
        entry = StackService(
            name=name,
            image=image,
            status=status,
            outdated=outdated,
            image_id=image_id,
            tag_id=now,
            platform=platforms.get(image, ""),
            repo_digests=digests.get(image, []),
            container_id=cid,
            container_name=container_name,
            network_mode=mode[:64],
            ports=ports.get(cid, []),
            exposed_ports=exposed.get(cid, []),
            networks=networks.get(cid, []),
            proxy=proxy,
            explicit_urls=urls,
            ui=ui,
            category=category,
        )
        if not project:
            standalone.append(entry.model_copy(update={"name": fallback}))
            continue
        if not _NAME.match(project):
            continue
        stack = stacks.get(project)
        if stack is None:
            paths = [f for f in files.split(",") if _PATH.match(f)]
            stack = stacks[project] = Stack(name=project, working_dir=wd if _PATH.match(wd) else "", config_files=paths)
        stack.services.append(entry)
        if own_id and cid == own_id:
            stack.is_dashboard = True
    routes: list[ProxyRoute] = []
    for lines in caddyfile.values():
        routes.extend(_parse_caddyfile(lines))
    unique: list[ProxyRoute] = []
    seen: set[tuple[str, str, bool, str]] = set()
    for route in routes:
        key = (route.host, route.path, route.tls, route.upstream)
        if key not in seen:
            seen.add(key)
            unique.append(route)
    error = None
    if docker == "none":
        error = "Docker is not installed on this machine"
    elif docker == "down":
        detail = " ".join(ln[2:] for ln in res.stdout.splitlines() if ln.startswith("! "))
        error = "Docker did not answer" + (f": {detail}" if detail else "") + ". Is it running?"
    elif res.rc != 0:
        error = f"exit code {res.rc}: {(res.stderr or res.stdout).strip()[-300:]}"
    for stack in stacks.values():
        stack.services.sort(key=lambda s: s.name)
    standalone.sort(key=lambda s: s.name)
    return StacksResult(
        docker=docker,
        compose=compose,
        docker_version=docker_version,
        stacks=sorted(stacks.values(), key=lambda s: s.name),
        standalone=standalone,
        routes=unique[:200],
        checked_at=datetime.now(UTC),
        error=error,
    )


def _compose_args(stack: Stack) -> str:
    args = ["-p", stack.name]
    if stack.working_dir:
        args += ["--project-directory", stack.working_dir]
    for f in stack.config_files:
        args += ["-f", f]
    return " ".join(shlex.quote(a) for a in args)


_PRELUDE = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
export LC_ALL=C COMPOSE_ANSI=never COMPOSE_PROGRESS=plain TERM=dumb
US=$(printf '\037')
if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then DC=docker-compose
else echo "Docker Compose is not installed on this machine" >&2; exit 3; fi
"""


def _in_dir(stack: Stack) -> str:
    if not stack.working_dir:
        return ":"
    wd = shlex.quote(stack.working_dir)
    gone = shlex.quote(f"its folder {stack.working_dir} is gone")
    return f"[ -d {wd} ] || {{ echo {gone} >&2; exit 4; }}; cd {wd}"


def pull_script(stacks: list[Stack]) -> str:
    """Pulls the images of every stack (restarting nothing). A failing stack is reported on a
    ``__PULLFAIL__`` line and does not stop the others."""
    out = [_PRELUDE]
    for s in stacks:
        names = " ".join(shlex.quote(x.name) for x in s.running)
        args = _compose_args(s)
        # --ignore-buildable skips services built from a Dockerfile; older Compose doesn't know it.
        pull = f"{_in_dir(s)} && $DC {args} pull --quiet"
        out.append(
            f"echo {shlex.quote('== Checking ' + s.name)}\n"
            f"o=$( ( {pull} --ignore-buildable {names} ) 2>&1 ); r=$?\n"
            f'if [ $r -ne 0 ] && printf %s "$o" | grep -qiE "unknown flag|unrecognized"; then\n'
            f"  o=$( ( {pull} {names} ) 2>&1 ); r=$?; fi\n"
            f'if [ $r -ne 0 ]; then printf "__PULLFAIL__%s%s%s%s\\n" "$US" {shlex.quote(s.name)} "$US" '
            f"\"$(printf %s \"$o\" | tail -n 3 | tr '\\n\\t' '  ')\"; fi\n"
        )
    out.append("exit 0\n")
    return "".join(out)


def parse_pull_failures(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if line.startswith("__PULLFAIL__" + US):
            parts = line.split(US, 2)
            if len(parts) == 3:
                out[parts[1]] = parts[2].strip()
    return out


def update_script(stack: Stack, prune: bool) -> str:
    """Pull and recreate what changed, for the services running now. Fails (exit 4) when the stack's
    folder is gone, before anything is touched."""
    names = " ".join(shlex.quote(x.name) for x in stack.running)
    args = _compose_args(stack)
    return (
        _PRELUDE
        + "set -e\n"
        + f"{_in_dir(stack)}\n"
        + f'echo "== Pulling {stack.name}"\n'
        + f"$DC {args} pull {names}\n"
        + f'echo "== Restarting what changed in {stack.name}"\n'
        + f"$DC {args} up -d --no-deps {names}\n"
        + ('echo "== Removing unused old images"\ndocker image prune -f\n' if prune else "")
    )

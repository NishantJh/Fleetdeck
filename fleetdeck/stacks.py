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

import re
import shlex
from datetime import UTC, datetime
from pathlib import Path

from .models import ExecResult, Stack, StackService, StacksResult
from .updates import _CONTAINER_ID

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
if ! docker info >/dev/null 2>&1; then
  echo "__DOCKER__=down"
  docker info 2>&1 | tail -n 3 | sed 's/^/! /'
  exit 0
fi
echo "__DOCKER__=yes"
if docker compose version >/dev/null 2>&1; then echo "__COMPOSE__=v2"
elif command -v docker-compose >/dev/null 2>&1; then echo "__COMPOSE__=v1"
else echo "__COMPOSE__=none"; fi
ids=$(docker ps -a -q --filter label=com.docker.compose.project 2>/dev/null)
[ -n "$ids" ] || exit 0
L='com.docker.compose'
lab() { printf '{{index .Config.Labels "%s"}}' "$1"; }
F="__C__${US}$(lab $L.project)${US}$(lab $L.service)"
F="$F${US}$(lab $L.project.working_dir)${US}$(lab $L.project.config_files)"
F="$F${US}{{.Config.Image}}${US}{{.Image}}${US}{{.State.Status}}${US}{{.Id}}"
docker inspect --format "$F" $ids 2>/dev/null
for img in $(docker inspect --format '{{.Config.Image}}' $ids 2>/dev/null | sort -u); do
  id=$(docker image inspect --format '{{.Id}}' "$img" 2>/dev/null) && echo "__I__${US}${img}${US}${id}"
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


def parse_scan(res: ExecResult, own_id: str | None = None) -> StacksResult:
    """The stacks a scan found. ``own_id``: the dashboard's own container, whose stack is marked."""
    docker, compose = "yes", "none"
    local: dict[str, str] = {}  # image reference -> ID of what that tag points at now
    rows: list[list[str]] = []
    for line in res.stdout.splitlines():
        if line.startswith("__DOCKER__="):
            docker = line.split("=", 1)[1]
        elif line.startswith("__COMPOSE__="):
            compose = line.split("=", 1)[1]
        elif line.startswith("__I__" + US):
            parts = line.split(US)
            if len(parts) == 3:
                local[parts[1]] = parts[2]
        elif line.startswith("__C__" + US):
            parts = line.split(US)
            if len(parts) == 9 and _NAME.match(parts[1]):
                rows.append(parts[1:])
    stacks: dict[str, Stack] = {}
    for project, service, wd, files, image, image_id, status, cid in rows:
        stack = stacks.get(project)
        if stack is None:
            paths = [f for f in files.split(",") if _PATH.match(f)]
            stack = stacks[project] = Stack(name=project, working_dir=wd if _PATH.match(wd) else "", config_files=paths)
        now = local.get(image)
        stack.services.append(
            StackService(name=service, image=image, status=status, outdated=bool(now) and now != image_id)
        )
        if own_id and cid == own_id:
            stack.is_dashboard = True
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
    return StacksResult(
        docker=docker,
        compose=compose,
        stacks=sorted(stacks.values(), key=lambda s: s.name),
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

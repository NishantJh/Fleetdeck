"""Updating the dashboard's own Docker container from inside the dashboard.

The pull runs while the request is open, so a failure is reported before anything restarts. The
recreate runs on the host, detached, after a short delay: it replaces the very container this
process lives in. Only the dashboard's own Compose service is named; nothing else is touched.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Stack, StackService, StacksResult
from .stacks import _PRELUDE, _compose_args, _in_dir

# On the host that runs the container, so it survives the recreate.
SELF_UPDATE_LOG = "/tmp/fleetdeck-self-update.log"
# In the data folder, so the restarted dashboard knows what happened.
MARKER = "self-update.json"


def read_result(data_dir: Path) -> dict | None:
    """The last self-update attempt (state, versions, log), or None."""
    try:
        data = json.loads((data_dir / MARKER).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(data_dir: Path, data: dict) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / f".{MARKER}.tmp"
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(data_dir / MARKER)
    except OSError:
        pass  # the dashboard still updates; only the result is lost


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def begin(data_dir: Path, *, version: str, image: str, target: str, service: str, stack: str) -> None:
    """Record that the recreate is about to replace this container, before it is started."""
    _write(
        data_dir,
        {
            "state": "recreating",
            "started_at": datetime.now(UTC).isoformat(),
            "from_version": version,
            "image": image,
            "target": target,
            "service": service,
            "stack": stack,
            "log": SELF_UPDATE_LOG,
        },
    )


def mark_failed(data_dir: Path, detail: str) -> None:
    data = read_result(data_dir) or {"started_at": datetime.now(UTC).isoformat()}
    data.update(state="failed", finished_at=datetime.now(UTC).isoformat(), detail=str(detail)[:300])
    _write(data_dir, data)


def finish_startup(data_dir: Path, app_started: datetime, version: str) -> None:
    """Called once at start-up: if a recreate was pending and this process is newer, it worked."""
    data = read_result(data_dir)
    if not data or data.get("state") != "recreating":
        return
    started = _time(data.get("started_at"))
    if started and app_started > started:
        data.update(state="done", finished_at=datetime.now(UTC).isoformat(), to_version=version)
        _write(data_dir, data)


def mark_stale(data_dir: Path, app_started: datetime, seconds: int = 90) -> None:
    """The old process is still serving long after a recreate was requested: it did not restart."""
    data = read_result(data_dir)
    if not data or data.get("state") != "recreating":
        return
    started = _time(data.get("started_at"))
    if started and app_started < started and (datetime.now(UTC) - started).total_seconds() > seconds:
        data.update(
            state="failed",
            finished_at=datetime.now(UTC).isoformat(),
            detail="The container did not restart. Check the log on the machine that runs Fleetdeck.",
        )
        _write(data_dir, data)


def find_dashboard(stacks: dict[str, StacksResult], own_id: str | None) -> tuple[str, Stack, StackService] | None:
    """(target id, stack, service) for the Compose stack that runs this dashboard, or None."""
    for target_id, result in stacks.items():
        for stack in result.stacks:
            if not stack.is_dashboard:
                continue
            service = next((s for s in stack.services if own_id and s.container_id == own_id), None)
            if service is None:
                service = next((s for s in stack.services if "fleetdeck" in f"{s.name} {s.image}".lower()), None)
            if service is None and stack.services:
                service = stack.services[0]
            if service is not None:
                return target_id, stack, service
    return None


def info(
    stacks: dict[str, StacksResult],
    own_id: str | None,
    version: str,
    targets: dict[str, Any],
    last: dict | None = None,
) -> dict:
    """What the dashboard menu shows: version, whether an update is known, and why it can't update."""
    found = find_dashboard(stacks, own_id)
    if found is None:
        return {
            "supported": False,
            "version": version,
            "last": last or {},
            "reason": "Fleetdeck cannot see its own Compose stack. Add the machine that runs it as a target, "
            "then run Check container updates once. Otherwise update it from a terminal: "
            "docker compose pull && docker compose up -d",
        }
    target_id, stack, service = found
    entry = targets.get(target_id)
    target = entry[0] if isinstance(entry, tuple) else entry
    reason = ""
    if not stack.running:
        reason = "The Fleetdeck container is not running."
    elif not stack.working_dir:
        reason = "Its Compose folder is unknown; update it from a terminal."
    elif target is None:
        reason = "The machine that runs Fleetdeck is not in the inventory."
    elif getattr(target, "check_only", False):
        reason = "That machine is marked check-only."
    return {
        "supported": not reason,
        "reason": reason,
        "version": version,
        "target_id": target_id,
        "target": getattr(target, "name", ""),
        "stack": stack.name,
        "service": service.name,
        "image": service.image,
        "available": bool(service.outdated or service.registry == "update"),
        "last": last or {},
    }


def pull_script(stack: Stack, service: str) -> str:
    """Pull one service's image; a buildable service falls back to a plain pull for older Compose."""
    svc = shlex.quote(service)
    return (
        _PRELUDE
        + "set -e\n"
        + f"{_in_dir(stack)}\n"
        + f'echo "== Pulling {service}"\n'
        + f"if ! o=$($DC {_compose_args(stack)} pull --quiet --ignore-buildable {svc} 2>&1); then\n"
        + '  case "$o" in\n'
        + '    *"unknown flag"*|*unrecognized*)\n'
        + f'      o=$($DC {_compose_args(stack)} pull --quiet {svc} 2>&1) || {{ echo "$o" >&2; exit 1; }} ;;\n'
        + '    *) echo "$o" >&2; exit 1 ;;\n'
        + "  esac\n"
        + "fi\n"
    )


def recreate_script(stack: Stack, service: str) -> str:
    """Write a small recreate script on the host and start it detached, so this container can stop."""
    body = (
        "#!/bin/sh\n"
        "set -e\n"
        f"{_in_dir(stack)}\n"
        "sleep 2\n"
        'if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC=docker-compose; fi\n'
        f"$DC {_compose_args(stack)} up -d --no-deps {shlex.quote(service)}\n"
    )
    return (
        _PRELUDE
        + f"cat > {SELF_UPDATE_LOG}.sh <<'__FLEETDECK__'\n"
        + body
        + "__FLEETDECK__\n"
        + "if command -v setsid >/dev/null 2>&1; then\n"
        + f"  setsid sh {SELF_UPDATE_LOG}.sh > {SELF_UPDATE_LOG} 2>&1 < /dev/null &\n"
        + "else\n"
        + f"  nohup sh {SELF_UPDATE_LOG}.sh > {SELF_UPDATE_LOG} 2>&1 < /dev/null &\n"
        + "fi\n"
        + 'echo "== Recreating the Fleetdeck container"\n'
    )

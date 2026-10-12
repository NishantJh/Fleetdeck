"""A plain-text report of everything that is failing, to attach when asking for help."""

from __future__ import annotations

import platform
import re
from datetime import UTC, datetime

from . import __version__
from .config import AppConfig
from .inventory import Inventory
from .jobs import JobManager

FAILED_JOBS = 10
LOG_TAIL = 30
_SECRET_KEY = re.compile(r"secret|password|passphrase|token|private", re.I)


def _secrets(value, key: str = "") -> set[str]:
    """Every configured secret value, so it can be blanked out if an error message echoes it."""
    if isinstance(value, dict):
        return set().union(*(_secrets(v, str(k)) for k, v in value.items()))
    if isinstance(value, list):
        return set().union(*(_secrets(v, key) for v in value))
    if isinstance(value, str) and len(value) >= 4 and _SECRET_KEY.search(key) and not key.endswith("_id"):
        return {value}
    return set()


def build_report(cfg: AppConfig, inventory: Inventory, jobs: JobManager) -> str:
    lines = [
        "Fleetdeck error report",
        f"Made: {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
        f"Version: {__version__}, Python {platform.python_version()}",
        "Passwords and tokens are removed. Machine names and addresses are kept; edit them out before",
        "posting this publicly if you prefer.",
        "",
        "== Could not reach ==",
    ]
    lines += [f"- {name}: {err}" for name, err in sorted(inventory.errors.items())] or ["(nothing)"]

    lines += ["", "== Machines with an error or a note =="]
    found = False
    for t, _ in inventory.targets.values():
        check = jobs.checks.get(t.id)
        error = check.error if check else None
        if not error and not t.note:
            continue
        found = True
        where = f" on {t.parent}" if t.parent else ""
        lines.append(f"- {t.name} ({t.platform} {t.kind}{where}, {t.status})")
        if t.note:
            lines.append(f"    note: {t.note}")
        if error:
            lines.append(f"    last check ({check.checked_at:%Y-%m-%d %H:%M UTC}): {error}")
    if not found:
        lines.append("(nothing)")

    failed = [j for j in jobs.jobs.values() if j.status == "failed"][-FAILED_JOBS:][::-1]
    lines += ["", f"== Failed jobs (newest {FAILED_JOBS}) =="]
    for j in failed:
        when = f"{j.finished_at:%Y-%m-%d %H:%M UTC}" if j.finished_at else "unfinished"
        lines.append(f"- {j.action} on {j.target_name}, {when}: {j.error or 'failed'}")
        lines += [f"    | {line}" for line in j.log[-LOG_TAIL:]]
    if not failed:
        lines.append("(nothing)")

    text = "\n".join(lines) + "\n"
    # A Discord or webhook URL is itself the secret.
    hidden = _secrets(cfg.model_dump()) | {n.url for n in cfg.notify if n.url}
    for s in sorted(hidden, key=len, reverse=True):
        text = text.replace(s, "[removed]")
    return text

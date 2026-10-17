"""Runs a long script detached on the target, so it survives the dashboard restarting.

An upgrade is started in its own folder on the target, under ``systemd-run`` (or ``setsid nohup``
without systemd), writing its output to a log file and its exit code to a file when it ends. The
dashboard reads the log in short windows. When the dashboard restarts mid-run (its host rebooted,
its container was updated), the job remembers the folder and how far it had read, and picks up
where it left off; the upgrade itself never noticed.

Every executor can run these scripts: they only start or read a background process and return.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable

from .updates import BOOT_ID_SCRIPT

log = logging.getLogger(__name__)

JOBS_DIR = "/var/lib/homelab-manager/jobs"
# Longest one read waits for news; it returns sooner when output arrives or the run ends.
WINDOW = 30
# How long the target may be unreachable (network blip, tunnel restarting) before the job fails.
UNREACHABLE_FOR = 600.0
RETRY_EVERY = 10.0
# Folders left behind by runs nobody came back for are removed after this many days.
KEEP_DAYS = 14
_EOF = "__HOMELAB_MANAGER_SCRIPT_END__"
_PATH = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH\n"
# Prints this boot of the target (see updates.BOOT_ID_SCRIPT), to tell a crashed run from a live one.
_BOOT = BOOT_ID_SCRIPT.replace('echo "__BOOT__=$b-$s"', 'echo "$b-$s"')


_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _check_id(run_id: str) -> str:
    if not _RUN_ID.match(run_id):
        raise ValueError(f"bad job id {run_id!r}")
    return run_id


class NotStarted(RuntimeError):
    """The run could not be set up, and nothing was started."""


def start_script(run_id: str, script: str, base: str = JOBS_DIR) -> str:
    if _EOF in script:
        raise ValueError("script contains the heredoc terminator")
    d = f"{base}/{_check_id(run_id)}"
    return (
        _PATH
        + f"""
umask 077
d={d}
mkdir -p {base} && mkdir "$d" || {{ echo "could not create $d" >&2; exit 90; }}
find {base} -mindepth 1 -maxdepth 1 -type d -mtime +{KEEP_DAYS} -exec rm -rf {{}} + 2>/dev/null
cat > "$d/script" <<'{_EOF}' || exit 90
{script}
{_EOF}
cat > "$d/run" <<'{_EOF}' || exit 90
d=$(dirname "$0")
{_BOOT.strip()} > "$d/boot"
echo $$ > "$d/pid"
sh "$d/script" > "$d/log" 2>&1 </dev/null
echo $? > "$d/rc.tmp" && mv "$d/rc.tmp" "$d/rc"
{_EOF}
# systemd-run puts it in its own unit, out of reach of the session that started it; setsid + nohup
# is the fallback without systemd (or with one too old for --collect).
how=
if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ] \\
  && systemd-run --unit=homelab-manager-{run_id} --description="Homelab Manager job" --collect --quiet \\
    "$(command -v sh)" "$d/run" >/dev/null 2>&1; then how=systemd-run
else
  if command -v setsid >/dev/null 2>&1; then detach=setsid; else detach=""; fi
  $detach nohup sh "$d/run" >/dev/null 2>&1 </dev/null &
  how=nohup
fi
i=0
while [ ! -f "$d/pid" ] && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
echo "__STARTED__=$how"
"""
    )


def follow_script(run_id: str, offset: int, window: int = WINDOW, base: str = JOBS_DIR) -> str:
    """Waits up to ``window`` seconds for output or the end, then prints the log from ``offset`` on
    stdout, and ``__OFF__``/``__RC__``/``__LOST__``/``__GONE__`` markers on stderr."""
    offset = int(offset)
    return (
        _PATH
        + f"""
d={base}/{_check_id(run_id)}
[ -d "$d" ] || {{ echo __GONE__=1 >&2; exit 0; }}
size() {{ s=$(wc -c < "$d/log" 2>/dev/null); echo $((${{s:-0}} + 0)); }}
i=0
while [ ! -f "$d/rc" ] && [ $i -lt {int(window)} ]; do
  [ $i -ge 3 ] && [ "$(size)" -gt {offset} ] && break
  sleep 1; i=$((i+1))
done
rc=; [ -f "$d/rc" ] && rc=$(cat "$d/rc")
n=$(size)
[ "$n" -gt {offset} ] && tail -c +{offset + 1} "$d/log" | head -c $((n - {offset}))
echo "__OFF__=$n" >&2
if [ -n "$rc" ]; then echo "__RC__=$rc" >&2
elif [ -f "$d/pid" ]; then
  now=$({_BOOT.strip().replace(chr(10), "; ")})
  if [ "$now" != "$(cat "$d/boot" 2>/dev/null)" ] || ! kill -0 "$(cat "$d/pid")" 2>/dev/null; then
    [ -f "$d/rc" ] || echo __LOST__=1 >&2
  fi
fi
"""
    )


def cleanup_script(run_id: str, base: str = JOBS_DIR) -> str:
    return f"rm -rf {base}/{_check_id(run_id)}\n"


def _markers(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("__") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip("_")] = v.strip()
    return out


async def start(executor, run_id: str, script: str, base: str = JOBS_DIR) -> str:
    """Starts ``script`` on the target; returns how it was detached (systemd-run or nohup)."""
    res = await executor.run(start_script(run_id, script, base), timeout=120)
    if res.rc == 90:
        raise NotStarted((res.stderr or res.stdout).strip() or "could not set up the job folder")
    how = _markers(res.stdout).get("STARTED")
    if res.rc != 0 or not how:
        raise RuntimeError(f"starting the job on the machine failed (exit code {res.rc}): {res.stderr.strip()}")
    return how


async def follow(
    executor,
    run_id: str,
    offset: int,
    on_output: Callable[[str, int], None],
    timeout: float,
    base: str = JOBS_DIR,
    window: int = WINDOW,
) -> int:
    """Reads the run's output until it ends and returns its exit code.

    ``on_output(text, offset)`` gets each new piece of the log and the byte offset after it.
    """
    deadline = time.monotonic() + timeout
    failing_since: float | None = None
    while True:
        try:
            res = await asyncio.wait_for(
                executor.run(follow_script(run_id, offset, window, base), timeout=window + 60), window + 90
            )
            meta = _markers(res.stderr)
            if "OFF" not in meta and "GONE" not in meta:
                raise RuntimeError(f"exit code {res.rc}: {(res.stderr or res.stdout).strip()[-300:]}")
        except Exception as e:  # noqa: BLE001 - unreachable for a while is retried
            now = time.monotonic()
            failing_since = failing_since or now
            if now - failing_since > UNREACHABLE_FOR:
                raise RuntimeError(
                    f"lost contact with the machine ({e or type(e).__name__}); the upgrade may still be running there"
                ) from e
            log.info("reading job %s failed, retrying: %s", run_id, e)
            await asyncio.sleep(RETRY_EVERY)
            continue
        failing_since = None
        if "GONE" in meta:
            raise RuntimeError("the job's folder on the machine is gone, so how the upgrade ended is unknown")
        new = int(meta["OFF"])
        if new > offset:
            on_output(res.stdout, new)
            offset = new
        if "RC" in meta:
            return int(meta["RC"])
        if "LOST" in meta:
            raise RuntimeError(
                "the upgrade stopped before it finished (the machine or container may have restarted). "
                "Check the machine, then run Update again"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(f"still running after {timeout / 60:.0f} min; it keeps going on the machine")


async def cleanup(executor, run_id: str, base: str = JOBS_DIR) -> None:
    try:
        await executor.run(cleanup_script(run_id, base), timeout=60)
    except Exception as e:  # noqa: BLE001 - housekeeping; old folders are removed later anyway
        log.info("could not remove job folder %s: %s", run_id, e)

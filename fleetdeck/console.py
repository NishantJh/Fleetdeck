"""Run a command on a machine from the dashboard (see docs/CONSOLE.md).

This is a path from a browser to a root shell, so everything here is about guarding it: it is off unless the
server config switches it on, only answers allowed networks, wants the password again before it works, warns
on plain HTTP, asks for confirmation on destructive-looking commands, and writes down every use twice.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import secrets
import shlex
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import Request

from .config import AuthConfig, ConsoleConfig
from .models import Target
from .security import verify_password
from .store import Store

log = logging.getLogger(__name__)

MAX_COMMAND = 16 * 1024
MAX_OUTPUT = 1024 * 1024  # characters kept per run
AUDIT_OUTPUT = 4096
MAX_RUNS = 4
ABSOLUTE_UNLOCK = 3600.0
MAX_TIMEOUT = 3600
KEEP_RUNS = 50
# Control characters other than tab and newline: terminal escape sequences in output must not reach the page.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")

# A seatbelt, not a wall: it catches slips, not a determined user (eval, base64, a script file).
_SEP = r"[^;&|\n]*"
_START = r"(?:^|[;&|(`\n]|\$\()\s*(?:sudo\s+(?:-\S+\s+)*)?"
GUARDS: list[tuple[str, re.Pattern]] = [
    (label, re.compile(rx))
    for label, rx in [
        ("rm -rf on / or home", rf"\brm\s{_SEP}-[a-z-]*r{_SEP}\s(?:/|/\*|~|~/|~/\*|\$home|\$home/)(?:\s|$|;)"),
        ("rm --no-preserve-root", r"--no-preserve-root"),
        ("mkfs", r"\bmkfs(?:\.\w+)?\b"),
        ("wipefs", r"\bwipefs\b"),
        ("dd onto a device", rf"\bdd\b{_SEP}\bof=/dev/"),
        ("write onto a disk", r">\s*/dev/(?:sd|nvme|vd|hd|xvd|mmcblk)"),
        ("shred on a device", rf"\bshred\b{_SEP}/dev/"),
        ("zpool/zfs destroy", r"\b(?:zpool|zfs)\s+destroy\b"),
        ("lvremove/vgremove/pvremove", r"\b(?:lv|vg|pv)remove\b"),
        ("sgdisk -Z", rf"\bsgdisk\s{_SEP}(?:-z\b|--zap-all)"),
        ("power off or reboot", rf"{_START}(?:reboot|shutdown|poweroff|halt)\b"),
        ("init 0 or 6", rf"{_START}init\s+[06]\b"),
        ("systemctl reboot/poweroff/halt", r"\bsystemctl\s+(?:--\S+\s+)*(?:reboot|poweroff|halt|kexec)\b"),
        ("pct destroy", r"\bpct\s+destroy\b"),
        ("qm destroy", r"\bqm\s+destroy\b"),
        ("incus delete", r"\bincus\s+delete\b"),
        ("docker rm -f", rf"\bdocker\s+(?:container\s+)?rm\s{_SEP}-\w*f"),
        ("docker system prune", r"\bdocker\s+system\s+prune\b"),
        ("docker volume rm", r"\bdocker\s+volume\s+(?:rm|prune)\b"),
        ("iptables -F", r"\biptables\s+(?:-f\b|--flush)"),
        ("nft flush", r"\bnft\s+flush\b"),
        ("ufw enable", r"\bufw\s+enable\b"),
        ("stopping ssh", rf"\bsystemctl\s+(?:stop|disable|mask)\s{_SEP}\bssh(?:d)?\b"),
        ("passwd -l", r"\bpasswd\s+-l\b"),
        ("the dashboard's sudo rule", r"sudoers\.d/homelab-manager"),
        ("the dashboard's SSH key", rf"\bhomelab\b{_SEP}authorized_keys"),
        ("fork bomb", r":\(\)\s*\{"),
    ]
]


def danger(command: str) -> list[str]:
    """What in the command looks destructive (empty: nothing). Matches on the lower-cased command with the
    whitespace squeezed."""
    text = re.sub(r"[ \t]+", " ", command.lower())
    return [label for label, rx in GUARDS if rx.search(text)]


def clean(text: str) -> str:
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def network_problem(request: Request, allow_from: list[str]) -> str | None:
    """Why this request may not use the console (None: allowed).

    The address is the one uvicorn settled on: the real client when the request came through a proxy listed in
    ``trusted_proxies``, else the connection's own address. A request that carries X-Forwarded-For but whose address is
    not in it came through a proxy we don't trust: its address is the proxy's, which says nothing about the client."""
    raw = client_ip(request)
    ip = _ip(raw)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and raw not in {p.strip() for p in forwarded.split(",")}:
        return (
            "this request came through a reverse proxy that the dashboard doesn't trust, so the visitor's address "
            "is unknown. Add the proxy to trusted_proxies"
        )
    if ip is None or not any(ip in ipaddress.ip_network(n, strict=False) for n in allow_from):
        return f"{raw} is not in console.allow_from"
    return None


def is_secure(request: Request) -> bool:
    """HTTPS (served here, or told so by a trusted proxy: uvicorn applies X-Forwarded-Proto then) or loopback."""
    if request.url.scheme == "https":
        return True
    ip = _ip(client_ip(request))
    return bool(ip and ip.is_loopback)


@dataclass
class Session:
    ip: str
    user: str
    created: float
    last: float


@dataclass
class Run:
    id: str
    target_id: str
    target: str
    container: str | None
    command: str
    user: str
    started: float = field(default_factory=time.time)
    status: str = "running"  # running | done | failed | stopped | timeout
    rc: int | None = None
    error: str | None = None
    duration: float | None = None
    lines: list[str] = field(default_factory=list)
    dropped: int = 0  # lines pushed out of the 1 MB window
    size: int = 0
    task: asyncio.Task | None = None
    audit_id: int | None = None

    def add(self, line: str) -> None:
        line = clean(line)
        self.lines.append(line)
        self.size += len(line) + 1
        while self.size > MAX_OUTPUT and len(self.lines) > 1:
            self.size -= len(self.lines.pop(0)) + 1
            self.dropped += 1

    def since(self, offset: int) -> tuple[list[str], int]:
        """The lines from absolute position ``offset`` on, and the next offset."""
        return self.lines[max(offset - self.dropped, 0) :], self.dropped + len(self.lines)


Notify = Callable[[str, str], Awaitable[object]]


class Console:
    def __init__(self, cfg: ConsoleConfig, auth: AuthConfig | None, store: Store | None, notify: Notify | None):
        self.cfg = cfg
        self.auth = auth
        self.store = store
        self.notify = notify
        self.sessions: dict[str, Session] = {}
        self.runs: dict[str, Run] = {}
        self._memory: deque[tuple[int, str]] = deque(maxlen=1000)  # audit rows when there is no state file
        self._next_id = 1
        self._pruned = 0.0

    # --- unlock -------------------------------------------------------------------------------------------

    def password_ok(self, password: str) -> bool:
        if self.auth is None:  # demo mode without a login: nothing to check
            return True
        if self.auth.password_hash:
            return verify_password(password.encode(), self.auth.password_hash)
        return secrets.compare_digest(password.encode(), (self.auth.password or "").encode())

    def unlock(self, ip: str) -> str:
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(ip, self.auth.username if self.auth else "demo", now, now)
        return token

    def lock(self, token: str) -> None:
        self.sessions.pop(token, None)

    def session(self, token: str | None, ip: str) -> Session | None:
        """The live session for this token and address (and it counts as use), else None."""
        if not token:
            return None
        s = self.sessions.get(token)
        if s is None:
            return None
        now = time.monotonic()
        if s.ip != ip or now - s.last > self.cfg.unlock_minutes * 60 or now - s.created > ABSOLUTE_UNLOCK:
            del self.sessions[token]
            return None
        s.last = now
        return s

    # --- running ------------------------------------------------------------------------------------------

    def busy_reason(self, target_id: str) -> str | None:
        running = [r for r in self.runs.values() if r.status == "running"]
        if any(r.target_id == target_id for r in running):
            return "a command is already running on this machine"
        if len(running) >= MAX_RUNS:
            return f"{MAX_RUNS} commands are already running; wait for one to finish"
        return None

    def start(
        self,
        target: Target,
        run_script: Callable[[str, Callable[[str, str], None], float], Awaitable[int]],
        *,
        command: str,
        timeout: int,
        container: str | None,
        container_id: str | None,
        as_root: bool,
        who: Session,
        request: Request,
        guarded: list[str],
    ) -> Run:
        run = Run(uuid.uuid4().hex[:12], target.id, target.name, container, command, who.user)
        http = not is_secure(request)
        row = {
            "kind": "run",
            "user": who.user,
            "ip": who.ip,
            "user_agent": request.headers.get("user-agent", "")[:200],
            "target_id": target.id,
            "target": target.name,
            "container": container,
            "command": command,
            "guarded": guarded,
            "http": http,
            "rc": None,
            "duration": None,
            "output": "",
            "status": "running",
        }
        run.audit_id = self.audit(row)
        self.runs[run.id] = run
        while len(self.runs) > KEEP_RUNS:
            oldest = next((k for k, r in self.runs.items() if r.status != "running"), None)
            if oldest is None:
                break
            del self.runs[oldest]
        script = _wrap(command, timeout, who, container_id, as_root)
        run.task = asyncio.create_task(self._execute(run, run_script, script, timeout, row))
        if guarded and self.cfg.notify and self.notify:
            asyncio.create_task(self._notify(run, guarded, who))
        return run

    async def _notify(self, run: Run, guarded: list[str], who: Session) -> None:
        where = run.target + (f" / {run.container}" if run.container else "")
        try:
            what = f"{who.user} from {who.ip} ran a guarded command on {where} ({', '.join(guarded)})"
            await self.notify("Fleetdeck: guarded command", f"{what}: {run.command[:200]}")
        except Exception:  # noqa: BLE001 - a failed notification must not hide the command's own result
            log.warning("could not send the guarded command notification", exc_info=True)

    async def _execute(self, run: Run, run_script, script: str, timeout: int, row: dict) -> None:
        def on_line(_stream: str, line: str) -> None:
            run.add(line)

        try:
            run.rc = rc = await run_script(script, on_line, timeout + 30)
            run.status = "timeout" if rc in (124, 137) else ("done" if rc == 0 else "failed")
        except asyncio.CancelledError:
            run.status = "stopped"
        except Exception as e:  # noqa: BLE001 - shown to the user, who needs the reason
            run.status, run.error = "failed", str(e) or type(e).__name__
        run.duration = round(time.time() - run.started, 1)
        row.update(
            rc=run.rc,
            duration=run.duration,
            status=run.status,
            output=clean("\n".join(run.lines))[-AUDIT_OUTPUT:],
            error=run.error,
        )
        self.audit(row, run.audit_id)

    def stop(self, run: Run) -> None:
        if run.task and not run.task.done():
            run.task.cancel()

    # --- audit --------------------------------------------------------------------------------------------

    def audit(self, row: dict, row_id: int | None = None) -> int:
        """Writes (or, with ``row_id``, rewrites) an audit row and returns its id."""
        now = datetime.now(UTC)
        row = {**row, "at": row.get("at") or now.isoformat(timespec="seconds")}
        data = json.dumps(row)
        if self.store is not None:
            try:
                if row_id is None:
                    row_id = self.store.add_console_log(row["at"], data)
                else:
                    self.store.update_console_log(row_id, data)
                if time.monotonic() - self._pruned > 3600:
                    self._pruned = time.monotonic()
                    cutoff = (now - timedelta(days=self.cfg.keep_days)).isoformat(timespec="seconds")
                    self.store.prune_console_log(cutoff)
                return row_id
            except Exception:  # noqa: BLE001 - keep the in-memory copy rather than lose the record
                log.exception("could not write the console audit log")
        if row_id is None:
            row_id, self._next_id = self._next_id, self._next_id + 1
        self._memory = deque([(i, d) for i, d in self._memory if i != row_id], maxlen=1000)
        self._memory.append((row_id, data))
        return row_id

    def audit_rows(self, limit: int, kind: str | None = None) -> list[dict]:
        if self.store is not None:
            raw = self.store.load_console_log(max(limit * 5, 200) if kind else limit)
        else:
            raw = [d for _, d in reversed(self._memory)]
        rows = []
        for d in raw:
            try:
                rows.append(json.loads(d))
            except ValueError:
                continue
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return rows[:limit]


def _wrap(command: str, timeout: int, who: Session, container_id: str | None, as_root: bool) -> str:
    """The script the machine runs: a syslog line (a record the dashboard can't erase), then the command
    under ``timeout`` so Stop and timeouts end it even on transports where closing the connection doesn't."""
    note = f"{who.user}@{who.ip}: {' '.join(command.split())[:200]}"
    inner = f"sh -c {shlex.quote(command)}"
    if container_id:
        inner = f"docker exec {'-u 0 ' if as_root else ''}{container_id} {inner}"
    return f"logger -t fleetdeck-console {shlex.quote(note)} 2>/dev/null\nexec timeout -k 5 {int(timeout)} {inner}\n"


DEMO_OUTPUT = {
    "hostname": "{name}",
    "uptime": " 09:41:07 up 12 days,  3:22,  1 user,  load average: 0.08, 0.11, 0.09",
    "whoami": "root",
    "df -h": (
        "Filesystem      Size  Used Avail Use% Mounted on\n"
        "/dev/sda2        98G   31G   63G  34% /\n"
        "tmpfs           3.9G     0  3.9G   0% /dev/shm"
    ),
    "free -h": (
        "               total        used        free      shared  buff/cache   available\n"
        "Mem:           7.7Gi       1.9Gi       3.6Gi        42Mi       2.2Gi       5.5Gi"
    ),
}


def demo_output(command: str, name: str) -> list[str]:
    """Canned output for the demo, which has no real machines."""
    text = DEMO_OUTPUT.get(" ".join(command.split()))
    if text is None:
        return [f"(demo) {command.strip()[:200]}", "No machine was touched: this is demo data."]
    return text.format(name=name).splitlines()

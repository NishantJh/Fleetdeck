"""HTTP routes for the command console. Only added to the app when the console is switched on (docs/CONSOLE.md)."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .config import AppConfig
from .console import (
    CONTAINER_ID,
    CONTAINER_NAME,
    MAX_COMMAND,
    MAX_TERMINALS,
    MAX_TIMEOUT,
    TERMINAL_IDLE,
    TERMINAL_MAX,
    Console,
    DemoTerminal,
    Session,
    client_ip,
    danger,
    demo_output,
    is_secure,
    network_problem,
)
from .inventory import Inventory
from .security import LoginThrottle
from .stacks import _own_id

STATIC = Path(__file__).parent / "static"
WATCH_EVERY = 5.0  # seconds between idle checks of an open terminal

PLAIN_HTTP = (
    "This connection isn't encrypted. Your password and every command and its output can be read on the network. "
    "Put the dashboard behind HTTPS (see the docs, Caddy)."
)
LIST_CONTAINERS = "docker ps -a --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.State}}' 2>&1"
DEMO_CONTAINERS = [
    {"id": "3f2a9c1d7b40", "name": "nginx", "image": "nginx:1.27", "state": "running"},
    {"id": "9b1e55a0c2de", "name": "postgres", "image": "postgres:16", "state": "running"},
]


class UnlockRequest(BaseModel):
    password: str = Field(max_length=1024)


class RunRequest(BaseModel):
    target: str
    container: str | None = Field(None, max_length=128)  # a container ID from /containers
    command: str = Field(min_length=1, max_length=MAX_COMMAND)
    timeout: int = Field(60, ge=1, le=MAX_TIMEOUT)
    as_root: bool = False  # in a container: docker exec -u 0
    confirm: str | None = Field(None, max_length=256)  # the machine's name, for a guarded command


def add_console_routes(
    app: FastAPI, cfg: AppConfig, console: Console, inventory: Inventory, throttle: LoginThrottle
) -> None:
    ccfg = cfg.console

    def _can_terminal(target, executor) -> bool:
        if target.id in ccfg.exclude or target.name in ccfg.exclude or executor is None:
            return False
        return target.source == "demo" or hasattr(executor, "open_terminal")

    def gate(request: Request, *, token: bool = True) -> Session | None:
        """Raises unless this request may use the console; returns its unlock session (when ``token``)."""
        if (why := network_problem(request, ccfg.allow_from)) is not None:
            raise HTTPException(403, f"the console isn't available from here: {why}")
        if not ccfg.allow_plain_http and not is_secure(request):
            raise HTTPException(403, "the console is blocked over plain HTTP (console.allow_plain_http is false)")
        if not token:
            return None
        session = console.session(request.headers.get("x-console-token"), client_ip(request))
        if session is None:
            raise HTTPException(401, "locked: enter the password to unlock the console")
        return session

    async def target_of(target_id: str):
        await inventory.refresh()
        if target_id not in inventory.targets:
            raise HTTPException(404, "unknown machine")
        target, executor = inventory.get(target_id)
        if target_id in ccfg.exclude or target.name in ccfg.exclude:
            raise HTTPException(403, "this machine is excluded from the console (console.exclude)")
        if executor is None or target.status not in ("running", "online"):
            raise HTTPException(400, f"can't reach {target.name} to run a command ({target.status})")
        return target, executor

    @app.get("/api/console")
    async def state(request: Request):
        problem = network_problem(request, ccfg.allow_from)
        if problem is None and not ccfg.allow_plain_http and not is_secure(request):
            problem = "blocked over plain HTTP"
        return {
            "enabled": True,
            "terminal": ccfg.terminal,
            "terminals": [i for i, (t, e) in inventory.targets.items() if _can_terminal(t, e)] if ccfg.terminal else [],
            "allowed": problem is None,
            "reason": problem,
            "secure": is_secure(request),
            "warning": None if is_secure(request) else PLAIN_HTTP,
            "unlocked": console.session(request.headers.get("x-console-token"), client_ip(request)) is not None,
            "unlock_minutes": ccfg.unlock_minutes,
            "exclude": ccfg.exclude,
        }

    @app.post("/api/console/unlock")
    async def unlock(req: UnlockRequest, request: Request):
        gate(request, token=False)
        ip = client_ip(request)
        if (left := throttle.blocked_for(ip)) > 0:
            raise HTTPException(429, f"too many failed attempts, try again in {int(left) + 1} s")
        ok = await asyncio.to_thread(console.password_ok, req.password)
        row = {"kind": "unlock" if ok else "unlock-failed", "user": console.auth.username if console.auth else "demo"}
        console.audit({**row, "ip": ip, "user_agent": request.headers.get("user-agent", "")[:200]})
        if not ok:
            throttle.failed(ip, req.password)
            raise HTTPException(403, "wrong password")
        throttle.succeeded(ip)
        return {"token": console.unlock(ip), "unlock_minutes": ccfg.unlock_minutes}

    @app.post("/api/console/lock")
    async def lock(request: Request):
        console.lock(request.headers.get("x-console-token", ""))
        return {"unlocked": False}

    @app.get("/api/targets/{target_id}/containers")
    async def containers(target_id: str, request: Request):
        gate(request)
        target, executor = await target_of(target_id)
        if target.source == "demo":
            return {"containers": DEMO_CONTAINERS if target.kind == "host" else [], "error": None}
        return await _containers(executor)

    async def _containers(executor) -> dict:
        res = await executor.run(LIST_CONTAINERS, None, 60)
        own = _own_id()
        found = []
        for line in res.stdout.splitlines():
            parts = line.split("|")
            if len(parts) != 4 or not CONTAINER_ID.match(parts[0]) or not CONTAINER_NAME.match(parts[1]):
                continue
            if own and own.startswith(parts[0]):
                continue  # the dashboard's own container is never offered
            found.append({"id": parts[0], "name": parts[1], "image": parts[2][:200], "state": parts[3]})
        error = None
        if not found and (res.rc != 0 or "not found" in res.stdout):
            error = "Docker isn't available on this machine"
        return {"containers": found, "error": error}

    @app.post("/api/console/run")
    async def run(req: RunRequest, request: Request):
        session = gate(request)
        target, executor = await target_of(req.target)
        if reason := console.busy_reason(target.id):
            raise HTTPException(429, reason)
        name = None
        if req.container:
            if not CONTAINER_ID.match(req.container):
                raise HTTPException(400, "bad container id")
            if target.source == "demo":
                found = DEMO_CONTAINERS
            else:
                found = (await _containers(executor))["containers"]
            match = next((c for c in found if c["id"] == req.container), None)
            if match is None:
                raise HTTPException(404, "that container isn't on this machine (or is the dashboard's own)")
            name = match["name"]
        matched = danger(req.command)
        where = name or target.name
        if matched and (req.confirm or "").strip() != where:
            raise HTTPException(
                409,
                f"guarded: this looks destructive ({', '.join(matched)}). Type the name of the machine, "
                f"{where!r}, to run it anyway.",
            )

        if target.source == "demo":

            async def runner(_script, on_line, _timeout) -> int:
                for line in demo_output(req.command, target.name):
                    on_line("stdout", line)
                    await asyncio.sleep(0.2)
                return 0

        else:

            async def runner(script, on_line, timeout) -> int:
                return (await executor.run(script, on_line, timeout)).rc

        r = console.start(
            target,
            runner,
            command=req.command,
            timeout=req.timeout,
            container=name,
            container_id=req.container,
            as_root=req.as_root,
            who=session,
            request=request,
            guarded=matched,
        )
        return {"id": r.id}

    def run_of(run_id: str):
        r = console.runs.get(run_id)
        if r is None:
            raise HTTPException(404, "unknown run")
        return r

    @app.get("/api/console/runs/{run_id}")
    async def get_run(run_id: str, request: Request, since: int = Query(0, ge=0)):
        gate(request)
        r = run_of(run_id)
        lines, nxt = r.since(since)
        return {
            "id": r.id,
            "status": r.status,
            "rc": r.rc,
            "error": r.error,
            "duration": r.duration,
            "lines": lines,
            "next": nxt,
        }

    @app.post("/api/console/runs/{run_id}/stop")
    async def stop_run(run_id: str, request: Request):
        gate(request)
        console.stop(run_of(run_id))
        return {"ok": True}

    @app.get("/api/console/audit")
    async def audit(request: Request, limit: int = Query(50, ge=1, le=500), kind: str | None = None):
        gate(request)
        if kind is not None and not re.fullmatch(r"[a-z-]{1,20}", kind):
            raise HTTPException(400, "bad kind")
        return {"rows": console.audit_rows(limit, kind)}

    def ws_origin_ok(ws: WebSocket) -> bool:
        """A WebSocket upgrade is a GET, so the CSRF check does not see it: the page that opened it must be ours."""
        origin = ws.headers.get("origin")
        return bool(origin) and urlsplit(origin).netloc.lower() == ws.headers.get("host", "").lower()

    @app.get("/terminal")
    async def terminal_page():
        """The terminal window. xterm.js sets style elements, which the dashboard's strict policy refuses, so it lives
        on its own page (opened as a separate window) that allows inline styles and nothing else extra."""
        csp = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        return FileResponse(STATIC / "terminal.html", headers={"Content-Security-Policy": csp})

    @app.websocket("/api/console/terminal")
    async def terminal(ws: WebSocket):
        if not ws_origin_ok(ws):
            await ws.close(code=1008)
            return
        await ws.accept()

        async def refuse(reason: str) -> None:
            await ws.close(code=4403, reason=reason[:120])

        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 15))
            cols, rows = max(10, min(int(hello["cols"]), 500)), max(2, min(int(hello["rows"]), 200))
            token, target_id, container = hello.get("token"), str(hello["target"]), hello.get("container")
            as_root = bool(hello.get("as_root"))
        except Exception:  # noqa: BLE001 - anything but a well-formed first message
            await refuse("bad first message")
            return
        if not ccfg.terminal:
            await refuse("the terminal is switched off (console.terminal)")
            return
        if (why := network_problem(ws, ccfg.allow_from)) is not None:
            await refuse(f"not available from here: {why}")
            return
        if not ccfg.allow_plain_http and not is_secure(ws):
            await refuse("blocked over plain HTTP (console.allow_plain_http is false)")
            return
        session = console.session(token, client_ip(ws))
        if session is None:
            await refuse("locked: unlock the console again")
            return
        try:
            target, executor = await target_of(target_id)
        except HTTPException as e:
            await refuse(str(e.detail))
            return
        if not _can_terminal(target, executor):
            await refuse(f"{target.name} cannot give a terminal (Run command works)")
            return
        if len(console.terminals) >= MAX_TERMINALS:
            await refuse(f"{MAX_TERMINALS} terminals are already open")
            return
        demo = target.source == "demo"
        name, command = target.name, None
        if container:
            if not CONTAINER_ID.match(str(container)):
                await refuse("bad container id")
                return
            found = DEMO_CONTAINERS if demo else (await _containers(executor))["containers"]
            match = next((c for c in found if c["id"] == container), None)
            if match is None:
                await refuse("that container is not on this machine (or is the dashboard's own)")
                return
            name = match["name"]
            shell = shlex.quote("command -v bash >/dev/null 2>&1 && exec bash || exec sh")
            command = f"docker exec -it {'-u 0 ' if as_root else ''}{container} sh -c {shell}"

        tid = uuid.uuid4().hex[:12]
        console.terminals.add(tid)
        started = time.monotonic()
        row = {
            "kind": "terminal",
            "user": session.user,
            "ip": session.ip,
            "user_agent": ws.headers.get("user-agent", "")[:200],
            "target_id": target.id,
            "target": target.name,
            "container": name if container else None,
            "http": not is_secure(ws),
            "status": "open",
            "duration": None,
        }
        audit_id = console.audit(row)
        pty = None
        reason = "closed"
        try:
            if not demo:
                # A line in the machine's own journal that the dashboard cannot erase.
                where = f"{session.user}@{session.ip}: terminal" + (f" in container {name}" if container else "")
                await executor.run(f"logger -t fleetdeck-console {shlex.quote(where)} 2>/dev/null; true", None, 30)
            if console.notify and ccfg.notify:
                asyncio.create_task(_notify_terminal(console, session, target.name, name if container else None))
            pty = DemoTerminal(name, cols, rows) if demo else await executor.open_terminal(cols, rows, command)
            last = time.monotonic()

            async def output() -> None:
                nonlocal last
                while data := await pty.read():
                    last = time.monotonic()
                    await ws.send_bytes(data)

            async def keys() -> None:
                nonlocal last
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    last = time.monotonic()
                    if msg.get("bytes") is not None:
                        pty.write(msg["bytes"])
                    elif msg.get("text"):
                        ctl = json.loads(msg["text"])
                        if "resize" in ctl:
                            c, r = ctl["resize"]
                            pty.resize(max(10, min(int(c), 500)), max(2, min(int(r), 200)))

            async def watchdog() -> str:
                while True:
                    await asyncio.sleep(WATCH_EVERY)
                    now = time.monotonic()
                    if now - last > TERMINAL_IDLE:
                        return "closed after 15 minutes without activity"
                    if now - started > TERMINAL_MAX:
                        return "closed after 4 hours"

            tasks = {asyncio.create_task(output()), asyncio.create_task(keys()), asyncio.create_task(watchdog())}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                if not t.cancelled() and t.exception() is None and isinstance(t.result(), str):
                    reason = t.result()
        except Exception as e:  # noqa: BLE001 - shown in the terminal window
            reason = f"could not open the terminal: {e}"
        finally:
            if pty is not None:
                try:
                    await pty.close()
                except Exception:  # noqa: BLE001
                    pass
            console.terminals.discard(tid)
            row.update(status="closed", duration=round(time.monotonic() - started, 1))
            console.audit(row, audit_id)
        try:
            await ws.close(code=4000, reason=reason[:120])
        except Exception:  # noqa: BLE001 - already gone
            pass


async def _notify_terminal(console: Console, session, machine: str, container: str | None) -> None:
    where = machine + (f" / {container}" if container else "")
    try:
        await console.notify(
            "Fleetdeck: terminal opened", f"{session.user} from {session.ip} opened a terminal on {where}"
        )
    except Exception:  # noqa: BLE001 - a failed notification must not stop the terminal
        pass

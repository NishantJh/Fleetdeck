"""HTTP routes for the command console. Only added to the app when the console is switched on (docs/CONSOLE.md)."""

from __future__ import annotations

import asyncio
import re

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .config import AppConfig
from .console import (
    CONTAINER_ID,
    CONTAINER_NAME,
    MAX_COMMAND,
    MAX_TIMEOUT,
    Console,
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
            "terminal": False,
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

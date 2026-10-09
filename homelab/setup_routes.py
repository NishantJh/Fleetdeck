"""HTTP API of the setup page (/setup). See :mod:`homelab.onboarding` for what it does."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import onboarding as ob
from .config import AppConfig, check_username
from .security import hash_password

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class AdminRequest(BaseModel):
    username: str = Field("admin", min_length=1, max_length=64)
    password: str = Field(min_length=10, max_length=1024)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        return check_username(v)


class ScriptRequest(ob.Machine):
    allow_from: str = ""


class ConnectRequest(ob.Machine):
    # The host key fingerprint the user confirmed (first contact, or a changed key).
    trust_fingerprint: str | None = None


class ConfigText(BaseModel):
    yaml: str = Field(max_length=1_000_000)


def _error(e: Exception) -> HTTPException:
    if isinstance(e, ValidationError):
        msg = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'config'}: {err['msg']}" for err in e.errors())
        return HTTPException(400, msg)
    return HTTPException(400, str(e))


def add_setup_routes(
    app: FastAPI, cfg: AppConfig, paths: ob.Paths, restart: Callable[[], None] | None, first_run: bool
) -> None:
    lock = asyncio.Lock()  # one config write at a time
    state = {"changed": False, "admin_created": False}

    def editable() -> None:
        if not paths.writable():
            raise HTTPException(
                409, f"{paths.config} is read-only here (an older install's config folder); edit it by hand"
            )

    def changed() -> None:
        state["changed"] = True

    @app.get("/setup")
    async def setup_page():
        return FileResponse(STATIC / "setup.html")

    @app.get("/api/setup")
    async def setup_state():
        writable = paths.writable()
        public_key = None
        if writable:
            try:
                public_key = await asyncio.to_thread(ob.ensure_identity, paths)
            except OSError as e:
                log.error("Can't create the dashboard's keys in %s: %s", paths.keys, e)
                writable = False
        on_disk = cfg
        if writable and paths.config.exists():
            try:
                on_disk = ob.validate_raw(ob.read_raw(paths))
            except (ValueError, ValidationError):
                pass
        return {
            "first_run": first_run,
            "writable": writable,
            "config_path": str(paths.config),
            "public_key": public_key,
            "machines": ob.machines(on_disk),
            "running": [m["name"] for m in ob.machines(cfg)],
            "restart_pending": state["changed"],
            "can_restart": restart is not None,
        }

    @app.post("/api/setup/admin")
    async def create_admin(req: AdminRequest):
        if not first_run or state["admin_created"]:
            raise HTTPException(409, "an admin login already exists")
        editable()
        async with lock:
            if state["admin_created"]:
                raise HTTPException(409, "an admin login already exists")
            data = ob.read_raw(paths)
            password_hash = await asyncio.to_thread(hash_password, req.password)
            if data:  # a config without auth: keep it, add the login
                data["auth"] = {"username": req.username, "password_hash": password_hash}
                data.pop("allow_no_auth", None)
            else:
                data = ob.new_config(paths, req.username, password_hash)
            try:
                ob.write_raw(paths, data)
            except (ValueError, ValidationError) as e:
                raise _error(e) from None
            state["admin_created"] = True
        log.info("Setup: created the admin login %r; restarting with it.", req.username)
        _restart_soon(restart)
        return {"restarting": restart is not None}

    @app.post("/api/setup/script")
    async def script(req: ScriptRequest):
        editable()
        try:
            allow = ob.parse_allow_from(req.allow_from)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        public_key = await asyncio.to_thread(ob.ensure_identity, paths)
        return {"script": ob.bootstrap_script(req, public_key, allow)}

    @app.post("/api/setup/connect")
    async def connect(req: ConnectRequest):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        await asyncio.to_thread(ob.ensure_identity, paths)
        machine = ob.Machine(kind=req.kind, name=req.name, address=req.address, port=req.port)
        result, entry = await ob.connect(machine, paths, req.trust_fingerprint)
        if entry is not None:
            async with lock:
                try:
                    data = ob.upsert(ob.read_raw(paths), machine.kind, entry)
                    data.setdefault("known_hosts", str(paths.known_hosts))
                    ob.write_raw(paths, data)
                except (ValueError, ValidationError) as e:
                    raise _error(e) from None
            changed()
        return {
            "ok": result.ok,
            "log": result.log,
            "fingerprint": result.fingerprint,
            "host_key_changed": result.host_key_changed,
            "needs_confirm": result.needs_confirm,
        }

    @app.delete("/api/setup/machines/{name}")
    async def remove_machine(name: str):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        async with lock:
            try:
                data = ob.read_raw(paths)
                entry = ob.remove(data, name)
                if entry is None:
                    raise HTTPException(404, "no such machine")
                ob.write_raw(paths, data)
            except (ValueError, ValidationError) as e:
                raise _error(e) from None
            # Forget its host key too (unless another entry uses the same address), so re-adding a
            # reinstalled machine starts from a fresh confirmation.
            ssh = entry if "host" in entry else entry.get("ssh") or {}
            if ssh.get("host"):
                endpoint = (str(ssh["host"]), int(ssh.get("port") or 22))
                if endpoint not in ob.ssh_endpoints(data):
                    ob.forget_host_key(paths, *endpoint)
        changed()
        log.info(
            "Removed %s. Its homelab user%s stay on the machine until removed there.",
            name,
            ", API token" if "token_id" in entry else ", certificate trust" if "client_cert" in entry else "",
        )
        return {"removed": name}

    @app.get("/api/setup/config")
    async def get_config():
        if first_run:
            raise HTTPException(409, "create the admin login first")
        text = paths.config.read_text() if paths.config.exists() else ""
        return {"yaml": text, "writable": paths.writable()}

    @app.put("/api/setup/config")
    async def put_config(req: ConfigText):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        async with lock:
            try:
                await asyncio.to_thread(ob.write_text, paths, req.yaml)
            except (ValueError, ValidationError) as e:
                raise _error(e) from None
        changed()
        return {"saved": True}

    @app.post("/api/setup/apply")
    async def apply():
        if first_run:
            raise HTTPException(409, "create the admin login first")
        if restart is None:
            raise HTTPException(409, "restart the container to apply the changes")
        _restart_soon(restart)
        return {"restarting": True}


def _restart_soon(restart: Callable[[], None] | None) -> None:
    # After the response has gone out.
    if restart is not None:
        asyncio.get_running_loop().call_later(0.3, restart)

"""HTTP API of the setup page (/setup). See :mod:`fleetdeck.onboarding` for what it does."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import onboarding as ob
from . import tunnels
from .config import AppConfig, check_username
from .docker_management import DockerManagementRequest, update_dockhand_mapping
from .models import DockerSettings
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


# Scripts a machine can fetch once it's told the link: id -> (script, expiry). Kept per process, so
# links outlive the in-process restarts of Apply.
DOWNLOAD_TTL = 3600
_downloads: dict[str, tuple[str, float]] = {}


def _offer_download(source: str) -> dict:
    """A link the machine can fetch the script from, with the script's sha256, which the command
    checks before running it (the link is plain HTTP on most homelabs)."""
    now = time.monotonic()
    for k in [k for k, (_, exp) in _downloads.items() if exp < now]:
        del _downloads[k]
    while len(_downloads) >= 200:
        del _downloads[next(iter(_downloads))]
    key = secrets.token_urlsafe(18)
    text = ob.download(source)
    _downloads[key] = (text, now + DOWNLOAD_TTL)
    return {"path": f"/install/{key}", "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _error(e: Exception) -> HTTPException:
    if isinstance(e, ValidationError):
        msg = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'config'}: {err['msg']}" for err in e.errors())
        return HTTPException(400, msg)
    return HTTPException(400, str(e))


def refuse_while_busy(busy: Callable[[], list[str]]) -> None:
    """A restart cancels running jobs, and an upgrade cut off halfway can leave apt broken."""
    if names := busy():
        raise HTTPException(409, f"wait for the running jobs to finish first ({', '.join(names)})")


def add_setup_routes(
    app: FastAPI,
    cfg: AppConfig,
    paths: ob.Paths,
    restart: Callable[[], None] | None,
    first_run: bool,
    busy: Callable[[], list[str]] = list,
) -> None:
    @app.exception_handler(RequestValidationError)
    async def safe_provider_validation(request: Request, exc: RequestValidationError):
        if request.url.path == "/api/setup/docker-management":
            # FastAPI normally includes rejected input in 422 responses; never echo a submitted token.
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]},
            )
        return await request_validation_exception_handler(request, exc)

    lock = asyncio.Lock()  # one config write at a time
    state = {"changed": False, "admin_created": False}
    tunnel_manager = tunnels.manager(paths.keys / "tunnels")

    def _pending_tunnel(req: ob.Machine) -> dict:
        try:
            data = ob.read_raw(paths)
        except ValueError:
            data = {}
        connected = frozenset(h.get("name", "") for h in data.get("hosts") or [] if h.get("tunnel"))
        return tunnels.pending(
            paths.keys, req.name, req.address, req.udp_port, req.port, ob.used_tunnels(data), connected
        )

    def _tokens_in_use(name: str) -> frozenset[str]:
        """Proxmox token names of this machine that the running dashboard or the saved config use."""
        ids = {p.token_id for p in cfg.proxmox if p.name == name}
        try:
            found = ob.find(ob.read_raw(paths), name)
        except ValueError:
            found = None
        if found and found[0] == "proxmox":
            ids.add(str(found[1].get("token_id", "")))
        return frozenset(i.partition("!")[2] for i in ids if "!" in i)

    def editable() -> None:
        if not paths.writable():
            raise HTTPException(
                409, f"{paths.config} is read-only here (an older install's config folder); edit it by hand"
            )

    def changed() -> None:
        state["changed"] = True

    @app.get("/install/{key}")
    async def install_script(key: str):
        # No login: the machine fetching it has none. The unguessable, expiring link is the access.
        found = _downloads.get(key)
        if found is None or found[1] < time.monotonic():
            raise HTTPException(404, "this setup link has expired; show the script on the setup page again")
        return PlainTextResponse(found[0])

    @app.get("/setup")
    @app.get("/machines")
    @app.get("/settings")
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
        tunnel = None
        if req.tunnel:
            async with lock:
                tunnel = await asyncio.to_thread(_pending_tunnel, req)
        source = ob.bootstrap_source(req, public_key, allow, tunnel)
        # A short download link, except for a tunnel: its script holds keys, and the machine usually
        # can't reach the dashboard anyway (that's what the tunnel is for).
        download = None if tunnel else _offer_download(source)
        return {"script": ob.wrap(source), "source": source, "download": download}

    @app.post("/api/setup/connect")
    async def connect(req: ConnectRequest):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        await asyncio.to_thread(ob.ensure_identity, paths)
        machine = ob.Machine(kind=req.kind, name=req.name, address=req.address, port=req.port)
        tunnel = None
        if req.tunnel:
            # SSH goes to the tunnel's local end; everything else (pinning, checks) is the same.
            async with lock:
                tunnel = tunnels.to_config(await asyncio.to_thread(_pending_tunnel, req))
            try:
                await asyncio.to_thread(tunnel_manager.ensure, req.name, tunnel)
            except RuntimeError:
                log.exception("Tunnel setup failed for machine '%s'", req.name)
                return {
                    "ok": False,
                    "log": ["Could not establish tunnel connection. Check tunnel configuration and try again."],
                    "fingerprint": None,
                    "host_key_changed": False,
                    "needs_confirm": False,
                }
            machine = machine.model_copy(update={"address": "127.0.0.1", "port": tunnel.local_port})
        result, entry = await ob.connect(machine, paths, req.trust_fingerprint, _tokens_in_use(req.name))
        if tunnel is not None:
            result.log = [
                (
                    f"No SSH answer through the tunnel to {req.address}. Run the script shown now there again (a "
                    f"tunnel not connected within a day gets new keys), and check that UDP {req.udp_port} is open "
                    "in its firewall and its provider's firewall."
                )
                if line.startswith("can't reach SSH")
                else line.replace(f"127.0.0.1:{tunnel.local_port}", req.address).replace("127.0.0.1", req.address)
                for line in result.log
            ]
            if entry is not None:
                entry["tunnel"] = tunnel.model_dump()
                result.log.append(f"Connected through the WireGuard tunnel to {req.address} (UDP {req.udp_port})")
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
        tunnels.forget(paths.keys, name)
        changed()
        log.info(
            "Removed %s. Its homelab user%s stay on the machine until removed there.",
            name,
            ", API token" if "token_id" in entry else ", certificate trust" if "client_cert" in entry else "",
        )
        return {"removed": name}

    @app.get("/api/setup/machines/{name}/uninstall")
    async def uninstall_script(name: str):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        try:
            found = ob.find(ob.read_raw(paths), name)
        except ValueError as e:
            raise _error(e) from None
        if found is None:
            raise HTTPException(404, "no such machine")
        fp = await asyncio.to_thread(ob.incus_cert_fingerprint, paths) if found[0] == "incus" else ""
        source = ob.uninstall_source(*found, fp)
        return {"script": ob.wrap(source), "source": source, "download": _offer_download(source)}

    @app.get("/api/setup/docker-management")
    async def get_docker_management(target_id: str):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        if target_id not in app.state.inventory.targets:
            raise HTTPException(404, "unknown target")
        async with lock:
            try:
                raw = ob.read_raw(paths)
                saved = ob.validate_raw(raw, str(paths.config))
            except (ValueError, ValidationError):
                raise HTTPException(400, "The saved configuration is invalid; review it in Settings") from None
            digest = hashlib.sha256(paths.config.read_text().encode() if paths.config.exists() else b"").hexdigest()
            dh = saved.dockhand
            mapping = next((m for m in dh.environments if m.target_id == target_id), None) if dh else None
            return {
                "revision": digest,
                "writable": paths.writable(),
                "mode": "dockhand" if mapping else app.state.jobs.docker_settings.get(target_id, DockerSettings()).mode,
                "url": dh.url if dh else "",
                "token_configured": bool(dh and dh.token),
                "poll_seconds": dh.poll_seconds if dh else 300,
                "allow_checks": bool(dh and dh.allow_checks),
                "allow_updates": bool(mapping and mapping.allow_updates),
                "environment_id": mapping.environment_id if mapping else None,
                "shared_hosts": len(dh.environments) if dh else 0,
                "can_restart": restart is not None,
                "restart_pending": state["changed"],
            }

    @app.put("/api/setup/docker-management")
    async def put_docker_management(req: DockerManagementRequest):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        refuse_while_busy(busy)
        if req.target_id not in app.state.inventory.targets:
            raise HTTPException(404, "unknown target")
        async with lock:
            old_text = paths.config.read_text() if paths.config.exists() else ""
            digest = hashlib.sha256(old_text.encode()).hexdigest()
            if req.revision != digest:
                raise HTTPException(409, "Configuration changed; reopen Docker management before saving")
            try:
                raw = ob.read_raw(paths)
                saved = ob.validate_raw(raw, str(paths.config))
                data = update_dockhand_mapping(raw, saved, req)
                validated = ob.validate_raw(data, str(paths.config))
                if validated.auth is None and not validated.allow_no_auth:
                    raise ValueError("Keep the auth section in Settings before saving provider changes")
            except (ValueError, ValidationError) as e:
                raise _error(e) from None
            try:
                mode = "external" if req.mode == "dockhand" else req.mode
                policy = DockerSettings(
                    mode=mode,
                    manager_name="Dockhand" if req.mode == "dockhand" else req.manager_name,
                    manager_url=validated.dockhand.url if req.mode == "dockhand" else req.manager_url,
                )
            except ValidationError as e:
                raise _error(e) from None
            try:
                await asyncio.to_thread(ob.write_raw, paths, data)
                refuse_while_busy(busy)
                app.state.jobs.stage_docker_settings(req.target_id, policy)
            except Exception:
                # Preserve the previous file if either the policy or the config could not be saved.
                await asyncio.to_thread(ob._write_private, paths.config, old_text)
                raise
        changed()
        return {"saved": True, "restart_pending": True, "can_restart": restart is not None}

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
        refuse_while_busy(busy)
        _restart_soon(restart)
        return {"restarting": True}


def _restart_soon(restart: Callable[[], None] | None) -> None:
    # After the response has gone out.
    if restart is not None:
        asyncio.get_running_loop().call_later(0.3, restart)

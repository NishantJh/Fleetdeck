"""HTTP API of the Settings page: structured edits of config.yaml, one section at a time.

config.yaml stays the single source of truth. Each save reads the file, changes only the keys of that section,
validates the whole config and writes it back (same writer as the setup page). ``${VAR}`` references and keys the
form does not know about are kept. Secrets are never sent to the browser: the form gets "configured" flags, and a
blank secret field keeps the saved value.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from copy import deepcopy
from typing import Literal

import asyncssh
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import onboarding as ob
from .config import AppConfig, NotifyConfig, ScheduleConfig, ServiceSource
from .notify import Notifier
from .secrets_migrate import find_plaintext, migrate
from .secrets_store import SecretError, SecretStore, secret_name
from .service_sources import scan_source

log = logging.getLogger(__name__)
TEST_TIMEOUT = 45
SECRET_PROBLEM = (
    "The secret could not be stored encrypted. Check keys/secrets.key (or FLEETDECK_SECRET_KEY) and the state database."
)


class FormError(ValueError):
    """A mistake in the form; ``text`` is written by us and safe to show."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


class SourceIn(BaseModel):
    """One services-only source as the form sends it. Blank secrets keep the saved ones."""

    name: str = Field(min_length=1, max_length=80)
    previous_name: str | None = Field(None, max_length=80)  # set when renaming, to find the saved secrets
    type: Literal["ssh", "docker-api"] = "ssh"
    address: str = Field("", max_length=253)
    ssh_host: str = Field("", max_length=253)
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_user: str = Field("root", min_length=1, max_length=64)
    sudo: bool = False
    password: str | None = Field(None, max_length=1024, repr=False)
    url: str = Field("", max_length=2048)
    token: str | None = Field(None, max_length=4096, repr=False)
    verify_ssl: bool = True
    portainer_endpoint: int | None = Field(None, ge=1)


class ServicesIn(BaseModel):
    revision: str
    enabled: bool = True
    show_stopped: bool = False
    addresses: dict[str, str] = Field(default_factory=dict, max_length=100)
    sources: list[SourceIn] = Field(default_factory=list, max_length=20)


class SourceTest(BaseModel):
    source: SourceIn
    trust_fingerprint: str | None = None


class ScheduleIn(BaseModel):
    revision: str
    check_every: str = Field("", max_length=16)  # 30m, 6h, 1d; blank = only when you click Check
    check_docker: bool = True
    auto_security_updates: bool = False


class SecurityIn(BaseModel):
    revision: str
    scan_images: bool = True
    min_severity: Literal["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    ignore_unfixed: bool = True


class UpdatesIn(BaseModel):
    revision: str
    max_parallel: int = Field(4, ge=1, le=64)
    snapshot_before_upgrade: bool = True
    snapshot_keep: int = Field(3, ge=1, le=100)
    community_scripts_update: bool = True


class TargetIn(BaseModel):
    """One notification target as the form sends it. Blank secrets keep the saved ones."""

    type: Literal["ntfy", "gotify", "discord", "telegram", "webhook"]
    name: str = Field("", max_length=80)
    previous_index: int | None = Field(None, ge=0)  # position in the saved list, to find the saved secrets
    url: str | None = Field(None, max_length=2048, repr=False)  # a secret for discord and webhook
    token: str | None = Field(None, max_length=4096, repr=False)
    chat_id: str = Field("", max_length=64)


class NotifyIn(BaseModel):
    revision: str
    targets: list[TargetIn] = Field(default_factory=list, max_length=20)


# Types whose url is itself the credential.
URL_SECRET = {"discord", "webhook"}


def duration_text(seconds: int | None) -> str:
    """Seconds back to the shortest form the form accepts (21600 -> 6h)."""
    if not seconds:
        return ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def apply_schedule(raw: dict, req: ScheduleIn) -> dict:
    data = deepcopy(raw)
    sch = data.get("schedule") or {}
    every = req.check_every.strip()
    if every:
        new = ScheduleConfig(check_every=every).check_every
        old = ScheduleConfig.model_validate({"check_every": sch.get("check_every")}).check_every
        if new != old:  # an unchanged value keeps the way it was written
            sch["check_every"] = every
    else:
        sch.pop("check_every", None)
    sch["check_docker"] = req.check_docker
    sch["auto_security_updates"] = req.auto_security_updates
    data["schedule"] = sch
    return data


def apply_security(raw: dict, req: SecurityIn) -> dict:
    data = deepcopy(raw)
    sec = data.get("security") or {}
    sec.update(scan_images=req.scan_images, min_severity=req.min_severity, ignore_unfixed=req.ignore_unfixed)
    data["security"] = sec
    return data


def apply_updates(raw: dict, req: UpdatesIn) -> dict:
    data = deepcopy(raw)
    data.update(
        max_parallel=req.max_parallel,
        snapshot_before_upgrade=req.snapshot_before_upgrade,
        snapshot_keep=req.snapshot_keep,
        community_scripts_update=req.community_scripts_update,
    )
    return data


def target_view(t: NotifyConfig) -> dict:
    """What the form may see: secrets only as flags (a Discord or webhook address is the secret)."""
    return {
        "type": t.type,
        "name": t.name or "",
        "url": "" if t.type in URL_SECRET else (t.url or ""),
        "url_set": bool(t.url),
        "token_set": bool(t.token),
        "chat_id": t.chat_id or "",
    }


def target_entry(
    req: TargetIn, old: dict | None, index: int, put: Callable[[str, str], str], used: set[str] | None = None
) -> dict:
    """The config entry for a target: the saved one with the form's fields applied. Secrets saved for another
    address or type are dropped, never sent to a new destination."""
    old = deepcopy(old) if old and old.get("type") == req.type else {}
    e = old
    e["type"] = req.type
    name = req.name.strip()
    _put(e, "name", name)
    label = name or str(index)
    if used is not None:
        if label.lower() in used:
            raise FormError("Two notification targets have the same name")
        used.add(label.lower())
    if req.type == "telegram":
        e.pop("url", None)
        e["chat_id"] = req.chat_id.strip()
        if req.token:
            e["token"] = put(secret_name("notify", label, "token"), req.token)
    elif req.type in URL_SECRET:
        e.pop("token", None)
        e.pop("chat_id", None)
        if req.url and req.url.strip():
            e["url"] = put(secret_name("notify", label, "url"), req.url.strip())
    else:
        e.pop("chat_id", None)
        url = (req.url or "").strip()
        if not url:
            raise FormError("Enter the address")
        if old.get("url") != url:
            e.pop("token", None)
        e["url"] = url
        if req.token:
            e["token"] = put(secret_name("notify", label, "token"), req.token)
    return e


def apply_notify(raw: dict, req: NotifyIn, put: Callable[[str, str], str]) -> dict:
    data = deepcopy(raw)
    saved = data.get("notify") or []
    used: set[str] = set()
    entries = []
    for i, t in enumerate(req.targets):
        old = saved[t.previous_index] if t.previous_index is not None and t.previous_index < len(saved) else None
        entries.append(target_entry(t, old if isinstance(old, dict) else None, i, put, used))
    if entries:
        data["notify"] = entries
    else:
        data.pop("notify", None)
    return data


def source_view(s: ServiceSource) -> dict:
    ssh = s.ssh
    return {
        "name": s.name,
        "type": s.type,
        "address": s.address or "",
        "ssh_host": ssh.host if ssh else "",
        "ssh_port": ssh.port if ssh else 22,
        "ssh_user": ssh.user if ssh else "root",
        "sudo": bool(ssh and ssh.sudo),
        "password_set": bool(ssh and ssh.password),
        "url": s.url or "",
        "token_set": bool(s.token),
        "verify_ssl": s.verify_ssl is not False,
        "portainer_endpoint": s.portainer_endpoint,
    }


def _put(d: dict, key: str, value) -> None:
    if value in (None, "", False):
        d.pop(key, None)
    else:
        d[key] = value


def source_entry(req: SourceIn, old: dict | None, paths: ob.Paths, put: Callable[[str, str], str]) -> dict:
    """The config entry for a source: the saved one with the form's fields applied. Secrets of another
    connection (a changed host, user or URL) are dropped, never sent to the new address."""
    old = deepcopy(old) if old and old.get("type", "ssh") == req.type else {}
    e = old
    e["name"] = req.name.strip()
    e["type"] = req.type
    _put(e, "address", req.address.strip())
    if req.type == "ssh":
        host = req.ssh_host.strip()
        if not host:
            raise FormError("Enter the machine's address")
        ssh = e.get("ssh") or {}
        if (ssh.get("host"), ssh.get("port", 22), ssh.get("user", "root")) != (host, req.ssh_port, req.ssh_user):
            ssh.pop("password", None)
        ssh.update(host=host, port=req.ssh_port, user=req.ssh_user)
        _put(ssh, "sudo", req.sudo)
        ssh.setdefault("key_file", str(paths.ssh_key))
        ssh.setdefault("known_hosts", str(paths.known_hosts))
        if req.password:
            ssh["password"] = put(secret_name("services", e["name"], "password"), req.password)
        e["ssh"] = ssh
        for k in ("url", "token", "verify_ssl", "portainer_endpoint"):
            e.pop(k, None)
    else:
        url = req.url.strip()
        if not url:
            raise FormError("Enter the API address")
        if old.get("url") != url:
            old.pop("token", None)
        e["url"] = url
        if req.token:
            e["token"] = put(secret_name("services", e["name"], "token"), req.token)
        if isinstance(e.get("verify_ssl"), str) and req.verify_ssl:
            pass  # a CA bundle path set by hand stays
        else:
            _put(e, "verify_ssl", None if req.verify_ssl else False)
            if req.verify_ssl:
                e.pop("verify_ssl", None)
        _put(e, "portainer_endpoint", req.portainer_endpoint)
        e.pop("ssh", None)
    return e


def apply_services(raw: dict, req: ServicesIn, paths: ob.Paths, put: Callable[[str, str], str]) -> dict:
    data = deepcopy(raw)
    services = data.get("services") or {}
    saved = {s.get("name"): s for s in services.get("sources") or [] if isinstance(s, dict)}
    names = [s.name.strip() for s in req.sources]
    if len(set(n.lower() for n in names)) != len(names):
        raise FormError("Two sources have the same name")
    services["sources"] = [
        source_entry(s, saved.get(s.previous_name or s.name.strip()), paths, put) for s in req.sources
    ]
    if not services["sources"]:
        del services["sources"]
    _put(services, "enabled", None if req.enabled else False)
    if req.enabled:
        services.pop("enabled", None)
    _put(services, "show_stopped", req.show_stopped)
    _put(services, "addresses", {k.strip(): v.strip() for k, v in req.addresses.items() if k.strip() and v.strip()})
    if services:
        data["services"] = services
    else:
        data.pop("services", None)
    return data


def add_settings_routes(
    app: FastAPI,
    cfg: AppConfig,
    paths: ob.Paths,
    lock: asyncio.Lock,
    editable: Callable[[], None],
    first_run: bool,
) -> None:
    def text() -> str:
        return paths.config.read_text() if paths.config.exists() else ""

    def on_disk() -> AppConfig:
        try:
            return ob.validate_raw(ob.read_raw(paths), str(paths.config))
        except (ValueError, ValidationError):
            return cfg

    def secrets() -> SecretStore:
        if not cfg.state_file:
            raise HTTPException(
                409, "Secrets are kept encrypted in the state database; set FLEETDECK_STATE (the image sets it)"
            )
        return SecretStore(cfg.state_file, paths.keys)

    def _rollback(written: dict[str, str | None]) -> None:
        for name, old in written.items():
            try:
                secrets().set(name, old) if old is not None else secrets().delete(name)
            except (SecretError, HTTPException):
                log.warning("could not undo secret %s after a failed save", name)

    @app.get("/api/settings/secrets")
    async def secrets_state():
        """How many secrets sit in plaintext in config.yaml (names of the keys only, never values)."""
        if first_run:
            raise HTTPException(409, "create the admin login first")
        try:
            raw = await asyncio.to_thread(ob.read_raw, paths)
        except ValueError:
            raw = {}
        return {"plaintext": find_plaintext(raw), "available": bool(cfg.state_file), "writable": paths.writable()}

    @app.post("/api/settings/secrets/migrate")
    async def move_secrets():
        """Move plaintext secrets into the encrypted store and put references in config.yaml.

        Checked first: the new config must resolve to exactly what the old one did, or nothing is written."""
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        async with lock:
            written: dict[str, str | None] = {}

            def put(name: str, value: str) -> str:
                written.setdefault(name, None)
                secrets().set(name, value)
                return f"${{SECRET:{name}}}"

            try:
                raw = ob.read_raw(paths)
                before = ob.validate_raw(raw, str(paths.config)).model_dump()
                new, moved = migrate(raw, put)
                if not moved:
                    return {"moved": []}
                after = ob.validate_raw(new, str(paths.config)).model_dump()
                if before != after:
                    raise FormError("The moved secrets did not read back the same; nothing was changed")
                await asyncio.to_thread(ob.write_raw, paths, new)
            except (ValueError, ValidationError, SecretError) as e:
                _rollback(written)
                raise HTTPException(400, _form_message(e)) from None
        return {"moved": moved}

    @app.get("/api/settings/services")
    async def get_services():
        if first_run:
            raise HTTPException(409, "create the admin login first")
        s = (await asyncio.to_thread(on_disk)).services
        return {
            "revision": hashlib.sha256(text().encode()).hexdigest(),
            "writable": paths.writable(),
            "enabled": s.enabled,
            "show_stopped": s.show_stopped,
            "addresses": s.addresses,
            "sources": [source_view(x) for x in s.sources],
        }

    @app.put("/api/settings/services")
    async def put_services(req: ServicesIn):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        async with lock:
            if req.revision != hashlib.sha256(text().encode()).hexdigest():
                raise HTTPException(409, "The configuration changed; reload the page before saving")
            written: dict[str, str | None] = {}  # name -> value to restore if the save fails

            def put(name: str, value: str) -> str:
                if name not in written:
                    try:
                        written[name] = secrets().get(name)
                    except SecretError:
                        written[name] = None
                try:
                    secrets().set(name, value)
                except SecretError:
                    raise FormError(SECRET_PROBLEM) from None
                return f"${{SECRET:{name}}}"

            try:
                data = apply_services(ob.read_raw(paths), req, paths, put)
                validated = await asyncio.to_thread(ob.write_raw, paths, data)
            except (ValueError, ValidationError) as e:
                _rollback(written)
                raise HTTPException(400, _form_message(e)) from None
        # Services are read on every request: apply without a restart.
        cfg.services = validated.services
        sources = getattr(app.state, "sources", None)
        if sources is not None and not sources.fake:
            sources.reload(validated.services.sources)
        return {"saved": True}

    def _revision() -> str:
        return hashlib.sha256(text().encode()).hexdigest()

    async def _save_section(req, apply: Callable[[dict, object], dict], live: Callable[[AppConfig], list[str]]):
        """Save one plain section: revision check, validate, write, then apply what can change without a restart.
        Returns the settings that need a restart."""
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        async with lock:
            if req.revision != _revision():
                raise HTTPException(409, "The configuration changed; reload the page before saving")
            try:
                validated = await asyncio.to_thread(ob.write_raw, paths, apply(ob.read_raw(paths), req))
            except (ValueError, ValidationError) as e:
                raise HTTPException(400, _form_message(e)) from None
        return {"saved": True, "restart_needed": live(validated)}

    @app.get("/api/settings/schedule")
    async def get_schedule():
        if first_run:
            raise HTTPException(409, "create the admin login first")
        s = (await asyncio.to_thread(on_disk)).schedule
        return {
            "revision": _revision(),
            "writable": paths.writable(),
            "check_every": duration_text(s.check_every),
            "check_docker": s.check_docker,
            "auto_security_updates": s.auto_security_updates,
        }

    @app.put("/api/settings/schedule")
    async def put_schedule(req: ScheduleIn):
        def live(new: AppConfig) -> list[str]:
            cfg.schedule = new.schedule
            sched = getattr(app.state, "scheduler", None)
            if sched is None:
                return []
            sched.check_docker = new.schedule.check_docker
            sched.auto_security = new.schedule.auto_security_updates
            if sched.interval != new.schedule.check_every:
                sched.interval = new.schedule.check_every
                if sched._task is not None:  # the running loop keeps its old sleep; start it again
                    sched._task.cancel()
                    sched._task = None
                sched.next_run = None
                if sched.interval:
                    sched.start()
            return []

        return await _save_section(req, apply_schedule, live)

    @app.get("/api/settings/security")
    async def get_security():
        if first_run:
            raise HTTPException(409, "create the admin login first")
        s = (await asyncio.to_thread(on_disk)).security
        return {
            "revision": _revision(),
            "writable": paths.writable(),
            "scan_images": s.scan_images,
            "min_severity": s.min_severity,
            "ignore_unfixed": s.ignore_unfixed,
        }

    @app.put("/api/settings/security")
    async def put_security(req: SecurityIn):
        def live(new: AppConfig) -> list[str]:
            was = cfg.security.scan_images
            cfg.security = new.security
            vulns = getattr(app.state, "vulns", None)
            if vulns is not None:  # the scanner reads these on every scan
                vulns.scanner.min_severity = new.security.min_severity
                vulns.scanner.ignore_unfixed = new.security.ignore_unfixed
            return ["Image scanning"] if new.security.scan_images != was else []

        return await _save_section(req, apply_security, live)

    @app.get("/api/settings/updates")
    async def get_updates():
        if first_run:
            raise HTTPException(409, "create the admin login first")
        c = await asyncio.to_thread(on_disk)
        return {
            "revision": _revision(),
            "writable": paths.writable(),
            "max_parallel": c.max_parallel,
            "snapshot_before_upgrade": c.snapshot_before_upgrade,
            "snapshot_keep": c.snapshot_keep,
            "community_scripts_update": c.community_scripts_update,
        }

    @app.put("/api/settings/updates")
    async def put_updates(req: UpdatesIn):
        def live(new: AppConfig) -> list[str]:
            was = cfg.max_parallel
            cfg.snapshot_before_upgrade = new.snapshot_before_upgrade
            cfg.snapshot_keep = new.snapshot_keep
            cfg.community_scripts_update = new.community_scripts_update
            cfg.max_parallel = new.max_parallel
            jobs = getattr(app.state, "jobs", None)
            if jobs is not None:
                jobs.snapshot_keep = new.snapshot_keep
                jobs.community_scripts_update = new.community_scripts_update
            return ["Parallel jobs"] if new.max_parallel != was else []

        return await _save_section(req, apply_updates, live)

    @app.get("/api/settings/notify")
    async def get_notify():
        if first_run:
            raise HTTPException(409, "create the admin login first")
        c = await asyncio.to_thread(on_disk)
        return {
            "revision": _revision(),
            "writable": paths.writable(),
            "targets": [target_view(t) for t in c.notify],
        }

    @app.put("/api/settings/notify")
    async def put_notify(req: NotifyIn):
        if first_run:
            raise HTTPException(409, "create the admin login first")
        editable()
        async with lock:
            if req.revision != _revision():
                raise HTTPException(409, "The configuration changed; reload the page before saving")
            written: dict[str, str | None] = {}  # name -> value to restore if the save fails

            def put(name: str, value: str) -> str:
                if name not in written:
                    try:
                        written[name] = secrets().get(name)
                    except SecretError:
                        written[name] = None
                try:
                    secrets().set(name, value)
                except SecretError:
                    raise FormError(SECRET_PROBLEM) from None
                return f"${{SECRET:{name}}}"

            try:
                data = apply_notify(ob.read_raw(paths), req, put)
                validated = await asyncio.to_thread(ob.write_raw, paths, data)
            except (ValueError, ValidationError) as e:
                _rollback(written)
                raise HTTPException(400, _form_message(e)) from None
        # The notifier reads its targets on every send: apply without a restart.
        cfg.notify = validated.notify
        notifier = getattr(app.state, "notifier", None)
        if notifier is not None:
            notifier.targets = validated.notify
        return {"saved": True}

    @app.post("/api/settings/notify/test")
    async def test_notify(req: TargetIn):
        """Send one test message to this target as typed. Never saves anything."""
        if first_run:
            raise HTTPException(409, "create the admin login first")
        try:
            saved = ob.read_raw(paths).get("notify") or []
            old = (
                saved[req.previous_index]
                if req.previous_index is not None and req.previous_index < len(saved)
                else None
            )
            entry = target_entry(req, old if isinstance(old, dict) else None, 0, lambda _n, v: v)
            target = ob.validate_raw({"notify": [entry]}).notify[0]
        except (ValueError, ValidationError) as e:
            return {"ok": False, "error": _form_message(e)}
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                r = await Notifier([target], cfg.public_url, client)._send_one(
                    target, "Fleetdeck test", "Notifications from your homelab dashboard work."
                )
            except Exception as e:  # httpx errors can carry the address; only fixed text goes back
                log.info("notification test failed: %s", type(e).__name__)
                return {"ok": False, "error": "Could not connect. Check the address and that the service is running."}
        if r.status_code >= 400:
            return {"ok": False, "error": f"The service answered HTTP {r.status_code}. Check the address and token."}
        return {"ok": True}

    @app.post("/api/settings/services/test")
    async def test_source(req: SourceTest):
        """Read the source once, the way the Services tab does (read-only). Never saves anything."""
        if first_run:
            raise HTTPException(409, "create the admin login first")
        try:
            saved = {
                s.get("name"): s
                for s in (ob.read_raw(paths).get("services") or {}).get("sources") or []
                if isinstance(s, dict)
            }
            # A test never stores anything: new secrets are used as typed.
            entry = source_entry(
                req.source, saved.get(req.source.previous_name or req.source.name.strip()), paths, lambda _n, v: v
            )
            source = ob.validate_raw({"services": {"sources": [entry]}}).services.sources[0]
        except (ValueError, ValidationError) as e:
            return {"ok": False, "error": _form_message(e)}
        out: dict = {"ok": False}
        if source.type == "ssh" and source.ssh and source.ssh.known_hosts == str(paths.known_hosts):
            ssh = source.ssh
            try:
                key = await ob.fetch_host_key(ssh.host, ssh.port)
            except ob.SetupError:
                return {"ok": False, "error": f"Can't reach SSH on {ssh.host}:{ssh.port}. Check the address and port."}
            fp = key.get_fingerprint("sha256")
            pinned = await asyncio.to_thread(ob._pinned_keys, paths, ssh.host, ssh.port)
            if not any(k.public_data == key.public_data for k in pinned):
                if req.trust_fingerprint != fp:
                    # Nothing is pinned until the person has compared the fingerprint with the machine's.
                    return {
                        "ok": False,
                        "needs_confirm": True,
                        "host_key_changed": bool(pinned),
                        "fingerprint": fp,
                        "error": f"{ssh.host} presents SSH host key {fp}. Compare it with the machine's, then trust it.",  # noqa: E501
                    }
                await asyncio.to_thread(ob._pin_host_key, paths, ssh.host, ssh.port, key)
        try:
            result = await asyncio.wait_for(scan_source(source), TEST_TIMEOUT)
        except TimeoutError:
            return {"ok": False, "error": f"No answer within {TEST_TIMEOUT} seconds"}
        except Exception as e:  # asyncssh, httpx, OSError: summarised for the person, details stay out of the reply
            log.info("services source test failed: %s", type(e).__name__)
            return {"ok": False, "error": _failure(e)}
        found = len(result.standalone) + sum(len(st.services) for st in result.stacks)
        out.update(ok=result.error is None, containers=found, error=result.error)
        return out


def _form_message(e: Exception) -> str:
    """What is wrong with a submitted form, in words we wrote (or the field model's own)."""
    if isinstance(e, FormError):
        return e.text
    if isinstance(e, SecretError):
        return SECRET_PROBLEM
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
    return "The settings refer to an environment variable or secret that is not set."


def _failure(e: Exception) -> str:
    """A short, fixed description of why a source could not be read."""
    if isinstance(e, asyncssh.PermissionDenied):
        return "The machine refused the login. Check the user, the SSH key or the password."
    if isinstance(e, httpx.HTTPStatusError):
        return f"The API answered HTTP {e.response.status_code}. Check the address, token and Portainer environment ID."
    if isinstance(e, httpx.HTTPError | OSError | asyncssh.Error):
        return "Could not connect. Check the address, port and that the service is running."
    return "The check failed. See the dashboard log for details."

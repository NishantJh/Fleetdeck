from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .backup import add_backup_routes
from .backup_runs import check_destination
from .config import MAX_UPDATE_COMMAND, AppConfig, check_update_command, config_path, load_config
from .console import Console, client_ip, network_problem
from .console_routes import add_console_routes
from .dockhand import DockhandReader, DockhandStatus, demo_status
from .dockhand_updates import DockhandUpdateRequest, excluded, install, validate_selection
from .inventory import Inventory, build_providers
from .jobs import JobManager
from .models import CheckResult, DockerSettings, Job, JobAction, StacksResult, Target
from .notify import Notifier
from .onboarding import Paths
from .providers.proxmox import PROTECTED_NOTE
from .report import build_report
from .scheduler import Scheduler
from .security import LoginThrottle, Sessions, add_login_routes, make_guard, password_ok, session_secret
from .setup_routes import add_setup_routes
from .store import Store
from .tunnels import TunnelManager
from .tunnels import manager as tunnel_manager

log = logging.getLogger("fleetdeck")
STATIC = Path(__file__).parent / "static"


class TargetView(BaseModel):
    target: Target
    check: CheckResult | None = None
    active_job: Job | None = None
    hidden: bool = False
    # Why this host can't be updated or rebooted right now (a guest or Docker on it is updating).
    blocked: str | None = None
    # What the last Docker check found (None: never checked).
    stacks: StacksResult | None = None
    # Looks like the machine this dashboard runs on: shown with a "dashboard" tag.
    runs_dashboard: bool = False
    docker_settings: DockerSettings = Field(default_factory=DockerSettings)
    # Configured monitor-only, including restrictions retained from legacy dashboard overrides.
    monitor_only: bool = False
    dockhand: DockhandStatus | None = None
    dockhand_updates_allowed: bool = False
    # The machine's own command that Update runs instead of the OS upgrade (from the config or set here).
    update_command: str | None = None


class ActionRequest(BaseModel):
    action: JobAction
    target_ids: list[str] = Field(max_length=1000)
    # Upgrade only: reboot afterwards when the upgrade leaves a reboot pending.
    reboot_if_required: bool = False
    # Upgrade only: snapshot guests first.
    snapshot_first: bool = False
    # Backup only: a folder (or mounted network share) on the machine to put it in, instead of the default.
    destination: str | None = Field(None, max_length=1024)
    # Stack update only: the Compose project to update, and whether to remove unused old images after.
    stack: str | None = Field(None, max_length=128)
    prune: bool = False


class BackupPlaceRequest(BaseModel):
    target_id: str
    # The folder to always back up to; None goes back to the default place.
    path: str | None = Field(None, max_length=1024)


class UpdateCommandRequest(BaseModel):
    target_id: str
    # None or empty removes it: Update runs the built-in OS upgrade again.
    command: str | None = Field(None, max_length=MAX_UPDATE_COMMAND)
    # The dashboard password again: the command runs as root.
    password: str = Field(max_length=1024)


class HideRequest(BaseModel):
    target_ids: list[str] = Field(max_length=1000)
    hidden: bool = True


class DockerSettingsRequest(DockerSettings):
    target_id: str


class DockhandCheckRequest(BaseModel):
    target_id: str = Field(min_length=1, max_length=512)


class MonitorOnlyRequest(BaseModel):
    target_ids: list[str] = Field(max_length=1000)
    monitor_only: bool = True


def _runs_dashboard(target: Target, check: CheckResult | None, hostname: str) -> bool:
    """Whether the target is (or holds) this dashboard: found by the check, protected in the Proxmox
    config for that reason, or named like the machine we run on (when not in a container)."""
    if check is not None and check.runs_dashboard:
        return True
    return target.note == PROTECTED_NOTE or target.name.lower() == hostname


def create_app(
    cfg: AppConfig | None = None,
    inventory: Inventory | None = None,
    *,
    config_file: str | None = None,
    restart: Callable[[], None] | None = None,
) -> FastAPI:
    """``config_file`` is the file the setup page edits; ``restart`` reloads the app with it."""
    cfg = cfg or load_config(config_file)
    paths = Paths(Path(config_file or config_path()).absolute())
    # No login yet (a fresh install): only the setup page works, and only with a one-time link from
    # the logs, until it has created one. A demo without real machines stays usable, but its setup
    # API still needs the link: it can SSH anywhere and write the config.
    setup_token = None
    if cfg.auth is None and not cfg.allow_no_auth:
        setup_token = secrets.token_urlsafe(24)
    locked = setup_token is not None and (cfg.has_real_targets or not cfg.demo)
    if locked and cfg.has_real_targets:
        log.warning("No `auth:` in the config: the dashboard stays locked until an admin login is set up.")
    inventory = inventory or Inventory(build_providers(cfg) if not locked else [])
    store = _open_store(cfg.state_file)
    backup_folder = cfg.dashboard_backup_folder
    if backup_folder is not None and not cfg.backup_folder:
        # The default sits in the data volume, so it is ours to make; a folder the user names must be there already.
        try:
            backup_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as e:
            log.warning("Could not create %s for backups saved on the dashboard: %s", backup_folder, e)
    jobs = JobManager(
        inventory,
        cfg.max_parallel,
        store,
        cfg.snapshot_keep,
        cfg.community_scripts_update,
        backup_folder,
        cfg.backup_age_recipient,
        cfg.backup_password,
        dockhand_targets={m.target_id: cfg.dockhand.url for m in cfg.dockhand.environments} if cfg.dockhand else {},
    )
    # Demo mode must never contact a configured external manager.
    dockhand = DockhandReader(cfg.dockhand, store=store) if cfg.dockhand and not cfg.demo and not locked else None
    demo_dockhand = (
        {m.target_id: demo_status(cfg.dockhand.url) for m in cfg.dockhand.environments}
        if cfg.demo and cfg.dockhand
        else {}
    )
    notifier = Notifier(cfg.notify, cfg.public_url) if cfg.notify else None
    scheduler = Scheduler(
        cfg.schedule.check_every,
        inventory,
        jobs,
        notifier,
        store,
        cfg.schedule.check_docker,
        cfg.schedule.auto_security_updates,
        dockhand=dockhand,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if locked:
            log.warning(
                "First run: open http://<this machine>:%s to create the admin login and add machines. "
                "It asks for the setup code below, which changes every time the container starts. "
                "Or open http://<this machine>:%s/setup#token=%s",
                _published_port(),
                _published_port(),
                setup_token,
            )
            log.warning("Setup code: %s", setup_token)
        elif setup_token:
            log.info("Setup page: http://<this machine>:%s/setup#token=%s", _published_port(), setup_token)
            log.info("Setup code: %s", setup_token)
        elif cfg.auth is not None:
            # Setup is done: the one-time link is gone, so say where to go instead.
            log.info(
                "Setup is done: open http://<this machine>:%s and log in as %r. "
                "Add or remove machines with its Setup button.",
                _published_port(),
                cfg.auth.username,
            )
        if cfg.auth is None and not locked:
            log.warning("No auth configured: anyone who can reach this dashboard can update your machines.")
            if not cfg.allowed_hosts:
                log.warning("allowed_hosts is not set: without auth, web pages can reach the API via DNS rebinding.")
        if notifier is not None and not scheduler.interval:
            log.warning("Notifications are set up but schedule.check_every isn't, so nothing will be sent.")
        # Tunnels to machines that can't be reached directly; restarted if one dies.
        wanted = {h.name: h.tunnel for h in cfg.hosts if h.tunnel}
        tunnels = tunnel_manager(paths.keys / "tunnels")
        await asyncio.to_thread(tunnels.sync, wanted)
        watchdog = asyncio.create_task(_keep_tunnels(tunnels, wanted)) if wanted else None
        scheduler.start()
        jobs.resume()  # upgrades and reboots a restart cut off, still going on their machines
        yield
        if watchdog is not None:
            watchdog.cancel()
        await scheduler.stop()  # first, so it can't queue checks during shutdown
        await jobs.shutdown()
        if dockhand is not None:
            await dockhand.aclose()
        if notifier is not None:
            await notifier.aclose()
        for provider in inventory.providers:
            client = getattr(provider, "client", None)
            if client is not None:
                await client.aclose()
        if store is not None:
            store.close()

    app = FastAPI(title="Fleetdeck", lifespan=lifespan)
    app.state.jobs = jobs
    app.state.inventory = inventory
    app.state.scheduler = scheduler
    app.state.dockhand = dockhand
    app.state.demo_dockhand = demo_dockhand
    app.state.setup_token = setup_token

    # The login page's cookie; HTTP Basic (scripts, curl -u) keeps working alongside it.
    sessions = Sessions(cfg.auth, session_secret(store)) if cfg.auth is not None else None
    throttle = LoginThrottle()
    guard = make_guard(cfg.auth, throttle, setup_token, locked, sessions)
    app.middleware("http")(guard)
    add_login_routes(app, cfg.auth, guard, throttle, sessions, STATIC / "login.html")
    if cfg.allowed_hosts:
        # Added last so it runs first: a request for a foreign Host never reaches the API.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=cfg.allowed_hosts)

    hostname = socket.gethostname().split(".")[0].lower()
    background: set[asyncio.Task] = set()  # notifications being sent, kept so they aren't garbage collected

    @app.get("/api/targets")
    async def list_targets(refresh: bool = False):
        await inventory.refresh(force=refresh)
        if dockhand is not None:
            await dockhand.refresh(force=refresh)
        errors = dict(inventory.errors)
        if cfg.dockhand:
            missing = [m for m in cfg.dockhand.environments if m.target_id not in inventory.targets]
            if missing:
                errors["Dockhand mapping"] = (
                    "No matching Fleetdeck machine for "
                    + ", ".join(f"environment {m.environment_id}: {m.target_id}" for m in missing)
                    + ". Use the exact target.id from /api/targets in dockhand.environments; "
                    "display names do not match."
                )
        views = [
            TargetView(
                target=t.model_copy(update={"can_start": False}) if t.can_start and t.id not in inventory.power else t,
                check=(check := jobs.checks.get(t.id)),
                active_job=_light(jobs.active_job(t.id)),
                hidden=t.id in jobs.hidden,
                blocked=jobs.update_blocked(t),
                stacks=jobs.stacks.get(t.id),
                runs_dashboard=_runs_dashboard(t, check, hostname),
                docker_settings=jobs.get_docker_settings(t.id),
                dockhand_updates_allowed=bool(
                    cfg.dockhand and any(m.target_id == t.id and m.allow_updates for m in cfg.dockhand.environments)
                ),
                monitor_only=jobs.monitor_only(t),
                dockhand=dockhand.status.get(t.id) if dockhand is not None else demo_dockhand.get(t.id),
                update_command=jobs.update_command(t),
            )
            for t, _ in inventory.targets.values()
        ]
        return {
            "targets": views,
            "errors": errors,
            "backup_places": jobs.backup_places,
            "backup_folder": str(backup_folder) if backup_folder else None,
            "settings": {
                "snapshot_before_upgrade": cfg.snapshot_before_upgrade,
                "community_scripts_update": cfg.community_scripts_update,
                "containers_visible": True,
                "dockhand_checks_allowed": bool(cfg.dockhand and cfg.dockhand.allow_checks),
                "dockhand_poll_seconds": cfg.dockhand.poll_seconds if cfg.dockhand else None,
                "dockhand_results_persistent": store is not None,
                "scheduled_container_checks": cfg.schedule.check_docker,
            },
        }

    @app.post("/api/dockhand/check", status_code=202)
    async def check_dockhand(req: DockhandCheckRequest):
        if not cfg.dockhand or not cfg.dockhand.allow_checks:
            raise HTTPException(403, "Enable dockhand.allow_checks in Settings for explicit Dockhand checks")
        await inventory.refresh()
        if req.target_id not in inventory.targets or req.target_id not in jobs.dockhand_targets:
            raise HTTPException(404, "No Dockhand mapping for this machine")
        if any(
            j.target_id == req.target_id and j.action == "dockhand-update" and j.status in {"queued", "running"}
            for j in jobs.jobs.values()
        ):
            raise HTTPException(409, "Wait for the container update to finish")
        if cfg.demo:
            previous = demo_dockhand[req.target_id]
            now = datetime.now(UTC)
            demo_dockhand[req.target_id] = previous.model_copy(
                update={
                    "last_check_at": now,
                    "synced_at": now,
                    "containers": [
                        c.model_copy(
                            update={
                                "status": "current" if c.status in {"unknown", "updated"} else c.status,
                                "checked_at": now,
                                "source": "fleetdeck_button",
                            }
                        )
                        for c in previous.containers
                    ],
                }
            )
            return {"started": True, "simulated": True}
        if dockhand is None:
            raise HTTPException(409, "Dockhand status integration unavailable")
        try:
            dockhand.request_check(req.target_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "No Dockhand mapping for this machine") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"started": True}

    def update_guard(target_id: str, own_job=None):
        mapping = (
            next((m for m in cfg.dockhand.environments if m.target_id == target_id), None) if cfg.dockhand else None
        )
        if not mapping or not mapping.allow_updates or target_id not in jobs.dockhand_targets:
            raise HTTPException(403, "Enable Install container updates for this Dockhand machine first")
        try:
            target, _ = inventory.get(target_id)
        except KeyError:
            raise HTTPException(404, "Unknown target") from None
        if (
            not target.updatable
            or target.check_only
            or target.status not in {"running", "online"}
            or jobs.monitor_only(target)
        ):
            raise HTTPException(409, "Machine is unavailable, check-only or monitor-only")
        host_id = jobs._host_id(target)
        related = {target_id, host_id}
        if target.kind == "host":
            related.update(j.target_id for j in jobs._guest_jobs(target))
        if any(
            j.id != own_job and j.target_id in related and j.status in {"queued", "running"} for j in jobs.jobs.values()
        ):
            raise HTTPException(409, "Wait for machine/host jobs to finish before updating containers")
        if host_id in jobs._rebooting or target_id in jobs._rebooting:
            raise HTTPException(409, "Machine or its host is rebooting")
        if dockhand and target_id in dockhand.tasks:
            raise HTTPException(409, "A Dockhand check is running")
        return target

    @app.get("/api/dockhand/update-options")
    async def dockhand_update_options(target_id: str):
        await inventory.refresh()
        update_guard(target_id)
        if dockhand:
            await dockhand.refresh(force=True)
        status = dockhand.status.get(target_id) if dockhand else demo_dockhand.get(target_id)
        if status is None or status.stale or status.error or status.checking or status.check_error:
            raise HTTPException(409, "Dockhand reports unavailable; refresh/check before updating")
        return {
            "containers": [{**c.model_dump(mode="json"), "excluded_reason": excluded(c)} for c in status.containers]
        }

    @app.post("/api/dockhand/update", status_code=202)
    async def update_dockhand(req: DockhandUpdateRequest):
        await inventory.refresh()
        update_guard(req.target_id)
        status = dockhand.status.get(req.target_id) if dockhand else demo_dockhand.get(req.target_id)
        try:
            validate_selection(status, req.containers)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

        async def operation(on_line):
            await inventory.refresh(force=True)

            def guard():
                return update_guard(req.target_id, job.id)

            guard()
            if cfg.demo:
                selected = validate_selection(demo_dockhand[req.target_id], req.containers)
                ids = {r.container_id for r in selected}
                current = demo_dockhand[req.target_id]
                demo_dockhand[req.target_id] = current.model_copy(
                    update={
                        "containers": [
                            c.model_copy(
                                update={
                                    "status": "current",
                                    "image_update": False,
                                    "checked_at": None,
                                    "updated_at": datetime.now(UTC),
                                    "source": "fleetdeck_update",
                                    "check_issue": None,
                                }
                            )
                            if c.container_id in ids
                            else c
                            for c in current.containers
                        ]
                    }
                )
                on_line("stdout", "== Simulated Dockhand update; no manager was contacted")
            elif dockhand:
                await install(dockhand, req.target_id, req.containers, guard, on_line)
            else:
                raise ValueError("Dockhand unavailable")

        try:
            job = jobs.submit_dockhand_update(req.target_id, operation)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"job": _light(job), "simulated": cfg.demo}

    @app.put("/api/docker-settings")
    async def set_docker_settings(req: DockerSettingsRequest):
        await inventory.refresh()
        try:
            settings = DockerSettings.model_validate(req.model_dump(exclude={"target_id"}))
            jobs.set_docker_settings(req.target_id, settings)
        except KeyError:
            raise HTTPException(404, "unknown target") from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return {"docker_settings": settings}

    @app.post("/api/actions")
    async def run_action(req: ActionRequest):
        if req.action == "rollback":
            raise HTTPException(400, "roll back from the upgrade job: POST /api/jobs/{id}/rollback")
        destination = None
        if req.destination and req.action == "backup":
            try:
                destination = check_destination(req.destination)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        await inventory.refresh()
        submitted, rejected = [], {}
        for tid in req.target_ids:
            try:
                submitted.append(
                    _light(
                        jobs.submit(
                            tid, req.action, req.reboot_if_required, snapshot_first=req.snapshot_first,
                            destination=destination, stack=req.stack, prune=req.prune,
                        )
                    )
                )  # fmt: skip
            except KeyError:
                rejected[tid] = "unknown target"
            except ValueError as e:
                rejected[tid] = str(e)
        return {"jobs": submitted, "rejected": rejected}

    @app.put("/api/backup-places")
    async def set_backup_place(req: BackupPlaceRequest):
        """Remember the folder a host's Back up button uses (None: back to the default place)."""
        await inventory.refresh()
        if req.target_id not in inventory.targets:
            raise HTTPException(404, "unknown target")
        try:
            path = check_destination(req.path) if req.path else None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        jobs.set_backup_place(req.target_id, path)
        return {"backup_places": jobs.backup_places}

    @app.put("/api/update-command")
    async def set_update_command(req: UpdateCommandRequest, request: Request):
        """Set a machine's own update command (e.g. omv-upgrade), or remove it. It runs as root, so this asks for
        the password again, works only from the console's networks (console.allow_from), and is logged."""
        if (why := network_problem(request, cfg.console.allow_from, cfg.trusted_proxies)) is not None:
            raise HTTPException(403, f"update commands can't be changed from here: {why}")
        ip = client_ip(request)
        if (left := throttle.blocked_for(ip)) > 0:
            raise HTTPException(429, f"too many failed attempts, try again in {int(left) + 1} s")
        user = cfg.auth.username if cfg.auth else "demo"
        if not await asyncio.to_thread(password_ok, cfg.auth, req.password):
            throttle.failed(ip)  # not the password: CodeQL flags hashing it, and the audit row counts each try
            _audit(store, {"kind": "update-command-failed", "user": user, "ip": ip, "target": req.target_id})
            raise HTTPException(403, "wrong password")
        throttle.succeeded(ip)
        await inventory.refresh()
        if req.target_id not in inventory.targets:
            raise HTTPException(404, "unknown target")
        target = inventory.targets[req.target_id][0]
        if target.update_command:
            raise HTTPException(400, f"{target.name}: its update command is set in the config file; change it there")
        try:
            command = check_update_command(req.command) if req.command and req.command.strip() else None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        before = jobs.update_commands.get(target.id)
        jobs.set_update_command(target.id, command)
        row = {"kind": "update-command", "user": user, "ip": ip, "target": target.id, "machine": target.name}
        _audit(store, {**row, "command": command, "before": before})
        what = f"set to: {command}" if command else "removed (back to the built-in OS upgrade)"
        log.warning("%s changed the update command of %s from %s: %s", user, target.name, ip, what)
        if notifier is not None and command != before:
            # Another way to run commands as root on the machine: worth telling about, like the console does.
            task = asyncio.create_task(
                notifier.send(f"Update command changed on {target.name}", f"{user} ({ip}) {what}")
            )
            background.add(task)
            task.add_done_callback(background.discard)
        return {"update_command": command}

    @app.post("/api/hidden")
    async def set_hidden(req: HideRequest):
        """Hide machines from the dashboard (and from scheduled checks), or show them again."""
        ids = list(dict.fromkeys(req.target_ids))
        if req.hidden:
            await inventory.refresh()
            unknown = [t for t in ids if t not in inventory.targets]
            if unknown:
                raise HTTPException(404, f"unknown target: {', '.join(unknown)}")
        jobs.set_hidden(ids, req.hidden)
        return {"hidden": sorted(jobs.hidden)}

    @app.post("/api/monitor-only")
    async def set_monitor_only(req: MonitorOnlyRequest):
        """Only monitor machines (checks and reports, no updates), or allow updates again. Machines set to
        monitor-only in the config stay that way."""
        ids = list(dict.fromkeys(req.target_ids))
        await inventory.refresh()
        unknown = [t for t in ids if t not in inventory.targets]
        if req.monitor_only and unknown:
            raise HTTPException(404, f"unknown target: {', '.join(unknown)}")
        in_config = [
            inventory.targets[t][0].name for t in ids if t not in unknown and inventory.targets[t][0].monitor_only
        ]
        if not req.monitor_only and in_config:
            raise HTTPException(400, f"set to monitor only in the config file: {', '.join(in_config)}")
        jobs.set_monitor_only(ids, req.monitor_only)
        return {"monitor_only": sorted(jobs.monitor_only_ids)}

    @app.get("/api/jobs")
    async def list_jobs(limit: int = Query(50, ge=1, le=500)):
        return [_light(j) for j in list(jobs.jobs.values())[-limit:][::-1]]

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, since: int = Query(0, ge=0)):
        job = jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        data = job.model_dump(mode="json")
        data["log"] = job.log[since:]
        data["log_offset"] = since
        return data

    @app.get("/api/report")
    async def error_report():
        """Everything that is failing, as a text file to attach when asking for help."""
        name = f"fleetdeck-errors-{datetime.now(UTC):%Y%m%d-%H%M}.txt"
        return PlainTextResponse(
            build_report(cfg, inventory, jobs), headers={"Content-Disposition": f'attachment; filename="{name}"'}
        )

    @app.get("/api/schedule")
    async def schedule():
        return {
            "check_every": scheduler.interval,
            "check_docker": scheduler.check_docker,
            "next_run": scheduler.next_run,
            "last_run": scheduler.last_run,
            "last_notified": scheduler.last_notified,
            "notify_targets": len(cfg.notify),
        }

    @app.post("/api/notify/test")
    async def notify_test():
        if notifier is None:
            raise HTTPException(400, "no notify targets configured")
        errors = await notifier.send("Fleetdeck test", "Notifications from your homelab dashboard work.")
        return {"sent": len(cfg.notify) - len(errors), "errors": errors}

    @app.post("/api/jobs/{job_id}/rollback")
    async def rollback(job_id: str):
        """Restore the snapshot an upgrade job took before it ran."""
        job = jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.action != "upgrade" or not job.snapshot:
            raise HTTPException(400, "this job took no snapshot")
        await inventory.refresh()
        snaps = inventory.snapshots.get(job.target_id)
        if snaps is not None:
            try:
                exists = job.snapshot in await snaps.list_ours()
            except Exception:  # noqa: BLE001 - let the job itself report what is wrong
                exists = True
            if not exists:
                raise HTTPException(
                    400,
                    f"snapshot {job.snapshot} no longer exists (only the newest {cfg.snapshot_keep} are kept); "
                    "roll back to an older one in Proxmox or Incus",
                )
        try:
            return _light(jobs.submit(job.target_id, "rollback", snapshot=job.snapshot))
        except KeyError:
            raise HTTPException(404, "target no longer exists") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    def busy() -> list[str]:
        names = {j.target_name for j in jobs.jobs.values() if j.status in ("queued", "running")}
        if dockhand is not None and any(not task.done() for task in dockhand.tasks.values()):
            names.add("Dockhand container checks")
        return sorted(names)

    restart = _when_idle(restart, busy) if restart is not None else None
    if cfg.console.enabled:
        # Off unless the server config says so, and never without a login (a demo without real machines excepted).
        if locked or setup_token is not None and cfg.has_real_targets:
            log.warning("The command console stays off until setup is finished.")
        elif cfg.auth is None and (cfg.has_real_targets or not cfg.demo):
            log.warning("console.enabled is set but there is no `auth:` login, so the command console stays off.")
        else:
            console = Console(cfg.console, cfg.auth, store, notifier.send if notifier else None)
            app.state.console = console
            # Its own lockout: the login's is reset by every request that carries the right password.
            add_console_routes(app, cfg, console, inventory, LoginThrottle())
            log.warning("The command console is ON: users who unlock it can run commands as root on your machines.")
    add_setup_routes(app, cfg, paths, restart, setup_token is not None, busy)
    add_backup_routes(app, cfg, paths, store, restart, busy, first_run=setup_token is not None)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", _RevalidatedStatic(directory=STATIC), name="static")
    return app


class _RevalidatedStatic(StaticFiles):
    """Static files the browser re-checks (cheap 304 via ETag) on every load.

    Without a Cache-Control header browsers cache app.js heuristically, so after an update a page can keep
    running the old script (e.g. without the "blocked" buttons) until a hard refresh.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _when_idle(restart: Callable[[], None], busy: Callable[[], list[str]]) -> Callable[[], None]:
    """``restart``, held back while jobs run (one may start between a request and its restart)."""

    def go() -> None:
        if names := busy():
            log.info("Restart waits for the running jobs to finish: %s", ", ".join(names))
            asyncio.get_running_loop().call_later(2, go)
        else:
            restart()

    return go


async def _keep_tunnels(tunnels: TunnelManager, wanted: dict) -> None:
    while True:
        await asyncio.sleep(30)
        await asyncio.to_thread(tunnels.sync, wanted)


def _published_port() -> str:
    """The port people open the dashboard on: the host side of the Docker mapping, when known."""
    return os.environ.get("FLEETDECK_PORT") or os.environ.get("PORT", "8080")


def _open_store(path: str | None) -> Store | None:
    if not path:
        log.info("No state_file configured: check results and job history are kept in memory only.")
        return None
    try:
        return Store(path)
    except Exception:  # noqa: BLE001 - a bad volume shouldn't stop the dashboard from starting
        log.exception("Could not open state file %s; keeping state in memory only.", path)
        return None


def _audit(store: Store | None, row: dict) -> None:
    """Into the console's audit log (shown under Run command when the console is on)."""
    if store is None:
        return
    at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        store.add_console_log(at, json.dumps({**row, "at": at}))
    except Exception:  # noqa: BLE001 - the change itself is logged to the app log too
        log.exception("could not write the audit log")


def _light(job: Job | None) -> Job | None:
    return job.model_copy(update={"log": []}) if job else None

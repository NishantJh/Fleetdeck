from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .backup import add_backup_routes
from .backup_runs import check_destination
from .config import AppConfig, config_path, load_config
from .inventory import Inventory, build_providers
from .jobs import JobManager
from .models import CheckResult, Job, JobAction, StacksResult, Target
from .notify import Notifier
from .onboarding import Paths
from .providers.proxmox import PROTECTED_NOTE
from .report import build_report
from .scheduler import Scheduler
from .security import LoginThrottle, make_guard
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
    # What the last Docker check found (None: never checked).
    stacks: StacksResult | None = None
    # Looks like the machine this dashboard runs on: shown with a "dashboard" tag.
    runs_dashboard: bool = False


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


class HideRequest(BaseModel):
    target_ids: list[str] = Field(max_length=1000)
    hidden: bool = True


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
    jobs = JobManager(inventory, cfg.max_parallel, store, cfg.snapshot_keep, cfg.community_scripts_update)
    notifier = Notifier(cfg.notify, cfg.public_url) if cfg.notify else None
    scheduler = Scheduler(cfg.schedule.check_every, inventory, jobs, notifier)

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
    app.state.setup_token = setup_token

    app.middleware("http")(make_guard(cfg.auth, LoginThrottle(), setup_token, locked))
    if cfg.allowed_hosts:
        # Added last so it runs first: a request for a foreign Host never reaches the API.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=cfg.allowed_hosts)

    hostname = socket.gethostname().split(".")[0].lower()

    @app.get("/api/targets")
    async def list_targets(refresh: bool = False):
        await inventory.refresh(force=refresh)
        views = [
            TargetView(
                target=t,
                check=(check := jobs.checks.get(t.id)),
                active_job=_light(jobs.active_job(t.id)),
                hidden=t.id in jobs.hidden,
                stacks=jobs.stacks.get(t.id),
                runs_dashboard=_runs_dashboard(t, check, hostname),
            )
            for t, _ in inventory.targets.values()
        ]
        return {
            "targets": views,
            "errors": inventory.errors,
            "backup_places": jobs.backup_places,
            "settings": {
                "snapshot_before_upgrade": cfg.snapshot_before_upgrade,
                "community_scripts_update": cfg.community_scripts_update,
            },
        }

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
        name = f"homelab-errors-{datetime.now(UTC):%Y%m%d-%H%M}.txt"
        return PlainTextResponse(
            build_report(cfg, inventory, jobs), headers={"Content-Disposition": f'attachment; filename="{name}"'}
        )

    @app.get("/api/schedule")
    async def schedule():
        return {
            "check_every": scheduler.interval,
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
        return sorted({j.target_name for j in jobs.jobs.values() if j.status in ("queued", "running")})

    restart = _when_idle(restart, busy) if restart is not None else None
    add_setup_routes(app, cfg, paths, restart, setup_token is not None, busy)
    add_backup_routes(app, cfg, paths, store, restart, busy, first_run=setup_token is not None)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


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


def _light(job: Job | None) -> Job | None:
    return job.model_copy(update={"log": []}) if job else None

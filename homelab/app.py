from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import AppConfig, load_config
from .inventory import Inventory, build_providers
from .jobs import JobManager
from .models import CheckResult, Job, JobAction, Target
from .notify import Notifier
from .scheduler import Scheduler
from .security import LoginThrottle, make_guard
from .store import Store

log = logging.getLogger("homelab")
STATIC = Path(__file__).parent / "static"


class TargetView(BaseModel):
    target: Target
    check: CheckResult | None = None
    active_job: Job | None = None


class ActionRequest(BaseModel):
    action: JobAction
    target_ids: list[str] = Field(max_length=1000)
    # Upgrade only: reboot afterwards when the upgrade leaves a reboot pending.
    reboot_if_required: bool = False


def create_app(cfg: AppConfig | None = None, inventory: Inventory | None = None) -> FastAPI:
    cfg = cfg or load_config()
    if cfg.auth is None and cfg.has_real_targets and not cfg.allow_no_auth:
        raise SystemExit(
            "Refusing to start: no `auth:` in the config, so anyone who can reach the dashboard could update "
            "and reboot your machines. Add auth (see config.example.yaml), or set `allow_no_auth: true` if "
            "something in front of the dashboard already handles login."
        )
    inventory = inventory or Inventory(build_providers(cfg))
    store = _open_store(cfg.state_file)
    jobs = JobManager(inventory, cfg.max_parallel, store)
    notifier = Notifier(cfg.notify, cfg.public_url) if cfg.notify else None
    scheduler = Scheduler(cfg.schedule.check_every, inventory, jobs, notifier)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if cfg.auth is None:
            log.warning("No auth configured: anyone who can reach this dashboard can update your machines.")
            if not cfg.allowed_hosts:
                log.warning("allowed_hosts is not set: without auth, web pages can reach the API via DNS rebinding.")
        if notifier is not None and not scheduler.interval:
            log.warning("Notifications are set up but schedule.check_every isn't, so nothing will be sent.")
        scheduler.start()
        yield
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

    app = FastAPI(title="Homelab Manager", lifespan=lifespan)
    app.state.jobs = jobs
    app.state.inventory = inventory
    app.state.scheduler = scheduler

    app.middleware("http")(make_guard(cfg.auth, LoginThrottle()))
    if cfg.allowed_hosts:
        # Added last so it runs first: a request for a foreign Host never reaches the API.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=cfg.allowed_hosts)

    @app.get("/api/targets")
    async def list_targets(refresh: bool = False):
        await inventory.refresh(force=refresh)
        views = [
            TargetView(target=t, check=jobs.checks.get(t.id), active_job=_light(jobs.active_job(t.id)))
            for t, _ in inventory.targets.values()
        ]
        return {"targets": views, "errors": inventory.errors}

    @app.post("/api/actions")
    async def run_action(req: ActionRequest):
        await inventory.refresh()
        submitted, rejected = [], {}
        for tid in req.target_ids:
            try:
                submitted.append(_light(jobs.submit(tid, req.action, req.reboot_if_required)))
            except KeyError:
                rejected[tid] = "unknown target"
            except ValueError as e:
                rejected[tid] = str(e)
        return {"jobs": submitted, "rejected": rejected}

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
        errors = await notifier.send("Homelab Manager test", "Notifications from your homelab dashboard work.")
        return {"sent": len(cfg.notify) - len(errors), "errors": errors}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


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

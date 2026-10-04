from __future__ import annotations

import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import AppConfig, load_config
from .inventory import Inventory, build_providers
from .jobs import JobManager
from .models import CheckResult, Job, JobAction, Target
from .notify import Notifier
from .scheduler import Scheduler

log = logging.getLogger("homelab")
STATIC = Path(__file__).parent / "static"


class TargetView(BaseModel):
    target: Target
    check: CheckResult | None = None
    active_job: Job | None = None


class ActionRequest(BaseModel):
    action: JobAction
    target_ids: list[str]


def create_app(cfg: AppConfig | None = None, inventory: Inventory | None = None) -> FastAPI:
    cfg = cfg or load_config()
    inventory = inventory or Inventory(build_providers(cfg))
    jobs = JobManager(inventory, cfg.max_parallel)
    notifier = Notifier(cfg.notify, cfg.public_url) if cfg.notify else None
    scheduler = Scheduler(cfg.schedule.check_every, inventory, jobs, notifier)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if cfg.auth is None:
            log.warning("No auth configured: anyone who can reach this dashboard can update your machines.")
        if notifier is not None and not scheduler.interval:
            log.warning("Notifications are set up but schedule.check_every isn't, so nothing will be sent.")
        scheduler.start()
        yield
        await scheduler.stop()
        if notifier is not None:
            await notifier.aclose()
        for provider in inventory.providers:
            client = getattr(provider, "client", None)
            if client is not None:
                await client.aclose()

    app = FastAPI(title="Homelab Manager", lifespan=lifespan)
    app.state.jobs = jobs
    app.state.inventory = inventory
    app.state.scheduler = scheduler

    if cfg.auth:
        auth = cfg.auth

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if not _basic_auth_ok(request.headers.get("authorization", ""), auth.username, auth.password):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="homelab"'})
            return await call_next(request)

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
                submitted.append(_light(jobs.submit(tid, req.action)))
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


def _basic_auth_ok(header: str, username: str, password: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, sep, pwd = base64.b64decode(encoded.strip(), validate=True).partition(b":")
    except (binascii.Error, ValueError):
        return False
    # Compare both halves even when the first one fails so timing doesn't leak which was wrong.
    user_ok = secrets.compare_digest(user, username.encode())
    pwd_ok = secrets.compare_digest(pwd, password.encode())
    return bool(sep) and user_ok and pwd_ok


def _light(job: Job | None) -> Job | None:
    return job.model_copy(update={"log": []}) if job else None

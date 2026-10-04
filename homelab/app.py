from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import AppConfig, load_config
from .inventory import Inventory, build_providers
from .jobs import JobManager
from .models import CheckResult, Job, JobAction, Target

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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if cfg.auth is None:
            log.warning("No auth configured: anyone who can reach this dashboard can update your machines.")
        yield

    app = FastAPI(title="Homelab Manager", lifespan=lifespan)
    app.state.jobs = jobs
    app.state.inventory = inventory

    if cfg.auth:
        expected = base64.b64encode(f"{cfg.auth.username}:{cfg.auth.password}".encode()).decode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            header = request.headers.get("authorization", "")
            if not secrets.compare_digest(header, f"Basic {expected}"):
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
    async def list_jobs(limit: int = 50):
        return [_light(j) for j in list(jobs.jobs.values())[-limit:][::-1]]

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, since: int = 0):
        job = jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        data = job.model_dump(mode="json")
        data["log"] = job.log[since:]
        data["log_offset"] = since
        return data

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _light(job: Job | None) -> Job | None:
    return job.model_copy(update={"log": []}) if job else None

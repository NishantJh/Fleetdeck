"""Read cached status and, when opted in, explicitly request registry checks in Dockhand.

An empty pending list is not proof of a successful check. Dockhand does not expose
successful/failed checks for every container there; scheduled logs provide additional
explicit outcomes, with identity checks and persistent last-confirmed results.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field

from .config import DockhandConfig
from .store import Store


class ContainerStatus(BaseModel):
    container_id: str = ""
    name: str
    image: str
    state: str
    stack: str | None = None
    status: str = "unknown"
    checked_at: datetime | None = None
    updated_at: datetime | None = None
    source: str | None = None
    image_update: bool | None = None
    newer_version: str | None = Field(default=None, max_length=128)
    advisory: bool = False
    pinned_digest: bool = False
    deferred_hours: float | None = Field(default=None, ge=0, le=100000)
    check_issue: str | None = None
    reported_at: datetime | None = None
    advisory_at: datetime | None = None


class DockhandStatus(BaseModel):
    manager_url: str
    docker_version: str | None = Field(default=None, max_length=64)
    synced_at: datetime | None = None
    containers: list[ContainerStatus] = Field(default_factory=list)
    error: str | None = None
    stale: bool = True
    simulated: bool = False
    checking: bool = False
    last_check_at: datetime | None = None
    check_error: str | None = None
    scheduled_check_at: datetime | None = None
    scheduled_status: str | None = None
    scheduled_checked: int | None = None
    scheduled_errors: int | None = None
    history_error: str | None = None


class SavedDockhand(BaseModel):
    status: DockhandStatus
    checked: list[ContainerStatus]


def issue_code(error: object) -> str:
    # Return only fixed categories, never registry error text which can contain credentials.
    value = str(error).lower()
    if any(word in value for word in ("429", "rate limit", "toomanyrequests")):
        return "rate_limit"
    if any(word in value for word in ("401", "403", "unauthor", "denied", "authentication")):
        return "authentication"
    if "timeout" in value or "timed out" in value:
        return "timeout"
    return "registry"


def findings(item: ContainerStatus, row: dict, at: datetime | None, *, pending: bool = False) -> ContainerStatus:
    update = row.get("hasImageUpdate" if pending else "hasUpdate")
    item.image_update = update if type(update) is bool else None
    version = row.get("newerVersion")
    if isinstance(version, dict) and isinstance(version.get("tag"), str):
        item.newer_version = version["tag"][:128]
    item.pinned_digest = row.get("pinnedToDigest") is True
    hours = row.get("releaseAgeRemainingHours")
    if type(hours) in (int, float) and 0 < hours <= 100000:
        item.deferred_hours = float(hours)
    if item.status in {"excluded", "local", "failed", "unknown"}:
        item.image_update = None
    if item.pinned_digest or item.status == "deferred":
        item.image_update = False
    if item.status == "current":
        item.image_update = False
    if item.status == "failed":
        item.check_issue = issue_code(row.get("error"))
    if pending and item.status != "deferred" and row.get("hasImageUpdate") is True:
        item.deferred_hours = None
    item.advisory = bool(item.newer_version or item.pinned_digest or item.deferred_hours or item.status == "advisory")
    if item.advisory:
        item.advisory_at = at
    if item.image_update:
        item.reported_at = at
    return item


def retain_findings(item: ContainerStatus, old: ContainerStatus | None) -> ContainerStatus:
    if old and old.container_id == item.container_id and old.image == item.image:
        # A failed/partial latest check cannot erase a previously confirmed update or advisory.
        if item.status in {"failed", "unknown"}:
            item.image_update = (
                old.image_update
                if old.image_update is not None
                else (True if old.status == "update" else False if old.status == "current" else None)
            )
            fresh_review = item.advisory
            item.advisory = item.advisory or old.advisory or old.status in {"advisory", "deferred"}
            item.newer_version = item.newer_version or old.newer_version
            item.pinned_digest = item.pinned_digest or old.pinned_digest
            item.deferred_hours = item.deferred_hours if item.deferred_hours is not None else old.deferred_hours
            if not fresh_review:
                item.advisory_at = old.advisory_at or old.checked_at
            item.reported_at = old.reported_at or (old.checked_at if old.status == "update" else None)
    return item


def timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def scheduled_results(execution: dict, containers: list, environment_id: int) -> list[ContainerStatus]:
    """Recognise explicit, timestamped outcomes only; never infer success from absence.

    Dockhand v1.0.56 stores successful per-container checks in text logs. Container
    creation time, exact unique name and image guard against reusing a run for a replacement.
    Unknown formats, partial runs, duplicates and missing creation dates fail closed.
    """
    started, finished = timestamp(execution.get("startedAt")), timestamp(execution.get("completedAt"))
    if (
        type(execution.get("environmentId")) is not int
        or execution.get("environmentId") != environment_id
        or execution.get("scheduleType") != "env_update_check"
        or execution.get("status") not in {"success", "warning"}
        or not started
        or not finished
        or finished < started
        or not isinstance(execution.get("logs"), str)
    ):
        return []
    names: dict[str, list[dict]] = {}
    for container in containers:
        names.setdefault(container["name"], []).append(container)
    outcomes: dict[str, ContainerStatus] = {}
    seen = set()
    check_lines = 0
    current = None
    for line in execution["logs"].splitlines():
        match = re.fullmatch(r"\[([^]\n]+)\] (.*)", line)
        if not match:
            current = None
            continue
        at, message = timestamp(match[1]), match[2]
        if not at or not started <= at <= finished:
            current = None
            continue
        check = re.fullmatch(r"  Checking: (.+) \((.+)\)", message)
        if check:
            check_lines += 1
            current = None
            candidates = names.get(check[1], [])
            if len(candidates) != 1:
                continue
            container = candidates[0]
            created = container.get("created")
            labels = container.get("labels") or {}
            if (
                not isinstance(created, (int, float))
                or isinstance(created, bool)
                or not 0 < created <= started.timestamp()
                or container.get("image") != check[2]
                or str(labels.get("dockhand.update", labels.get("wud.watch", ""))).lower() in {"false", "no", "0"}
                or str(labels.get("dockhand.watch.digest", labels.get("wud.watch.digest", ""))).lower()
                in {"false", "no", "0"}
            ):
                continue
            if container["id"] in seen:
                outcomes.pop(container["id"], None)
                continue
            seen.add(container["id"])
            current = container
            continue
        outer_error = re.fullmatch(r"  \[(.+)\] Error:.*", message)
        if current is not None and outer_error and outer_error[1] == current["name"]:
            previous = outcomes.get(current["id"])
            outcomes[current["id"]] = retain_findings(
                ContainerStatus(
                    container_id=current["id"],
                    name=current["name"],
                    image=current["image"],
                    state=str(current.get("state", "unknown"))[:40],
                    status="failed",
                    checked_at=at,
                    source="dockhand_schedule",
                    check_issue=issue_code(message),
                ),
                previous,
            )
        if not message.startswith("    "):
            current = None
        if current is None:
            continue
        value = message.strip()
        status = None
        if value == "Up to date":
            previous = outcomes.get(current["id"])
            status = previous.status if previous else "current"
        elif value == "UPDATE AVAILABLE":
            status = "update"
        elif value.startswith("Error:"):
            status = "failed"
        elif value == "Local image - skipping update check":
            status = "local"
        elif value.startswith("Update deferred:"):
            status = "deferred"
        elif value.startswith(("NEWER VERSION:", "Reported only - the digest is pinned")):
            previous = outcomes.get(current["id"])
            status = previous.status if previous and previous.status in {"update", "deferred"} else "advisory"
        if status:
            previous = outcomes.get(current["id"])
            record = ContainerStatus(
                container_id=current["id"],
                name=current["name"],
                image=current["image"],
                state=str(current.get("state", "unknown"))[:40],
                status=status,
                checked_at=at,
                source="dockhand_schedule",
            )
            if previous:
                record = previous.model_copy(update={"status": status, "checked_at": at})
            if value == "UPDATE AVAILABLE":
                record.image_update, record.reported_at = True, at
            elif value == "Up to date" and not record.pinned_digest and record.image_update is not True:
                record.image_update = False
            elif value.startswith("NEWER VERSION:"):
                record.newer_version = value.split(":", 1)[1].strip().split(" ", 1)[0][:128]
            elif value.startswith("Reported only - the digest is pinned"):
                record.pinned_digest, record.image_update = True, False
            elif value.startswith("Update deferred:"):
                number = re.search(r"Update deferred: (\d+(?:\.\d+)?)", value)
                if number and 0 < float(number[1]) <= 100000:
                    record.deferred_hours = float(number[1])
                record.image_update = False
            if status == "failed":
                record.check_issue = issue_code(value)
            if value.startswith(("NEWER VERSION:", "Reported only - the digest is pinned", "Update deferred:")):
                record.advisory_at = at
            record.advisory = bool(
                record.newer_version
                or record.pinned_digest
                or record.deferred_hours
                or status in {"advisory", "deferred"}
            )
            outcomes[current["id"]] = record
    details = execution.get("details") or {}
    expected = details.get("containersChecked") if isinstance(details, dict) else None
    if type(expected) is not int or expected != check_lines:
        return []  # Truncated, duplicated or unexpected log structure cannot prove a healthy run.
    return list(outcomes.values())


def demo_status(url: str) -> DockhandStatus:
    """An in-memory fixture for a mapped demo machine; never an HTTP request."""
    now = datetime.now(UTC)
    result = DockhandStatus(
        manager_url=url,
        docker_version="28.5.1",
        synced_at=now,
        stale=False,
        simulated=True,
        scheduled_check_at=now,
        scheduled_status="success",
        scheduled_checked=3,
        scheduled_errors=0,
        containers=[
            ContainerStatus(
                name="web",
                image="example/web:1.0",
                state="running",
                stack="demo-app",
                image_update=True,
                newer_version="1.1",
                advisory=True,
                status="update",
                checked_at=now,
            ),
            ContainerStatus(
                name="worker",
                image="example/worker:latest",
                state="running",
                stack="demo-app",
                status="current",
                checked_at=now,
                source="dockhand_schedule",
            ),
            ContainerStatus(
                name="standalone",
                image="example/tool:latest",
                state="running",
                status="update",
                image_update=True,
                checked_at=now,
            ),
            ContainerStatus(
                name="database",
                image="example/database:16.2",
                state="running",
                stack="database",
                status="advisory",
                image_update=False,
                newer_version="16.15",
                advisory=True,
                checked_at=now,
                source="fleetdeck_button",
            ),
            ContainerStatus(
                name="fleetdeck",
                image="fleetdeck-local:ui-v1",
                state="running",
                stack="fleetdeck",
                status="local",
                checked_at=now,
            ),
            ContainerStatus(
                name="excluded", image="example/internal:1", state="running", stack="internal", status="excluded"
            ),
            ContainerStatus(name="replacement", image="example/replacement:latest", state="running", status="unknown"),
            ContainerStatus(
                name="waiting",
                image="example/waiting:latest",
                state="running",
                stack="waiting",
                status="deferred",
                image_update=False,
                deferred_hours=6,
                advisory=True,
                checked_at=now,
            ),
            ContainerStatus(
                name="pinned",
                image="example/pinned:2@sha256:demo",
                state="running",
                stack="pinned",
                status="advisory",
                image_update=False,
                pinned_digest=True,
                advisory=True,
                checked_at=now,
            ),
            ContainerStatus(
                name="rate-limited",
                image="example/limited:latest",
                state="running",
                stack="limited",
                status="failed",
                image_update=True,
                check_issue="rate_limit",
                reported_at=now,
                checked_at=now,
            ),
        ],
    )

    for item in result.containers:
        item.container_id = f"demo-{item.name}"
    result.containers.append(
        ContainerStatus(
            container_id="demo-stopped",
            name="stopped-service",
            image="example/stopped:latest",
            state="exited",
            status="update",
            image_update=True,
            checked_at=now,
        )
    )
    return result


def normalize(containers: object, pending: object) -> list[ContainerStatus]:
    if not isinstance(containers, list) or not isinstance(pending, dict):
        raise ValueError("Unsupported Dockhand response")
    updates = pending.get("pendingUpdates")
    if not isinstance(updates, list):
        raise ValueError("Unsupported Dockhand pending records")
    by_id = {r["containerId"]: r for r in updates if isinstance(r, dict) and isinstance(r.get("containerId"), str)}
    result = []
    for c in containers:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not isinstance(c.get("name"), str):
            raise ValueError("Unsupported Dockhand container record")
        labels = c.get("labels") or {}
        if not isinstance(labels, dict):
            raise ValueError("Unsupported Dockhand labels")
        if str(labels.get("dockhand.hidden", "")).lower() in {"true", "yes", "1"}:
            continue
        row = by_id.get(c["id"], {})
        if pending.get("minimumReleaseAgeHours") == 0:
            row = {**row, "releaseAgeRemainingHours": None}
        status = "unknown"
        if str(labels.get("dockhand.update", labels.get("wud.watch", ""))).lower() in {"false", "no", "0"}:
            status = "excluded"
        elif row.get("pinnedToDigest") is True:
            status = "advisory"
        elif (
            type(row.get("releaseAgeRemainingHours")) in (int, float)
            and row["releaseAgeRemainingHours"] > 0
            and pending.get("minimumReleaseAgeHours") != 0
            and row.get("hasImageUpdate") is not True
        ):
            status = "deferred"
        elif row.get("hasImageUpdate") is True:
            cooldown = pending.get("minimumReleaseAgeHours")
            held = (row.get("releaseAgeRemainingHours") or 0) > 0 and cooldown != 0
            status = "deferred" if held else "update"
        elif row.get("newerVersion"):
            status = "advisory"
        try:
            checked = (
                datetime.fromisoformat(str(row["checkedAt"]).replace("Z", "+00:00")) if row.get("checkedAt") else None
            )
            if checked and checked.tzinfo is None:
                checked = checked.replace(tzinfo=UTC)
        except ValueError:
            checked = None
        result.append(
            findings(
                ContainerStatus(
                    container_id=c["id"],
                    name=c["name"][:256],
                    image=str(c.get("image", ""))[:512],
                    state=str(c.get("state", "unknown"))[:40],
                    stack=str(labels["com.docker.compose.project"])[:256]
                    if labels.get("com.docker.compose.project")
                    else None,
                    status=status,
                    checked_at=checked,
                ),
                row,
                checked,
                pending=True,
            )
        )
    return sorted(result, key=lambda c: (c.stack or "", c.name))


class DockhandReader:
    def __init__(self, cfg: DockhandConfig, *, transport=None, store: Store | None = None):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.status: dict[str, DockhandStatus] = {}
        self.next_poll = 0.0
        self.lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.updating: set[str] = set()
        self.update_generation: dict[str, int] = {}
        self.last_started: dict[str, float] = {}
        self.checked: dict[str, dict[str, ContainerStatus]] = {}
        self.store = store
        for mapping in cfg.environments:
            saved = store.get_value(self._cache_key(mapping.target_id)) if store else None
            if saved:
                try:
                    cache = SavedDockhand.model_validate_json(saved)
                    for record in [*cache.status.containers, *cache.checked]:
                        if record.status == "updated" and record.source == "fleetdeck_update" and record.updated_at:
                            record.status = "current"
                            record.image_update = False
                    self.status[mapping.target_id] = cache.status.model_copy(update={"stale": True, "checking": False})
                    self.checked[mapping.target_id] = {c.container_id: c for c in cache.checked}
                except ValueError:
                    pass  # Corrupt saved state must not establish a successful check.

    def _cache_key(self, target_id: str) -> str:
        mapping = next(m for m in self.cfg.environments if m.target_id == target_id)
        scope = f"{self.cfg.url}\n{mapping.environment_id}\n{target_id}"
        return "dockhand:v1:" + hashlib.sha256(scope.encode()).hexdigest()

    def _save(self, target_id: str) -> None:
        if self.store:
            self.store.set_value(
                self._cache_key(target_id),
                SavedDockhand(
                    status=self.status[target_id], checked=list(self.checked.get(target_id, {}).values())
                ).model_dump_json(),
            )

    async def _history(self, environment_id: int, containers: list) -> tuple[dict, list[ContainerStatus]]:
        data = await self._read_json(
            "schedules/executions",
            {
                "environmentId": environment_id,
                "scheduleType": "env_update_check",
                "limit": 1,
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("executions"), list):
            raise ValueError("Unsupported history")
        if not data["executions"]:
            return {}, []
        row = data["executions"][0]
        if (
            not isinstance(row, dict)
            or type(row.get("environmentId")) is not int
            or row.get("environmentId") != environment_id
            or row.get("scheduleType") != "env_update_check"
            or type(row.get("id")) is not int
            or not 0 < row["id"] < 2**53
        ):
            raise ValueError("Unmapped history")
        details = row.get("details") or {}
        if not isinstance(details, dict):
            raise ValueError("Unsupported summary")

        def count(key):
            value = details.get(key)
            return value if type(value) is int and 0 <= value < 2**31 else None

        summary = {
            "scheduled_check_at": timestamp(row.get("completedAt")) or timestamp(row.get("startedAt")),
            "scheduled_status": row.get("status")
            if row.get("status") in {"success", "warning", "failed", "running", "queued", "skipped"}
            else "unknown",
            "scheduled_checked": count("containersChecked"),
            "scheduled_errors": count("errors"),
        }
        if row.get("status") not in {"success", "warning"}:
            return summary, []
        execution = await self._read_json(f"schedules/executions/{row['id']}", {})
        if not isinstance(execution, dict) or execution.get("id") != row["id"]:
            raise ValueError("Unmatched execution")
        return summary, scheduled_results(execution, containers, environment_id)

    def request_check(self, target_id: str, *, scheduled: bool = False) -> None:
        if not self.cfg.allow_checks:
            raise PermissionError("Enable dockhand.allow_checks in Settings to allow explicit Dockhand checks")
        mapping = next((m for m in self.cfg.environments if m.target_id == target_id), None)
        if mapping is None:
            raise KeyError(target_id)
        if (
            target_id in self.updating
            or target_id in self.tasks
            or time.monotonic() - self.last_started.get(target_id, -60) < 30
        ):
            raise ValueError("A Dockhand check is running or was started recently; wait before retrying")
        if len(self.tasks) >= 4:
            raise ValueError("Four Dockhand checks are already running; wait before starting another")
        previous = self.status.get(target_id, DockhandStatus(manager_url=self.cfg.url))
        self.status[target_id] = previous.model_copy(update={"checking": True, "check_error": None})
        self.last_started[target_id] = time.monotonic()
        self.tasks[target_id] = asyncio.create_task(self._check(target_id, mapping.environment_id, scheduled=scheduled))

    def _merge(self, target_id: str, items: list[ContainerStatus]) -> list[ContainerStatus]:
        known = self.checked.setdefault(target_id, {})
        identity = {"container_id", "name", "image", "state", "stack"}
        for index, item in enumerate(items):
            old = known.get(item.container_id)
            if old and old.image != item.image:
                old = None
            if old and old.source == "fleetdeck_update" and item.status != "unknown":
                known.pop(item.container_id, None)
                old = None  # Explicit provider findings supersede a deployment-only marker.
            if item.status == "excluded":
                known.pop(item.container_id, None)
                continue
            if old and (item.checked_at is None or (old.checked_at and old.checked_at >= item.checked_at)):
                record = item.model_copy(
                    update={key: getattr(old, key) for key in ContainerStatus.model_fields if key not in identity}
                )
                # An explicit positive record without a timestamp must not be erased by an older zero.
                if item.status == "update" and item.checked_at is None:
                    record.image_update = True
                    record.reported_at = None
                items[index] = record
            else:
                items[index] = retain_findings(item, old)
            if (
                items[index].checked_at is not None
                or items[index].status == "update"
                or items[index].updated_at is not None
            ):
                known[item.container_id] = items[index]
        return items

    async def _check(self, target_id: str, environment_id: int, *, scheduled: bool = False) -> None:
        try:
            # This endpoint checks registry metadata only; deployments live in dockhand_updates.py.
            async with asyncio.timeout(180):
                containers = await self._get("containers", environment_id)
                response = await self.client.post(
                    f"{self.cfg.url}/api/containers/check-updates",
                    params={"env": environment_id},
                    headers={"Accept": "application/json"},
                    timeout=180,
                )
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise ValueError("Oversized check response")
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ValueError("Dockhand did not return complete check results")
                results = {
                    r["containerId"]: r
                    for r in data["results"]
                    if isinstance(r, dict) and isinstance(r.get("containerId"), str)
                }
                items = normalize(containers, {"pendingUpdates": []})
                now = datetime.now(UTC)
                for item in items:
                    row = results.get(item.container_id)
                    if not row:
                        item.checked_at = now
                        item.check_issue = "missing_result"
                        item.source = "fleetdeck_schedule" if scheduled else "fleetdeck_button"
                        retain_findings(item, self.checked.get(target_id, {}).get(item.container_id))
                        continue
                    if row.get("error"):
                        item.status = "failed"
                    elif row.get("updateDisabled") is True or item.status == "excluded":
                        item.status = "excluded"
                    elif row.get("isLocalImage") is True:
                        item.status = "local"
                    elif row.get("pinnedToDigest") is True:
                        item.status = "advisory"
                    elif (row.get("releaseAgeRemainingHours") or 0) > 0:
                        item.status = "deferred"
                    elif row.get("hasUpdate") is True:
                        item.status = "update"
                    elif row.get("newerVersion"):
                        item.status = "advisory"
                    elif row.get("hasUpdate") is False:
                        item.status = "current"
                    else:
                        item.checked_at = now
                        item.check_issue = "missing_result"
                        item.source = "fleetdeck_schedule" if scheduled else "fleetdeck_button"
                        retain_findings(item, self.checked.get(target_id, {}).get(item.container_id))
                        continue  # incomplete records are never a successful check
                    item.checked_at = now
                    item.source = "fleetdeck_schedule" if scheduled else "fleetdeck_button"
                    findings(item, row, now)
                    retain_findings(item, self.checked.get(target_id, {}).get(item.container_id))
                if items and not any(item.status != "unknown" for item in items):
                    raise ValueError("No complete check results matched the current container inventory")
                self.checked[target_id] = {c.container_id: c for c in items if c.checked_at is not None}
                self.status[target_id] = DockhandStatus(
                    manager_url=self.cfg.url,
                    synced_at=now,
                    stale=False,
                    last_check_at=now,
                    containers=items,
                    scheduled_check_at=self.status[target_id].scheduled_check_at,
                    scheduled_status=self.status[target_id].scheduled_status,
                    scheduled_checked=self.status[target_id].scheduled_checked,
                    scheduled_errors=self.status[target_id].scheduled_errors,
                    history_error=self.status[target_id].history_error,
                )
                self._save(target_id)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError):
            previous = self.status[target_id]
            self.status[target_id] = previous.model_copy(
                update={
                    "checking": False,
                    "check_error": (
                        "Dockhand check did not complete. Check connection, token permissions and API version. "
                        "A timed-out check may still be running in Dockhand; previous results are retained."
                    ),
                }
            )
            self._save(target_id)
        finally:
            self.tasks.pop(target_id, None)

    async def refresh(self, *, force: bool = False) -> None:
        if not force and time.monotonic() < self.next_poll:
            return
        async with self.lock:
            if not force and time.monotonic() < self.next_poll:
                return
            # Limit network fan-out, including for malformed or unreachable configured instances.
            sem = asyncio.Semaphore(4)

            async def read(mapping):
                async with sem:
                    if mapping.target_id in self.tasks or mapping.target_id in self.updating:
                        return
                    generation = self.update_generation.get(mapping.target_id, 0)
                    previous = self.status.get(mapping.target_id, DockhandStatus(manager_url=self.cfg.url))
                    try:
                        containers = await self._get("containers", mapping.environment_id)
                        pending = await self._get("containers/pending-updates", mapping.environment_id)
                        # Some Dockhand versions return [] when the environment is unreachable.
                        if not containers:
                            raise ValueError("Empty inventory cannot establish environment health")
                        items = normalize(containers, pending)
                        docker_version = await self._docker_version(mapping.environment_id)
                        summary = {
                            key: getattr(previous, key)
                            for key in (
                                "scheduled_check_at",
                                "scheduled_status",
                                "scheduled_checked",
                                "scheduled_errors",
                            )
                        }
                        history_error = None
                        outcomes = []
                        try:
                            summary, outcomes = await self._history(mapping.environment_id, containers)
                        except (httpx.HTTPError, ValueError, TypeError, KeyError):
                            history_error = (
                                "Dockhand check history unavailable. Check read access to schedules "
                                "and API compatibility; container inventory is still synced."
                            )
                        if (
                            mapping.target_id in self.tasks
                            or mapping.target_id in self.updating
                            or generation != self.update_generation.get(mapping.target_id, 0)
                        ):
                            return
                        known = self.checked.setdefault(mapping.target_id, {})
                        for outcome in outcomes:
                            old = known.get(outcome.container_id)
                            if not old or not old.checked_at or outcome.checked_at > old.checked_at:
                                known[outcome.container_id] = retain_findings(outcome, old)
                        previous = self.status.get(mapping.target_id, previous)
                        self.status[mapping.target_id] = DockhandStatus(
                            manager_url=self.cfg.url,
                            synced_at=datetime.now(UTC),
                            docker_version=docker_version,
                            containers=self._merge(mapping.target_id, items),
                            stale=False,
                            last_check_at=previous.last_check_at,
                            check_error=previous.check_error,
                            history_error=history_error,
                            **summary,
                        )
                        # Drop retired/replaced records instead of growing the saved cache forever.
                        images = {item.container_id: item.image for item in items}
                        self.checked[mapping.target_id] = {
                            k: v for k, v in self.checked.get(mapping.target_id, {}).items() if images.get(k) == v.image
                        }
                        self._save(mapping.target_id)
                    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                        if mapping.target_id in self.updating or generation != self.update_generation.get(
                            mapping.target_id, 0
                        ):
                            return
                        # Never expose response bodies, URLs or exceptions that could contain credentials.
                        reason = "Dockhand status unavailable; check connection, permissions and API version."
                        if isinstance(exc, httpx.HTTPStatusError):
                            code = exc.response.status_code
                            reason = {
                                401: "Dockhand rejected authentication (HTTP 401). Check the Fleetdeck token.",
                                403: "Dockhand denied access (HTTP 403). Check token permissions for this environment.",
                                404: "Dockhand returned HTTP 404. Check its base URL, environment ID and API version.",
                            }.get(code, f"Dockhand returned HTTP {code}. Redirects are not followed.")
                        elif isinstance(exc, httpx.TimeoutException):
                            reason = "Dockhand connection timed out. Check reachability from the Fleetdeck container."
                        elif isinstance(exc, httpx.RequestError):
                            reason = "Could not connect to Dockhand. Check DNS, network access and its TLS certificate."
                        self.status[mapping.target_id] = previous.model_copy(
                            update={
                                "error": reason,
                                "stale": True,
                            }
                        )

            await asyncio.gather(*(read(m) for m in self.cfg.environments))
            self.next_poll = time.monotonic() + self.cfg.poll_seconds

    async def _docker_version(self, env: int) -> str | None:
        # Optional read-only metadata: older/restricted APIs must still supply container reports.
        try:
            data = await self._get("system", env)
            docker = data.get("docker") if isinstance(data, dict) else None
            if isinstance(docker, dict):
                value = docker.get("serverVersion") or docker.get("version")
                if isinstance(value, str) and re.fullmatch(r"[0-9][A-Za-z0-9.+~_-]{0,63}", value):
                    return value
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            pass
        return None

    async def _get(self, path: str, env: int):
        # Report reads use fixed internal paths; never forward browser-supplied routes.
        return await self._read_json(path, {"env": env})

    async def _read_json(self, path: str, params: dict):
        response = await self.client.get(f"{self.cfg.url}/api/{path}", params=params)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("Dockhand response is too large")
        return response.json()

    async def aclose(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()

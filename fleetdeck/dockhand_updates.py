"""Confirmed, opt-in Dockhand image deployments. No schedules or automatic retries."""

from __future__ import annotations

import re
import socket
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from .dockhand import ContainerStatus, normalize


class ContainerSelection(BaseModel):
    container_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    image: str = Field(min_length=1, max_length=512)
    state: Literal["running", "stopped", "exited", "created"]


class DockhandUpdateRequest(BaseModel):
    target_id: str = Field(min_length=1, max_length=512)
    confirmed: Literal[True]
    containers: list[ContainerSelection] = Field(min_length=1, max_length=100)


def excluded(row: ContainerStatus) -> str | None:
    own = socket.gethostname().lower()
    if (len(own) >= 12 and re.fullmatch(r"[a-f0-9]+", own) and row.container_id.startswith(own)) or re.search(
        r"(^|[/_.-])(dockhand|fleetdeck|homelab-manager)(?=$|[:@/_.-])", f"{row.name}/{row.image}".lower()
    ):
        return "Manager/dashboard self-update: use Dockhand directly"
    if row.state not in {"running", "stopped", "exited", "created"}:
        return "Container state requires manual review"
    if row.pinned_digest or row.newer_version or row.deferred_hours or row.advisory:
        return "Version, pinned digest or held update requires manual review"
    if row.status != "update" or row.image_update is not True or row.check_issue:
        return "No confirmed image update"
    at = row.checked_at
    if at is None or at.tzinfo is None or not timedelta(0) <= datetime.now(UTC) - at <= timedelta(hours=24):
        return "Request a fresh container check first"
    return None


def validate_selection(status, selections: list[ContainerSelection]) -> list[ContainerStatus]:
    if status is None or status.stale or status.error or status.checking or status.check_error:
        raise ValueError("Dockhand reports are unavailable or a check is running; refresh/check before updating")
    if len({s.container_id for s in selections}) != len(selections):
        raise ValueError("Select each container once")
    rows = {r.container_id: r for r in status.containers}
    selected = []
    for selection in selections:
        row = rows.get(selection.container_id)
        if row is None or row.name != selection.name or row.image != selection.image or row.state != selection.state:
            raise ValueError("Container identity changed; reopen the update confirmation")
        if reason := excluded(row):
            raise ValueError(reason)
        selected.append(row)
    return selected


async def install(reader, target_id: str, selections: list[ContainerSelection], guard, on_line) -> None:
    mapping = next(m for m in reader.cfg.environments if m.target_id == target_id)
    if not mapping.allow_updates:
        raise PermissionError("Install container updates is disabled for this machine")
    if target_id in reader.tasks or target_id in reader.updating:
        raise ValueError("A Dockhand operation is already running")
    reader.updating.add(target_id)
    sent = False
    try:
        guard()
        containers = await reader._get("containers", mapping.environment_id)
        pending = await reader._get("containers/pending-updates", mapping.environment_id)
        items = reader._merge(target_id, normalize(containers, pending))
        current = reader.status.get(target_id)
        if current is None:
            raise ValueError("Refresh Dockhand reports first")
        selected = validate_selection(current.model_copy(update={"containers": items}), selections)
        raw = {c["id"]: c for c in containers}
        for row in selected:
            labels = raw[row.container_id].get("labels") or {}
            if any(
                str(labels.get(k, "")).lower() in {"false", "no", "0"}
                for k in ("dockhand.update", "wud.watch", "dockhand.watch.digest", "wud.watch.digest")
            ) or re.search(
                r"dockhand|fleetdeck|homelab-manager", str(labels.get("com.docker.compose.service", "")), re.I
            ):
                raise ValueError("Container labels exclude this update; review it in Dockhand")
        guard()  # policy and machine state may have changed while reads were in flight
        on_line("stdout", f"== Asking Dockhand to update {len(selected)} selected container(s)")
        # Invalidate confirmed results BEFORE transmitting. Interrupted/uncertain requests never look healthy.
        for row in selected:
            reader.checked.get(target_id, {}).pop(row.container_id, None)
        invalid = [
            r.model_copy(
                update={
                    "status": "unknown",
                    "image_update": None,
                    "checked_at": None,
                    "check_issue": "missing_result",
                    "reported_at": None,
                }
            )
            if r.container_id in {s.container_id for s in selections}
            else r
            for r in current.containers
        ]
        reader.status[target_id] = current.model_copy(update={"containers": invalid})
        reader._save(target_id)
        reader.update_generation[target_id] = reader.update_generation.get(target_id, 0) + 1
        sent = True
        response = await reader.client.post(
            f"{reader.cfg.url}/api/containers/batch-update",
            params={"env": mapping.environment_id},
            json={"containerIds": [s.container_id for s in selections]},
            timeout=300,
        )
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("Unsupported Dockhand update response; inspect Dockhand before retrying")
        data = response.json()
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != len(selected):
            raise ValueError("Incomplete Dockhand update response; inspect Dockhand before retrying")
        # Dockhand recreates containers and returns NEW IDs, so match unique confirmed names.
        by_name = {r.get("containerName"): r for r in results if isinstance(r, dict)}
        if len(by_name) != len(selected) or set(by_name) != {r.name for r in selected}:
            raise ValueError("Unmatched Dockhand update response; inspect Dockhand before retrying")
        failed = False
        confirmed = {}
        for row in selected:
            result = by_name[row.name]
            ok = result.get("success") is True and not result.get("error") and not result.get("warnings")
            failed |= not ok
            new_id = result.get("containerId")
            if ok and isinstance(new_id, str) and new_id and new_id != row.container_id:
                confirmed[new_id] = row
            on_line("stdout", f"== {row.name}: {'updated' if ok else 'failed, skipped or warning; inspect Dockhand'}")
        # Reconcile the replacement identity without pretending an install is a registry check.
        if confirmed:
            try:
                fresh = normalize(await reader._get("containers", mapping.environment_id), {"pendingUpdates": []})
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                on_line("stdout", "== Updates confirmed; replacement inventory could not be synced yet")
            else:
                now = datetime.now(UTC)
                for item in fresh:
                    before = confirmed.get(item.container_id)
                    if before and item.name == before.name and item.image == before.image:
                        item.status = "current"
                        item.updated_at = now
                        item.source = "fleetdeck_update"
                        item.checked_at = None
                        item.image_update = False
                        item.check_issue = None
                        reader.checked.setdefault(target_id, {})[item.container_id] = item
                reader.status[target_id] = current.model_copy(
                    update={"containers": reader._merge(target_id, fresh), "synced_at": now}
                )
                reader._save(target_id)
        if failed:
            raise ValueError("Some containers were not confirmed updated; inspect Dockhand before retrying")
        on_line("stdout", "== Dockhand confirmed updates; replacement containers are up to date as of this update.")
    except (httpx.HTTPError, TimeoutError):
        raise ValueError(
            "Dockhand update response unavailable. It may still be running; inspect Dockhand before retrying."
        ) from None
    finally:
        reader.updating.discard(target_id)
        if sent:
            reader.next_poll = 0

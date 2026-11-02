"""One per-machine answer to "who manages this machine's containers": Fleetdeck, monitor only, an external
manager (Dockhand today), a link to another tool, or off. Never connects to an external manager.

The choice is a DockerSettings row in SQLite. The connection to an external manager (URL, token, environment
per machine) stays in config.yaml because it holds a secret and is read at start-up; saving writes both."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from .config import AppConfig, DockhandConfig
from .models import DockerSettings


@dataclass(frozen=True)
class ExternalManager:
    """A container manager Fleetdeck can talk to. Adding one means a registry entry, a config block with
    its connection (URL, token) and a reader; the per-machine choice is always DockerSettings."""

    label: str
    config_key: str


EXTERNAL_MANAGERS = {"dockhand": ExternalManager(label="Dockhand", config_key="dockhand")}


def external_settings(manager: str, url: str | None) -> DockerSettings:
    """The per-machine setting that says "this external manager runs this machine's containers"."""
    return DockerSettings(
        mode="external", manager=manager, manager_name=EXTERNAL_MANAGERS[manager].label, manager_url=url
    )


class DockerManagementRequest(BaseModel):
    target_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=64, max_length=64)
    mode: Literal["managed", "monitor", "dockhand", "external", "disabled"]
    url: str = Field("", max_length=2048)
    token: str | None = Field(None, max_length=4096, repr=False)
    poll_seconds: int = Field(300, ge=60, le=86400)
    allow_checks: bool = False
    allow_updates: bool = False
    environment_id: int | None = Field(None, ge=1)
    manager_name: str = Field("External manager", min_length=1, max_length=80)
    manager_url: str | None = Field(None, max_length=2048)


def update_dockhand_mapping(raw: dict, current: AppConfig, req: DockerManagementRequest) -> dict:
    """Preserve other settings, mappings and secret references; validate before the caller writes."""
    data = deepcopy(raw)
    dockhand = deepcopy(data.get("dockhand") or {})
    mappings = [m for m in dockhand.get("environments", []) if m.get("target_id") != req.target_id]
    if req.mode == "dockhand":
        if req.environment_id is None:
            raise ValueError("Enter the Dockhand environment ID for this machine")
        if any(m.get("environment_id") == req.environment_id for m in mappings):
            raise ValueError("This environment is already mapped to another machine")
        url = DockhandConfig._url(req.url)
        old = current.dockhand
        new_token = req.token if req.token else None
        if old and url != old.url and not new_token:
            raise ValueError("Enter a token when changing the Dockhand URL; the saved token is not sent to a new URL")
        if not old and not new_token:
            raise ValueError("Enter a Dockhand token for the new connection")
        if not old or url != old.url:
            dockhand["url"] = url
        if new_token:
            dockhand["token"] = new_token
        dockhand.update(poll_seconds=req.poll_seconds, allow_checks=req.allow_checks)
        mapping = {"target_id": req.target_id, "environment_id": req.environment_id}
        if req.allow_updates:
            mapping["allow_updates"] = True
        mappings.append(mapping)
        dockhand["environments"] = mappings
        data["dockhand"] = dockhand
    elif mappings:
        dockhand["environments"] = mappings
        data["dockhand"] = dockhand
    else:
        data.pop("dockhand", None)
    return data

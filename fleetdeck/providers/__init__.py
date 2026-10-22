"""Providers discover targets and hand out an executor for each one."""

from __future__ import annotations

import re
from typing import Protocol

from ..executors import Executor
from ..models import Target

Discovered = list[tuple[Target, Executor]]


def parse_tags(raw: str | None) -> list[str]:
    """Tags written as "a;b" (Proxmox) or "a,b" (the Incus user.tags setting)."""
    return [t for t in re.split(r"[;,\s]+", raw or "") if t]


class Provider(Protocol):
    name: str

    async def discover(self) -> Discovered: ...

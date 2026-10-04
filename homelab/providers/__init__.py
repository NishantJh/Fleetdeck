"""Providers discover targets and hand out an executor for each one."""

from __future__ import annotations

from typing import Protocol

from ..executors import Executor
from ..models import Target

Discovered = list[tuple[Target, Executor]]


class Provider(Protocol):
    name: str

    async def discover(self) -> Discovered: ...

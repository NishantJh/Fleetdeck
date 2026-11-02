"""Explicit guest starts through Proxmox; never force locks, resume or retry a start."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol
from urllib.parse import quote

import httpx


class GuestPower(Protocol):
    async def start(self) -> None: ...


class ProxmoxGuestPower:
    def __init__(self, client: httpx.AsyncClient, node: str, kind: str, vmid: int, timeout: float = 120):
        if kind not in ("qemu", "lxc"):
            raise ValueError("only Proxmox VMs and LXCs can be started")
        self.client, self.node, self.kind, self.vmid = client, node, kind, vmid
        self.timeout = timeout

    async def _request(self, method: str, path: str):
        response = await self.client.request(method, path)
        if response.status_code in (401, 403):
            raise RuntimeError("Proxmox denied access; check the token's VM.Audit and VM.PowerMgmt permissions")
        response.raise_for_status()
        return response.json()["data"]

    async def start(self) -> None:
        try:
            await self._start()
        except TimeoutError:
            raise TimeoutError("Boot status is uncertain; check Proxmox before trying again") from None

    async def _start(self) -> None:
        prefix = f"/nodes/{quote(self.node, safe='')}/{self.kind}/{self.vmid}"
        async with asyncio.timeout(self.timeout):
            status = await self._request("GET", prefix + "/status/current")
            if status.get("status") != "stopped":
                raise ValueError("Guest is no longer stopped; refresh inventory before trying again")
            # No skiplock, force, migration or alternative endpoint is ever requested.
            upid = await self._request("POST", prefix + "/status/start")
            if not isinstance(upid, str) or not upid.startswith("UPID:"):
                raise RuntimeError("Proxmox did not return a start task; check Proxmox before trying again")
            deadline = time.monotonic() + self.timeout
            while True:
                task = await self._request(
                    "GET", f"/nodes/{quote(self.node, safe='')}/tasks/{quote(upid, safe='')}/status"
                )
                if task.get("status") == "stopped":
                    if task.get("exitstatus") != "OK":
                        raise RuntimeError("Boot task failed; see the task log in Proxmox")
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Boot status is uncertain; check Proxmox before trying again")
                await asyncio.sleep(1)
            status = await self._request("GET", prefix + "/status/current")
            if status.get("status") != "running":
                raise RuntimeError("Proxmox finished the task but the guest is not running; check Proxmox")

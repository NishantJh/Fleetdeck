"""Snapshots of Proxmox and Incus guests, taken before an upgrade so it can be rolled back.

Only snapshots this dashboard created (names starting with ``PREFIX``) are ever pruned.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

import httpx

# Proxmox snapshot names: a letter first, then letters, digits, "-" and "_"; at most 40 characters.
PREFIX = "homelab_"


def snapshot_name() -> str:
    return PREFIX + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


class Snapshots(Protocol):
    async def create(self, name: str, description: str) -> None: ...
    async def rollback(self, name: str) -> None: ...
    async def list_ours(self) -> list[str]: ...
    async def delete(self, name: str) -> None: ...


async def prune(snaps: Snapshots, keep: int) -> list[str]:
    """Delete the oldest dashboard snapshots beyond ``keep``. Returns the names deleted."""
    ours = sorted(await snaps.list_ours())  # names sort by their timestamp
    doomed = ours[: max(len(ours) - keep, 0)]
    for name in doomed:
        await snaps.delete(name)
    return doomed


def _raise(r: httpx.Response, what: str) -> None:
    if r.status_code >= 400:
        raise RuntimeError(f"{what} failed ({r.status_code}): {r.text.strip()[:300]}")


class PveSnapshots:
    """`/nodes/{node}/{lxc|qemu}/{vmid}/snapshot` on the Proxmox API."""

    def __init__(self, client: httpx.AsyncClient, node: str, kind: str, vmid: int, task_timeout: float = 900):
        self.client = client
        self.node = node
        self.base = f"/nodes/{node}/{'lxc' if kind == 'lxc' else 'qemu'}/{vmid}/snapshot"
        self.task_timeout = task_timeout

    async def _task(self, r: httpx.Response, what: str) -> None:
        _raise(r, what)
        upid = r.json()["data"]
        deadline = time.monotonic() + self.task_timeout
        while True:
            s = await self.client.get(f"/nodes/{self.node}/tasks/{upid}/status")
            _raise(s, what)
            status = s.json()["data"]
            if status.get("status") == "stopped":
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"{what} did not finish in {self.task_timeout:.0f}s")
            await asyncio.sleep(1)
        if status.get("exitstatus") != "OK":
            raise RuntimeError(f"{what} failed: {status.get('exitstatus')}")

    async def create(self, name: str, description: str) -> None:
        r = await self.client.post(self.base, data={"snapname": name, "description": description})
        await self._task(r, "snapshot")

    async def rollback(self, name: str) -> None:
        # start=1 brings the guest back up afterwards (a snapshot without RAM state leaves it stopped).
        r = await self.client.post(f"{self.base}/{name}/rollback", data={"start": 1})
        await self._task(r, "rollback")

    async def list_ours(self) -> list[str]:
        r = await self.client.get(self.base)
        _raise(r, "listing snapshots")
        return [s["name"] for s in r.json()["data"] if s.get("name", "").startswith(PREFIX)]

    async def delete(self, name: str) -> None:
        r = await self.client.delete(f"{self.base}/{name}")
        await self._task(r, f"deleting snapshot {name}")


class IncusSnapshots:
    """`/1.0/instances/{name}/snapshots` on the Incus API."""

    def __init__(self, client: httpx.AsyncClient, name: str, project: str, op_timeout: int = 900):
        self.client = client
        self.base = f"/1.0/instances/{name}"
        self.params = {"project": project}
        self.op_timeout = op_timeout

    async def _wait(self, r: httpx.Response, what: str) -> None:
        _raise(r, what)
        op = r.json()["operation"]
        w = await self.client.get(
            f"{op}/wait", params={**self.params, "timeout": self.op_timeout}, timeout=self.op_timeout + 30
        )
        _raise(w, what)
        md = w.json()["metadata"]
        if md.get("status") != "Success":
            raise RuntimeError(f"{what} failed: {md.get('err') or md.get('status')}")

    async def create(self, name: str, description: str) -> None:
        r = await self.client.post(f"{self.base}/snapshots", params=self.params, json={"name": name, "stateful": False})
        await self._wait(r, "snapshot")

    async def rollback(self, name: str) -> None:
        # Incus stops and restarts a running instance around the restore.
        r = await self.client.put(self.base, params=self.params, json={"restore": name})
        await self._wait(r, "restore")

    async def list_ours(self) -> list[str]:
        r = await self.client.get(f"{self.base}/snapshots", params=self.params)
        _raise(r, "listing snapshots")
        names = [url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] for url in r.json()["metadata"]]
        return [n for n in names if n.startswith(PREFIX)]

    async def delete(self, name: str) -> None:
        r = await self.client.delete(f"{self.base}/snapshots/{name}", params=self.params)
        await self._wait(r, f"deleting snapshot {name}")

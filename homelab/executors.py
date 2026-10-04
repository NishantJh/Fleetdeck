"""Ways of running a shell script on a target.

Each executor takes a POSIX sh script, runs it as root on the target and returns
an :class:`ExecResult`. ``on_line(stream, line)`` is called for output lines as they
arrive (SSH streams live; the API-based executors report once the command ends).
"""

from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import Callable
from typing import Protocol

import asyncssh
import httpx

from .config import SSHConfig
from .models import ExecResult
from .snapshots import Snapshots

LineCallback = Callable[[str, str], None]
DEFAULT_TIMEOUT = 3600.0
# Fail fast on a host that is down instead of waiting for the OS TCP timeout.
CONNECT_TIMEOUT = 15.0


class Executor(Protocol):
    async def run(
        self, script: str, on_line: LineCallback | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> ExecResult: ...


def _emit(on_line: LineCallback | None, stream: str, text: str) -> None:
    if on_line:
        for line in text.splitlines():
            on_line(stream, line)


class SSHExecutor:
    def __init__(self, cfg: SSHConfig):
        self.cfg = cfg

    def _connect_kwargs(self) -> dict:
        kw: dict = {
            "host": self.cfg.host,
            "port": self.cfg.port,
            "username": self.cfg.user,
            # None disables host key checking; set known_hosts in config to enforce it.
            "known_hosts": self.cfg.known_hosts,
            "connect_timeout": CONNECT_TIMEOUT,
        }
        if self.cfg.key_file:
            kw["client_keys"] = [self.cfg.key_file]
        if self.cfg.password:
            kw["password"] = self.cfg.password
        return kw

    def _as_root(self, command: str) -> str:
        return f"sudo -n {command}" if self.cfg.sudo else command

    async def run_command(
        self, command: str, on_line: LineCallback | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> ExecResult:
        out: list[str] = []
        err: list[str] = []

        async def pump(stream, name: str, buf: list[str]) -> None:
            async for line in stream:
                buf.append(line)
                if on_line:
                    on_line(name, line.rstrip("\n"))

        async with asyncssh.connect(**self._connect_kwargs()) as conn:
            # Package output isn't always valid UTF-8; don't let one odd byte kill an upgrade log.
            proc = await conn.create_process(self._as_root(command), errors="replace")
            try:
                await asyncio.wait_for(
                    asyncio.gather(pump(proc.stdout, "stdout", out), pump(proc.stderr, "stderr", err)),
                    timeout,
                )
                await proc.wait()
            finally:
                proc.close()
            rc = proc.exit_status if proc.exit_status is not None else -1
        return ExecResult(rc=rc, stdout="".join(out), stderr="".join(err))

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        return await self.run_command(f"sh -c {shlex.quote(script)}", on_line, timeout)


class PctExecutor:
    """Runs inside a Proxmox LXC via `pct exec` on the node (the PVE API has no LXC exec)."""

    snapshots: Snapshots | None = None

    def __init__(self, node_ssh: SSHExecutor, vmid: int):
        self.node_ssh = node_ssh
        self.vmid = vmid

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        cmd = f"pct exec {self.vmid} -- sh -c {shlex.quote(script)}"
        return await self.node_ssh.run_command(cmd, on_line, timeout)


class PveAgentExecutor:
    """Runs inside a Proxmox VM through the QEMU guest agent."""

    snapshots: Snapshots | None = None

    def __init__(self, client: httpx.AsyncClient, node: str, vmid: int):
        self.client = client
        self.base = f"/nodes/{node}/qemu/{vmid}/agent"

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        r = await self.client.post(f"{self.base}/exec", data={"command": ["sh", "-c", script]})
        if r.status_code >= 400:
            raise RuntimeError(f"guest agent exec failed ({r.status_code}): {r.text.strip()}")
        pid = r.json()["data"]["pid"]
        deadline = time.monotonic() + timeout
        while True:
            s = await self.client.get(f"{self.base}/exec-status", params={"pid": pid})
            s.raise_for_status()
            data = s.json()["data"]
            if data.get("exited"):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"guest agent command still running after {timeout:.0f}s")
            await asyncio.sleep(2)
        stdout, stderr = data.get("out-data", ""), data.get("err-data", "")
        _emit(on_line, "stdout", stdout)
        _emit(on_line, "stderr", stderr)
        return ExecResult(rc=int(data.get("exitcode", -1)), stdout=stdout, stderr=stderr)


class IncusExecutor:
    """Runs inside an Incus container or VM via the REST exec API (VMs need incus-agent)."""

    snapshots: Snapshots | None = None

    def __init__(self, client: httpx.AsyncClient, name: str, project: str):
        self.client = client
        self.name = name
        self.params = {"project": project}

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        body = {
            "command": ["sh", "-c", script],
            "record-output": True,
            "interactive": False,
            "wait-for-websocket": False,
        }
        r = await self.client.post(f"/1.0/instances/{self.name}/exec", params=self.params, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"incus exec failed ({r.status_code}): {r.text.strip()}")
        op = r.json()["operation"]
        w = await self.client.get(f"{op}/wait", params={**self.params, "timeout": int(timeout)}, timeout=timeout + 30)
        w.raise_for_status()
        md = w.json()["metadata"]
        if md.get("status") != "Success":
            raise RuntimeError(f"incus exec: {md.get('err') or md.get('status')}")
        meta = md.get("metadata") or {}
        output = meta.get("output") or {}
        stdout = await self._fetch_log(output.get("1"))
        stderr = await self._fetch_log(output.get("2"))
        _emit(on_line, "stdout", stdout)
        _emit(on_line, "stderr", stderr)
        return ExecResult(rc=int(meta.get("return", -1)), stdout=stdout, stderr=stderr)

    async def _fetch_log(self, path: str | None) -> str:
        if not path:
            return ""
        r = await self.client.get(path, params=self.params)
        text = r.text if r.status_code < 400 else ""
        try:
            await self.client.delete(path, params=self.params)
        except httpx.HTTPError:
            pass
        return text

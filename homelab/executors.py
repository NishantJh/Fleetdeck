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

from .config import SSHConfig, unreadable
from .models import ExecResult

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
        if not self.cfg.known_hosts and not self.cfg.insecure_skip_host_key_check:
            raise RuntimeError(
                f"no known_hosts for {self.cfg.host}, so its SSH host key can't be checked. Add it to a "
                "known_hosts file (ssh-keyscan) and set known_hosts, or set insecure_skip_host_key_check: true"
            )
        for path in (self.cfg.key_file, self.cfg.known_hosts):
            if problem := unreadable(path):
                raise RuntimeError(problem)
        kw: dict = {
            "host": self.cfg.host,
            "port": self.cfg.port,
            "username": self.cfg.user,
            # None disables host key checking (only reachable with insecure_skip_host_key_check).
            "known_hosts": self.cfg.known_hosts or None,
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

    def __init__(self, node_ssh: SSHExecutor, vmid: int):
        self.node_ssh = node_ssh
        self.vmid = vmid

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        cmd = f"pct exec {self.vmid} -- sh -c {shlex.quote(script)}"
        return await self.node_ssh.run_command(cmd, on_line, timeout)


_AGENT_START = (
    "install and start it inside the VM (Debian/Ubuntu: apt install qemu-guest-agent && "
    "systemctl enable --now qemu-guest-agent). If you only just turned on the QEMU Guest Agent "
    "option, shut the VM down and start it again from Proxmox (a reboot from inside is not enough)"
)


def agent_error(r: httpx.Response) -> str:
    """Turn a failed guest agent API call into a message that says what to do."""
    try:
        msg = (r.json() or {}).get("message") or ""
    except ValueError:
        msg = ""
    # PVE puts most error text in the HTTP reason phrase, e.g. "500 QEMU guest agent is not running".
    msg = " ".join((msg or r.reason_phrase or r.text).split())
    low = msg.lower()
    if r.status_code == 401:
        return "Proxmox rejected the API token; reconnect this Proxmox host on the setup page"
    if r.status_code == 403 or "permission check failed" in low:
        return (
            "the Proxmox API token may not run commands through the guest agent "
            "(it needs VM.GuestAgent.Unrestricted on PVE 9, VM.Monitor on PVE 8); "
            f"run the Proxmox script from the setup page again. Proxmox said: {msg}"
        )
    if "not running" in low:
        return f"the QEMU guest agent is not running inside this VM: {_AGENT_START}"
    if "timeout" in low or r.status_code == 596:
        return (
            "the QEMU guest agent did not answer. Check `systemctl status qemu-guest-agent` inside "
            f"the VM; if it is not installed, {_AGENT_START}. Proxmox said: {msg}"
        )
    if "disabled" in low or "blocked" in low:
        return (
            "the guest agent in this VM refuses to run commands (guest-exec is turned off). Allow it "
            "in the agent's settings, e.g. remove guest-exec from BLOCK_RPCS in "
            f"/etc/sysconfig/qemu-ga on Fedora/RHEL. Proxmox said: {msg}"
        )
    return f"guest agent exec failed ({r.status_code}): {msg}"


class PveAgentExecutor:
    """Runs inside a Proxmox VM through the QEMU guest agent."""

    def __init__(self, client: httpx.AsyncClient, node: str, vmid: int):
        self.client = client
        self.base = f"/nodes/{node}/qemu/{vmid}/agent"

    async def run(self, script, on_line=None, timeout=DEFAULT_TIMEOUT) -> ExecResult:
        r = await self.client.post(f"{self.base}/exec", data={"command": ["sh", "-c", script]})
        if r.status_code >= 400:
            raise RuntimeError(agent_error(r))
        pid = r.json()["data"]["pid"]
        deadline = time.monotonic() + timeout
        while True:
            s = await self.client.get(f"{self.base}/exec-status", params={"pid": pid})
            if s.status_code >= 400:
                raise RuntimeError(agent_error(s))
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

"""Executor tests against an in-process SSH server and mocked Proxmox/Incus APIs."""

import asyncio
import json
import subprocess

import asyncssh
import httpx
import pytest

from homelab.config import SSHConfig
from homelab.executors import IncusExecutor, PctExecutor, PveAgentExecutor, SSHExecutor


async def _shell_process(process: asyncssh.SSHServerProcess) -> None:
    proc = await asyncio.create_subprocess_shell(
        process.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    process.stdout.write(out.decode())
    process.stderr.write(err.decode())
    process.exit(proc.returncode)


class _AllowAll(asyncssh.SSHServer):
    def begin_auth(self, username):
        return False  # no auth needed


@pytest.fixture
async def ssh_server(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen(
        "127.0.0.1", 0, server_host_keys=[key], server_factory=_AllowAll, process_factory=_shell_process
    )
    port = server.sockets[0].getsockname()[1]
    yield SSHConfig(host="127.0.0.1", port=port, user="tester")
    server.close()
    await server.wait_closed()


async def test_ssh_executor_streams_lines(ssh_server):
    lines = []
    res = await SSHExecutor(ssh_server).run(
        "echo one; echo two >&2; echo three; exit 7", lambda stream, line: lines.append((stream, line))
    )
    assert res.rc == 7
    assert res.stdout == "one\nthree\n"
    assert res.stderr == "two\n"
    assert ("stderr", "two") in lines and ("stdout", "three") in lines


async def test_ssh_executor_quotes_script(ssh_server):
    res = await SSHExecutor(ssh_server).run("""printf '%s\\n' "it's \\"quoted\\"" $((1+1))""")
    assert res.stdout == 'it\'s "quoted"\n2\n'


async def test_pct_executor_command(ssh_server):
    # No pct here, so check the command it would send to the node.
    ex = PctExecutor(SSHExecutor(ssh_server), 101)
    seen = {}

    async def fake(cmd, on_line=None, timeout=0):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    ex.node_ssh.run_command = fake
    await ex.run("echo hi")
    assert seen["cmd"] == "pct exec 101 -- sh -c 'echo hi'"


async def test_pve_agent_executor(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    polls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/agent/exec"):
            form = httpx.QueryParams(req.content.decode())
            assert form.get_list("command") == ["sh", "-c", "echo hi"]
            return httpx.Response(200, json={"data": {"pid": 42}})
        assert req.url.params["pid"] == "42"
        polls["n"] += 1
        if polls["n"] < 2:
            return httpx.Response(200, json={"data": {"exited": 0}})
        return httpx.Response(200, json={"data": {"exited": 1, "exitcode": 0, "out-data": "hi\n"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pve/api2/json")
    res = await PveAgentExecutor(client, "pve", 100).run("echo hi")
    assert (res.rc, res.stdout) == (0, "hi\n")
    assert polls["n"] == 2


async def test_incus_executor():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert req.url.params["project"] == "lab"
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["command"] == ["sh", "-c", "echo hi"] and body["record-output"] is True
            return httpx.Response(202, json={"operation": "/1.0/operations/abc"})
        if req.url.path == "/1.0/operations/abc/wait":
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "status": "Success",
                        "metadata": {
                            "return": 0,
                            "output": {
                                "1": "/1.0/instances/web/logs/exec-output/x.stdout",
                                "2": "/1.0/instances/web/logs/exec-output/x.stderr",
                            },
                        },
                    }
                },
            )
        if req.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(200, text="hi\n" if req.url.path.endswith("stdout") else "")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://incus")
    res = await IncusExecutor(client, "web", "lab").run("echo hi")
    assert (res.rc, res.stdout) == (0, "hi\n")
    assert ("DELETE", "/1.0/instances/web/logs/exec-output/x.stdout") in calls


_real_sleep = asyncio.sleep


async def _no_sleep(_):
    await _real_sleep(0)

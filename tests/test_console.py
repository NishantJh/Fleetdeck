import json
import time

import pytest
from starlette.requests import Request

from fleetdeck.config import AuthConfig, ConsoleConfig
from fleetdeck.console import Console, Run, Session, _wrap, clean, danger, is_secure, network_problem
from fleetdeck.security import hash_password
from fleetdeck.store import Store

ALLOW = ConsoleConfig().allow_from


def _request(ip: str, headers: dict | None = None, scheme: str = "http") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "client": (ip, 5000),
            "headers": raw,
            "scheme": scheme,
            "server": ("x", 80),
        }
    )


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /*",
        "sudo rm -fr ~",
        "rm -r -f $HOME",
        "mkfs.ext4 /dev/sdb1",
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "echo x > /dev/sda",
        "zpool destroy tank",
        "lvremove vg/lv",
        "reboot",
        "ls; shutdown -h now",
        "systemctl poweroff",
        "init 0",
        "pct destroy 101",
        "qm destroy 100",
        "incus delete web",
        "docker rm -f web",
        "docker system prune -a",
        "iptables -F",
        "ufw enable",
        "systemctl stop sshd",
        "passwd -l root",
        "rm /etc/sudoers.d/homelab-manager",
        ":(){ :|:& };:",
        "RM   -RF    /",
    ],
)
def test_guard_catches(command):
    assert danger(command)


@pytest.mark.parametrize(
    "command",
    [
        "ls -la /",
        "rm -rf /tmp/build",
        "rm file.txt",
        "df -h",
        "grep reboot /var/log/syslog",
        "journalctl -b",
        "docker ps -a",
    ],
)
def test_guard_leaves_normal_commands(command):
    assert danger(command) == []


def test_clean_strips_escape_and_control_characters():
    assert clean("a\x1b[31mred\x1b[0m\tb\x07\r\nc") == "a[31mred[0m\tb\nc"


def test_network_gate():
    assert network_problem(_request("192.168.1.20"), ALLOW) is None
    assert network_problem(_request("100.64.3.4"), ALLOW) is None  # Tailscale
    assert network_problem(_request("::1"), ALLOW) is None
    assert network_problem(_request("::ffff:10.1.2.3"), ALLOW) is None
    assert "not in console.allow_from" in network_problem(_request("8.8.8.8"), ALLOW)
    assert network_problem(_request("100.64.3.4"), ["10.0.0.0/8"]) is not None
    assert network_problem(_request("testclient"), ALLOW) is not None


def test_proxy_that_is_not_trusted_is_refused():
    # uvicorn rewrote the address to the real client (trusted proxy): the address is in X-Forwarded-For.
    assert network_problem(_request("192.168.1.20", {"x-forwarded-for": "192.168.1.20"}), ALLOW) is None
    # Not trusted: the address is the proxy's own (private), while the header names an outside visitor.
    why = network_problem(_request("172.18.0.2", {"x-forwarded-for": "203.0.113.9"}), ALLOW)
    assert why and "trusted_proxies" in why


def test_secure_connection():
    assert is_secure(_request("192.168.1.20", scheme="https"))
    assert is_secure(_request("127.0.0.1"))
    assert not is_secure(_request("192.168.1.20"))


def _console(**kw) -> Console:
    return Console(ConsoleConfig(**kw), AuthConfig(username="admin", password="pw"), None, None)


def test_unlock_expires_idle_and_is_tied_to_the_address():
    c = _console(unlock_minutes=1)
    token = c.unlock("10.0.0.5")
    assert c.session(token, "10.0.0.5")
    assert c.session(token, "10.0.0.6") is None  # another address
    assert c.session(token, "10.0.0.5") is None  # and that dropped it
    token = c.unlock("10.0.0.5")
    c.sessions[token].last -= 61
    assert c.session(token, "10.0.0.5") is None
    token = c.unlock("10.0.0.5")
    c.sessions[token].created -= 3601
    assert c.session(token, "10.0.0.5") is None


def test_password_check_plain_and_hashed():
    assert _console().password_ok("pw") and not _console().password_ok("nope")
    hashed = Console(ConsoleConfig(), AuthConfig(username="a", password_hash=hash_password("pw")), None, None)
    assert hashed.password_ok("pw") and not hashed.password_ok("PW")


def test_run_keeps_only_the_last_megabyte_and_offsets_stay_absolute():
    run = Run("r", "t", "t", None, "x", "u")
    for i in range(30):
        run.add(f"{i:04d}" + "x" * 100_000)
    assert run.size <= 1024 * 1024 and run.dropped > 0
    lines, nxt = run.since(0)
    assert nxt == 30 and len(lines) == 30 - run.dropped
    assert run.since(28) == (run.lines[-2:], 30)
    assert run.since(30) == ([], 30)


def test_wrap_quotes_the_command_and_logs_it():
    who = Session("10.0.0.5", "admin", 0, 0)
    script = _wrap("echo hi; ls", 30, who, None, False)
    assert script.startswith("logger -t fleetdeck-console 'admin@10.0.0.5: echo hi; ls'")
    assert "exec timeout -k 5 30 sh -c 'echo hi; ls'" in script
    assert "sh -c 'echo '\"'\"'x'\"'\"''" in _wrap("echo 'x'", 5, who, None, False)
    assert "exec timeout -k 5 5 docker exec -u 0 3f2a9c1d7b40 sh -c id" in _wrap("id", 5, who, "3f2a9c1d7b40", True)
    assert "docker exec 3f2a9c1d7b40 sh" in _wrap("id", 5, who, "3f2a9c1d7b40", False)


def test_audit_log_in_the_store_is_rewritten_in_place_and_pruned(tmp_path):
    store = Store(tmp_path / "s.db")
    c = Console(ConsoleConfig(keep_days=1), None, store, None)
    old = c.audit({"kind": "run", "command": "old", "at": "2020-01-01T00:00:00+00:00"})
    row_id = c.audit({"kind": "run", "command": "ls -l", "status": "running"})
    c.audit({"kind": "run", "command": "ls -l", "status": "done", "rc": 0}, row_id)  # same row, now finished
    c.audit({"kind": "unlock"})
    assert old != row_id
    rows = c.audit_rows(10)
    assert [r["kind"] for r in rows] == ["unlock", "run"]  # the 2020 row was pruned
    assert rows[1]["status"] == "done" and rows[1]["rc"] == 0
    assert [r["kind"] for r in c.audit_rows(10, "run")] == ["run"]
    assert json.loads(store.load_console_log(1)[0])["kind"] == "unlock"


def test_audit_log_in_memory_without_a_state_file():
    c = _console()
    rid = c.audit({"kind": "run", "status": "running"})
    c.audit({"kind": "run", "status": "done"}, rid)
    assert [r["status"] for r in c.audit_rows(5)] == ["done"]


async def test_run_records_output_status_and_audit():
    c = _console()
    seen = []

    async def runner(script, on_line, timeout):
        seen.append(timeout)
        on_line("stdout", "hello\x1b[31m")
        return 3

    class T:
        id, name = "t1", "pi"

    now = time.monotonic()
    run = c.start(
        T(),
        runner,
        command="false",
        timeout=10,
        container=None,
        container_id=None,
        as_root=False,
        who=Session("192.168.1.20", "admin", now, now),
        request=_request("192.168.1.20"),
        guarded=[],
    )
    assert c.busy_reason("t1")  # running now
    await run.task
    assert run.status == "failed" and run.rc == 3 and run.lines == ["hello[31m"]
    assert seen == [40]  # the machine's own `timeout` fires first
    row = c.audit_rows(1)[0]
    assert row["rc"] == 3 and row["http"] is True and row["output"] == "hello[31m"
    assert c.busy_reason("t1") is None

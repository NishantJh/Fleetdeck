import asyncio
import stat

import pytest
from fastapi.testclient import TestClient

from fleetdeck import detached
from fleetdeck.app import create_app
from fleetdeck.config import AppConfig
from fleetdeck.inventory import Inventory
from fleetdeck.jobs import JobManager
from fleetdeck.models import ExecResult, Stack, StackService, StacksResult, Target
from fleetdeck.stacks import (
    SCAN_SCRIPT,
    US,
    own_container_id,
    parse_pull_failures,
    parse_scan,
    pull_script,
    update_script,
)
from fleetdeck.store import Store

PATH_LINE = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
OWN = "a" * 64

# A stand-in for the docker command: answers the questions the scripts ask and logs the Compose calls.
DOCKER = r"""#!/bin/sh
d=$(dirname "$0")
MANIFEST=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
case "$1 $2" in
  "info "*|"compose version") exit 0 ;;
  "ps -a") echo abc; exit 0 ;;
  "image inspect")
    case "$*" in *--format*) echo sha256:new ;; *) echo "[{\"Manifests\":[{\"ID\":\"sha256:$MANIFEST\"}]}]" ;; esac
    exit 0 ;;
  "image prune") echo "prune $*" >> "$d/calls"; exit 0 ;;
esac
if [ "$1" = inspect ]; then
  case "$*" in *'{{.Config.Image}}'*'__C__'*|*__C__*) cat "$d/rows" ;; *) echo web:1 ;; esac
  exit 0
fi
if [ "$1" = compose ]; then
  echo "compose $PWD $*" >> "$d/calls"
  case "$*" in *" pull "*) [ -f "$d/pullfail" ] && { cat "$d/pullfail" >&2; exit 1; } ;; esac
  case "$*" in *"--ignore-buildable"*)
    [ -f "$d/oldcompose" ] && { echo "unknown flag: --ignore-buildable" >&2; exit 1; } ;; esac
  echo "ok"; exit 0
fi
exit 0
"""


def _row(project="web", service="app", wd="/tmp", files="/tmp/compose.yml", image="web:1", cid="b" * 64, **kw):
    fields = [
        "__C__",
        project,
        service,
        wd,
        files,
        image,
        kw.get("image_id", "sha256:old"),
        kw.get("status", "running"),
        cid,
    ]
    return US.join(fields)


def _scan(*rows, docker="yes", compose="v2", images=(("web:1", "sha256:new"),)):
    out = [f"__DOCKER__={docker}", f"__COMPOSE__={compose}", *rows]
    out += [US.join(["__I__", img, i, *known]) for img, i, *known in images]
    return ExecResult(rc=0, stdout="\n".join(out) + "\n")


def test_parse_scan_groups_services_and_flags_old_images():
    res = parse_scan(
        _scan(
            _row(service="app", image_id="sha256:old"),
            _row(service="db", image="db:1", image_id="sha256:same"),
            _row(project="other", service="x", image_id="sha256:new", status="exited"),
            images=(("web:1", "sha256:new"), ("db:1", "sha256:same")),
        )
    )
    web, other = res.stacks[1], res.stacks[0]
    assert (web.name, other.name) == ("web", "other")
    assert [(s.name, s.outdated) for s in web.services] == [("app", True), ("db", False)]
    assert web.update_available and web.state == "running"
    assert web.config_files == ["/tmp/compose.yml"] and web.working_dir == "/tmp"
    assert other.state == "stopped" and not other.update_available


def test_classic_store_compares_ids_only():
    res = parse_scan(_scan(_row(image_id="sha256:new"), _row(service="b", image_id="sha256:old")))
    assert [(s.name, s.outdated) for s in res.stacks[0].services] == [("app", False), ("b", True)]
    app = res.stacks[0].services[1]
    assert (app.image_id, app.tag_id) == ("sha256:old", "sha256:new")


def test_containerd_store_platform_manifest_counts_as_current():
    # The tag is the multi-platform index (Id 36c2...); the container reports the platform manifest (504a...),
    # which the tag's JSON lists.
    index, manifest, other = "sha256:" + "36" * 32, "sha256:" + "50" * 32, "sha256:" + "ab" * 32
    known = f"{manifest},{other}"
    images = (("web:1", index, known),)
    assert not parse_scan(_scan(_row(image_id=manifest), images=images)).stacks[0].update_available
    # Pulled a really different image: the container's ID is not among the tag's digests.
    old = "sha256:" + "77" * 32
    assert parse_scan(_scan(_row(image_id=old), images=images)).stacks[0].update_available


def test_unknown_tag_is_not_flagged():
    assert not parse_scan(_scan(_row(image_id="sha256:x"), images=())).stacks[0].update_available


def test_stopped_services_do_not_count_as_update_available():
    res = parse_scan(_scan(_row(image_id="sha256:old", status="exited")))
    assert res.stacks[0].state == "stopped" and not res.stacks[0].update_available


def test_parse_scan_marks_the_dashboards_own_stack_and_drops_odd_names():
    res = parse_scan(_scan(_row(project="fleetdeck", cid=OWN), _row(project="x; rm -rf /", cid="c" * 64)), OWN)
    assert [(s.name, s.is_dashboard) for s in res.stacks] == [("fleetdeck", True)]


def test_parse_scan_docker_missing_or_down():
    none = parse_scan(ExecResult(rc=0, stdout="__DOCKER__=none\n"))
    assert none.docker == "none" and "not installed" in none.error
    down = parse_scan(ExecResult(rc=0, stdout="__DOCKER__=down\n! permission denied\n"))
    assert "permission denied" in down.error and "running" in down.error


def test_own_container_id():
    line = f"1 2 3:4 /var/lib/docker/containers/{OWN} /etc/hostname rw - ext4 /dev/sda rw"
    assert own_container_id(line) == OWN
    assert own_container_id("1 2 3:4 / / rw - ext4 /dev/sda rw") is None


def _stack(**kw):
    kw.setdefault("services", [StackService(name="app", image="web:1", status="running")])
    return Stack(name="web", working_dir="/srv/web", config_files=["/srv/web/compose.yml"], **kw)


def test_update_script_quotes_and_only_touches_running_services():
    stack = _stack(
        services=[
            StackService(name="app", image="i", status="running"),
            StackService(name="off", image="i", status="exited"),
        ]
    )
    script = update_script(stack, prune=False)
    assert "pull app\n" in script and "up -d --no-deps app\n" in script and "off" not in script
    assert "prune" not in script and "prune" in update_script(stack, prune=True)
    tricky = Stack(
        name="web", working_dir="/srv/my dir", config_files=["/srv/a b.yml"],
        services=[StackService(name="app", image="i", status="running")],
    )  # fmt: skip
    assert "'/srv/my dir'" in update_script(tricky, False) and "'/srv/a b.yml'" in update_script(tricky, False)


class StubExecutor:
    """Runs the scripts in a local shell with the fake docker first on PATH, jobs folder in a temp dir."""

    detached = True

    def __init__(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        self.dir = tmp_path / "bin"
        self.dir.mkdir()
        (self.dir / "docker").write_text(DOCKER)
        (self.dir / "docker").chmod(0o755 | stat.S_IXUSR)
        self.base = str(tmp_path / "jobs")
        self.set_rows(_row(wd=str(tmp_path), files=str(tmp_path / "compose.yml")))
        (tmp_path / "compose.yml").write_text("services: {}\n")

    def set_rows(self, *rows):
        (self.dir / "rows").write_text("\n".join(rows) + "\n")

    @property
    def calls(self):
        f = self.dir / "calls"
        return f.read_text().splitlines() if f.exists() else []

    async def run(self, script, on_line=None, timeout=0):
        script = script.replace(PATH_LINE, f"export PATH={self.dir}:$PATH")
        script = script.replace(detached.JOBS_DIR, self.base)
        p = await asyncio.create_subprocess_exec("sh", "-c", script, stdout=-1, stderr=-1)
        out, err = await p.communicate()
        if on_line:
            for line in out.decode().splitlines():
                on_line("stdout", line)
        return ExecResult(rc=p.returncode, stdout=out.decode(), stderr=err.decode())


def _jobs(ex, store=None):
    t = Target(id="t1", name="t1", platform="ssh", source="hosts", kind="host")
    inv = Inventory([])
    inv.targets = {"t1": (t, ex)}
    return JobManager(inv, store=store)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(detached, "WINDOW", 4)
    real = detached.follow
    monkeypatch.setattr(detached, "follow", lambda *a, **k: real(*a, **{**k, "window": 4}))


async def _run(jobs, *a, **kw):
    job = jobs.submit("t1", *a, **kw)
    await asyncio.gather(*jobs._tasks)
    return job


async def test_scan_script_output_parses(tmp_path):
    ex = StubExecutor(tmp_path)
    res = parse_scan(await ex.run(SCAN_SCRIPT))
    assert res.docker == "yes" and res.compose == "v2"
    assert [(s.name, s.services[0].name, s.update_available) for s in res.stacks] == [("web", "app", True)]
    assert ("sha256:" + "c" * 64) in (await ex.run(SCAN_SCRIPT)).stdout  # the tag's manifests are collected


async def test_check_pulls_then_reports_and_update_recreates(tmp_path):
    ex = StubExecutor(tmp_path)
    jobs = _jobs(ex)
    job = await _run(jobs, "stacks")
    assert job.status == "succeeded", job.error
    assert jobs.stacks["t1"].stacks[0].update_available
    assert any(" pull --quiet" in c for c in ex.calls) and not any(" up " in c for c in ex.calls)
    assert any("update available (app)" in line for line in job.log)

    ex.set_rows(_row(wd=str(tmp_path), files=str(tmp_path / "compose.yml"), image_id="sha256:new"))
    up = await _run(jobs, "stack-update", stack="web", prune=True)
    assert up.status == "succeeded", up.error
    assert any(c.startswith(f"compose {tmp_path} ") and " up -d --no-deps app" in c for c in ex.calls)
    assert any(c.startswith("prune") for c in ex.calls)
    assert not jobs.stacks["t1"].stacks[0].update_available
    assert "== Stack is running the newest images" in up.log


async def test_older_compose_without_ignore_buildable_still_checks(tmp_path):
    ex = StubExecutor(tmp_path)
    (ex.dir / "oldcompose").write_text("")
    job = await _run(_jobs(ex), "stacks")
    assert job.status == "succeeded" and not any(line.startswith("!") for line in job.log)
    assert any(" pull --quiet app" in c and "--ignore-buildable" not in c for c in ex.calls)


async def test_pull_failure_is_kept_on_that_stack_and_does_not_fail_the_check(tmp_path):
    ex = StubExecutor(tmp_path)
    (ex.dir / "pullfail").write_text("pull access denied for web, repository does not exist")
    jobs = _jobs(ex)
    job = await _run(jobs, "stacks")
    assert job.status == "succeeded"
    assert "pull access denied" in jobs.stacks["t1"].stacks[0].error
    assert any(line.startswith("! web: could not fetch images") for line in job.log)


async def test_update_fails_clearly_when_the_folder_is_gone(tmp_path):
    ex = StubExecutor(tmp_path)
    jobs = _jobs(ex)
    await _run(jobs, "stacks")
    ex.set_rows(_row(wd=str(tmp_path / "gone"), files=str(tmp_path / "gone/compose.yml")))
    await _run(jobs, "stacks")  # the check also reports it, then the update refuses to start
    job = await _run(jobs, "stack-update", stack="web")
    assert job.status == "failed" and "exited with code 4" in job.error
    assert any("is gone" in line for line in job.log)
    assert not any(" up " in c for c in ex.calls)


async def test_update_refused_for_dashboard_stopped_and_unknown_stacks(tmp_path):
    jobs = _jobs(StubExecutor(tmp_path))
    fresh = _jobs(StubExecutor(tmp_path / "other"))
    jobs._set_stacks(
        "t1",
        StacksResult(
            checked_at="2026-10-12T00:00:00Z",
            stacks=[
                _stack(is_dashboard=True),
                Stack(name="off", services=[StackService(name="a", image="i", status="exited")]),
            ],
        ),
    )
    with pytest.raises(ValueError, match="runs this dashboard"):
        jobs.submit("t1", "stack-update", stack="web")
    with pytest.raises(ValueError, match="stopped"):
        jobs.submit("t1", "stack-update", stack="off")
    with pytest.raises(ValueError, match="check Docker"):
        jobs.submit("t1", "stack-update", stack="nope")
    with pytest.raises(ValueError, match="check Docker"):
        fresh.submit("t1", "stack-update", stack="web")


async def test_no_docker_is_not_an_error(tmp_path):
    ex = StubExecutor(tmp_path)

    async def run(script, on_line=None, timeout=0):
        return ExecResult(rc=0, stdout="__DOCKER__=none\n")

    ex.run = run
    jobs = _jobs(ex)
    job = await _run(jobs, "stacks")
    assert job.status == "succeeded" and jobs.stacks["t1"].docker == "none"


async def test_docker_not_answering_fails_the_check(tmp_path):
    ex = StubExecutor(tmp_path)

    async def run(script, on_line=None, timeout=0):
        return ExecResult(rc=0, stdout="__DOCKER__=down\n! permission denied\n")

    ex.run = run
    job = await _run(_jobs(ex), "stacks")
    assert job.status == "failed" and "permission denied" in job.error


async def test_same_stack_update_is_not_started_twice_but_another_stack_is(tmp_path):
    ex = StubExecutor(tmp_path)
    jobs = _jobs(ex)
    other = Stack(name="two", services=[StackService(name="a", image="i", status="running")])
    jobs._set_stacks("t1", StacksResult(checked_at="2026-10-12T00:00:00Z", stacks=[_stack(), other]))
    a = jobs.submit("t1", "stack-update", stack="web")
    assert jobs.submit("t1", "stack-update", stack="web") is a
    assert jobs.submit("t1", "stack-update", stack="two") is not a
    await asyncio.gather(*jobs._tasks)


def test_results_survive_a_restart(tmp_path):
    store = Store(tmp_path / "s.db")
    result = StacksResult(checked_at="2026-10-12T00:00:00Z", stacks=[_stack(error="boom")])
    store.save_stacks("t1", result)
    again = Store(tmp_path / "s.db").load_stacks()["t1"]
    assert again.stacks[0].error == "boom" and again.stacks[0].config_files == ["/srv/web/compose.yml"]


def test_pull_script_reports_failures_per_stack(tmp_path):
    assert parse_pull_failures(f"== Checking a\n__PULLFAIL__{US}a{US}denied\n") == {"a": "denied"}
    assert "echo '== Checking web'" in pull_script([_stack()])


def test_demo_docker_check_and_update_through_the_api():
    import time

    with TestClient(create_app(AppConfig(demo=True))) as client:
        views = client.get("/api/targets").json()["targets"]
        vm = next(v for v in views if v["target"]["name"] == "docker-vm")["target"]["id"]
        assert next(v for v in views if v["target"]["id"] == vm)["stacks"] is None
        client.post("/api/actions", json={"action": "stacks", "target_ids": [vm]})
        deadline = time.time() + 20
        while time.time() < deadline:
            stacks = next(v for v in client.get("/api/targets").json()["targets"] if v["target"]["id"] == vm)["stacks"]
            if stacks:
                break
            time.sleep(0.2)
        media = next(s for s in stacks["stacks"] if s["name"] == "media")
        assert media["update_available"] and media["state"] == "running"
        r = client.post("/api/actions", json={"action": "stack-update", "target_ids": [vm], "stack": "media"}).json()
        assert not r["rejected"] and r["jobs"][0]["stack"] == "media"
        while time.time() < deadline:
            if client.get("/api/jobs").json()[0]["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.2)
        stacks = next(v for v in client.get("/api/targets").json()["targets"] if v["target"]["id"] == vm)["stacks"]
        assert not next(s for s in stacks["stacks"] if s["name"] == "media")["update_available"]
        bad = client.post("/api/actions", json={"action": "stack-update", "target_ids": [vm], "stack": "zzz"}).json()
        assert "check Docker" in bad["rejected"][vm]


def test_a_hostile_working_dir_is_never_expanded_by_the_shell():
    from fleetdeck.stacks import _in_dir

    stack = Stack(name="x", working_dir="/x$(id>/tmp/p)`id`", services=[])
    script = _in_dir(stack)
    assert '"' not in script  # no double quotes, where $( ) and backticks would still run
    assert "echo 'its folder /x$(id>/tmp/p)`id` is gone'" in script


@pytest.mark.parametrize(
    "version, expected",
    [
        ("28.5.1", "28.5.1"),
        ("27.5.0+vendor.1", "27.5.0+vendor.1"),
        ("unknown", None),
        ("<script>bad</script>", None),
        ("1" * 65, None),
    ],
)
def test_scan_reports_engine_version_without_requiring_it(version, expected):
    result = parse_scan(ExecResult(rc=0, stdout=f"__DOCKER__=yes\n__DOCKER_VERSION__={version}\n__COMPOSE__=v2\n"))
    assert result.docker_version == expected
    assert not result.error
    assert not result.stacks


def test_parse_scan_reads_registry_digests():
    digest, other = "web@sha256:" + "a" * 64, "other@sha256:" + "b" * 64
    res = _scan(_row())
    res.stdout += US.join(["__R__", "web:1", f"{digest},{other}"]) + "\n"
    assert parse_scan(res).stacks[0].services[0].repo_digests == [digest, other]
    assert parse_scan(_scan(_row())).stacks[0].services[0].repo_digests == []


def _row_v2(cid="c1" * 32, project="web", service="app", image="web:1", name="/web-app", mode="bridge"):
    return US.join(
        ["__C__", project, service, "/tmp", "/tmp/compose.yml", image, "sha256:new", "running", cid, mode, name]
    )


def test_parse_scan_reads_ports_networks_and_proxy_labels():
    cid = "c1" * 32
    out = [
        _row_v2(cid=cid),
        US.join(["__P__", cid, '{"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}']),
        US.join(["__E__", cid, '{"80/tcp": {}}']),
        US.join(["__N__", cid, "bridge", "bridge=172.18.0.2;lan=192.168.1.5;"]),
        US.join(["__ND__", "bridge", "bridge"]),
        US.join(["__ND__", "lan", "macvlan"]),
        US.join(["__L__", cid, "caddy", "app.lan"]),
        US.join(["__L__", cid, "traefik.http.routers.app.rule", "Host(`app.example.com`) && PathPrefix(`/ui`)"]),
        US.join(["__L__", cid, "traefik.http.routers.app.tls", "true"]),
        US.join(["__L__", cid, "fleetdeck.url", "https://manual.lan"]),
        US.join(["__L__", cid, "fleetdeck.category", "Tools"]),
        US.join(["__L__", cid, "traefik.http.middlewares.app.basicauth.users", "user:hash"]),
    ]
    res = parse_scan(_scan(*out))
    service = res.stacks[0].services[0]
    assert [(p.host_port, p.container_port) for p in service.ports] == [(8080, 80)]
    assert service.exposed_ports == [80]
    assert [(n.name, n.driver) for n in service.networks] == [("bridge", "bridge"), ("lan", "macvlan")]
    assert [(r.kind, r.host, r.path, r.tls) for r in service.proxy] == [
        ("caddy", "app.lan", "/", True),
        ("traefik", "app.example.com", "/ui", True),
    ]
    assert service.explicit_urls == ["https://manual.lan"]
    assert service.category == "Tools"


def test_parse_scan_reads_standalone_containers_only_when_they_have_identity():
    cid = "d2" * 32
    out = [
        _row_v2(cid=cid, project="", service="", image="glances:latest", name="/glances", mode="host"),
        US.join(["__L__", cid, "fleetdeck.ui", "true"]),
        US.join(["__E__", cid, '{"61208/tcp": {}}']),
    ]
    res = parse_scan(_scan(*out))
    assert not res.stacks
    assert [(s.name, s.network_mode, s.exposed_ports, s.ui) for s in res.standalone] == [
        ("glances", "host", [61208], True)
    ]


def test_hostile_label_values_are_dropped():
    cid = "e3" * 32
    out = [
        _row_v2(cid=cid),
        US.join(["__L__", cid, "fleetdeck.url", "javascript:alert(1)"]),
        US.join(["__L__", cid, "caddy", "*.lan"]),
        US.join(["__L__", cid, "traefik.http.routers.app.rule", "HostRegexp(`{sub}.lan`)"]),
        US.join(["__L__", cid, "fleetdeck.category", "bad category!<script>"]),
    ]
    res = parse_scan(_scan(*out))
    service = res.stacks[0].services[0]
    assert service.explicit_urls == []
    assert service.proxy == []
    assert service.category == ""


def test_parse_caddyfile_sites_and_upstreams():
    from fleetdeck.stacks import _parse_caddyfile

    routes = _parse_caddyfile(
        [
            "app.lan {",
            "  reverse_proxy 172.18.0.5:80",
            "}",
            "http://plain.lan, www.plain.lan {",
            "  handle {",
            "    reverse_proxy app:3000",
            "  }",
            "}",
            "*.wild.lan {",
            "  reverse_proxy app:3000",
            "}",
        ]
    )
    assert [(r.host, r.tls, r.upstream) for r in routes] == [
        ("app.lan", True, "172.18.0.5:80"),
        ("plain.lan", False, "app:3000"),
        ("www.plain.lan", False, "app:3000"),
    ]


def test_parse_scan_reads_caddy_routes():
    cid = "f4" * 32
    out = [
        _row_v2(cid=cid),
        US.join(["__CF__", cid, "app.lan {"]),
        US.join(["__CF__", cid, "  reverse_proxy 172.18.0.5:80"]),
        US.join(["__CF__", cid, "}"]),
    ]
    res = parse_scan(_scan(*out))
    assert [(r.host, r.upstream) for r in res.routes] == [("app.lan", "172.18.0.5:80")]


def test_scan_reads_the_local_image_platform():
    from fleetdeck.models import ExecResult
    from fleetdeck.stacks import US, parse_scan

    row = US.join(["__C__", "p", "web", "/w", "", "caddy:2", "sha256:aa", "running", "c" * 64, "bridge", "/p-web-1"])
    img = US.join(["__I__", "caddy:2", "sha256:aa", "", "linux/arm64/v8"])
    res = parse_scan(ExecResult(rc=0, stdout=f"{row}\n{img}\n", stderr=""))
    assert res.stacks[0].services[0].platform == "linux/arm64/v8"

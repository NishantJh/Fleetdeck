import asyncio

import httpx
import pytest

from fleetdeck import registry
from fleetdeck.inventory import Inventory
from fleetdeck.jobs import JobManager
from fleetdeck.models import ExecResult, Target
from fleetdeck.registry import ACCEPT, RegistryClient, Verdict, parse_ref
from fleetdeck.stacks import SCAN_SCRIPT, US

D1, D2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(registry.asyncio, "sleep", fake_sleep)
    return sleeps


def _client(handler):
    return RegistryClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("nginx", ("registry-1.docker.io", "library/nginx", "latest", None)),
        ("nginx:1.27", ("registry-1.docker.io", "library/nginx", "1.27", None)),
        ("linuxserver/sonarr:4", ("registry-1.docker.io", "linuxserver/sonarr", "4", None)),
        ("docker.io/library/redis:7", ("registry-1.docker.io", "library/redis", "7", None)),
        ("ghcr.io/acme/web:v1", ("ghcr.io", "acme/web", "v1", None)),
        ("localhost:5000/app", ("localhost:5000", "app", "latest", None)),
        ("ghcr.io/acme/web:v1@" + D1, ("ghcr.io", "acme/web", "v1", D1)),
    ],
)
def test_parse_ref(image, expected):
    assert parse_ref(image) == expected


async def test_head_with_accept_header_and_digest_comparison():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"Docker-Content-Digest": D1})

    c = _client(handler)
    assert await c.check("ghcr.io/acme/web:v1", [f"ghcr.io/acme/web@{D1}"]) == Verdict("current")
    assert seen[0].method == "HEAD" and str(seen[0].url) == "https://ghcr.io/v2/acme/web/manifests/v1"
    assert seen[0].headers["accept"] == ACCEPT and "manifest.list" in ACCEPT and "oci.image.index" in ACCEPT
    # The tag moved: the machine's digest is no longer the registry's.
    assert (await c.check("ghcr.io/acme/web:v2", [f"ghcr.io/acme/web@{D2}"])).status == "update"


async def test_manifest_list_digest_is_matched_among_several_repo_digests():
    c = _client(lambda r: httpx.Response(200, headers={"Docker-Content-Digest": D2}))
    v = await c.check("ghcr.io/a/b:1", [f"mirror.example/a/b@{D1}", f"ghcr.io/a/b@{D2}"])
    assert v.status == "current"


async def test_anonymous_token_flow_is_cached_per_scope():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "auth.example":
            return httpx.Response(200, json={"token": "tok", "expires_in": 300})
        if request.headers.get("authorization") != "Bearer tok":
            challenge = 'Bearer realm="https://auth.example/token",service="reg.example",scope="repository:a/b:pull"'
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        return httpx.Response(200, headers={"Docker-Content-Digest": D1})

    c = _client(handler)
    assert (await c.check("reg.example/a/b:1", [f"reg.example/a/b@{D1}"])).status == "current"
    token_calls = [u for u in calls if "auth.example" in u]
    assert token_calls == ["https://auth.example/token?service=reg.example&scope=repository%3Aa%2Fb%3Apull"]
    assert (await c.check("reg.example/a/b:2", [f"reg.example/a/b@{D1}"])).status == "current"  # same scope
    assert len([u for u in calls if "auth.example" in u]) == 1


async def test_docker_hub_official_image_gets_library_prefix():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, headers={"Docker-Content-Digest": D1})

    await _client(handler).check("nginx:1.27", [f"nginx@{D1}"])
    assert urls == ["https://registry-1.docker.io/v2/library/nginx/manifests/1.27"]


async def test_429_and_5xx_are_retried_with_backoff(no_waiting):
    answers = iter([429, 503, 200])

    def handler(request):
        code = next(answers)
        return httpx.Response(code, headers={"Docker-Content-Digest": D1} if code == 200 else {})

    assert (await _client(handler).check("ghcr.io/a/b:1", [f"x@{D1}"])).status == "current"
    assert no_waiting == [1.0, 5.0]


async def test_gives_up_after_the_last_retry_as_unknown(no_waiting):
    v = await _client(lambda r: httpx.Response(429)).check("ghcr.io/a/b:1", [f"x@{D1}"])
    assert v.status == "unknown" and v.reason == "error" and v.can_pull_instead
    assert no_waiting == [1.0, 5.0, 15.0]


async def test_never_says_update_when_it_cannot_know():
    def private(request):
        challenge = 'Bearer realm="https://auth.example/t",service="s"'
        if request.url.host == "auth.example":
            return httpx.Response(401)
        return httpx.Response(401, headers={"WWW-Authenticate": challenge})

    c = _client(private)
    v = await c.check("ghcr.io/me/secret:1", [f"ghcr.io/me/secret@{D1}"])
    assert (v.status, v.reason, v.can_pull_instead) == ("unknown", "auth", True)
    assert (await _client(lambda r: httpx.Response(404)).check("ghcr.io/a/b:1", [f"x@{D1}"])).reason == "auth"
    assert (await _client(lambda r: httpx.Response(200)).check("ghcr.io/a/b:1", [f"x@{D1}"])).reason == "error"

    def boom(request):
        raise httpx.ConnectError("no route")

    assert (await _client(boom).check("ghcr.io/a/b:1", [f"x@{D1}"])).reason == "error"


async def test_pinned_and_local_images_are_unknown_without_a_request():
    def handler(request):
        raise AssertionError("must not call the registry")

    c = _client(handler)
    pinned = await c.check(f"ghcr.io/a/b@{D1}", [f"ghcr.io/a/b@{D1}"])
    assert (pinned.status, pinned.reason, pinned.can_pull_instead) == ("unknown", "pinned", False)
    local = await c.check("myapp:dev", [])
    assert (local.status, local.reason, local.can_pull_instead) == ("unknown", "local", False)


async def test_results_are_reused_between_machines():
    n = []

    def handler(request):
        n.append(1)
        return httpx.Response(200, headers={"Docker-Content-Digest": D1})

    c = _client(handler)
    await c.check("ghcr.io/a/b:1", [f"x@{D1}"])
    await c.check("ghcr.io/a/b:1", [f"x@{D2}"])
    assert len(n) == 1


# The Docker check no longer pulls where a registry answers.


class Executor:
    """Answers the scan script with fixed rows and records every other script."""

    detached = False

    def __init__(self, images):
        self.images, self.scripts = images, []

    async def run(self, script, on_line=None, timeout=0):
        if script == SCAN_SCRIPT:
            out = ["__DOCKER__=yes", "__COMPOSE__=v2"]
            for i, (image, digests) in enumerate(self.images.items()):
                row = ["__C__", f"stack{i}", "svc", "/srv", "/srv/c.yml", image, "sha256:cc", "running", "c" * 64]
                out += [US.join(row), US.join(["__I__", image, "sha256:cc", "sha256:cc"])]
                out.append(US.join(["__R__", image, ",".join(digests)]))
            return ExecResult(rc=0, stdout="\n".join(out) + "\n")
        self.scripts.append(script)
        return ExecResult(rc=0)


class FakeRegistry:
    def __init__(self, verdicts):
        self.verdicts, self.asked = verdicts, []

    async def check(self, image, digests):
        self.asked.append(image)
        return self.verdicts[image]


async def _check(images, verdicts):
    ex = Executor(images)
    inv = Inventory([])
    inv.targets = {"t1": (Target(id="t1", name="t1", platform="ssh", source="hosts", kind="host"), ex)}
    jobs = JobManager(inv)
    jobs.registry = FakeRegistry(verdicts)
    job = jobs.submit("t1", "stacks")
    await asyncio.gather(*jobs._tasks)
    return job, jobs, ex


async def test_check_compares_digests_and_does_not_pull():
    job, jobs, ex = await _check(
        {"a:1": [f"a@{D1}"], "b:1": [f"b@{D1}"], "c:1": [f"c@{D1}"]},
        {
            "a:1": Verdict("current"),
            "b:1": Verdict("update"),
            "c:1": Verdict("unknown", "pinned", "pinned by digest"),
        },
    )
    assert job.status == "succeeded" and ex.scripts == []  # nothing was pulled
    by = {s.services[0].image: s for s in jobs.stacks["t1"].stacks}
    assert not by["a:1"].update_available and by["b:1"].update_available
    assert by["b:1"].services[0].registry == "update"
    c = by["c:1"].services[0]
    assert not by["c:1"].update_available and (c.registry, c.registry_note) == ("unknown", "pinned by digest")


async def test_unreachable_registry_falls_back_to_pulling_only_that_stack():
    job, jobs, ex = await _check(
        {"a:1": [f"a@{D1}"], "b:1": [f"b@{D1}"]},
        {"a:1": Verdict("current"), "b:1": Verdict("unknown", "error", "could not reach the registry")},
    )
    assert job.status == "succeeded"
    assert len(ex.scripts) == 1 and "stack1" in ex.scripts[0] and "stack0" not in ex.scripts[0]


async def test_without_a_registry_client_the_check_still_pulls():
    ex = Executor({"a:1": [f"a@{D1}"]})
    inv = Inventory([])
    inv.targets = {"t1": (Target(id="t1", name="t1", platform="ssh", source="hosts", kind="host"), ex)}
    jobs = JobManager(inv)
    assert jobs.registry is None
    jobs.submit("t1", "stacks")
    await asyncio.gather(*jobs._tasks)
    assert len(ex.scripts) == 1 and "pull --quiet" in ex.scripts[0]


def test_scheduled_docker_checks_are_on_by_default():
    from fleetdeck.config import ScheduleConfig

    assert ScheduleConfig().check_docker is True

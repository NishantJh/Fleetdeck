"""Asks image registries whether a tag now points at something other than what a machine has, without pulling.

Runs inside the dashboard's container, never on a machine. For ``registry/repo:tag`` it sends
``HEAD /v2/<repo>/manifests/<tag>`` and reads ``Docker-Content-Digest``, which is the digest of the manifest
list (or manifest) that ``docker pull`` would store in the image's RepoDigests. If that digest is among the
machine's RepoDigests the image is current, otherwise a newer one exists.

Only answers it can stand behind: images pinned by digest, images built on the machine (no RepoDigests),
registries that need a login and network errors are "unknown", never "update". The caller then falls back to
pulling where that makes sense.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
DOCKER_HUB = "registry-1.docker.io"
_HUB_NAMES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
TIMEOUT = 15.0
# Waits before the 2nd, 3rd and 4th try of a request that got 429 or a 5xx.
RETRY_DELAYS = (1.0, 5.0, 15.0)
RESULT_TTL = 300.0  # the same tag asked about by several machines is looked up once
MAX_PARALLEL = 8
_PARAMS = re.compile(r'(\w+)="([^"]*)"')
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

Reason = Literal["", "pinned", "local", "auth", "error"]


@dataclass(frozen=True)
class Verdict:
    status: Literal["current", "update", "unknown"]
    # Why it is unknown: pinned (by digest), local (no registry digest), auth (private or not found),
    # error (network, rate limit, odd answer).
    reason: Reason = ""
    note: str = ""

    @property
    def can_pull_instead(self) -> bool:
        """Pulling on the machine may settle it (it has the machine's own registry login and network)."""
        return self.status == "unknown" and self.reason in ("auth", "error")


class RegistryError(Exception):
    def __init__(self, reason: Reason, note: str):
        super().__init__(note)
        self.reason, self.note = reason, note


def parse_ref(image: str) -> tuple[str, str, str | None, str | None]:
    """(registry host, repository, tag, pinned digest) of an image reference. Docker Hub gets ``library/`` for
    official images. No tag means ``latest``."""
    ref, _, pinned = image.partition("@")
    first, sep, rest = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        host, path = first, rest
    else:
        host, path = "docker.io", ref
    name, tag = path, "latest"
    last = path.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = path.rsplit(":", 1)
    if host in _HUB_NAMES:
        host = DOCKER_HUB
        if "/" not in name:
            name = f"library/{name}"
    return host, name, tag, pinned or None


class RegistryClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
        self._tokens: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._results: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._manifests: dict[tuple[str, str, str], tuple[set[str], float]] = {}
        self._sem = asyncio.Semaphore(MAX_PARALLEL)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def check(self, image: str, repo_digests: list[str]) -> Verdict:
        """Is the tag of ``image`` still what the machine has (``repo_digests``: its RepoDigests)?"""
        host, repo, tag, pinned = parse_ref(image)
        if pinned:
            return Verdict("unknown", "pinned", "pinned by digest: the tag is not followed")
        local = {d.partition("@")[2] for d in repo_digests if "@" in d}
        if not local:
            return Verdict("unknown", "local", "built on the machine or not from a registry: no digest to compare")
        try:
            remote = await self.remote_digest(host, repo, tag)
        except RegistryError as e:
            return Verdict("unknown", e.reason, e.note)
        if remote in local:
            return Verdict("current")
        # A multi-platform tag's HEAD digest is the index. Some image stores put only the platform
        # manifest digest in RepoDigests, which made every current multi-platform image look behind.
        if local & await self.platform_digests(host, repo, tag):
            return Verdict("current")
        return Verdict("update")

    async def platform_digests(self, host: str, repo: str, tag: str) -> set[str]:
        """The platform manifest digests listed by a multi-platform tag; empty for a single manifest."""
        key = (host, repo, tag)
        cached = self._manifests.get(key)
        if cached and time.monotonic() - cached[1] < RESULT_TTL:
            return cached[0]
        try:
            digests = await self._manifest_digests(host, repo, tag)
        except RegistryError:
            return set()
        self._manifests[key] = (digests, time.monotonic())
        return digests

    async def _manifest_digests(self, host: str, repo: str, tag: str) -> set[str]:
        url = f"https://{host}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": ACCEPT}
        r = await self._request(url, headers, "GET")
        if r.status_code == 401:
            token = await self._token(host, repo, r.headers.get("www-authenticate", ""))
            r = await self._request(url, {**headers, "Authorization": f"Bearer {token}"}, "GET")
        if r.status_code != 200 or len(r.content) > 2_000_000:
            return set()
        try:
            data = r.json()
        except ValueError:
            return set()
        manifests = data.get("manifests") if isinstance(data, dict) else None
        if not isinstance(manifests, list):
            return set()
        found = {
            item["digest"]
            for item in manifests[:100]
            if isinstance(item, dict) and isinstance(item.get("digest"), str) and _DIGEST.match(item["digest"])
        }
        # Some registries answer HEAD with a platform manifest but GET with the index. The header
        # digest lets a local index digest match that case too.
        header = r.headers.get("docker-content-digest", "")
        if _DIGEST.match(header):
            found.add(header)
        return found

    async def remote_digest(self, host: str, repo: str, tag: str) -> str:
        key = (host, repo, tag)
        cached = self._results.get(key)
        if cached and time.monotonic() - cached[1] < RESULT_TTL:
            return cached[0]
        async with self._sem:
            digest = await self._head(host, repo, tag)
        self._results[key] = (digest, time.monotonic())
        return digest

    async def _head(self, host: str, repo: str, tag: str) -> str:
        url = f"https://{host}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": ACCEPT}
        r = await self._request(url, headers)
        if r.status_code == 401:
            token = await self._token(host, repo, r.headers.get("www-authenticate", ""))
            r = await self._request(url, {**headers, "Authorization": f"Bearer {token}"})
        if r.status_code in (401, 403, 404):
            raise RegistryError("auth", f"{host} says {r.status_code}: private, needs a login, or not found")
        if r.status_code != 200:
            raise RegistryError("error", f"{host} answered {r.status_code}")
        digest = r.headers.get("docker-content-digest", "")
        if not _DIGEST.match(digest):
            raise RegistryError("error", f"{host} did not give a manifest digest")
        return digest

    async def _request(self, url: str, headers: dict[str, str], method: str = "HEAD") -> httpx.Response:
        """One request, tried again after 1, 5 and 15 s while the registry says 429 or 5xx."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                r = await self.client.request(method, url, headers=headers)
            except httpx.HTTPError as e:
                raise RegistryError("error", f"could not reach the registry: {str(e) or type(e).__name__}") from None
            if (r.status_code == 429 or r.status_code >= 500) and attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            return r
        raise AssertionError("unreachable")  # pragma: no cover

    async def _token(self, host: str, repo: str, challenge: str) -> str:
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise RegistryError("auth", f"{host} wants a login")
        p = dict(_PARAMS.findall(params))
        realm = p.get("realm", "")
        if not realm.startswith("https://"):
            raise RegistryError("error", f"{host} sent an unusable token address")
        service, scope = p.get("service", ""), p.get("scope") or f"repository:{repo}:pull"
        key = (realm, service, scope)
        cached = self._tokens.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        query = urlencode([(k, v) for k, v in (("service", service), ("scope", scope)) if v])
        r = await self._request(f"{realm}?{query}", {}, "GET")
        if r.status_code in (401, 403):
            raise RegistryError("auth", f"{host} wants a login")
        if r.status_code != 200:
            raise RegistryError("error", f"{host} token service answered {r.status_code}")
        try:
            body = r.json()
            token = body.get("token") or body["access_token"]
            ttl = float(body.get("expires_in") or 60)
        except (ValueError, KeyError, AttributeError, TypeError):
            raise RegistryError("error", f"{host} token service gave an unusable answer") from None
        self._tokens[key] = (token, time.monotonic() + max(ttl - 10, 5))
        return token

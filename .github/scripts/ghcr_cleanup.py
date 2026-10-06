"""Delete untagged versions of the homelab-manager image on GHCR.

Multi-arch images are an OCI index (tagged) pointing at per-platform manifests that have no tag
of their own, so "untagged" does not mean "unused". This only deletes untagged versions that no
tagged version references, keeps the newest KEEP of them and anything younger than MIN_AGE_HOURS.
Set DRY_RUN=true to only print.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

OWNER = os.environ["OWNER"].lower()
PACKAGE = os.environ.get("PACKAGE", "homelab-manager")
TOKEN = os.environ["GITHUB_TOKEN"]
ACTOR = os.environ.get("GITHUB_ACTOR", "github-actions")
KEEP = int(os.environ.get("KEEP", "5"))
MIN_AGE = timedelta(hours=float(os.environ.get("MIN_AGE_HOURS", "1")))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
API = "https://api.github.com"
MANIFEST_TYPES = ",".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def request(url, headers=None, method="GET"):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.headers


def api(path, method="GET"):
    return request(
        API + path,
        {
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method,
    )


def package_base():
    for kind in ("users", "orgs"):
        base = f"/{kind}/{OWNER}/packages/container/{PACKAGE}"
        try:
            api(base + "/versions?per_page=1")
            return base
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    sys.exit(f"package ghcr.io/{OWNER}/{PACKAGE} not found")


def list_versions(base):
    versions, page = [], 1
    while True:
        body, _ = api(f"{base}/versions?per_page=100&page={page}")
        chunk = json.loads(body)
        versions += chunk
        if len(chunk) < 100:
            return versions
        page += 1


def registry_token():
    basic = base64.b64encode(f"{ACTOR}:{TOKEN}".encode()).decode()
    body, _ = request(
        f"https://ghcr.io/token?service=ghcr.io&scope=repository:{OWNER}/{PACKAGE}:pull",
        {"Authorization": f"Basic {basic}"},
    )
    return json.loads(body)["token"]


def children(digest, token, seen):
    """Digests referenced by the manifest `digest`, recursively (index -> platforms, attestations)."""
    if digest in seen:
        return
    seen.add(digest)
    body, _ = request(
        f"https://ghcr.io/v2/{OWNER}/{PACKAGE}/manifests/{digest}",
        {"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES},
    )
    for m in json.loads(body).get("manifests", []):
        children(m["digest"], token, seen)


def select_deletions(versions, referenced, keep, min_age, now):
    """Untagged, unreferenced, older than min_age, and not among the newest `keep` of those."""
    candidates = [
        v
        for v in versions
        if not v["metadata"]["container"]["tags"]
        and v["name"] not in referenced
        and now - datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")) >= min_age
    ]
    candidates.sort(key=lambda v: v["created_at"], reverse=True)
    return candidates[keep:]


def main():
    base = package_base()
    versions = list_versions(base)
    tagged = [v for v in versions if v["metadata"]["container"]["tags"]]
    if not tagged:
        sys.exit("no tagged versions found; refusing to delete anything")
    token, referenced = registry_token(), set()
    for v in tagged:
        children(v["name"], token, referenced)
    doomed = select_deletions(versions, referenced, KEEP, MIN_AGE, datetime.now(UTC))
    print(
        f"{len(versions)} versions: {len(tagged)} tagged, {len(referenced)} digests in use, "
        f"{len(doomed)} untagged to delete (keeping newest {KEEP})"
    )
    for v in doomed:
        print(f"{'would delete' if DRY_RUN else 'deleting'} {v['name']} ({v['created_at']})")
        if not DRY_RUN:
            api(f"{base}/versions/{v['id']}", method="DELETE")


if __name__ == "__main__":
    main()

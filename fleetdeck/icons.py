"""App icons for the Services tab, downloaded once from the dashboard-icons set and served from this origin.

The browser only ever loads ``/api/icons/<slug>``, so the page's CSP (``img-src 'self'``) is unchanged. Icons come
from homarr-labs/dashboard-icons (Apache-2.0) through jsDelivr, are kept under ``<data>/icons/`` and are never fetched
again. A slug the set doesn't have is remembered for a week, so a missing icon costs one request, not one per page.
With ``services.icons`` off nothing is downloaded and the tiles keep their coloured letters.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

SOURCE = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons@main"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_BYTES = 512 * 1024
MISS_TTL = 7 * 24 * 3600
TIMEOUT = 10.0
TYPES = {"svg": "image/svg+xml", "png": "image/png"}


def valid_slug(slug: str) -> bool:
    return bool(SLUG.match(slug))


class IconCache:
    def __init__(self, folder: Path, *, enabled: bool = True, client: httpx.AsyncClient | None = None):
        self.folder = folder
        self.enabled = enabled
        self._client = client
        self._locks: dict[str, asyncio.Lock] = {}

    def _cached(self, slug: str) -> tuple[Path, str] | None:
        for ext, media in TYPES.items():
            path = self.folder / f"{slug}.{ext}"
            if path.is_file():
                return path, media
        return None

    def _missing(self, slug: str) -> bool:
        marker = self.folder / f"{slug}.missing"
        try:
            return time.time() - marker.stat().st_mtime < MISS_TTL
        except OSError:
            return False

    async def get(self, slug: str) -> tuple[Path, str] | None:
        """The icon file and its media type, downloading it the first time. None if there is no icon."""
        if not valid_slug(slug):
            return None
        if found := self._cached(slug):
            return found
        if not self.enabled or self._missing(slug):
            return None
        lock = self._locks.setdefault(slug, asyncio.Lock())
        async with lock:
            if found := self._cached(slug):
                return found
            return await self._download(slug)

    async def _download(self, slug: str) -> tuple[Path, str] | None:
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
        try:
            for ext, media in TYPES.items():
                try:
                    r = await client.get(f"{SOURCE}/{ext}/{slug}.{ext}")
                except httpx.HTTPError:
                    return None  # offline or blocked: try again next time, no marker
                if r.status_code == 404:
                    continue
                if r.status_code != 200 or len(r.content) > MAX_BYTES or not _looks_like(ext, r.content):
                    return None
                try:
                    self.folder.mkdir(parents=True, exist_ok=True)
                    path = self.folder / f"{slug}.{ext}"
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(r.content)
                    tmp.replace(path)
                except OSError as e:  # read-only data folder: letters, and a note in the log
                    log.warning("Can't save app icon %s: %s", slug, e)
                    return None
                return path, media
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                (self.folder / f"{slug}.missing").touch()
            except OSError:
                pass
            return None
        finally:
            if self._client is None:
                await client.aclose()


def _looks_like(ext: str, body: bytes) -> bool:
    if ext == "png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    head = body[:1024].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in body[:4096].lower())


# Image names whose repository differs from the icon's name in the set.
ALIASES = {
    "adguardhome": "adguard-home",
    "pihole": "pi-hole",
    "pms-docker": "plex",
    "homeassistant": "home-assistant",
    "nextcloud-aio-mastercontainer": "nextcloud",
}
GENERIC = {"server", "app", "web", "frontend", "backend"}


def icon_slug(image: str = "", name: str = "") -> str:
    """The dashboard-icons name for an image (``lscr.io/linuxserver/radarr:latest`` -> ``radarr``) or a link name."""
    if image and " " not in image:
        parts = image.split("@")[0].lower().split("/")
        parts[-1] = parts[-1].split(":")[0]
        repo = parts.pop()
        if repo in GENERIC and parts and not any(c in parts[-1] for c in ".:") and parts[-1] != "localhost":
            repo = parts.pop()  # vaultwarden/server -> vaultwarden
        slug = ALIASES.get(repo) or re.sub(r"-(server|web|app)$", "", repo)
    else:
        slug = ALIASES.get(name.lower().replace(" ", ""), "") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug if valid_slug(slug) else ""

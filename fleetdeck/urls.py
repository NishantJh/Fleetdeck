"""Validation for URLs, hostnames and paths that come from labels or config.

The Services tab renders links it did not create. Nothing here fetches a URL; it only makes sure a
value can safely become an ``http(s)`` link in the browser.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# A hostname, IPv4 or bracketed IPv6, optionally with a port. No userinfo, path or wildcard.
_HOST = re.compile(
    r"^(?:\*\.)?(?P<host>\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?)"
    r"(?::(?P<port>[0-9]{1,5}))?$"
)
_PATH = re.compile(r"^/[^\x00-\x1f\x7f]{0,255}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_host(value: object) -> str | None:
    """A concrete host (no wildcard), optionally with a port, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 253 or _CONTROL.search(value) or "@" in value or "/" in value or " " in value:
        return None
    if value.startswith("*."):
        return None
    m = _HOST.fullmatch(value)
    if not m:
        return None
    if (port := m.group("port")) is not None and not 1 <= int(port) <= 65535:
        return None
    return value


def safe_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip() or "/"
    if not _PATH.fullmatch(value):
        return None
    return value


def safe_url(value: object) -> str | None:
    """An absolute http(s) URL with a concrete host and no embedded credentials, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2048 or _CONTROL.search(value):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # raises ValueError for an invalid port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname
    if len(host) > 253 or _CONTROL.search(host) or " " in host or "*" in host:
        return None
    return value

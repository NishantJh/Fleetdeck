"""Request guards for the dashboard: auth with brute-force throttling, CSRF and security headers."""

from __future__ import annotations

import base64
import binascii
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import Response

from .config import AuthConfig

SECURITY_HEADERS = {
    # The UI has no inline scripts or styles, so a strict policy works.
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LoginThrottle:
    """Blocks an IP for a while after too many wrong passwords."""

    def __init__(self, max_failures: int = 10, window: float = 300.0, block: float = 300.0, max_ips: int = 10_000):
        self.max_failures, self.window, self.block, self.max_ips = max_failures, window, block, max_ips
        self._failures: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}

    def blocked_for(self, ip: str) -> float:
        until = self._blocked.get(ip, 0.0)
        left = until - time.monotonic()
        if left <= 0 and until:
            del self._blocked[ip]
        return max(left, 0.0)

    def failed(self, ip: str) -> None:
        now = time.monotonic()
        if len(self._failures) >= self.max_ips:
            self._prune(now)
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._blocked[ip] = now + self.block
            self._failures.pop(ip, None)
        else:
            self._failures[ip] = recent

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def _prune(self, now: float) -> None:
        self._failures = {ip: ts for ip, ts in self._failures.items() if ts and now - ts[-1] < self.window}
        self._blocked = {ip: until for ip, until in self._blocked.items() if until > now}
        # Still full (a flood of distinct addresses): forget the oldest half rather than grow forever.
        if len(self._failures) >= self.max_ips:
            keep = sorted(self._failures.items(), key=lambda kv: kv[1][-1])[len(self._failures) // 2 :]
            self._failures = dict(keep)


def basic_auth_ok(header: str, username: str, password: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, sep, pwd = base64.b64decode(encoded.strip(), validate=True).partition(b":")
    except (binascii.Error, ValueError):
        return False
    # Compare both halves even when the first one fails so timing doesn't leak which was wrong.
    user_ok = secrets.compare_digest(user, username.encode())
    pwd_ok = secrets.compare_digest(pwd, password.encode())
    return bool(sep) and user_ok and pwd_ok


def cross_site(request: Request) -> bool:
    """True for a state-changing request sent by another site (CSRF)."""
    if request.method in SAFE_METHODS:
        return False
    if request.headers.get("sec-fetch-site") == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return False  # not a browser (curl, scripts); they don't carry ambient credentials
    host = request.headers.get("host", "")
    return origin == "null" or urlsplit(origin).netloc.lower() != host.lower()


def make_guard(auth: AuthConfig | None, throttle: LoginThrottle):
    async def guard(request: Request, call_next):
        response = await _check(request, call_next)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    async def _check(request: Request, call_next):
        if cross_site(request):
            return Response("cross-site request refused", status_code=403)
        if auth is not None:
            ip = request.client.host if request.client else "?"
            if (left := throttle.blocked_for(ip)) > 0:
                return Response(
                    "too many failed logins, try again later",
                    status_code=429,
                    headers={"Retry-After": str(int(left) + 1)},
                )
            header = request.headers.get("authorization", "")
            if not basic_auth_ok(header, auth.username, auth.password):
                if header:  # the browser's first, credential-less request isn't a failed guess
                    throttle.failed(ip)
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="homelab"'})
            throttle.succeeded(ip)
        return await call_next(request)

    return guard

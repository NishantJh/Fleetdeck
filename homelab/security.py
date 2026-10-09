"""Request guards for the dashboard: auth with brute-force throttling, CSRF and security headers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import itertools
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

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
    """Blocks an IP for a while after too many wrong passwords.

    Each distinct wrong credential counts once per window: a browser keeps resending a mistyped
    password with every request, which mustn't lock its owner out after one typo.
    """

    def __init__(self, max_failures: int = 10, window: float = 300.0, block: float = 300.0, max_ips: int = 10_000):
        self.max_failures, self.window, self.block, self.max_ips = max_failures, window, block, max_ips
        # ip -> {credential fingerprint: last seen}
        self._failures: dict[str, dict[str, float]] = {}
        self._blocked: dict[str, float] = {}
        self._anon = itertools.count()

    def blocked_for(self, ip: str) -> float:
        until = self._blocked.get(ip, 0.0)
        left = until - time.monotonic()
        if left <= 0 and until:
            del self._blocked[ip]
        return max(left, 0.0)

    def failed(self, ip: str, credential: str | None = None) -> None:
        now = time.monotonic()
        if len(self._failures) >= self.max_ips:
            self._prune(now)
        key = hashlib.sha256(credential.encode()).hexdigest()[:16] if credential is not None else f"#{next(self._anon)}"
        recent = {k: t for k, t in self._failures.get(ip, {}).items() if now - t < self.window}
        recent[key] = now
        if len(recent) >= self.max_failures:
            self._blocked[ip] = now + self.block
            self._failures.pop(ip, None)
        else:
            self._failures[ip] = recent

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def _prune(self, now: float) -> None:
        self._failures = {
            ip: seen for ip, seen in self._failures.items() if seen and now - max(seen.values()) < self.window
        }
        self._blocked = {ip: until for ip, until in self._blocked.items() if until > now}
        # Still full (a flood of distinct addresses): forget the oldest half rather than grow forever.
        if len(self._failures) >= self.max_ips:
            keep = sorted(self._failures.items(), key=lambda kv: max(kv[1].values()))[len(self._failures) // 2 :]
            self._failures = dict(keep)


_SCRYPT = {"n": 2**15, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash`` (base64), for ``auth.password_hash``."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, maxmem=2**26, dklen=32, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(dk)}"


def verify_password(password: bytes, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, want = encoded.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password, salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=2**26, dklen=32)
    except (ValueError, binascii.Error):
        return False
    return secrets.compare_digest(dk, base64.b64decode(want))


def _credentials(header: str) -> tuple[bytes, bytes] | None:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, sep, pwd = base64.b64decode(encoded.strip(), validate=True).partition(b":")
    except (binascii.Error, ValueError):
        return None
    return (user, pwd) if sep else None


def basic_auth_ok(header: str, auth: AuthConfig) -> bool:
    creds = _credentials(header)
    if creds is None:
        return False
    user, pwd = creds
    # Check both halves even when the first one fails so timing doesn't leak which was wrong.
    user_ok = secrets.compare_digest(user, auth.username.encode())
    if auth.password_hash:
        pwd_ok = verify_password(pwd, auth.password_hash)
    else:
        pwd_ok = secrets.compare_digest(pwd, (auth.password or "").encode())
    return user_ok and pwd_ok


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


SETUP_PATHS = ("/setup", "/static/")


def make_guard(auth: AuthConfig | None, throttle: LoginThrottle, setup_token: str | None = None):
    """``setup_token`` puts the app in first-run mode: only the setup page works, and its API only
    with the one-time token printed in the logs, until it has created an admin login."""
    # A password hash is deliberately slow to check, and the browser sends the login with every
    # request; remember the headers that were right (by digest) instead of re-hashing each time.
    good: set[str] = set()

    async def auth_ok(header: str) -> bool:
        if not auth.password_hash:
            return basic_auth_ok(header, auth)
        digest = hashlib.sha256(header.encode()).hexdigest()
        if digest in good:
            return True
        if not await asyncio.to_thread(basic_auth_ok, header, auth):
            return False
        if len(good) > 64:
            good.clear()
        good.add(digest)
        return True

    async def guard(request: Request, call_next):
        response = await _check(request, call_next)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    async def _check(request: Request, call_next):
        if cross_site(request):
            return Response("cross-site request refused", status_code=403)
        if setup_token is not None:
            path = request.url.path
            if path == "/":
                return RedirectResponse("/setup")
            if path.startswith(SETUP_PATHS):
                return await call_next(request)
            if path.startswith("/api/setup"):
                if secrets.compare_digest(request.headers.get("x-setup-token", ""), setup_token):
                    return await call_next(request)
                return Response(
                    "setup link missing or out of date: open the link printed in the container logs",
                    status_code=403,
                )
            return Response("finish setup first: open /setup", status_code=403)
        if auth is not None:
            ip = request.client.host if request.client else "?"
            if (left := throttle.blocked_for(ip)) > 0:
                return Response(
                    "too many failed logins, try again later",
                    status_code=429,
                    headers={"Retry-After": str(int(left) + 1)},
                )
            header = request.headers.get("authorization", "")
            if not await auth_ok(header):
                if header:  # the browser's first, credential-less request isn't a failed guess
                    throttle.failed(ip, header)
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="homelab"'})
            throttle.succeeded(ip)
        return await call_next(request)

    return guard

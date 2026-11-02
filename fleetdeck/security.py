"""Request guards for the dashboard: login page and sessions, auth with brute-force throttling, CSRF and security
headers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import itertools
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

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


def password_ok(auth: AuthConfig | None, password: str) -> bool:
    """The dashboard password, asked again before something that runs as root (console, update commands)."""
    if auth is None:  # demo mode without a login: nothing to check
        return True
    if auth.password_hash:
        return verify_password(password.encode(), auth.password_hash)
    return secrets.compare_digest(password.encode(), (auth.password or "").encode())


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


SESSION_COOKIE = "fleetdeck_session"
SESSION_SHORT = 12 * 3600  # "keep me signed in" unticked: a browser-session cookie, good for at most this long
SESSION_LONG = 30 * 86400


class Sessions:
    """Signed login cookies, so the browser logs in once on the login page instead of sending the password with
    every request. ``expiry.mac``: nothing is stored on the server. The key mixes a secret kept in the state
    database (or made per run without one) with the login itself, so changing the username or password ends
    every session."""

    def __init__(self, auth: AuthConfig, secret: bytes):
        login = "\n".join((auth.username, auth.password_hash or auth.password or "")).encode()
        self._key = hmac.new(secret, login, hashlib.sha256).digest()

    def _mac(self, expiry: int) -> str:
        return hmac.new(self._key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def issue(self, ttl: int) -> str:
        expiry = int(time.time()) + ttl
        return f"{expiry}.{self._mac(expiry)}"

    def valid(self, token: str) -> bool:
        expiry, _, mac = token.partition(".")
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        return secrets.compare_digest(mac, self._mac(int(expiry)))


def session_secret(store) -> bytes:
    """The signing secret, kept in the state database so logins survive a restart or an update."""
    if store is None:
        return os.urandom(32)
    if (saved := store.get_value("session_secret")) is None:
        saved = secrets.token_hex(32)
        store.set_value("session_secret", saved)
    return bytes.fromhex(saved)


def safe_next(target: str | None) -> str:
    """Where to go after logging in: a path on this site only, never another site (``//evil``, or a backslash,
    which browsers read as a slash)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


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
INSTALL_PATH = "/install/"
# Reachable without logging in: the login page, its API and the static files it needs (all public in the repo).
LOGIN_PATHS = {"/login", "/api/login", "/api/logout"}


def make_guard(
    auth: AuthConfig | None,
    throttle: LoginThrottle,
    setup_token: str | None = None,
    locked: bool = True,
    sessions: Sessions | None = None,
):
    """``setup_token`` (when there is no login) makes the setup API need the one-time token printed in
    the logs. ``locked`` is first-run mode: nothing but the setup page works until a login exists.
    ``sessions`` lets the login page's cookie stand in for the password; HTTP Basic still works for scripts."""
    # A password hash is deliberately slow to check, and the browser sends the login with every
    # request; remember the headers that were right (by digest) instead of re-hashing each time.
    good: set[str] = set()
    # Each check takes 32 MB (scrypt); don't let a burst of wrong passwords take the memory.
    hashing = asyncio.Semaphore(2)

    async def auth_ok(header: str) -> bool:
        if not auth.password_hash:
            return basic_auth_ok(header, auth)
        digest = hashlib.sha256(header.encode()).hexdigest()
        if digest in good:
            return True
        async with hashing:
            ok = await asyncio.to_thread(basic_auth_ok, header, auth)
        if not ok:
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
        if request.url.path.startswith(INSTALL_PATH):
            return await call_next(request)  # setup scripts for machines: the link is the key
        if setup_token is not None:
            path = request.url.path
            if path.startswith("/api/setup"):
                if secrets.compare_digest(request.headers.get("x-setup-token", ""), setup_token):
                    return await call_next(request)
                return Response(
                    "setup code missing or out of date: it is in the container's logs (Setup code: ...)",
                    status_code=403,
                )
            if locked:
                if path == "/":
                    return RedirectResponse("/setup")
                if path.startswith(SETUP_PATHS):
                    return await call_next(request)
                return Response("finish setup first: open /setup", status_code=403)
        if auth is not None:
            path = request.url.path
            if path in LOGIN_PATHS or path.startswith("/static/"):
                return await call_next(request)
            if sessions is not None and sessions.valid(request.cookies.get(SESSION_COOKIE, "")):
                return await call_next(request)
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
                if sessions is None:
                    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="homelab"'})
                # No WWW-Authenticate: it would make the browser pop up its own login box. Pages go to the login page.
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    url = path + (f"?{request.url.query}" if request.url.query else "")
                    return RedirectResponse(f"/login?next={quote(url, safe='')}", status_code=303)
                return Response("login required", status_code=401)
            throttle.succeeded(ip)
        return await call_next(request)

    guard.auth_ok = auth_ok
    return guard


class LoginRequest(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)
    remember: bool = False


def add_login_routes(app: FastAPI, auth: AuthConfig | None, guard, throttle: LoginThrottle, sessions, page: Path):
    """The login page and its API. ``guard`` is the middleware from ``make_guard``: its password check (cached,
    with the scrypt work limited) is reused, and wrong passwords here count towards the same lockout."""

    @app.get("/login")
    async def login_page(request: Request, next: str | None = None):  # noqa: A002 - the query parameter's name
        if sessions is None:
            return RedirectResponse("/", status_code=303)
        if sessions.valid(request.cookies.get(SESSION_COOKIE, "")):
            return RedirectResponse(safe_next(next), status_code=303)
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request):
        if sessions is None:
            return JSONResponse({"detail": "this dashboard has no login"}, status_code=404)
        ip = request.client.host if request.client else "?"
        if (left := throttle.blocked_for(ip)) > 0:
            wait = int(left) + 1
            return JSONResponse(
                {"detail": "Too many failed attempts. Try again in a few minutes.", "retry_after": wait},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        header = "Basic " + base64.b64encode(f"{body.username}:{body.password}".encode()).decode()
        if ":" in body.username or not await guard.auth_ok(header):
            throttle.failed(ip, header)
            return JSONResponse({"detail": "Wrong username or password."}, status_code=401)
        throttle.succeeded(ip)
        ttl = SESSION_LONG if body.remember else SESSION_SHORT
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            sessions.issue(ttl),
            max_age=ttl if body.remember else None,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax")
        return response

    @app.get("/api/session")
    async def session():
        """For the dashboard's Sign out button (behind the guard, so only a logged-in browser gets here)."""
        return {"login": sessions is not None, "username": auth.username if auth else None}

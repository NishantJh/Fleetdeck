"""Certificate pinning for the Proxmox and Incus APIs.

Both ship self-signed certificates and are usually reached by IP, which a CA bundle often can't
verify. Pinning the certificate's SHA-256 fingerprint works in every case: on first use the
server's certificate is fetched, compared with the configured fingerprint, and then trusted as the
only trust anchor for that connection, so every later handshake is verified against exactly it.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from cryptography import x509

FETCH_TIMEOUT = 15.0


def _fmt(fp: str) -> str:
    return ":".join(fp[i : i + 2] for i in range(0, len(fp), 2)).upper()


def pinned_context(host: str, port: int, fingerprint: str, cert: tuple[str, str] | None = None) -> ssl.SSLContext:
    """An SSL context that trusts only the server's current certificate, if it matches ``fingerprint``."""
    pem = ssl.get_server_certificate((host, port), timeout=FETCH_TIMEOUT)
    actual = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    if actual != fingerprint:
        raise RuntimeError(
            f"TLS certificate of {host}:{port} does not match tls_fingerprint (it is {_fmt(actual)}). "
            "If the certificate was renewed on purpose, update tls_fingerprint."
        )
    # An expired certificate fails verification with an opaque OpenSSL error; say what to do instead.
    expires = x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc
    if expires < datetime.now(UTC):
        raise RuntimeError(
            f"TLS certificate of {host}:{port} expired on {expires:%Y-%m-%d}. Renew it (on Proxmox: "
            "`pvecm updatecerts --force`, then `systemctl restart pveproxy`) and update tls_fingerprint."
        )
    ctx = ssl.create_default_context(cadata=pem)
    # The pinned certificate itself is the trust anchor (it is usually a leaf signed by the node's
    # own CA, hence PARTIAL_CHAIN), and it was matched by fingerprint, not by name.
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.check_hostname = False
    if cert:
        ctx.load_cert_chain(*cert)
    return ctx


class PinnedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that pins the server certificate (see module docstring)."""

    def __init__(self, url: str, fingerprint: str, cert: tuple[str, str] | None = None):
        parts = urlsplit(url)
        self.host = parts.hostname or ""
        self.port = parts.port or 443
        self.fingerprint = fingerprint
        self.cert = cert
        self._inner: httpx.AsyncHTTPTransport | None = None
        self._lock = asyncio.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        inner = await self._transport()
        try:
            return await inner.handle_async_request(request)
        except httpx.ConnectError as e:
            if not _verify_failed(e):
                raise
            # The certificate changed (renewed, or expired) since it was pinned. Pin afresh once, which
            # either works or fails with a message that says what changed. Only drop the transport
            # that failed: a concurrent request may already have replaced it.
            async with self._lock:
                if self._inner is inner:
                    self._inner = None
                    await inner.aclose()
            return await (await self._transport()).handle_async_request(request)

    async def _transport(self) -> httpx.AsyncHTTPTransport:
        async with self._lock:
            if self._inner is None:
                # A failure (host down, mismatch) leaves _inner unset, so the next request retries.
                ctx = await asyncio.to_thread(pinned_context, self.host, self.port, self.fingerprint, self.cert)
                self._inner = httpx.AsyncHTTPTransport(verify=ctx)
            return self._inner

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()


def _verify_failed(e: BaseException) -> bool:
    while e is not None:
        if isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False

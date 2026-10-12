import asyncio
import datetime as dt
import hashlib
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from fleetdeck.tls import PinnedTransport, pinned_context


def _cert(subject: str, issuer_cert=None, issuer_key=None, ca=False, days=30):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_cert.subject if issuer_cert else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=max(1, -days + 1)))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    cert = builder.sign(issuer_key or key, hashes.SHA256())
    return cert, key


@pytest.fixture
def node_cert(tmp_path):
    """Like Proxmox: a leaf for some other name, signed by the node's private CA."""
    ca, ca_key = _cert("Proxmox Virtual Environment", ca=True)
    leaf, key = _cert("pve.lan", ca, ca_key)
    crt, pem_key = tmp_path / "node.crt", tmp_path / "node.key"
    crt.write_bytes(leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    pem_key.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    fp = hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()
    return str(crt), str(pem_key), fp


@pytest.fixture
async def https_server(node_cert):
    crt, key, _ = node_cert
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(crt, key)

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port


async def test_pinned_transport_trusts_matching_certificate(node_cert, https_server):
    _, _, fp = node_cert
    url = f"https://127.0.0.1:{https_server}"
    async with httpx.AsyncClient(base_url=url, transport=PinnedTransport(url, fp)) as client:
        r = await client.get("/")
        assert r.text == "ok"
        assert (await client.get("/")).status_code == 200  # later handshakes verify against the pin


async def test_pinned_transport_rejects_other_certificate(https_server):
    url = f"https://127.0.0.1:{https_server}"
    async with httpx.AsyncClient(base_url=url, transport=PinnedTransport(url, "00" * 32)) as client:
        with pytest.raises(RuntimeError, match="does not match tls_fingerprint"):
            await client.get("/")


async def test_pinned_context_refuses_a_different_server(node_cert, https_server, tmp_path):
    """A context pinned to one certificate fails the handshake with any other (a MITM)."""
    _, _, fp = node_cert
    ctx = await asyncio.to_thread(pinned_context, "127.0.0.1", https_server, fp)
    other, other_key = _cert("evil")
    crt, key = tmp_path / "evil.crt", tmp_path / "evil.key"
    crt.write_bytes(other.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        other_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    evil = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    evil.load_cert_chain(crt, key)
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=evil)
    port = server.sockets[0].getsockname()[1]
    async with server:
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection("127.0.0.1", port, ssl=ctx)


def _write(tmp_path, name, cert, key):
    crt, pem = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return str(crt), str(pem), hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def _serve(ctx):
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)


async def test_expired_pinned_certificate_says_so(tmp_path):
    crt, key, fp = _write(tmp_path, "old", *_cert("pve.lan", days=-1))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(crt, key)
    server = await _serve(ctx)
    async with server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(RuntimeError, match="expired on"):
            await asyncio.to_thread(pinned_context, "127.0.0.1", port, fp)


async def test_renewed_certificate_is_repinned_with_a_clear_error(tmp_path):
    crt, key, fp = _write(tmp_path, "a", *_cert("pve.lan"))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(crt, key)
    server = await _serve(ctx)
    async with server:
        url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with httpx.AsyncClient(base_url=url, transport=PinnedTransport(url, fp)) as client:
            assert (await client.get("/")).text == "ok"
            # The node gets a new certificate while the dashboard is running.
            new_crt, new_key, _ = _write(tmp_path, "b", *_cert("pve.lan"))
            ctx.load_cert_chain(new_crt, new_key)
            with pytest.raises(RuntimeError, match="does not match tls_fingerprint"):
                await client.get("/")

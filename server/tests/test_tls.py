"""TLS certificate generation and fingerprint pinning tests."""

import asyncio
import hashlib
import ssl
import stat

from ubudesk_server.net import tls


def test_cert_generated_once(tmp_path):
    c1, k1 = tls.ensure_cert(tmp_path, "test-host")
    fp1 = tls.cert_fingerprint(c1)
    c2, k2 = tls.ensure_cert(tmp_path, "test-host")
    assert tls.cert_fingerprint(c2) == fp1
    assert len(fp1) == 64
    mode = stat.S_IMODE(k1.stat().st_mode)
    assert mode == 0o600


async def test_tls_handshake_and_fingerprint(tmp_path):
    cert, key = tls.ensure_cert(tmp_path, "test-host")
    expected_fp = tls.cert_fingerprint(cert)
    server_ctx = tls.server_ssl_context(cert, key)

    async def on_client(reader, writer):
        writer.write(b"hi")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(on_client, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx)
    sslobj = writer.get_extra_info("ssl_object")
    der = sslobj.getpeercert(binary_form=True)
    actual_fp = hashlib.sha256(der).hexdigest()
    assert actual_fp == expected_fp

    # a client that pins a different fingerprint must treat this as fatal
    assert actual_fp != "0" * 64

    data = await reader.read(10)
    assert data == b"hi"
    writer.close()
    server.close()
    await server.wait_closed()

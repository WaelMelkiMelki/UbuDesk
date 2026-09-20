"""End-to-end integration tests: real server + tools/test_client.py logic,
headless, over loopback. Covers plain TCP and TLS."""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

from ubudesk_server.config import Config, state_dir
from ubudesk_server.net.server import UbuDeskServer

pytest.importorskip("av")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_test_client():
    spec = importlib.util.spec_from_file_location(
        "ubudesk_test_client", REPO_ROOT / "tools" / "test_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


tc = _load_test_client()


async def _start_server(tls: bool, port: int = 0) -> UbuDeskServer:
    config = Config.load()
    config.port = port
    config.bind = "127.0.0.1"
    config.source = "test"
    config.tls = tls
    server = UbuDeskServer(config)
    await server.start()
    # discover the actual port
    server.config.port = server._server.sockets[0].getsockname()[1]
    return server


async def test_stream_over_plain_tcp():
    server = await _start_server(tls=False)
    pin = server.pins.issue()
    try:
        client = tc.Client("127.0.0.1", server.config.port, use_tls=False)
        await client.connect()
        await client.handshake(pin=pin, token=None)
        assert client.token
        started = await client.start_stream(480, 300, 30, "extend")
        assert started["codec"] == "h264"
        assert started["width"] == 480

        first = None
        frames = 0
        keyframes = 0
        idr_requested = None
        idr_latency = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            ftype, payload = await asyncio.wait_for(client.read_frame(), 3)
            if ftype != tc.TYPE_VIDEO:
                continue
            pts_us, flags = tc.VIDEO_HEADER.unpack_from(payload)
            data = payload[tc.VIDEO_HEADER.size :]
            if first is None:
                first = (flags, data)
            frames += 1
            if flags & 1:
                keyframes += 1
                if idr_requested is not None and idr_latency is None:
                    idr_latency = time.monotonic() - idr_requested
            if frames == 15 and idr_requested is None:
                await client.send({"t": "idr"})
                idr_requested = time.monotonic()

        assert frames >= 30, f"only {frames} frames in 3 s"
        flags, data = first
        assert flags & 1, "first frame must be a keyframe"
        nals = tc.annexb_nal_types(data)
        assert 7 in nals and 8 in nals and 5 in nals, "first AU must contain SPS/PPS/IDR"
        assert idr_latency is not None and idr_latency < 1.0, f"IDR latency {idr_latency}"

        # touch input reaches FakeInput
        session = next(iter(server._sessions))
        await client.send({"t": "touch", "a": "down", "id": 0, "x": 0.5, "y": 0.5})
        await client.send({"t": "touch", "a": "up", "id": 0})
        for _ in range(40):
            if session._input is not None and session._input.events:
                break
            await asyncio.sleep(0.05)
        events = session._input.events
        assert ("touch_down", 0, 0.5, 0.5) in events
        assert ("touch_up", 0) in events

        await client.close()
    finally:
        await server.stop()


async def test_stream_over_tls_with_fingerprint():
    server = await _start_server(tls=True)
    pin = server.pins.issue()
    fp = server.fingerprint
    assert fp
    try:
        client = tc.Client("127.0.0.1", server.config.port, use_tls=True, fingerprint=fp)
        await client.connect()
        await client.handshake(pin=pin, token=None)
        started = await client.start_stream(320, 200, 30, "mirror")
        assert started["t"] == "started"
        got_video = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0 and not got_video:
            ftype, _payload = await asyncio.wait_for(client.read_frame(), 3)
            got_video = ftype == tc.TYPE_VIDEO
        assert got_video
        await client.close()
    finally:
        await server.stop()


async def test_tls_wrong_fingerprint_rejected():
    server = await _start_server(tls=True)
    try:
        client = tc.Client("127.0.0.1", server.config.port, use_tls=True, fingerprint="ab" * 32)
        with pytest.raises(RuntimeError, match="fingerprint mismatch"):
            await client.connect()
    finally:
        await server.stop()


async def test_token_reconnect_and_revoke():
    server = await _start_server(tls=False)
    pin = server.pins.issue()
    try:
        c1 = tc.Client("127.0.0.1", server.config.port, use_tls=False)
        await c1.connect()
        await c1.handshake(pin=pin, token=None)
        token = c1.token
        client_id = c1.client_id
        await c1.close()

        c2 = tc.Client("127.0.0.1", server.config.port, use_tls=False)
        c2.client_id = client_id
        await c2.connect()
        await c2.handshake(pin=None, token=token)
        await c2.close()

        server.devices.remove(client_id)
        c3 = tc.Client("127.0.0.1", server.config.port, use_tls=False)
        c3.client_id = client_id
        await c3.connect()
        with pytest.raises(RuntimeError, match="auth_fail|unknown_token"):
            await c3.handshake(pin=None, token=token)
        await c3.close()
    finally:
        await server.stop()


async def test_clean_shutdown_releases_port():
    server = await _start_server(tls=False)
    port = server.config.port
    await server.stop()
    # port must be reusable immediately
    srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    srv.close()
    await srv.wait_closed()


async def test_state_dir_isolated(tmp_path):
    # sanity: the autouse fixture isolates state
    assert "state" in str(state_dir())


@pytest.mark.parametrize("tls", [False, True])
async def test_cli_revocation_closes_active_stream_and_refuses_reconnect(tls, monkeypatch):
    from ubudesk_server.net import session as session_module

    monkeypatch.setattr(session_module, "AUTHORIZATION_CHECK_INTERVAL_S", 0.05)
    server = await _start_server(tls=tls)
    client = tc.Client("127.0.0.1", server.config.port, use_tls=tls, fingerprint=server.fingerprint)
    try:
        await client.connect()
        await client.handshake(pin=server.pins.issue(), token=None)
        token = client.token
        await client.start_stream(320, 200, 30, "mirror")
        # Use the real CLI in another process, not the server's DeviceStore.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "ubudesk_server",
            "devices",
            "--revoke",
            client.client_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 5)
        assert proc.returncode == 0, stderr.decode()
        assert "revoked" in stdout.decode()

        async def until_closed():
            revoked = False
            while True:
                try:
                    kind, payload = await client.read_frame()
                except (asyncio.IncompleteReadError, ConnectionError):
                    return revoked
                if kind == tc.TYPE_CONTROL:
                    import json

                    msg = json.loads(payload)
                    revoked |= msg.get("code") == "revoked"

        assert await asyncio.wait_for(until_closed(), 2)
        reconnect = tc.Client(
            "127.0.0.1", server.config.port, use_tls=tls, fingerprint=server.fingerprint
        )
        reconnect.client_id = client.client_id
        try:
            await reconnect.connect()
            with pytest.raises(RuntimeError, match="unknown_token"):
                await reconnect.handshake(pin=None, token=token)
        finally:
            await reconnect.close()
        assert len(server.devices) == 0  # auth must not restore the deleted entry
    finally:
        await client.close()
        await server.stop()

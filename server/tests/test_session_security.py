"""Security tests: no unauthenticated path may reach capture or input."""

import asyncio
import contextlib
import json

import pytest

from ubudesk_server.capture.base import StreamInfo, StreamSettings, VideoSource
from ubudesk_server.input.fake import FakeInput
from ubudesk_server.net.auth import DeviceStore, PinManager, generate_token
from ubudesk_server.net.session import Session, SourceFactory
from ubudesk_server.protocol import (
    TYPE_CONTROL,
    encode_control,
    read_frame,
)


class SpySource(VideoSource):
    """Counts start() calls so tests can prove capture was (not) reached."""

    instances: list["SpySource"] = []

    def __init__(self):
        self.started = 0
        SpySource.instances.append(self)

    def start(self, settings: StreamSettings, on_frame) -> StreamInfo:
        self.started += 1
        return StreamInfo(settings.width, settings.height, settings.fps, "spy")

    def stop(self) -> None:
        pass

    def request_keyframe(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_spy():
    SpySource.instances.clear()
    yield


class Harness:
    """Runs a Session over an in-memory socket pair."""

    def __init__(self, tmp_path):
        self.devices = DeviceStore(tmp_path / "devices.json")
        self.pins = PinManager()
        self.fake_input = FakeInput()
        self.factory = SourceFactory(lambda s: (SpySource(), self.fake_input))
        self.session = None
        self.task = None
        self.reader = None
        self.writer = None

    async def __aenter__(self):
        server_ready = asyncio.Event()
        sessions = []

        async def on_client(reader, writer):
            session = Session(
                reader,
                writer,
                server_id="srv-id",
                server_name="test-server",
                devices=self.devices,
                pins=self.pins,
                source_factory=self.factory,
            )
            sessions.append(session)
            await session.run()

        self.server = await asyncio.start_server(on_client, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        server_ready.set()
        self.sessions = sessions
        return self

    async def __aexit__(self, *exc):
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()

    async def send(self, msg):
        self.writer.write(encode_control(msg))
        await self.writer.drain()

    async def recv(self, timeout=5.0):
        ftype, payload = await asyncio.wait_for(read_frame(self.reader), timeout)
        assert ftype == TYPE_CONTROL
        return json.loads(payload)

    async def expect_closed(self, timeout=5.0):
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await asyncio.wait_for(read_frame(self.reader), timeout)


HELLO = {
    "t": "hello",
    "proto": 1,
    "client_id": "cid-1",
    "name": "t",
    "app_version": "0",
    "screen": {"w": 100, "h": 100},
    "codecs": ["h264"],
}
START = {
    "t": "start",
    "width": 640,
    "height": 400,
    "fps": 30,
    "bitrate_kbps": 4000,
    "mode": "extend",
}


async def test_start_before_auth_closes_connection(tmp_path):
    async with Harness(tmp_path) as h:
        await h.send(HELLO)
        assert (await h.recv())["t"] == "auth_required"
        await h.send(START)  # not allowed before auth
        await h.expect_closed()
        assert all(s.started == 0 for s in SpySource.instances)


async def test_input_before_auth_closes_connection(tmp_path):
    async with Harness(tmp_path) as h:
        await h.send(HELLO)
        await h.recv()
        await h.send({"t": "touch", "a": "down", "id": 0, "x": 0.5, "y": 0.5})
        await h.expect_closed()
        assert h.fake_input.events == []


async def test_non_hello_first_closes(tmp_path):
    async with Harness(tmp_path) as h:
        await h.send({"t": "ping", "seq": 1, "ts": 1})
        await h.expect_closed()


async def test_wrong_pin_gets_auth_fail(tmp_path):
    async with Harness(tmp_path) as h:
        h.pins.issue()
        await h.send(HELLO)
        await h.recv()
        await h.send({"t": "auth", "method": "pin", "pin": "999999x"})
        msg = await h.recv()
        assert msg == {"t": "auth_fail", "reason": "bad_pin"}
        await h.expect_closed()


async def test_unknown_token_rejected(tmp_path):
    async with Harness(tmp_path) as h:
        token_b64, _ = generate_token()
        await h.send(HELLO)
        await h.recv()
        await h.send({"t": "auth", "method": "token", "token": token_b64})
        msg = await h.recv()
        assert msg["t"] == "auth_fail" and msg["reason"] == "unknown_token"


async def test_pin_pairing_flow_and_token_reuse(tmp_path):
    async with Harness(tmp_path) as h:
        pin = h.pins.issue()
        await h.send(HELLO)
        assert (await h.recv())["t"] == "auth_required"
        await h.send({"t": "auth", "method": "pin", "pin": pin})
        ok = await h.recv()
        assert ok["t"] == "auth_ok" and ok["token"]
        token = ok["token"]
        await h.send(START)
        started = await h.recv()
        assert started["t"] == "started" and started["encoder"] == "spy"
        assert SpySource.instances[-1].started == 1
        # input now reaches the backend
        await h.send({"t": "touch", "a": "down", "id": 0, "x": 0.25, "y": 0.75})
        for _ in range(50):
            if h.fake_input.events:
                break
            await asyncio.sleep(0.05)
        assert ("touch_down", 0, 0.25, 0.75) in h.fake_input.events
        await h.send({"t": "bye"})

    # reconnect with the stored token
    async with Harness(tmp_path) as h2:
        h2.devices = DeviceStore(tmp_path / "devices.json")
        # recreate harness store binding: session uses h2.devices set in __aenter__,
        # which already re-read the same file.
        await h2.send(HELLO)
        await h2.recv()
        await h2.send({"t": "auth", "method": "token", "token": token})
        ok = await h2.recv()
        assert ok["t"] == "auth_ok"


async def test_bad_proto_version_rejected(tmp_path):
    async with Harness(tmp_path) as h:
        bad = dict(HELLO, proto=99)
        await h.send(bad)
        msg = await h.recv()
        assert msg["t"] == "error" and msg["code"] == "bad_request"


async def test_out_of_range_start_rejected(tmp_path):
    async with Harness(tmp_path) as h:
        pin = h.pins.issue()
        await h.send(HELLO)
        await h.recv()
        await h.send({"t": "auth", "method": "pin", "pin": pin})
        await h.recv()
        await h.send(dict(START, width=100000))
        msg = await h.recv()
        assert msg["t"] == "error" and msg["code"] == "bad_request"
        assert all(s.started == 0 for s in SpySource.instances)

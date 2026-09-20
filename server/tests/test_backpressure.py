"""Backlog / drop-to-keyframe behavior of the session video queue."""

import asyncio
import json

import pytest

from ubudesk_server.capture.base import StreamInfo, StreamSettings, VideoSource
from ubudesk_server.input.fake import FakeInput
from ubudesk_server.net.auth import DeviceStore, PinManager
from ubudesk_server.net.session import QUEUE_MAX_FRAMES, Session, SourceFactory
from ubudesk_server.protocol import TYPE_CONTROL, TYPE_VIDEO, encode_control, read_frame


class ManualSource(VideoSource):
    """A source whose frames are pushed manually by the test."""

    def __init__(self):
        self.on_frame = None
        self.keyframe_requests = 0

    def start(self, settings: StreamSettings, on_frame) -> StreamInfo:
        self.on_frame = on_frame
        return StreamInfo(settings.width, settings.height, settings.fps, "manual")

    def stop(self):
        pass

    def request_keyframe(self):
        self.keyframe_requests += 1


async def test_backlog_drops_until_keyframe(tmp_path):
    source = ManualSource()
    devices = DeviceStore(tmp_path / "devices.json")
    pins = PinManager()
    pin = pins.issue()

    async def on_client(reader, writer):
        session = Session(
            reader,
            writer,
            server_id="s",
            server_name="s",
            devices=devices,
            pins=pins,
            source_factory=SourceFactory(lambda s: (source, FakeInput())),
        )
        await session.run()

    server = await asyncio.start_server(on_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def send(msg):
        writer.write(encode_control(msg))
        await writer.drain()

    async def recv_control():
        while True:
            ftype, payload = await asyncio.wait_for(read_frame(reader), 5)
            if ftype == TYPE_CONTROL:
                return json.loads(payload)

    await send(
        {
            "t": "hello",
            "proto": 1,
            "client_id": "c",
            "name": "t",
            "app_version": "0",
            "screen": {},
            "codecs": ["h264"],
        }
    )
    await recv_control()
    await send({"t": "auth", "method": "pin", "pin": pin})
    await recv_control()
    await send(
        {
            "t": "start",
            "width": 320,
            "height": 200,
            "fps": 30,
            "bitrate_kbps": 1000,
            "mode": "extend",
        }
    )
    started = await recv_control()
    assert started["t"] == "started"
    assert source.on_frame is not None

    key = b"\x00\x00\x00\x01\x65KEY"
    delta = b"\x00\x00\x00\x01\x41DELTA"
    # Leave the startup keyframe gate first, so this tests actual queue
    # overflow rather than merely discarding pre-IDR startup deltas.
    source.on_frame(key, 0, True, True)
    ftype, payload = await asyncio.wait_for(read_frame(reader), 5)
    assert ftype == TYPE_VIDEO and payload[8] & 1
    requests_before_flood = source.keyframe_requests

    # Flood the queue far beyond its capacity with delta frames.
    for i in range(QUEUE_MAX_FRAMES * 6):
        source.on_frame(delta, i, False, False)
    # After the flood, a keyframe arrives.
    source.on_frame(key, 999, True, True)
    await asyncio.sleep(0.2)

    # The session must have requested a keyframe due to backlog.
    assert source.keyframe_requests > requests_before_flood

    # Read what actually got sent: total video frames must be far below the
    # flood size, and the keyframe must be among them.
    got_frames = 0
    got_key = False

    async def drain():
        nonlocal got_frames, got_key
        while True:
            ftype, payload = await asyncio.wait_for(read_frame(reader), 0.5)
            if ftype == TYPE_VIDEO:
                got_frames += 1
                if payload[8] & 1:
                    got_key = True

    with pytest.raises(asyncio.TimeoutError):
        await drain()

    assert got_key, "keyframe must survive the backlog purge"
    assert got_frames <= QUEUE_MAX_FRAMES * 2 + 2, f"sent too many backlogged frames: {got_frames}"

    writer.close()
    server.close()
    await server.wait_closed()

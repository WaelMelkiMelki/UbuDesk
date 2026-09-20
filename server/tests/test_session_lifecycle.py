"""Connection-phase and capture-ownership regressions, without a desktop/GPU."""

import asyncio
import contextlib
import struct
import threading

import pytest

from ubudesk_server.capture.base import CaptureError, StreamInfo, VideoSource
from ubudesk_server.input.fake import FakeInput
from ubudesk_server.net import session as session_module
from ubudesk_server.net.auth import DeviceStore, PinManager
from ubudesk_server.net.session import Session, SourceFactory, State
from ubudesk_server.protocol import TYPE_CONTROL, TYPE_VIDEO, decode_control, encode_control

HELLO = {"t": "hello", "proto": 1, "client_id": "phone", "codecs": ["h264"]}
START = {"t": "start", "width": 320, "height": 200, "fps": 30, "bitrate_kbps": 1000}


class ControlledSource(VideoSource):
    def __init__(self, *, blocked=False, failure=None, cooperative=True, stop_failure=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.cancelled = threading.Event()
        self.failure = failure
        self.cooperative = cooperative
        self.stop_failure = stop_failure
        self.starts = 0
        self.stops = 0
        self.key_requests = 0
        self.on_frame = None

    def start(self, settings, on_frame):
        self.starts += 1
        self.on_frame = on_frame
        self.entered.set()
        assert self.release.wait(5), "test did not release capture worker"
        if self.failure is not None:
            raise self.failure
        return StreamInfo(settings.width, settings.height, settings.fps, "controlled")

    def cancel_start(self):
        self.cancelled.set()
        if self.cooperative:
            self.release.set()

    def stop(self):
        self.stops += 1
        if self.stop_failure:
            raise RuntimeError("stop failed")

    def request_keyframe(self):
        self.key_requests += 1


class TrackingInput(FakeInput):
    def __init__(self):
        super().__init__()
        self.closes = 0

    def close(self):
        self.closes += 1


class MemoryWriter:
    """Each server write is a complete protocol frame; close wakes the reader."""

    def __init__(self, reader):
        self.reader = reader
        self.frames = asyncio.Queue()
        self.closed = asyncio.Event()

    def get_extra_info(self, _key):
        return None

    def write(self, wire):
        if self.closed.is_set():
            raise ConnectionError("closed")
        self.frames.put_nowait((wire[0], wire[5:]))

    async def drain(self):
        pass

    def close(self):
        self.closed.set()
        self.reader.feed_eof()

    async def wait_closed(self):
        pass


class Harness:
    def __init__(self, path, source=None, input_backend=None):
        self.source = source if source is not None else ControlledSource()
        self.input = input_backend if input_backend is not None else TrackingInput()
        self.reader = asyncio.StreamReader()
        self.writer = MemoryWriter(self.reader)
        self.devices = DeviceStore(path / "devices.json")
        self.pins = PinManager()
        self.session = Session(
            self.reader,
            self.writer,
            server_id="pc",
            server_name="PC",
            devices=self.devices,
            pins=self.pins,
            source_factory=SourceFactory(lambda _: (self.source, self.input)),
        )

    async def __aenter__(self):
        self.task = asyncio.create_task(self.session.run())
        return self

    async def __aexit__(self, *_args):
        self.source.release.set()
        self.reader.feed_eof()
        await self.session.close()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(self.task, 2)

    def send(self, msg):
        self.reader.feed_data(encode_control(msg))

    async def recv(self, timeout=2):
        kind, payload = await asyncio.wait_for(self.writer.frames.get(), timeout)
        assert kind == TYPE_CONTROL
        return decode_control(payload)

    async def authenticate(self):
        pin = self.pins.issue()
        self.send(HELLO)
        assert (await self.recv())["t"] == "auth_required"
        self.send({"t": "auth", "method": "pin", "pin": pin})
        assert (await self.recv())["t"] == "auth_ok"

    async def start(self):
        self.send(START)
        assert await asyncio.to_thread(self.source.entered.wait, 2)


async def test_pin_entry_can_take_longer_than_stream_idle_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "IDLE_TIMEOUT_S", 0.1)
    async with Harness(tmp_path) as h:
        pin = h.pins.issue()
        h.send(HELLO)
        assert (await h.recv())["t"] == "auth_required"
        await asyncio.sleep(0.2)  # user reads the security code and types the PIN
        assert not h.writer.closed.is_set()
        h.send({"t": "auth", "method": "pin", "pin": pin})
        assert (await h.recv())["t"] == "auth_ok"


async def test_pairing_deadline_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "PAIRING_TIMEOUT_S", 0.05)
    async with Harness(tmp_path) as h:
        h.send(HELLO)
        await h.recv()
        await asyncio.wait_for(h.writer.closed.wait(), 1)


async def test_unknown_frames_do_not_extend_pairing_deadline(tmp_path):
    async with Harness(tmp_path) as h:
        h.send(HELLO)
        await h.recv()
        # The pending frame wakes the reader, but must not renew the deadline.
        h.session._handshake_deadline = asyncio.get_running_loop().time() - 1
        h.reader.feed_data(struct.pack(">BI", 0xFF, 0))
        await asyncio.wait_for(h.writer.closed.wait(), 1)


async def test_pre_auth_ping_still_cannot_bypass_authentication(tmp_path):
    async with Harness(tmp_path) as h:
        h.send(HELLO)
        await h.recv()
        h.send({"t": "ping"})
        await asyncio.wait_for(h.writer.closed.wait(), 1)
        assert h.source.starts == 0


async def test_pings_continue_while_capture_permission_is_pending(tmp_path):
    async with Harness(tmp_path, ControlledSource(blocked=True)) as h:
        await h.authenticate()
        await h.start()
        h.send({"t": "ping", "seq": 42, "ts": 123})
        assert await h.recv(timeout=0.5) == {"t": "pong", "seq": 42, "ts": 123}
        assert h.source.stops == 0
        h.source.release.set()
        assert (await h.recv())["t"] == "started"


@pytest.mark.parametrize(
    "failure", [CaptureError("capture_failed", "denied"), RuntimeError("boom")]
)
async def test_start_failure_cleans_both_resources_and_allows_retry(tmp_path, failure):
    async with Harness(tmp_path, ControlledSource(failure=failure)) as h:
        await h.authenticate()
        await h.start()
        error = await h.recv()
        assert error["t"] == "error" and error["code"] == "capture_failed"
        assert h.source.stops == 1
        assert h.input.closes == 1
        assert h.session.state is State.READY
        h.source = ControlledSource()
        h.input = TrackingInput()
        await h.start()
        assert (await h.recv())["t"] == "started"


async def test_disconnect_cancels_pending_capture_and_never_resurrects_session(tmp_path):
    async with Harness(tmp_path, ControlledSource(blocked=True)) as h:
        await h.authenticate()
        await h.start()
        h.send({"t": "bye"})
        await asyncio.wait_for(h.task, 1)
        assert h.source.cancelled.is_set()
        assert h.session.state is State.CLOSED
        assert h.source.stops == 1
        assert h.input.closes == 1
        assert h.writer.frames.empty()  # no late started/error messages


async def test_late_uncancellable_start_result_is_disposed(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "STARTUP_CLEANUP_TIMEOUT_S", 0.02, raising=False)
    source = ControlledSource(blocked=True, cooperative=False)
    async with Harness(tmp_path, source) as h:
        await h.authenticate()
        await h.start()
        startup = h.session._start_task
        await h.session.close()
        assert h.session.state is State.CLOSED
        source.release.set()
        await asyncio.wait_for(startup, 1)
        assert source.stops == 1
        assert h.input.closes == 1
        assert h.session.state is State.CLOSED
        assert h.writer.frames.empty()


async def test_cancelling_start_task_does_not_abandon_executor_resources(tmp_path):
    async with Harness(tmp_path, ControlledSource(blocked=True)) as h:
        await h.authenticate()
        await h.start()
        startup = h.session._start_task
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert h.source.cancelled.is_set()
        assert h.source.stops == 1
        assert h.input.closes == 1
        assert h.session.state is State.READY


async def test_overlapping_starts_are_rejected_without_second_factory_call(tmp_path):
    async with Harness(tmp_path, ControlledSource(blocked=True)) as h:
        await h.authenticate()
        await h.start()
        h.send(START)
        assert (await h.recv(timeout=0.5))["code"] == "bad_request"
        assert h.source.starts == 1
        h.source.release.set()
        assert (await h.recv())["t"] == "started"


async def test_cleanup_releases_mouse_keys_and_touch_even_when_source_stop_fails(tmp_path):
    async with Harness(tmp_path, ControlledSource(stop_failure=True)) as h:
        await h.authenticate()
        await h.start()
        assert (await h.recv())["t"] == "started"
        h.send({"t": "touch", "a": "down", "id": 0, "x": 0.5, "y": 0.5})
        h.send({"t": "mouse", "a": "down", "b": "left"})
        h.send({"t": "key", "code": 30, "down": True})
        h.send({"t": "bye"})
        await asyncio.wait_for(h.task, 1)
        await h.session.close()  # idempotent
        assert ("touch_up", 0) in h.input.events
        assert ("mouse_button", "left", False) in h.input.events
        assert ("key", 30, False) in h.input.events
        assert h.source.stops == 1
        assert h.input.closes == 1
        assert h.writer.closed.is_set()


async def test_restart_ignores_old_source_frames_and_waits_for_a_keyframe(tmp_path):
    async with Harness(tmp_path) as h:
        await h.authenticate()
        await h.start()
        assert (await h.recv())["t"] == "started"
        old_source = h.source
        h.source = ControlledSource()
        h.input = TrackingInput()
        await h.start()
        assert (await h.recv())["t"] == "started"
        assert old_source.stops == 1
        old_source.on_frame(b"old key", 1, True, True)
        h.source.on_frame(b"new delta", 2, False, False)
        h.source.on_frame(b"new key", 3, True, True)
        kind, payload = await asyncio.wait_for(h.writer.frames.get(), 1)
        assert kind == TYPE_VIDEO
        assert payload[9:] == b"new key"
        assert h.source.key_requests >= 1


@pytest.mark.parametrize("starting", [False, True])
async def test_revocation_stops_idle_and_pending_capture_sessions(tmp_path, monkeypatch, starting):
    monkeypatch.setattr(session_module, "AUTHORIZATION_CHECK_INTERVAL_S", 0.02)
    async with Harness(tmp_path, ControlledSource(blocked=True)) as h:
        await h.authenticate()
        if starting:
            await h.start()
        DeviceStore(tmp_path / "devices.json").remove("phone")
        assert (await h.recv())["code"] == "revoked"
        await asyncio.wait_for(h.task, 1)
        assert h.session.state is State.CLOSED
        assert h.source.starts == int(starting)
        assert h.source.stops == int(starting)


async def test_re_pairing_same_client_id_invalidates_old_session(tmp_path, monkeypatch):
    from ubudesk_server.net.auth import generate_token

    monkeypatch.setattr(session_module, "AUTHORIZATION_CHECK_INTERVAL_S", 0.02)
    async with Harness(tmp_path) as h:
        await h.authenticate()
        _, new_hash = generate_token()
        DeviceStore(tmp_path / "devices.json").add("phone", "Phone", new_hash)
        assert (await h.recv())["code"] == "revoked"
        await asyncio.wait_for(h.task, 1)
        assert h.session.state is State.CLOSED


@pytest.mark.parametrize("bitrate", [-1, 0, 60001])
async def test_invalid_initial_bitrate_never_reaches_capture(tmp_path, bitrate):
    async with Harness(tmp_path) as h:
        await h.authenticate()
        h.send(dict(START, bitrate_kbps=bitrate))
        assert (await h.recv())["code"] == "bad_request"
        assert h.source.starts == 0
        assert h.session.state is State.READY

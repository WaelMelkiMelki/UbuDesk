"""Per-client session state machine.

States: HELLO -> AUTH -> READY -> STREAMING -> CLOSED

Security invariant (tested in tests/test_auth.py): no message other than
`hello` and `auth` is processed before AUTH succeeds, and capture/input are
only reachable in READY/STREAMING.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections.abc import Callable
from typing import Any

from .. import PROTOCOL_VERSION
from ..capture.base import CaptureError, StreamSettings, VideoSource
from ..input.base import InputBackend
from ..protocol import (
    TYPE_CONTROL,
    TYPE_VIDEO,
    VIDEO_FLAG_CONFIG,
    VIDEO_FLAG_KEYFRAME,
    ProtocolError,
    encode_control,
    encode_video,
    read_frame,
)
from .auth import DeviceStore, PinManager, generate_token

log = logging.getLogger(__name__)

QUEUE_MAX_FRAMES = 5
IDLE_TIMEOUT_S = 6.0
MAX_TOUCH_SLOTS = 10


class State(enum.Enum):
    HELLO = enum.auto()
    AUTH = enum.auto()
    READY = enum.auto()
    STREAMING = enum.auto()
    CLOSED = enum.auto()


class SourceFactory:
    """Creates a VideoSource + InputBackend pair for one session."""

    def __init__(self, create: Callable[[StreamSettings], tuple[VideoSource, InputBackend]]):
        self._create = create

    def __call__(self, settings: StreamSettings) -> tuple[VideoSource, InputBackend]:
        return self._create(settings)


class Session:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        server_id: str,
        server_name: str,
        devices: DeviceStore,
        pins: PinManager,
        source_factory: SourceFactory,
        on_pair: Callable[[], None] | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._server_id = server_id
        self._server_name = server_name
        self._devices = devices
        self._pins = pins
        self._source_factory = source_factory
        self._on_pair = on_pair

        self.state = State.HELLO
        self.client_id = ""
        self.client_name = ""
        self._source: VideoSource | None = None
        self._input: InputBackend | None = None
        self._loop = asyncio.get_running_loop()
        self._video_queue: asyncio.Queue[tuple[bytes, int, bool, bool] | None] = asyncio.Queue(
            maxsize=QUEUE_MAX_FRAMES * 2
        )
        self._sender_task: asyncio.Task | None = None
        self._dropping_until_key = False
        self._frames_sent = 0
        self._frames_dropped = 0
        self._active_slots: set[int] = set()
        self._pressed_keys: set[int] = set()
        self._peer = writer.get_extra_info("peername")

    # ------------------------------------------------------------------ main

    async def run(self) -> None:
        log.info("client connected: %s", self._peer)
        try:
            while self.state is not State.CLOSED:
                try:
                    ftype, payload = await asyncio.wait_for(
                        read_frame(self._reader), timeout=IDLE_TIMEOUT_S
                    )
                except TimeoutError:
                    log.info("client idle > %.0fs; closing", IDLE_TIMEOUT_S)
                    break
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                if ftype == TYPE_CONTROL:
                    from ..protocol import decode_control

                    msg = decode_control(payload)
                    if not await self._handle(msg):
                        break
                elif ftype == TYPE_VIDEO:
                    raise ProtocolError("client must not send video frames")
                # unknown frame types: payload already consumed, ignore
        except ProtocolError as exc:
            log.warning("protocol violation from %s: %s", self._peer, exc)
        except Exception:
            log.exception("session error")
        finally:
            await self.close()

    async def _handle(self, msg: dict[str, Any]) -> bool:
        """Returns False when the session should end."""
        t = msg["t"]

        if self.state is State.HELLO:
            if t != "hello":
                raise ProtocolError(f"expected hello, got {t!r}")
            return await self._on_hello(msg)

        if self.state is State.AUTH:
            if t != "auth":
                raise ProtocolError(f"expected auth, got {t!r}")
            return await self._on_auth(msg)

        # authenticated states ------------------------------------------------
        if t == "start":
            return await self._on_start(msg)
        if t == "ping":
            await self._send({"t": "pong", "seq": msg.get("seq"), "ts": msg.get("ts")})
            return True
        if t == "idr":
            if self._source is not None:
                self._source.request_keyframe()
            return True
        if t == "bitrate":
            kbps = msg.get("kbps")
            if isinstance(kbps, int) and self._source is not None:
                self._source.set_bitrate(kbps)
            return True
        if t == "stats":
            log.debug("client stats: %s", msg)
            return True
        if t == "bye":
            return False
        if t in ("touch", "mouse", "scroll", "key", "text"):
            if self.state is State.STREAMING:
                self._on_input(t, msg)
            return True
        # unknown "t": ignore per spec
        return True

    # ------------------------------------------------------------- handshake

    async def _on_hello(self, msg: dict[str, Any]) -> bool:
        proto = msg.get("proto")
        if proto != PROTOCOL_VERSION:
            await self._send(
                {
                    "t": "error",
                    "code": "bad_request",
                    "message": f"unsupported protocol version {proto}",
                }
            )
            return False
        client_id = msg.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise ProtocolError("hello missing client_id")
        self.client_id = client_id
        self.client_name = str(msg.get("name", "unknown"))
        codecs = msg.get("codecs", [])
        if "h264" not in codecs:
            await self._send(
                {"t": "error", "code": "bad_request", "message": "client must support h264"}
            )
            return False
        self.state = State.AUTH
        await self._send(
            {
                "t": "auth_required",
                "server_id": self._server_id,
                "server_name": self._server_name,
                "methods": ["token", "pin"],
            }
        )
        return True

    async def _on_auth(self, msg: dict[str, Any]) -> bool:
        method = msg.get("method")
        if method == "token":
            token = msg.get("token", "")
            if isinstance(token, str) and self._devices.verify_token(self.client_id, token):
                self.state = State.READY
                await self._send({"t": "auth_ok"})
                log.info("client %s (%s) authenticated by token", self.client_name, self.client_id)
                return True
            await self._send({"t": "auth_fail", "reason": "unknown_token"})
            return False

        if method == "pin":
            pin = msg.get("pin", "")
            result = self._pins.verify(str(pin))
            if result == "ok":
                token_b64, token_hash = generate_token()
                self._devices.add(self.client_id, self.client_name, token_hash)
                self.state = State.READY
                await self._send({"t": "auth_ok", "token": token_b64})
                log.info("client %s (%s) paired via PIN", self.client_name, self.client_id)
                if self._on_pair is not None:
                    self._on_pair()
                return True
            reason = {"bad_pin": "bad_pin", "pin_expired": "pin_expired", "locked": "locked"}[
                result
            ]
            fail: dict[str, Any] = {"t": "auth_fail", "reason": reason}
            if result == "locked":
                fail["retry_after_s"] = self._pins.lockout_remaining()
            await self._send(fail)
            return False

        raise ProtocolError(f"unknown auth method {method!r}")

    # -------------------------------------------------------------- streaming

    async def _on_start(self, msg: dict[str, Any]) -> bool:
        if self.state is State.STREAMING:
            # Restart with new parameters (e.g. resolution change).
            await self._stop_stream()

        try:
            width = int(msg.get("width", 1920))
            height = int(msg.get("height", 1200))
            fps = int(msg.get("fps", 60))
            bitrate = int(msg.get("bitrate_kbps", 15000))
        except (TypeError, ValueError):
            await self._send({"t": "error", "code": "bad_request", "message": "bad start params"})
            return True
        mode = msg.get("mode", "extend")
        if mode not in ("extend", "mirror"):
            mode = "extend"
        if not (128 <= width <= 7680 and 128 <= height <= 4320 and 1 <= fps <= 120):
            await self._send(
                {"t": "error", "code": "bad_request", "message": "start params out of range"}
            )
            return True

        settings = StreamSettings(width, height, fps, bitrate, mode)
        try:
            # The portal dialog can block for a long time; run in a worker thread.
            source, input_backend, info = await asyncio.get_running_loop().run_in_executor(
                None, self._start_source, settings
            )
        except CaptureError as exc:
            await self._send({"t": "error", "code": exc.code, "message": exc.message})
            return True

        self._source = source
        self._input = input_backend
        self.state = State.STREAMING
        self._dropping_until_key = False
        self._sender_task = asyncio.create_task(self._video_sender())
        await self._send(
            {
                "t": "started",
                "width": info.width,
                "height": info.height,
                "fps": info.fps,
                "codec": "h264",
                "encoder": info.encoder,
            }
        )
        log.info(
            "streaming to %s: %dx%d@%d %s (%s)",
            self.client_name,
            info.width,
            info.height,
            info.fps,
            settings.mode,
            info.encoder,
        )
        return True

    def _start_source(self, settings: StreamSettings):
        source, input_backend = self._source_factory(settings)
        info = source.start(settings, self._on_frame_threadsafe)
        return source, input_backend, info

    def _on_frame_threadsafe(self, data: bytes, pts_us: int, key: bool, has_config: bool) -> None:
        """Called from the capture thread; hop onto the asyncio loop."""
        self._loop.call_soon_threadsafe(self._enqueue_frame, data, pts_us, key, has_config)

    def _enqueue_frame(self, data: bytes, pts_us: int, key: bool, has_config: bool) -> None:
        if self.state is not State.STREAMING:
            return
        if self._dropping_until_key:
            if not key:
                self._frames_dropped += 1
                return
            self._dropping_until_key = False
        if self._video_queue.qsize() >= QUEUE_MAX_FRAMES:
            # Backlog: drop everything until the next keyframe and ask for one.
            self._frames_dropped += 1
            if not self._dropping_until_key:
                self._dropping_until_key = True
                self._drain_queue_nonkey()
                if self._source is not None:
                    self._source.request_keyframe()
                log.debug("send backlog; dropping until next keyframe")
            if not key:
                return
            self._dropping_until_key = False
        with contextlib.suppress(asyncio.QueueFull):
            self._video_queue.put_nowait((data, pts_us, key, has_config))

    def _drain_queue_nonkey(self) -> None:
        kept = []
        while not self._video_queue.empty():
            item = self._video_queue.get_nowait()
            if item is not None and item[2]:
                kept.append(item)
        for item in kept:
            with contextlib.suppress(asyncio.QueueFull):
                self._video_queue.put_nowait(item)

    async def _video_sender(self) -> None:
        try:
            while True:
                item = await self._video_queue.get()
                if item is None:
                    return
                data, pts_us, key, has_config = item
                flags = (VIDEO_FLAG_KEYFRAME if key else 0) | (
                    VIDEO_FLAG_CONFIG if has_config else 0
                )
                self._writer.write(encode_video(pts_us, flags, data))
                await self._writer.drain()
                self._frames_sent += 1
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("video sender failed")

    async def _stop_stream(self) -> None:
        if self._sender_task is not None:
            self._sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender_task
            self._sender_task = None
        if self._source is not None:
            source = self._source
            self._source = None
            await asyncio.get_running_loop().run_in_executor(None, source.stop)
        if self._input is not None:
            self._release_stuck_input()
            self._input.close()
            self._input = None
        while not self._video_queue.empty():
            self._video_queue.get_nowait()
        if self.state is State.STREAMING:
            self.state = State.READY

    # ----------------------------------------------------------------- input

    def _on_input(self, t: str, msg: dict[str, Any]) -> None:
        backend = self._input
        if backend is None:
            return
        try:
            if t == "touch":
                a = msg.get("a")
                slot = int(msg.get("id", 0))
                if not 0 <= slot < MAX_TOUCH_SLOTS:
                    return
                x = float(msg.get("x", 0.0))
                y = float(msg.get("y", 0.0))
                if a == "down":
                    self._active_slots.add(slot)
                    backend.touch_down(slot, x, y)
                elif a == "move":
                    backend.touch_move(slot, x, y)
                elif a == "up":
                    self._active_slots.discard(slot)
                    backend.touch_up(slot)
                elif a == "cancel":
                    self._active_slots.discard(slot)
                    backend.touch_cancel(slot)
            elif t == "mouse":
                a = msg.get("a")
                if a == "move":
                    backend.mouse_move(float(msg.get("x", 0.0)), float(msg.get("y", 0.0)))
                elif a in ("down", "up"):
                    b = msg.get("b", "left")
                    if "x" in msg and "y" in msg:
                        backend.mouse_move(float(msg["x"]), float(msg["y"]))
                    backend.mouse_button(str(b), a == "down")
            elif t == "scroll":
                backend.scroll(float(msg.get("dx", 0.0)), float(msg.get("dy", 0.0)))
            elif t == "key":
                code = int(msg.get("code", 0))
                down = bool(msg.get("down"))
                if 0 < code < 0x300:
                    if down:
                        self._pressed_keys.add(code)
                    else:
                        self._pressed_keys.discard(code)
                    backend.key(code, down)
            elif t == "text":
                s = msg.get("s")
                if isinstance(s, str) and len(s) <= 1024:
                    backend.text(s)
        except (TypeError, ValueError) as exc:
            log.debug("bad input message %s: %s", msg, exc)

    def _release_stuck_input(self) -> None:
        """Lift any fingers/keys still down when the client vanishes."""
        backend = self._input
        if backend is None:
            return
        for slot in list(self._active_slots):
            with contextlib.suppress(Exception):
                backend.touch_up(slot)
        for code in list(self._pressed_keys):
            with contextlib.suppress(Exception):
                backend.key(code, False)
        self._active_slots.clear()
        self._pressed_keys.clear()

    # ----------------------------------------------------------------- close

    async def _send(self, msg: dict[str, Any]) -> None:
        self._writer.write(encode_control(msg))
        await self._writer.drain()

    async def close(self) -> None:
        if self.state is State.CLOSED:
            return
        prev = self.state
        self.state = State.CLOSED
        if prev is State.STREAMING:
            self.state = State.STREAMING  # let _stop_stream see it
            await self._stop_stream()
            self.state = State.CLOSED
        with contextlib.suppress(Exception):
            self._writer.close()
            await self._writer.wait_closed()
        log.info(
            "client disconnected: %s (sent=%d dropped=%d)",
            self._peer,
            self._frames_sent,
            self._frames_dropped,
        )

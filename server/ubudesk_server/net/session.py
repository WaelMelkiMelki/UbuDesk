"""Per-client session state machine.

States: HELLO -> AUTH -> READY -> STARTING -> STREAMING -> CLOSED

Security invariant (tested in tests/test_session_security.py): no message other than
`hello` and `auth` is processed before AUTH succeeds. Capture startup is
reachable only after authentication; input is accepted only while STREAMING.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import threading
from collections.abc import Callable
from typing import Any

from .. import PROTOCOL_VERSION
from ..capture.base import CaptureError, StreamInfo, StreamSettings, VideoSource
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
from .auth import DeviceStore, PinManager, generate_token, token_fingerprint

log = logging.getLogger(__name__)

QUEUE_MAX_FRAMES = 5
IDLE_TIMEOUT_S = 6.0
PAIRING_TIMEOUT_S = 120.0
AUTHORIZATION_CHECK_INTERVAL_S = 1.0
STARTUP_CLEANUP_TIMEOUT_S = 1.0
MAX_TOUCH_SLOTS = 10


class State(enum.Enum):
    HELLO = enum.auto()
    AUTH = enum.auto()
    READY = enum.auto()
    STARTING = enum.auto()
    STREAMING = enum.auto()
    CLOSED = enum.auto()


class SourceFactory:
    """Creates a VideoSource + InputBackend pair for one session."""

    def __init__(self, create: Callable[[StreamSettings], tuple[VideoSource, InputBackend]]):
        self._create = create

    def __call__(self, settings: StreamSettings) -> tuple[VideoSource, InputBackend]:
        return self._create(settings)


class _CaptureAttempt:
    """One worker-owned startup. Cancellation signals; it never calls stop concurrently."""

    def __init__(self, generation: int):
        self.generation = generation
        self.cancelled = threading.Event()
        self.source: VideoSource | None = None

    def cancel(self) -> None:
        self.cancelled.set()
        source = self.source
        if source is not None:
            try:
                source.cancel_start()
            except Exception:
                log.exception("capture startup cancellation failed")


Capture = tuple[VideoSource, InputBackend, StreamInfo]


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
        self._handshake_deadline = self._loop.time() + IDLE_TIMEOUT_S
        self._authorized_hash: str | None = None
        self._authorization_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._attempt: _CaptureAttempt | None = None
        self._generation = 0
        self._close_task: asyncio.Task[None] | None = None
        self._video_queue: asyncio.Queue[tuple[bytes, int, bool, bool] | None] = asyncio.Queue(
            maxsize=QUEUE_MAX_FRAMES * 2
        )
        self._sender_task: asyncio.Task | None = None
        self._dropping_until_key = False
        self._frames_sent = 0
        self._frames_dropped = 0
        self._active_slots: set[int] = set()
        self._pressed_keys: set[int] = set()
        self._pressed_buttons: set[str] = set()
        self._peer = writer.get_extra_info("peername")

    # ------------------------------------------------------------------ main

    async def run(self) -> None:
        log.info("client connected: %s", self._peer)
        try:
            while self.state is not State.CLOSED:
                timeout = IDLE_TIMEOUT_S
                if self.state in (State.HELLO, State.AUTH):
                    timeout = max(0.0, self._handshake_deadline - self._loop.time())
                try:
                    ftype, payload = await asyncio.wait_for(read_frame(self._reader), timeout)
                except asyncio.TimeoutError:
                    log.info("client timed out in %s; closing", self.state.name)
                    break
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                if self.state is State.CLOSED:
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
        if self.state is State.CLOSED:
            return False

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
        self._handshake_deadline = self._loop.time() + PAIRING_TIMEOUT_S
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
            if isinstance(token, str) and await asyncio.to_thread(
                self._devices.verify_token, self.client_id, token
            ):
                if self.state is State.CLOSED:
                    return False
                self._authenticated(token_fingerprint(token))
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
                await asyncio.to_thread(
                    self._devices.add, self.client_id, self.client_name, token_hash
                )
                if self.state is State.CLOSED:
                    return False
                self._authenticated(token_hash)
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

    def _authenticated(self, token_hash: str | None) -> None:
        assert token_hash is not None
        self._authorized_hash = token_hash
        self.state = State.READY
        self._authorization_task = asyncio.create_task(self._watch_authorization())

    async def _watch_authorization(self) -> None:
        """CLI revocation also ends idle, starting, and streaming sessions."""
        assert self._authorized_hash is not None
        try:
            while self.state is not State.CLOSED:
                await asyncio.sleep(AUTHORIZATION_CHECK_INTERVAL_S)
                if not await asyncio.to_thread(
                    self._devices.is_authorized, self.client_id, self._authorized_hash
                ):
                    log.info("pairing revoked for %s; closing session", self.client_id)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            self._send(
                                {"t": "error", "code": "revoked", "message": "Pairing was revoked."}
                            ),
                            0.5,
                        )
                    break
            else:
                return
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("could not recheck device authorization; closing session")
        await self.close()

    # -------------------------------------------------------------- streaming

    async def _on_start(self, msg: dict[str, Any]) -> bool:
        if self.state is State.STARTING:
            await self._send(
                {"t": "error", "code": "bad_request", "message": "capture startup already pending"}
            )
            return True
        try:
            width = int(msg.get("width", 1920))
            height = int(msg.get("height", 1200))
            fps = int(msg.get("fps", 60))
            bitrate = int(msg.get("bitrate_kbps", 15000))
        except (TypeError, ValueError, OverflowError):
            await self._send({"t": "error", "code": "bad_request", "message": "bad start params"})
            return True
        mode = msg.get("mode", "extend")
        if mode not in ("extend", "mirror"):
            mode = "extend"
        if not (
            128 <= width <= 7680
            and 128 <= height <= 4320
            and 1 <= fps <= 120
            and 500 <= bitrate <= 60000
        ):
            await self._send(
                {"t": "error", "code": "bad_request", "message": "start params out of range"}
            )
            return True

        # Do not await startup in the reader loop: permission dialogs need human
        # time, while pings, disconnects and revocations must remain responsive.
        self.state = State.STARTING
        self._generation += 1
        attempt = _CaptureAttempt(self._generation)
        self._attempt = attempt
        settings = StreamSettings(width, height, fps, bitrate, mode)
        self._start_task = asyncio.create_task(self._start_stream(settings, attempt))
        return True

    async def _start_stream(self, settings: StreamSettings, attempt: _CaptureAttempt) -> None:
        capture: Capture | None = None
        error: CaptureError | None = None
        try:
            await self._stop_stream()
            if attempt.cancelled.is_set() or self.state is State.CLOSED:
                return
            worker = self._loop.run_in_executor(None, self._start_source, settings, attempt)
            try:
                # Cancelling an asyncio executor future does NOT stop its thread.
                # Keep ownership of its result so late resources can be disposed.
                capture = await asyncio.shield(worker)
            except asyncio.CancelledError:
                attempt.cancel()
                with contextlib.suppress(Exception):
                    capture = await asyncio.shield(worker)
                raise
            if capture is None or self.state is State.CLOSED or attempt.cancelled.is_set():
                return
            source, input_backend, info = capture
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
            if self.state is State.CLOSED or attempt.cancelled.is_set():
                return
            # Ownership transfers atomically, with no await between the check
            # and publication. CLOSED is terminal, even if start() finishes late.
            self._source, self._input = source, input_backend
            capture = None
            self._dropping_until_key = True
            self.state = State.STREAMING
            self._sender_task = asyncio.create_task(self._video_sender())
            try:
                source.request_keyframe()  # startup frames may have been discarded
            except Exception:
                log.exception("initial keyframe request failed")
            log.info(
                "streaming to %s: %dx%d@%d %s (%s)",
                self.client_name,
                info.width,
                info.height,
                info.fps,
                settings.mode,
                info.encoder,
            )
        except CaptureError as exc:
            error = exc
        except asyncio.CancelledError:
            attempt.cancel()
            raise
        except Exception:
            log.exception("capture startup failed")
            error = CaptureError("capture_failed", "Capture startup failed; check the server log.")
        finally:
            if capture is not None:
                await asyncio.to_thread(self._dispose_capture, capture[0], capture[1])
            attempt.source = None
            self._attempt = None
            self._start_task = None
            if self.state is State.STARTING:
                self.state = State.READY
        if error is not None and self.state is not State.CLOSED:
            try:
                await self._send({"t": "error", "code": error.code, "message": error.message})
            except ConnectionError:
                await self.close()

    def _start_source(self, settings: StreamSettings, attempt: _CaptureAttempt) -> Capture | None:
        if attempt.cancelled.is_set():
            return None
        source, input_backend = self._source_factory(settings)
        attempt.source = source
        committed = False
        try:
            if attempt.cancelled.is_set():
                return None

            def on_frame(data: bytes, pts_us: int, key: bool, has_config: bool) -> None:
                self._on_frame_threadsafe(data, pts_us, key, has_config, attempt.generation)

            info = source.start(settings, on_frame)
            if attempt.cancelled.is_set():
                return None
            committed = True
            return source, input_backend, info
        finally:
            if not committed:
                self._dispose_capture(source, input_backend)

    def _on_frame_threadsafe(
        self, data: bytes, pts_us: int, key: bool, has_config: bool, generation: int | None = None
    ) -> None:
        """Called from capture threads, including a possibly late/cancelled source."""
        with contextlib.suppress(RuntimeError):  # loop may already have shut down
            self._loop.call_soon_threadsafe(
                self._enqueue_frame, data, pts_us, key, has_config, generation
            )

    def _enqueue_frame(
        self, data: bytes, pts_us: int, key: bool, has_config: bool, generation: int | None = None
    ) -> None:
        if self.state is not State.STREAMING:
            return
        if generation is not None and generation != self._generation:
            return
        if self._dropping_until_key:
            if not key:
                self._frames_dropped += 1
                return
            self._dropping_until_key = False
        if self._video_queue.qsize() >= QUEUE_MAX_FRAMES:
            self._frames_dropped += 1
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
        # Detach before awaiting; concurrent close/restart must never dispose the
        # same capture twice. Release held input BEFORE the portal is closed.
        sender, self._sender_task = self._sender_task, None
        source, self._source = self._source, None
        backend, self._input = self._input, None
        slots, self._active_slots = self._active_slots, set()
        keys, self._pressed_keys = self._pressed_keys, set()
        buttons, self._pressed_buttons = self._pressed_buttons, set()
        while not self._video_queue.empty():
            self._video_queue.get_nowait()
        if self.state is State.STREAMING:
            self.state = State.READY
        if sender is not None:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
        if source is not None or backend is not None:
            await asyncio.to_thread(self._dispose_capture, source, backend, slots, keys, buttons)

    @staticmethod
    def _dispose_capture(
        source: VideoSource | None,
        backend: InputBackend | None,
        slots: set[int] | None = None,
        keys: set[int] | None = None,
        buttons: set[str] | None = None,
    ) -> None:
        if backend is not None:
            for slot in slots or ():
                with contextlib.suppress(Exception):
                    backend.touch_up(slot)
            for code in keys or ():
                with contextlib.suppress(Exception):
                    backend.key(code, False)
            for button in buttons or ():
                with contextlib.suppress(Exception):
                    backend.mouse_button(button, False)
            try:
                backend.close()
            except Exception:
                log.exception("input cleanup failed")
        if source is not None:
            try:
                source.stop()
            except Exception:
                log.exception("capture cleanup failed")

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
                    if a == "down":
                        self._pressed_buttons.add(str(b))
                    else:
                        self._pressed_buttons.discard(str(b))
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

    # ----------------------------------------------------------------- close

    async def _send(self, msg: dict[str, Any]) -> None:
        if self.state is State.CLOSED:
            return
        self._writer.write(encode_control(msg))
        await self._writer.drain()

    async def close(self) -> None:
        if self._close_task is None:
            self.state = State.CLOSED
            self._generation += 1
            if self._attempt is not None:
                self._attempt.cancel()
            # Close the connection immediately, not after a human permission
            # dialog or a slow native capture cleanup has finished.
            with contextlib.suppress(Exception):
                self._writer.close()
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        if self._authorization_task is not None:
            self._authorization_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._authorization_task
        await self._stop_stream()
        startup = self._start_task
        if startup is not None:
            try:
                await asyncio.wait_for(asyncio.shield(startup), STARTUP_CLEANUP_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Keep the task alive. It still owns the worker's eventual result
                # and will dispose it; never cancel/forget an executor future.
                log.info("capture startup is still unwinding in the background")
            except asyncio.CancelledError:
                pass
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        log.info(
            "client disconnected: %s (sent=%d dropped=%d)",
            self._peer,
            self._frames_sent,
            self._frames_dropped,
        )

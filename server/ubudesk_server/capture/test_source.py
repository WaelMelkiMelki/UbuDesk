"""Test-pattern video source: a moving ball + frame counter, encoded with
libx264 through PyAV. Pure userspace - runs headless in CI and in any sandbox,
no GStreamer, no display, no GPU.

The encoded output is real H.264 (Annex-B, no B-frames, SPS/PPS repeated on
every IDR), so it exercises the exact same client decode path as live capture.
"""

from __future__ import annotations

import logging
import threading
import time

from .base import CaptureError, OnFrame, StreamInfo, StreamSettings, VideoSource, even

log = logging.getLogger(__name__)


class TestPatternSource(VideoSource):
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._force_key = threading.Event()
        self._bitrate_kbps = 0
        self._reconfigure = threading.Event()
        self._settings: StreamSettings | None = None

    # -- VideoSource ---------------------------------------------------------

    def start(self, settings: StreamSettings, on_frame: OnFrame) -> StreamInfo:
        try:
            import av  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise CaptureError("capture_failed", f"PyAV not installed: {exc}") from exc

        width = even(settings.width)
        height = even(settings.height)
        fps = max(1, min(120, settings.fps))
        self._settings = StreamSettings(width, height, fps, settings.bitrate_kbps, settings.mode)
        self._bitrate_kbps = settings.bitrate_kbps
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(on_frame,), name="ubudesk-testsrc", daemon=True
        )
        self._thread.start()
        return StreamInfo(width, height, fps, "libx264 (test pattern)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def request_keyframe(self) -> None:
        self._force_key.set()

    def set_bitrate(self, kbps: int) -> None:
        self._bitrate_kbps = max(500, min(60000, kbps))
        self._reconfigure.set()

    # -- encoding loop -------------------------------------------------------

    def _make_encoder(self, width: int, height: int, fps: int):
        import av

        ctx = av.CodecContext.create("libx264", "w")
        ctx.width = width
        ctx.height = height
        ctx.pix_fmt = "yuv420p"
        ctx.framerate = fps  # type: ignore[assignment]
        ctx.bit_rate = self._bitrate_kbps * 1000
        ctx.options = {
            "tune": "zerolatency",
            "preset": "ultrafast",
            "profile": "baseline",
            "x264-params": f"bframes=0:keyint={2 * fps}:min-keyint={fps}:"
            "repeat-headers=1:annexb=1:scenecut=0",
        }
        return ctx

    def _run(self, on_frame: OnFrame) -> None:
        import av
        from av.video.frame import PictureType

        assert self._settings is not None
        s = self._settings
        w, h, fps = s.width, s.height, s.fps
        ctx = self._make_encoder(w, h, fps)
        frame = av.VideoFrame(w, h, "yuv420p")
        painter = _Painter(w, h)
        interval = 1.0 / fps
        next_deadline = time.monotonic()
        index = 0
        log.info("test source running: %dx%d@%d, %d kbps", w, h, fps, self._bitrate_kbps)

        while not self._stop.is_set():
            if self._reconfigure.is_set():
                self._reconfigure.clear()
                ctx = self._make_encoder(w, h, fps)
                self._force_key.set()
                log.info("test source: encoder reconfigured to %d kbps", self._bitrate_kbps)

            painter.paint(frame, index)
            frame.pts = index
            if self._force_key.is_set():
                self._force_key.clear()
                frame.pict_type = PictureType.I
            else:
                frame.pict_type = PictureType.NONE

            try:
                packets = ctx.encode(frame)
            except av.FFmpegError as exc:  # pragma: no cover
                log.error("test source encode failed: %s", exc)
                break
            for pkt in packets:
                data = bytes(pkt)
                pts_us = int((pkt.pts or index) * 1_000_000 // fps)
                key = bool(pkt.is_keyframe)
                # repeat-headers=1 guarantees SPS/PPS in every IDR AU
                on_frame(data, pts_us, key, key)

            index += 1
            next_deadline += interval
            delay = next_deadline - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -1.0:  # fell badly behind; resync instead of bursting
                next_deadline = time.monotonic()

        log.info("test source stopped after %d frames", index)


class _Painter:
    """Draws a gray background, a bouncing ball and a binary frame counter
    into a YUV420p frame, without numpy."""

    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.bg_y = bytes([0x50]) * (width * height)
        self.bg_u = bytes([0x80]) * (width * height // 4)
        # slight tint so the picture is obviously "in color"
        self.bg_v = bytes([0x90]) * (width * height // 4)
        self.radius = max(8, min(width, height) // 16)

    def paint(self, frame, index: int) -> None:
        w, h, r = self.w, self.h, self.radius
        y = bytearray(self.bg_y)

        # bouncing ball (drawn as a filled circle in the luma plane)
        span_x = max(1, w - 2 * r)
        span_y = max(1, h - 2 * r)
        tx = (index * 7) % (2 * span_x)
        ty = (index * 5) % (2 * span_y)
        cx = r + (tx if tx < span_x else 2 * span_x - tx)
        cy = r + (ty if ty < span_y else 2 * span_y - ty)
        row_white = bytes([0xEB]) * (2 * r + 1)
        for dy in range(-r, r + 1):
            yy = cy + dy
            if 0 <= yy < h:
                half = int((r * r - dy * dy) ** 0.5)
                x0 = max(0, cx - half)
                x1 = min(w, cx + half)
                if x1 > x0:
                    y[yy * w + x0 : yy * w + x1] = row_white[: x1 - x0]

        # frame counter: 16-bit binary strip along the top edge
        cell = max(2, w // 64)
        bar_h = max(2, h // 40)
        for bit in range(16):
            value = 0xEB if (index >> (15 - bit)) & 1 else 0x10
            block = bytes([value]) * cell
            x0 = bit * cell
            for yy in range(bar_h):
                y[yy * w + x0 : yy * w + x0 + cell] = block

        frame.planes[0].update(bytes(y))
        frame.planes[1].update(self.bg_u)
        frame.planes[2].update(self.bg_v)

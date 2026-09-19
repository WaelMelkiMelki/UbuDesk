"""GStreamer pipeline for real screen capture (PipeWire -> H.264).

Requires PyGObject + GStreamer, present on a normal Ubuntu desktop after
`server/scripts/install-deps.sh`. Cannot run headless in CI - the CI path is
capture/test_source.py.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import encoders
from .base import CaptureError, OnFrame, StreamInfo, StreamSettings, VideoSource, even
from .portal_session import PortalError, PortalSession

log = logging.getLogger(__name__)


class PortalSource(VideoSource):
    """VideoSource backed by an xdg-desktop-portal ScreenCast stream.

    Owns the PortalSession (also used for input injection by the input
    backend - see input/portal_input.py) and a GStreamer pipeline:

        pipewiresrc fd=F path=N do-timestamp=true keepalive-time=100
          ! videorate drop-only=true max-rate=FPS
          ! videoconvert ! videoscale
          ! video/x-raw,format=I420,width=W,height=H
          ! ENCODER
          ! video/x-h264,stream-format=byte-stream,alignment=au
          ! h264parse config-interval=-1
          ! appsink emit-signals=true sync=false max-buffers=2 drop=true

    keepalive-time=100 re-sends the last frame every 100 ms; PipeWire screen
    casts only produce frames on damage, and a fully static desktop would
    otherwise starve (and eventually disconnect) the client.
    """

    def __init__(self, mode: str, encoder_pref: str = "auto", restore_token: str = ""):
        self._mode = mode
        self._encoder_pref = encoder_pref
        self._restore_token = restore_token
        self._pipeline: Any = None
        self._encoder_element: Any = None
        self._encoder_name = ""
        self._glib_loop_thread: threading.Thread | None = None
        self._glib_loop: Any = None
        self.session: PortalSession | None = None
        self._fps = 30
        self._stopped = threading.Event()

    # -- VideoSource ---------------------------------------------------------

    def start(self, settings: StreamSettings, on_frame: OnFrame) -> StreamInfo:
        if not encoders.gst_available():
            raise CaptureError(
                "capture_failed",
                "PyGObject/GStreamer not available. Run server/scripts/install-deps.sh "
                "and create the venv with --system-site-packages.",
            )
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst

        if not Gst.is_initialized():
            Gst.init(None)

        width = even(settings.width)
        height = even(settings.height)
        fps = max(1, min(120, settings.fps))
        self._fps = fps

        # 1. portal session (may pop the GNOME permission dialog)
        try:
            self.session = PortalSession(restore_token=self._restore_token)
            self.session.open(self._mode, want_input=True)
        except PortalError as exc:
            code = "no_virtual_monitor" if "no_virtual_monitor" in str(exc) else "capture_failed"
            raise CaptureError(code, str(exc)) from exc

        # 2. encoder
        factory = encoders.find_encoder(self._encoder_pref)
        if factory is None:
            self.session.close()
            raise CaptureError(
                "capture_failed",
                f"no usable H.264 encoder for preference {self._encoder_pref!r}; "
                "install gstreamer1.0-plugins-ugly for x264enc",
            )
        self._encoder_name = factory
        log.info("using encoder: %s", factory)

        # 3. pipeline
        enc_frag = encoders.encoder_fragment(factory, settings.bitrate_kbps, fps)
        desc = (
            f"pipewiresrc name=src fd={self.session.pipewire_fd} "
            f"path={self.session.node_id} do-timestamp=true keepalive-time=100 "
            f"! videorate drop-only=true max-rate={fps} "
            f"! videoconvert ! videoscale "
            f"! video/x-raw,format=I420,width={width},height={height} "
            f"! {enc_frag} "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! h264parse config-interval=-1 "
            f"! appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
        )
        log.debug("pipeline: %s", desc)
        try:
            self._pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            self.session.close()
            raise CaptureError("capture_failed", f"pipeline construction failed: {exc}") from exc

        self._encoder_element = self._pipeline.get_by_name("enc")
        applied = encoders.apply_properties(
            self._encoder_element, factory, settings.bitrate_kbps, fps
        )
        if applied:
            log.info("encoder properties applied: %s", applied)

        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample, on_frame)

        # GLib loop for bus messages (portal D-Bus already pumped in open()).
        self._start_glib_loop()

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise CaptureError("capture_failed", "pipeline refused to start (state FAILURE)")
        # Wait for PLAYING (or error) up to 10 s.
        ret, state, _pending = self._pipeline.get_state(10 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE or state != Gst.State.PLAYING:
            self.stop()
            raise CaptureError("capture_failed", f"pipeline did not reach PLAYING ({ret})")

        actual = self.session.stream_size or (width, height)
        if actual != (width, height):
            log.info(
                "portal created %sx%s, scaling to requested %sx%s in-pipeline",
                actual[0],
                actual[1],
                width,
                height,
            )
        self.request_keyframe()
        return StreamInfo(width, height, fps, factory)

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self._pipeline is not None:
            from gi.repository import Gst

            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        if self._glib_loop is not None:
            self._glib_loop.quit()
            self._glib_loop = None
        if self.session is not None:
            self.session.close()

    def request_keyframe(self) -> None:
        if self._encoder_element is None:
            return
        from gi.repository import Gst, GstVideo

        event = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
        pad = self._encoder_element.get_static_pad("src")
        if pad is not None:
            pad.push_event(event)

    def set_bitrate(self, kbps: int) -> None:
        if self._encoder_element is None:
            return
        kbps = max(500, min(60000, kbps))
        if encoders.set_bitrate(self._encoder_element, self._encoder_name, kbps):
            log.info("encoder bitrate set to %d kbps", kbps)
        else:
            log.warning("encoder %s has no runtime bitrate property", self._encoder_name)

    # -- internals -----------------------------------------------------------

    def _on_new_sample(self, sink, on_frame: OnFrame):
        from gi.repository import Gst

        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        pts = buf.pts
        pts_us = int(pts / 1000) if pts != Gst.CLOCK_TIME_NONE else 0
        # h264parse config-interval=-1 repeats SPS/PPS before each IDR.
        on_frame(data, pts_us, keyframe, keyframe)
        return Gst.FlowReturn.OK

    def _start_glib_loop(self) -> None:
        from gi.repository import GLib, Gst

        self._glib_loop = GLib.MainLoop()
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()

        def on_message(_bus, message):
            if message.type == Gst.MessageType.ERROR:
                err, dbg = message.parse_error()
                log.error("gstreamer error: %s (%s)", err, dbg)
            elif message.type == Gst.MessageType.EOS:
                log.warning("gstreamer EOS")
            return True

        bus.connect("message", on_message)

        def run():
            self._glib_loop.run()

        self._glib_loop_thread = threading.Thread(target=run, name="ubudesk-glib", daemon=True)
        self._glib_loop_thread.start()

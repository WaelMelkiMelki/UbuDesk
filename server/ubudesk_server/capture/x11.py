"""X11 capture backend: mirror via ximagesrc, extend via the xrandr ladder.

Extend ladder (first step that works wins):

  1. xrandr VIRTUAL output (xf86-video-dummy / intel virtual heads):
     --newmode + --addmode + --output VIRTUALn --mode ... --right-of primary
  2. Any *disconnected* physical connector forced on the same way
     (works on modesetting/amdgpu/intel for most connector types).
     EVDI note: when the evdi kernel module is loaded its virtual connector
     shows up as a disconnected DVI output, so it is picked up by this step.
  3. `xrandr --fb` enlarge + `--setmonitor UbuDesk` region (no real CRTC:
     some compositors will not render there; best effort, still lets windows
     be dragged into the region on many setups).
  4. Nothing works -> CaptureError("no_virtual_monitor") and the client is
     told to use mirror mode.

All xrandr interaction goes through an injectable runner so the ladder logic
is unit-testable without a display. Cleanup is registered with atexit AND
executed on stop(); stale state from a crashed previous run is removed on
start (mode/monitor names are deterministic: "ubudesk_*" / "UbuDesk").
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import encoders
from .base import CaptureError, OnFrame, StreamInfo, StreamSettings, VideoSource, even

log = logging.getLogger(__name__)

MONITOR_NAME = "UbuDesk"
MODE_PREFIX = "ubudesk_"

Runner = Callable[[list[str]], "CmdResult"]


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def real_runner(cmd: list[str]) -> CmdResult:
    log.debug("run: %s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)  # noqa: S603
        return CmdResult(p.returncode, p.stdout, p.stderr)
    except (subprocess.SubprocessError, OSError) as exc:
        return CmdResult(127, "", str(exc))


# --------------------------------------------------------------------------
# xrandr output parsing (pure functions, unit tested)


@dataclass
class XOutput:
    name: str
    connected: bool
    active: bool  # has a CRTC / geometry right now
    geometry: tuple[int, int, int, int] | None = None  # w, h, x, y
    modes: list[str] = field(default_factory=list)


@dataclass
class XScreen:
    fb_width: int
    fb_height: int
    outputs: list[XOutput]

    def primary_geometry(self) -> tuple[int, int, int, int] | None:
        for o in self.outputs:
            if o.active and o.geometry:
                return o.geometry
        return None

    def find(self, name: str) -> XOutput | None:
        for o in self.outputs:
            if o.name == name:
                return o
        return None


_SCREEN_RE = re.compile(r"current (\d+) x (\d+)")
_OUTPUT_RE = re.compile(
    r"^(\S+) (connected|disconnected)(?: primary)?(?: (\d+)x(\d+)\+(\d+)\+(\d+))?"
)
_MODE_RE = re.compile(r"^\s+(\S+)\s+[\d.]+")


def parse_xrandr_query(text: str) -> XScreen:
    fb_w = fb_h = 0
    outputs: list[XOutput] = []
    current: XOutput | None = None
    for line in text.splitlines():
        if line.startswith("Screen "):
            m = _SCREEN_RE.search(line)
            if m:
                fb_w, fb_h = int(m.group(1)), int(m.group(2))
            continue
        m = _OUTPUT_RE.match(line)
        if m:
            geo = None
            if m.group(3):
                geo = (int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6)))
            current = XOutput(
                name=m.group(1),
                connected=m.group(2) == "connected",
                active=geo is not None,
                geometry=geo,
            )
            outputs.append(current)
            continue
        if current is not None:
            mm = _MODE_RE.match(line)
            if mm:
                current.modes.append(mm.group(1))
    return XScreen(fb_w, fb_h, outputs)


def pick_extend_output(screen: XScreen) -> XOutput | None:
    """Ladder steps 1+2: best candidate output for a forced mode."""
    virtuals = [o for o in screen.outputs if re.match(r"VIRTUAL\d+$", o.name) and not o.active]
    if virtuals:
        return virtuals[0]
    # step 2: disconnected inactive connector (EVDI connectors land here too)
    for o in screen.outputs:
        if not o.connected and not o.active:
            return o
    return None


def cvt_modeline_fallback(width: int, height: int, fps: int) -> tuple[str, list[str]]:
    """CVT-RB-ish modeline when the `cvt` binary is unavailable.

    Returns (mode_name, modeline_args). Reduced blanking: hblank=160,
    vertical blank sized for >=460us. Good enough for virtual outputs.
    """
    htotal = width + 160
    h_sync_start = width + 48
    h_sync_end = width + 48 + 32
    # vertical blanking: at least 460us worth of lines, min 3+6+6
    period_us = 1_000_000.0 / fps
    h_period_us = (period_us - 460.0) / height
    vbi = max(int(460.0 / h_period_us) + 1, 15)
    vtotal = height + vbi
    v_sync_start = height + 3
    v_sync_end = height + 3 + 6
    pclk_khz = htotal * vtotal * fps / 1000.0
    pclk_mhz = round(pclk_khz / 250.0) * 0.25  # quantize to 0.25 MHz
    name = f"{MODE_PREFIX}{width}x{height}_{fps}"
    args = [
        f"{pclk_mhz:.2f}",
        str(width),
        str(h_sync_start),
        str(h_sync_end),
        str(htotal),
        str(height),
        str(v_sync_start),
        str(v_sync_end),
        str(vtotal),
        "+hsync",
        "-vsync",
    ]
    return name, args


def parse_cvt_output(text: str, width: int, height: int, fps: int) -> tuple[str, list[str]] | None:
    """Parse `cvt -r W H F` output into (mode_name, modeline_args)."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Modeline"):
            parts = line.split()
            # Modeline "1920x1080R" 138.50 1920 1968 2000 2080 1080 1083 1088 1111 +hsync -vsync
            args = parts[2:]
            name = f"{MODE_PREFIX}{width}x{height}_{fps}"
            return name, args
    return None


# --------------------------------------------------------------------------
# ladder driver


class XrandrExtend:
    """Creates and tears down the virtual X11 output/region."""

    def __init__(self, runner: Runner = real_runner, cvt_path: str | None = None):
        self.run = runner
        self._cvt_path = cvt_path if cvt_path is not None else shutil.which("cvt")
        self.method: str = ""  # "output" | "setmonitor"
        self.output_name: str = ""
        self.mode_name: str = ""
        self.region: tuple[int, int, int, int] | None = None  # w,h,x,y
        self._orig_fb: tuple[int, int] | None = None
        self._cleaned = False

    # ---- queries ----

    def query(self) -> XScreen:
        res = self.run(["xrandr", "--query"])
        if not res.ok:
            raise CaptureError(
                "capture_failed", f"xrandr --query failed: {res.stderr or res.stdout}"
            )
        return parse_xrandr_query(res.stdout)

    def modeline(self, width: int, height: int, fps: int) -> tuple[str, list[str]]:
        if self._cvt_path:
            res = self.run([self._cvt_path, "-r", str(width), str(height), str(fps)])
            if res.ok:
                parsed = parse_cvt_output(res.stdout, width, height, fps)
                if parsed:
                    return parsed
        return cvt_modeline_fallback(width, height, fps)

    # ---- stale-state cleanup (called on every start) ----

    def clean_stale(self) -> None:
        screen = self.query()
        # remove leftover UbuDesk monitor region
        self.run(["xrandr", "--delmonitor", MONITOR_NAME])
        # disable any output still running one of our modes, then delete them
        for output in screen.outputs:
            stale = [m for m in output.modes if m.startswith(MODE_PREFIX)]
            if not stale:
                continue
            if output.active:
                self.run(["xrandr", "--output", output.name, "--off"])
            for mode in stale:
                self.run(["xrandr", "--delmode", output.name, mode])
        # rmmode for any remaining ubudesk_* mode is harmless if absent
        # (xrandr prints an error we ignore)

    # ---- create ----

    def create(self, width: int, height: int, fps: int) -> tuple[int, int, int, int]:
        """Returns the capture region (w, h, x, y). Raises CaptureError."""
        self.clean_stale()
        screen = self.query()
        primary = screen.primary_geometry()
        if primary is None:
            raise CaptureError("capture_failed", "xrandr reports no active output")

        candidate = pick_extend_output(screen)
        if candidate is not None:
            self._create_on_output(candidate, width, height, fps, screen)
        else:
            self._create_setmonitor(width, height, screen)

        atexit.register(self.cleanup)
        assert self.region is not None
        return self.region

    def _create_on_output(
        self, output: XOutput, width: int, height: int, fps: int, screen: XScreen
    ) -> None:
        name, args = self.modeline(width, height, fps)
        self.mode_name = name
        self.output_name = output.name

        res = self.run(["xrandr", "--newmode", name, *args])
        if not res.ok and "already" not in (res.stderr + res.stdout):
            raise CaptureError(
                "no_virtual_monitor", f"xrandr --newmode failed: {res.stderr or res.stdout}"
            )
        res = self.run(["xrandr", "--addmode", output.name, name])
        if not res.ok:
            self.run(["xrandr", "--rmmode", name])
            raise CaptureError(
                "no_virtual_monitor",
                f"xrandr --addmode {output.name} failed (driver refuses forced modes): "
                f"{res.stderr or res.stdout}",
            )
        res = self.run(
            ["xrandr", "--output", output.name, "--mode", name, "--right-of", _primary_name(screen)]
        )
        if not res.ok:
            self.run(["xrandr", "--delmode", output.name, name])
            self.run(["xrandr", "--rmmode", name])
            raise CaptureError(
                "no_virtual_monitor",
                f"xrandr could not enable {output.name}: {res.stderr or res.stdout}",
            )
        self.method = "output"
        # geometry: to the right of the primary
        after = self.query()
        new = after.find(output.name)
        if new and new.geometry:
            w, h, x, y = new.geometry
            self.region = (w, h, x, y)
        else:
            prim = screen.primary_geometry()
            assert prim is not None
            self.region = (width, height, prim[0] + prim[2], prim[3])
        log.info("X11 extend: forced mode %s on %s, region=%s", name, output.name, self.region)

    def _create_setmonitor(self, width: int, height: int, screen: XScreen) -> None:
        prim = screen.primary_geometry()
        assert prim is not None
        pw, ph, px, py = prim
        new_x = px + pw
        fb_w = max(screen.fb_width, new_x + width)
        fb_h = max(screen.fb_height, max(py + ph, height))
        self._orig_fb = (screen.fb_width, screen.fb_height)

        res = self.run(["xrandr", "--fb", f"{fb_w}x{fb_h}"])
        if not res.ok:
            raise CaptureError(
                "no_virtual_monitor", f"xrandr --fb failed: {res.stderr or res.stdout}"
            )
        mm_w = width * 254 // 960  # assume ~96 dpi
        mm_h = height * 254 // 960
        res = self.run(
            [
                "xrandr",
                "--setmonitor",
                MONITOR_NAME,
                f"{width}/{mm_w}x{height}/{mm_h}+{new_x}+{py}",
                "none",
            ]
        )
        if not res.ok:
            self.run(["xrandr", "--fb", f"{self._orig_fb[0]}x{self._orig_fb[1]}"])
            raise CaptureError(
                "no_virtual_monitor", f"xrandr --setmonitor failed: {res.stderr or res.stdout}"
            )
        self.method = "setmonitor"
        self.region = (width, height, new_x, py)
        log.info("X11 extend: --setmonitor region %s (no free connector found)", self.region)
        log.warning(
            "setmonitor fallback: some compositors do not render into CRTC-less "
            "regions; if the streamed area stays black, load the evdi module or "
            "use mirror mode (see docs/TROUBLESHOOTING.md)"
        )

    # ---- teardown ----

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            if self.method == "output" and self.output_name:
                self.run(["xrandr", "--output", self.output_name, "--off"])
                if self.mode_name:
                    self.run(["xrandr", "--delmode", self.output_name, self.mode_name])
                    self.run(["xrandr", "--rmmode", self.mode_name])
            elif self.method == "setmonitor":
                self.run(["xrandr", "--delmonitor", MONITOR_NAME])
                if self._orig_fb:
                    self.run(["xrandr", "--fb", f"{self._orig_fb[0]}x{self._orig_fb[1]}"])
        finally:
            with contextlib.suppress(Exception):
                atexit.unregister(self.cleanup)
        log.info("X11 extend: cleaned up (%s)", self.method or "nothing to do")


def _primary_name(screen: XScreen) -> str:
    for o in screen.outputs:
        if o.active:
            return o.name
    return screen.outputs[0].name if screen.outputs else "default"


# --------------------------------------------------------------------------
# VideoSource


class X11Source(VideoSource):
    """ximagesrc capture of either an existing monitor (mirror) or the
    ladder-created virtual region (extend)."""

    def __init__(self, mode: str, encoder_pref: str = "auto", runner: Runner = real_runner):
        self._mode = mode
        self._encoder_pref = encoder_pref
        self._runner = runner
        self._extend: XrandrExtend | None = None
        self._pipeline: Any = None
        self._encoder_element: Any = None
        self._encoder_name = ""
        self._glib_loop: Any = None
        self._glib_thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def start(self, settings: StreamSettings, on_frame: OnFrame) -> StreamInfo:
        if not os.environ.get("DISPLAY"):
            raise CaptureError("capture_failed", "DISPLAY is not set; not an X11 session?")
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

        # 1. region to capture
        if self._mode == "extend":
            self._extend = XrandrExtend(runner=self._runner)
            rw, rh, rx, ry = self._extend.create(width, height, fps)
        else:
            screen = XrandrExtend(runner=self._runner).query()
            prim = screen.primary_geometry()
            if prim is None:
                raise CaptureError("capture_failed", "xrandr reports no active monitor to mirror")
            rw, rh, rx, ry = prim

        # 2. encoder
        factory = encoders.find_encoder(self._encoder_pref)
        if factory is None:
            self._teardown_extend()
            raise CaptureError(
                "capture_failed",
                "no usable H.264 encoder; install gstreamer1.0-plugins-ugly for x264enc",
            )
        self._encoder_name = factory
        log.info("X11 %s: region=%dx%d+%d+%d encoder=%s", self._mode, rw, rh, rx, ry, factory)

        # 3. pipeline
        tail = encoders.pipeline_tail(factory, width, height, fps, settings.bitrate_kbps)
        desc = (
            f"ximagesrc use-damage=false show-pointer=true "
            f"startx={rx} starty={ry} endx={rx + rw - 1} endy={ry + rh - 1} "
            f"do-timestamp=true ! video/x-raw,framerate={fps}/1 ! {tail}"
        )
        log.debug("pipeline: %s", desc)
        try:
            self._pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            self._teardown_extend()
            raise CaptureError("capture_failed", f"pipeline construction failed: {exc}") from exc

        self._encoder_element = self._pipeline.get_by_name("enc")
        applied = encoders.apply_properties(
            self._encoder_element, factory, settings.bitrate_kbps, fps
        )
        if applied:
            log.info("encoder properties applied: %s", applied)

        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample, on_frame)
        self._start_glib_loop()

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise CaptureError("capture_failed", "X11 pipeline refused to start")
        ret, state, _ = self._pipeline.get_state(10 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE or state != Gst.State.PLAYING:
            self.stop()
            raise CaptureError("capture_failed", f"X11 pipeline did not reach PLAYING ({ret})")

        self.request_keyframe()
        return StreamInfo(width, height, fps, factory)

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        pipeline, self._pipeline = self._pipeline, None
        glib_loop, self._glib_loop = self._glib_loop, None
        self._encoder_element = None
        try:
            if pipeline is not None:
                from gi.repository import Gst

                pipeline.set_state(Gst.State.NULL)
        finally:
            try:
                if glib_loop is not None:
                    glib_loop.quit()
            finally:
                self._teardown_extend()

    def _teardown_extend(self) -> None:
        if self._extend is not None:
            self._extend.cleanup()
            self._extend = None

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

    # internals shared with PortalSource (kept separate to avoid coupling)

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
            return True

        bus.connect("message", on_message)
        self._glib_thread = threading.Thread(
            target=self._glib_loop.run, name="ubudesk-glib-x11", daemon=True
        )
        self._glib_thread.start()

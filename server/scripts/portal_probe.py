#!/usr/bin/env python3
"""Portal capability probe - run this on your Ubuntu desktop and paste the
output into a bug report or back to the developer.

Usage (inside a normal GNOME session, NOT over ssh):

    server/.venv/bin/python server/scripts/portal_probe.py            # inspect only
    server/.venv/bin/python server/scripts/portal_probe.py --capture  # + open a
        VIRTUAL session, print streams, grab 100 frames through GStreamer

The capture step pops the GNOME screen-share dialog once.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def inspect() -> int:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except ImportError:
        print("FAIL: PyGObject not importable. Run scripts/install-deps.sh and use the venv.")
        return 1

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def prop(iface: str, name: str):
        try:
            v = bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (iface, name)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            return v.unpack()[0]
        except Exception as exc:  # noqa: BLE001
            return f"<unavailable: {exc}>"

    import os
    import platform

    print(f"machine: {platform.platform()}")
    print(f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')}")
    print(f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP')}")
    print()
    sc = "org.freedesktop.portal.ScreenCast"
    rd = "org.freedesktop.portal.RemoteDesktop"
    print(f"ScreenCast.version            = {prop(sc, 'version')}")
    src = prop(sc, "AvailableSourceTypes")
    print(f"ScreenCast.AvailableSourceTypes = {src} (1=MONITOR 2=WINDOW 4=VIRTUAL)")
    if isinstance(src, int):
        print(f"  -> VIRTUAL monitor support: {'YES' if src & 4 else 'NO'}")
    print(
        f"ScreenCast.AvailableCursorModes = {prop(sc, 'AvailableCursorModes')} "
        "(1=HIDDEN 2=EMBEDDED 4=METADATA)"
    )
    print(f"RemoteDesktop.version         = {prop(rd, 'version')}")
    print(
        f"RemoteDesktop.AvailableDeviceTypes = {prop(rd, 'AvailableDeviceTypes')} "
        "(1=KEYBOARD 2=POINTER 4=TOUCHSCREEN)"
    )
    return 0


def capture(mode: str, frames: int) -> int:
    from ubudesk_server.capture.portal_session import PortalError, PortalSession

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
    except (ImportError, ValueError):
        print("FAIL: GStreamer GI bindings missing")
        return 1

    print(f"\n==> Opening a portal session (mode={mode}); accept the GNOME dialog…")
    session = PortalSession()
    try:
        session.open(mode, want_input=False)
    except PortalError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"    node_id={session.node_id} size={session.stream_size} fd={session.pipewire_fd}")

    desc = (
        f"pipewiresrc fd={session.pipewire_fd} path={session.node_id} do-timestamp=true "
        f"keepalive-time=100 ! videoconvert ! fakesink name=sink"
    )
    pipeline = Gst.parse_launch(desc)
    pipeline.set_state(Gst.State.PLAYING)
    print(f"==> Capturing {frames} buffers…")

    sink = pipeline.get_by_name("sink")
    count = {"n": 0}

    def on_handoff(_sink, _buffer, _pad):
        count["n"] += 1

    sink.set_property("signal-handoffs", True)
    sink.connect("handoff", on_handoff)

    import time

    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 15
    while count["n"] < frames and time.monotonic() < deadline:
        ctx.iteration(False)
        time.sleep(0.001)

    pipeline.set_state(Gst.State.NULL)
    session.close()
    got = count["n"]
    print(f"==> Captured {got} buffers in <=15 s: {'OK' if got >= frames else 'TOO FEW'}")
    return 0 if got >= frames else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture", action="store_true", help="also open a session and grab frames"
    )
    parser.add_argument("--mode", choices=["extend", "mirror"], default="extend")
    parser.add_argument("--frames", type=int, default=100)
    args = parser.parse_args()
    rc = inspect()
    if rc == 0 and args.capture:
        rc = capture(args.mode, args.frames)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

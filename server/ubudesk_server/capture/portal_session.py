"""xdg-desktop-portal RemoteDesktop + ScreenCast session (Wayland/GNOME).

This is the primary capture and input path on Ubuntu 24.04+. One combined
portal session per client connection gives us:

  * a VIRTUAL monitor (extend mode) or an existing MONITOR (mirror mode)
    as a PipeWire stream, and
  * NotifyPointer*/NotifyTouch*/NotifyKeyboard* input injection whose
    coordinates map onto that same stream.

Closing the session removes the virtual monitor and returns windows to the
main display - Mutter handles the cleanup.

IMPORTANT: this module talks to a real desktop session and CANNOT run in CI.
It was written against the documented portal API (ScreenCast v4+,
RemoteDesktop v1+) and must be verified on real hardware with
`server/scripts/portal_probe.py` (see docs/MANUAL_TEST.md).
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import uuid
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
IFACE_SCREENCAST = "org.freedesktop.portal.ScreenCast"
IFACE_REMOTEDESKTOP = "org.freedesktop.portal.RemoteDesktop"
IFACE_REQUEST = "org.freedesktop.portal.Request"
IFACE_SESSION = "org.freedesktop.portal.Session"

# ScreenCast source types (bitmask)
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
SOURCE_VIRTUAL = 4

# RemoteDesktop device types (bitmask)
DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2
DEVICE_TOUCHSCREEN = 4

CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2

PERSIST_UNTIL_REVOKED = 2


class PortalError(Exception):
    pass


class PortalSession:
    """Blocking wrapper around one combined RemoteDesktop+ScreenCast session.

    Must be used from a thread that runs (or can iterate) the GLib main
    context; the helper methods pump the default main context while waiting
    for Request.Response signals.
    """

    def __init__(self, restore_token: str = "", cancelled: threading.Event | None = None) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self._Gio = Gio
        self._GLib = GLib
        self._cancelled = cancelled if cancelled is not None else threading.Event()
        self._cancellable = Gio.Cancellable()
        if self._cancelled.is_set():
            self._cancellable.cancel()
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, self._cancellable)
        self._token_counter = 0
        # Gio shares its session-bus connection. Overlapping/late startups must
        # not reuse another PortalSession's request or session object paths.
        self._token_prefix = uuid.uuid4().hex
        self.session_handle: str | None = None
        self.restore_token: str = restore_token
        self.new_restore_token: str = ""
        self.node_id: int | None = None
        self.stream_size: tuple[int, int] | None = None
        self.pipewire_fd: int = -1
        self.devices: int = 0

        sender = self._bus.get_unique_name()  # e.g. ":1.42"
        self._sender_path = re.sub(r"\.", "_", sender.lstrip(":"))

    def cancel(self) -> None:
        """Signal cancellation from the asyncio thread; teardown stays on the worker."""
        self._cancelled.set()
        self._cancellable.cancel()

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise PortalError("capture startup cancelled")

    # ------------------------------------------------------------------ dbus

    def _next_token(self, prefix: str) -> str:
        self._token_counter += 1
        return f"{prefix}{self._token_prefix}_{self._token_counter}"

    def _request_path(self, handle_token: str) -> str:
        return f"{PORTAL_PATH}/request/{self._sender_path}/{handle_token}"

    def _call_with_response(
        self,
        iface: str,
        method: str,
        params,
        options: dict[str, Any],
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Call a portal method that replies via a Request.Response signal.

        Subscribes to the predicted request object path BEFORE the call, per
        the portal docs, to avoid the race where the response arrives first.
        """
        GLib = self._GLib
        Gio = self._Gio
        self._check_cancelled()

        handle_token = self._next_token("ubudesk")
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", handle_token)
        request_path = self._request_path(handle_token)

        result: dict[str, Any] = {}
        done = {"flag": False}

        def on_response(_conn, _sender, _path, _iface, _signal, parameters):
            code, results = parameters.unpack()
            result["code"] = code
            result["results"] = results
            done["flag"] = True

        sub_id = self._bus.signal_subscribe(
            PORTAL_BUS,
            IFACE_REQUEST,
            "Response",
            request_path,
            None,
            Gio.DBusSignalFlags.NO_MATCH_RULE,
            on_response,
        )
        try:
            # The outer constructor wants native values for o/s/a{sv}, not
            # already-wrapped Variants (only the dictionary VALUES are variants).
            args = [p.unpack() for p in params] + [options]
            signature = "(" + "".join(_sig(p) for p in params) + "a{sv})"
            self._bus.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                iface,
                method,
                GLib.Variant(signature, tuple(args)),
                None,
                Gio.DBusCallFlags.NONE,
                int(timeout_s * 1000),
                self._cancellable,
            )
            # Pump the main context until the Response signal lands.
            context = GLib.MainContext.default()
            deadline = GLib.get_monotonic_time() + int(timeout_s * 1_000_000)
            while not done["flag"]:
                self._check_cancelled()
                context.iteration(False)
                if GLib.get_monotonic_time() > deadline:
                    raise PortalError(f"{iface}.{method}: timed out waiting for response")
                if not done["flag"]:
                    self._cancelled.wait(0.01)  # don't busy-spin during a permission dialog
            self._check_cancelled()
        except Exception as exc:
            # Abandoning a Request locally does not dismiss the desktop dialog.
            # Close it explicitly, with a fresh (non-cancelled) D-Bus call.
            with contextlib.suppress(Exception):
                self._bus.call_sync(
                    PORTAL_BUS,
                    request_path,
                    IFACE_REQUEST,
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
            if self._cancelled.is_set():
                raise PortalError("capture startup cancelled") from exc
            raise
        finally:
            self._bus.signal_unsubscribe(sub_id)

        if result.get("code") != 0:
            raise PortalError(
                f"{iface}.{method}: user denied or portal error (code {result.get('code')})"
            )
        return result.get("results", {})

    # --------------------------------------------------------------- session

    def open(
        self,
        mode: str,
        want_input: bool = True,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        """Create the session, select sources/devices and start.

        mode: "extend" -> VIRTUAL source; "mirror" -> MONITOR source.
        Raises PortalError, including code "no_virtual_monitor" semantics when
        VIRTUAL is not supported by this portal.
        """
        GLib = self._GLib
        self._check_cancelled()

        session_token = self._next_token("ubudesksess")
        # Keep the predicted handle even if CreateSession is cancelled before
        # its response arrives, so stop() can close a remotely-created session.
        self.session_handle = f"{PORTAL_PATH}/session/{self._sender_path}/{session_token}"
        results = self._call_with_response(
            IFACE_REMOTEDESKTOP,
            "CreateSession",
            [],
            {"session_handle_token": GLib.Variant("s", session_token)},
        )
        self.session_handle = results.get("session_handle") or self.session_handle
        log.info("portal session created: %s", self.session_handle)

        if on_closed is not None:
            self._bus.signal_subscribe(
                PORTAL_BUS,
                IFACE_SESSION,
                "Closed",
                self.session_handle,
                None,
                self._Gio.DBusSignalFlags.NO_MATCH_RULE,
                lambda *a: on_closed(),
            )

        source_type = SOURCE_VIRTUAL if mode == "extend" else SOURCE_MONITOR
        available = self.available_source_types()
        if source_type == SOURCE_VIRTUAL and not available & SOURCE_VIRTUAL:
            raise PortalError(
                "no_virtual_monitor: this portal does not support VIRTUAL sources "
                f"(AvailableSourceTypes={available}). Use --mode mirror, or see "
                "docs/TROUBLESHOOTING.md for the fallback ladder."
            )

        select_opts: dict[str, Any] = {
            "types": GLib.Variant("u", source_type),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", CURSOR_EMBEDDED),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        if self.restore_token:
            select_opts["restore_token"] = GLib.Variant("s", self.restore_token)
        self._call_with_response(
            IFACE_SCREENCAST,
            "SelectSources",
            [_objpath(GLib, self.session_handle)],
            select_opts,
        )

        if want_input:
            dev_opts: dict[str, Any] = {
                "types": GLib.Variant("u", DEVICE_KEYBOARD | DEVICE_POINTER | DEVICE_TOUCHSCREEN),
                "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
            }
            if self.restore_token:
                dev_opts["restore_token"] = GLib.Variant("s", self.restore_token)
            self._call_with_response(
                IFACE_REMOTEDESKTOP,
                "SelectDevices",
                [_objpath(GLib, self.session_handle)],
                dev_opts,
            )

        results = self._call_with_response(
            IFACE_REMOTEDESKTOP,
            "Start",
            [_objpath(GLib, self.session_handle), _str(GLib, "")],
            {},
            timeout_s=300.0,  # the user has to click the GNOME dialog
        )
        streams = results.get("streams", [])
        if not streams:
            raise PortalError("capture_failed: portal Start returned no streams")
        node_id, props = streams[0]
        self.node_id = int(node_id)
        size = props.get("size")
        if size:
            self.stream_size = (int(size[0]), int(size[1]))
        self.devices = int(results.get("devices", 0))
        self.new_restore_token = str(results.get("restore_token", "") or "")
        log.info(
            "portal started: node=%s size=%s devices=%s restore_token=%s",
            self.node_id,
            self.stream_size,
            self.devices,
            "yes" if self.new_restore_token else "no",
        )

        self.pipewire_fd = self._open_pipewire_remote()

    def available_source_types(self) -> int:
        try:
            variant = self._bus.call_sync(
                PORTAL_BUS,
                PORTAL_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                self._GLib.Variant("(ss)", (IFACE_SCREENCAST, "AvailableSourceTypes")),
                None,
                self._Gio.DBusCallFlags.NONE,
                5000,
                self._cancellable,
            )
            self._check_cancelled()
            return int(variant.unpack()[0])
        except Exception as exc:  # noqa: BLE001
            self._check_cancelled()
            log.warning("could not read AvailableSourceTypes: %s", exc)
            return 0

    def _open_pipewire_remote(self) -> int:
        GLib = self._GLib
        Gio = self._Gio
        result, fd_list = self._bus.call_with_unix_fd_list_sync(
            PORTAL_BUS,
            PORTAL_PATH,
            IFACE_SCREENCAST,
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_handle, {})),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE,
            10000,
            None,
            self._cancellable,
        )
        fd_index = result.unpack()[0]
        fd = fd_list.get(fd_index)
        log.info("OpenPipeWireRemote: fd=%d", fd)
        return fd

    # ----------------------------------------------------------------- input

    def _call_notify(self, method: str, signature: str, args: tuple) -> None:
        GLib = self._GLib
        Gio = self._Gio
        self._bus.call_sync(
            PORTAL_BUS,
            PORTAL_PATH,
            IFACE_REMOTEDESKTOP,
            method,
            GLib.Variant(signature, args),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )

    def pointer_motion_absolute(self, x_px: float, y_px: float) -> None:
        self._call_notify(
            "NotifyPointerMotionAbsolute",
            "(oa{sv}udd)",
            (self.session_handle, {}, self.node_id, float(x_px), float(y_px)),
        )

    def pointer_button(self, evdev_button: int, pressed: bool) -> None:
        self._call_notify(
            "NotifyPointerButton",
            "(oa{sv}iu)",
            (self.session_handle, {}, evdev_button, 1 if pressed else 0),
        )

    def pointer_axis_discrete(self, axis: int, steps: int) -> None:
        self._call_notify(
            "NotifyPointerAxisDiscrete",
            "(oa{sv}ui)",
            (self.session_handle, {}, axis, steps),
        )

    def touch_down(self, slot: int, x_px: float, y_px: float) -> None:
        self._call_notify(
            "NotifyTouchDown",
            "(oa{sv}uudd)",
            (self.session_handle, {}, self.node_id, slot, float(x_px), float(y_px)),
        )

    def touch_motion(self, slot: int, x_px: float, y_px: float) -> None:
        self._call_notify(
            "NotifyTouchMotion",
            "(oa{sv}uudd)",
            (self.session_handle, {}, self.node_id, slot, float(x_px), float(y_px)),
        )

    def touch_up(self, slot: int) -> None:
        self._call_notify("NotifyTouchUp", "(oa{sv}u)", (self.session_handle, {}, slot))

    def keyboard_keycode(self, evdev_code: int, pressed: bool) -> None:
        self._call_notify(
            "NotifyKeyboardKeycode",
            "(oa{sv}iu)",
            (self.session_handle, {}, evdev_code, 1 if pressed else 0),
        )

    def keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        self._call_notify(
            "NotifyKeyboardKeysym",
            "(oa{sv}iu)",
            (self.session_handle, {}, keysym, 1 if pressed else 0),
        )

    # ----------------------------------------------------------------- close

    def close(self) -> None:
        """Close the session; Mutter removes the virtual monitor."""
        if self.session_handle is None:
            return
        try:
            self._bus.call_sync(
                PORTAL_BUS,
                self.session_handle,
                IFACE_SESSION,
                "Close",
                None,
                None,
                self._Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            log.info("portal session closed")
        except Exception as exc:  # noqa: BLE001
            log.warning("portal session close failed: %s", exc)
        finally:
            self.session_handle = None
            if self.pipewire_fd >= 0:
                import os

                with contextlib.suppress(OSError):
                    os.close(self.pipewire_fd)
                self.pipewire_fd = -1


def _sig(variant) -> str:
    return variant.get_type_string()


def _objpath(GLib, path: str):
    return GLib.Variant("o", path)


def _str(GLib, s: str):
    return GLib.Variant("s", s)

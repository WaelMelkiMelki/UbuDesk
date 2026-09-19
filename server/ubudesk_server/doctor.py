"""`ubudesk doctor`: environment diagnostics.

Prints PASS/WARN/FAIL per check plus a final verdict about virtual-monitor
support, and exact fix commands. `--json` for machine-readable output.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass, field

# ScreenCast source type bits
SOURCE_MONITOR = 1
SOURCE_VIRTUAL = 4

GST_ELEMENTS = [
    "pipewiresrc",
    "videoconvert",
    "videoscale",
    "videorate",
    "h264parse",
    "x264enc",
    "vah264enc",
    "nvh264enc",
]

REQUIRED_ELEMENTS = {"videoconvert", "videoscale", "videorate", "h264parse", "x264enc"}


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str
    fix: str = ""


@dataclass
class Report:
    checks: list[CheckResult] = field(default_factory=list)
    virtual_monitor: str = "UNKNOWN"
    verdict: str = ""

    def add(self, name: str, status: str, detail: str, fix: str = "") -> None:
        self.checks.append(CheckResult(name, status, detail, fix))

    @property
    def has_fail(self) -> bool:
        return any(c.status == "FAIL" for c in self.checks)


def run_doctor() -> Report:
    r = Report()
    _check_os(r)
    _check_session(r)
    _check_pipewire(r)
    _check_portal(r)
    _check_gstreamer(r)
    _check_uinput(r)
    _check_adb(r)
    _check_port(r)
    _verdict(r)
    return r


def _check_os(r: Report) -> None:
    pretty = "unknown"
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    r.add("os", "PASS", f"{pretty} ({platform.machine()})")

    gnome = shutil.which("gnome-shell")
    if gnome:
        try:
            out = subprocess.run(  # noqa: S603
                [gnome, "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            r.add("gnome", "PASS", out)
        except (subprocess.SubprocessError, OSError):
            r.add("gnome", "WARN", "gnome-shell present but --version failed")
    else:
        r.add(
            "gnome",
            "WARN",
            "gnome-shell not found; the portal VIRTUAL path is GNOME-specific",
            "UbuDesk extend mode is tested on GNOME (Ubuntu default desktop)",
        )


def _check_session(r: Report) -> None:
    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session == "wayland":
        r.add("session", "PASS", "XDG_SESSION_TYPE=wayland (primary supported path)")
    elif session == "x11":
        r.add(
            "session",
            "WARN",
            "XDG_SESSION_TYPE=x11: extend mode uses the X11 fallback (best effort)",
            "log into an 'Ubuntu (Wayland)' session for the primary path",
        )
    else:
        r.add(
            "session",
            "FAIL",
            f"XDG_SESSION_TYPE={session or '(unset)'}: no graphical session detected",
            "run inside a logged-in GNOME desktop session",
        )


def _check_pipewire(r: Report) -> None:
    try:
        out = subprocess.run(  # noqa: S603
            ["pgrep", "-x", "pipewire"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0:
            r.add("pipewire", "PASS", "pipewire is running")
        else:
            r.add(
                "pipewire",
                "FAIL",
                "pipewire process not found",
                "systemctl --user start pipewire (Ubuntu 22.04+ has it by default)",
            )
    except (subprocess.SubprocessError, OSError):
        r.add("pipewire", "WARN", "could not check for pipewire (pgrep missing?)")


def _get_portal_property(iface: str, prop: str):
    """Read a property from org.freedesktop.portal.Desktop; None if unavailable."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        variant = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (iface, prop)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return variant.unpack()[0]
    except Exception:  # noqa: BLE001
        return None


def _check_portal(r: Report) -> None:
    try:
        import gi  # noqa: F401
    except ImportError:
        r.add(
            "pygobject",
            "FAIL",
            "PyGObject (python3-gi) not importable",
            "sudo apt install python3-gi, and create the venv with --system-site-packages",
        )
        r.add("portal", "FAIL", "cannot check the portal without PyGObject")
        return
    r.add("pygobject", "PASS", "PyGObject importable")

    sc_version = _get_portal_property("org.freedesktop.portal.ScreenCast", "version")
    if sc_version is None:
        r.add(
            "portal",
            "FAIL",
            "org.freedesktop.portal.ScreenCast unreachable",
            "sudo apt install xdg-desktop-portal xdg-desktop-portal-gnome; re-login",
        )
        return
    r.add("portal", "PASS", f"ScreenCast portal version {sc_version}")

    src_types = _get_portal_property("org.freedesktop.portal.ScreenCast", "AvailableSourceTypes")
    if src_types is None:
        r.add("portal_sources", "WARN", "could not read AvailableSourceTypes")
    else:
        virtual = bool(int(src_types) & SOURCE_VIRTUAL)
        monitor = bool(int(src_types) & SOURCE_MONITOR)
        r.virtual_monitor = "SUPPORTED" if virtual else "NOT SUPPORTED"
        detail = f"AvailableSourceTypes={int(src_types)} (monitor={monitor}, virtual={virtual})"
        r.add(
            "portal_sources",
            "PASS" if virtual else "WARN",
            detail,
            "" if virtual else "extend mode unavailable via portal; mirror mode still works",
        )

    cursor = _get_portal_property("org.freedesktop.portal.ScreenCast", "AvailableCursorModes")
    if cursor is not None:
        r.add("portal_cursor", "PASS", f"AvailableCursorModes={int(cursor)}")

    rd_version = _get_portal_property("org.freedesktop.portal.RemoteDesktop", "version")
    if rd_version is None:
        r.add(
            "remote_desktop",
            "WARN",
            "RemoteDesktop portal unreachable: input injection will use uinput fallback",
            "sudo apt install xdg-desktop-portal-gnome",
        )
    else:
        devices = _get_portal_property(
            "org.freedesktop.portal.RemoteDesktop", "AvailableDeviceTypes"
        )
        r.add(
            "remote_desktop",
            "PASS",
            f"RemoteDesktop portal version {rd_version}, "
            f"AvailableDeviceTypes={int(devices) if devices is not None else '?'} "
            "(1=keyboard 2=pointer 4=touch)",
        )


def _check_gstreamer(r: Report) -> None:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
    except (ImportError, ValueError):
        r.add(
            "gstreamer",
            "FAIL",
            "GStreamer GI bindings not available",
            "sudo apt install gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base "
            "gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly gstreamer1.0-pipewire",
        )
        return
    from gi.repository import Gst

    r.add("gstreamer", "PASS", Gst.version_string())
    missing_required = []
    found_encoders = []
    for name in GST_ELEMENTS:
        present = Gst.ElementFactory.find(name) is not None
        if name in ("x264enc", "vah264enc", "nvh264enc") and present:
            found_encoders.append(name)
        if name in REQUIRED_ELEMENTS and not present:
            missing_required.append(name)
        status = "PASS" if present else ("FAIL" if name in REQUIRED_ELEMENTS else "WARN")
        fixes = {
            "pipewiresrc": "sudo apt install gstreamer1.0-pipewire",
            "x264enc": "sudo apt install gstreamer1.0-plugins-ugly",
            "vah264enc": "sudo apt install gstreamer1.0-plugins-bad (optional, VA-API hw encode)",
            "nvh264enc": "sudo apt install gstreamer1.0-plugins-bad (optional, NVENC hw encode)",
            "h264parse": "sudo apt install gstreamer1.0-plugins-bad",
        }
        r.add(
            f"gst:{name}",
            status,
            "present" if present else "missing",
            "" if present else fixes.get(name, "install the matching gstreamer plugin package"),
        )
    if found_encoders:
        r.add("encoder", "PASS", f"usable H.264 encoders: {', '.join(found_encoders)}")
    else:
        r.add(
            "encoder",
            "FAIL",
            "no H.264 encoder found",
            "sudo apt install gstreamer1.0-plugins-ugly",
        )


def _check_uinput(r: Report) -> None:
    path = "/dev/uinput"
    if not os.path.exists(path):
        r.add(
            "uinput",
            "WARN",
            "/dev/uinput does not exist (only needed for X11 fallback input)",
            "sudo modprobe uinput",
        )
        return
    if os.access(path, os.W_OK):
        r.add("uinput", "PASS", "/dev/uinput writable (X11 fallback input available)")
    else:
        r.add(
            "uinput",
            "WARN",
            "/dev/uinput not writable (only needed for X11 fallback input)",
            "sudo usermod -aG input $USER && install the udev rule "
            "(server/packaging/99-ubudesk.rules), then re-login",
        )


def _check_adb(r: Report) -> None:
    if shutil.which("adb"):
        r.add("adb", "PASS", "adb present (USB mode available)")
    else:
        r.add(
            "adb",
            "WARN",
            "adb not found (USB mode unavailable)",
            "sudo apt install android-tools-adb",
        )


def _check_port(r: Report) -> None:
    port = 7777
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        r.add("port", "PASS", f"port {port} is free")
    except OSError:
        r.add(
            "port",
            "WARN",
            f"port {port} is in use (is another ubudesk running?)",
            "ubudesk serve --port <other>  # or stop the process using it",
        )
    finally:
        s.close()
    if shutil.which("ufw"):
        r.add(
            "firewall",
            "WARN",
            "ufw is installed; if enabled it may block the port",
            f"sudo ufw allow {port}/tcp",
        )


def _verdict(r: Report) -> None:
    if r.virtual_monitor == "SUPPORTED":
        r.verdict = "virtual monitor: SUPPORTED (extend mode should work)"
    elif r.virtual_monitor == "NOT SUPPORTED":
        r.verdict = (
            "virtual monitor: NOT SUPPORTED by the portal "
            "(fallback: mirror mode; see docs/TROUBLESHOOTING.md for the X11/EVDI paths)"
        )
    else:
        r.verdict = "virtual monitor: UNKNOWN (portal not reachable from this environment)"


def format_report(r: Report, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(
            {
                "checks": [asdict(c) for c in r.checks],
                "virtual_monitor": r.virtual_monitor,
                "verdict": r.verdict,
                "ok": not r.has_fail,
            },
            indent=2,
        )
    lines = []
    for c in r.checks:
        lines.append(f"[{c.status:<4}] {c.name}: {c.detail}")
        if c.fix and c.status != "PASS":
            lines.append(f"       fix: {c.fix}")
    lines.append("")
    lines.append(r.verdict)
    return "\n".join(lines)

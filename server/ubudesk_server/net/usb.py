"""USB mode helper: adb reverse so the phone reaches the server via
127.0.0.1:PORT over the USB cable."""

from __future__ import annotations

import shutil
import subprocess


def setup_adb_reverse(port: int) -> tuple[bool, str]:
    adb = shutil.which("adb")
    if adb is None:
        return False, (
            "adb not found. Install it with: sudo apt install android-tools-adb\n"
            "Then enable USB debugging on the phone and plug in the cable."
        )
    try:
        devices = subprocess.run(  # noqa: S603
            [adb, "devices"], capture_output=True, text=True, timeout=10, check=True
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"adb devices failed: {exc}"
    lines = [
        line for line in devices.stdout.splitlines()[1:] if line.strip() and "\tdevice" in line
    ]
    if not lines:
        return False, (
            "No authorized Android device found over USB.\n"
            "Check: cable connected, USB debugging enabled, 'allow' tapped on the phone.\n"
            f"adb output:\n{devices.stdout}"
        )
    try:
        subprocess.run(  # noqa: S603
            [adb, "reverse", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return False, f"adb reverse failed: {exc.stderr or exc}"
    return True, (
        f"USB mode ready: the phone can now reach this PC at 127.0.0.1:{port}.\n"
        "In the UbuDesk app tap 'USB' (or add server 127.0.0.1)."
    )

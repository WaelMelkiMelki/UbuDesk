"""uinput fallback input backend (X11 sessions or when the portal denies
input devices). Requires python-evdev and write access to /dev/uinput.

udev rule (installed by packaging/install.sh):
    KERNEL=="uinput", MODE="0660", GROUP="input", TAG+="uaccess"
plus `sudo usermod -aG input $USER` and re-login.

NOT verified on real hardware in this environment - see docs/MANUAL_TEST.md.
"""

from __future__ import annotations

import logging

from .base import BUTTON_CODES, InputBackend, clamp01

log = logging.getLogger(__name__)

ABS_MAX = 32767


class UinputInput(InputBackend):
    """Absolute pointer + keyboard via /dev/uinput.

    Touch events are collapsed onto the pointer (slot 0 acts as the finger);
    a proper multitouch uinput device is future work.
    """

    def __init__(self) -> None:
        try:
            from evdev import AbsInfo, UInput
            from evdev import ecodes as e
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "python-evdev is not installed; pip install 'ubudesk-server[uinput]'"
            ) from exc

        self._e = e
        caps = {
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, ABS_MAX, 0, 0, 0)),
            ],
            e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_TOUCH]
            + list(range(e.KEY_ESC, e.KEY_MICMUTE)),
            e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL],
        }
        try:
            self._ui = UInput(caps, name="UbuDesk Virtual Input", version=1)
        except PermissionError as exc:  # pragma: no cover
            raise RuntimeError(
                "/dev/uinput is not writable. Install the udev rule and add your "
                "user to the 'input' group (see docs/TROUBLESHOOTING.md)."
            ) from exc
        self._touch_active = False

    def _syn(self) -> None:
        self._ui.syn()

    def _move_abs(self, x: float, y: float) -> None:
        e = self._e
        self._ui.write(e.EV_ABS, e.ABS_X, int(clamp01(x) * ABS_MAX))
        self._ui.write(e.EV_ABS, e.ABS_Y, int(clamp01(y) * ABS_MAX))

    # touch collapses to left-button drags with the absolute pointer
    def touch_down(self, slot: int, x: float, y: float) -> None:
        if slot != 0:
            return
        self._move_abs(x, y)
        self._ui.write(self._e.EV_KEY, self._e.BTN_LEFT, 1)
        self._touch_active = True
        self._syn()

    def touch_move(self, slot: int, x: float, y: float) -> None:
        if slot != 0:
            return
        self._move_abs(x, y)
        self._syn()

    def touch_up(self, slot: int) -> None:
        if slot != 0 or not self._touch_active:
            return
        self._ui.write(self._e.EV_KEY, self._e.BTN_LEFT, 0)
        self._touch_active = False
        self._syn()

    def touch_cancel(self, slot: int) -> None:
        self.touch_up(slot)

    def mouse_move(self, x: float, y: float) -> None:
        self._move_abs(x, y)
        self._syn()

    def mouse_button(self, button: str, pressed: bool) -> None:
        code = BUTTON_CODES.get(button)
        if code is None:
            return
        self._ui.write(self._e.EV_KEY, code, 1 if pressed else 0)
        self._syn()

    def scroll(self, dx: float, dy: float) -> None:
        e = self._e
        if dy:
            self._ui.write(e.EV_REL, e.REL_WHEEL, -int(dy))  # +dy = scroll down
        if dx:
            self._ui.write(e.EV_REL, e.REL_HWHEEL, int(dx))
        self._syn()

    def key(self, evdev_code: int, pressed: bool) -> None:
        self._ui.write(self._e.EV_KEY, evdev_code, 1 if pressed else 0)
        self._syn()

    def text(self, s: str) -> None:
        log.warning("uinput backend cannot type unicode text directly; dropped %r", s)

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._ui.close()

"""Input injection through the RemoteDesktop portal, sharing the capture
session so coordinates land on the streamed (virtual) monitor.

NOT verified on real hardware in this environment - see docs/MANUAL_TEST.md.
"""

from __future__ import annotations

import logging

from ..capture.portal_session import PortalSession
from .base import AXIS_HORIZONTAL, AXIS_VERTICAL, BUTTON_CODES, InputBackend, clamp01

log = logging.getLogger(__name__)


class PortalInput(InputBackend):
    def __init__(self, session: PortalSession, stream_width: int, stream_height: int):
        self._session = session
        self._w = stream_width
        self._h = stream_height

    def _px(self, x: float, y: float) -> tuple[float, float]:
        return clamp01(x) * self._w, clamp01(y) * self._h

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - input must never kill the stream
            log.warning("portal input call failed: %s", exc)

    def touch_down(self, slot: int, x: float, y: float) -> None:
        px, py = self._px(x, y)
        self._safe(self._session.touch_down, slot, px, py)

    def touch_move(self, slot: int, x: float, y: float) -> None:
        px, py = self._px(x, y)
        self._safe(self._session.touch_motion, slot, px, py)

    def touch_up(self, slot: int) -> None:
        self._safe(self._session.touch_up, slot)

    def touch_cancel(self, slot: int) -> None:
        # The portal has no cancel; lift the finger.
        self._safe(self._session.touch_up, slot)

    def mouse_move(self, x: float, y: float) -> None:
        px, py = self._px(x, y)
        self._safe(self._session.pointer_motion_absolute, px, py)

    def mouse_button(self, button: str, pressed: bool) -> None:
        code = BUTTON_CODES.get(button)
        if code is not None:
            self._safe(self._session.pointer_button, code, pressed)

    def scroll(self, dx: float, dy: float) -> None:
        if dy:
            self._safe(self._session.pointer_axis_discrete, AXIS_VERTICAL, int(dy))
        if dx:
            self._safe(self._session.pointer_axis_discrete, AXIS_HORIZONTAL, int(dx))

    def key(self, evdev_code: int, pressed: bool) -> None:
        self._safe(self._session.keyboard_keycode, evdev_code, pressed)

    def text(self, s: str) -> None:
        for ch in s:
            keysym = _char_to_keysym(ch)
            self._safe(self._session.keyboard_keysym, keysym, True)
            self._safe(self._session.keyboard_keysym, keysym, False)


def _char_to_keysym(ch: str) -> int:
    cp = ord(ch)
    # Latin-1 range maps directly; everything else uses the Unicode keysym scheme.
    if 0x20 <= cp <= 0xFF:
        return cp
    return 0x01000000 + cp

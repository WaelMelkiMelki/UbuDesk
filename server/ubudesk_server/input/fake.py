"""FakeInput: records every injected event. Used by CI tests and by
`--source test` runs so input handling can be exercised headless."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .base import InputBackend, clamp01

log = logging.getLogger(__name__)


@dataclass
class FakeInput(InputBackend):
    events: list[tuple] = field(default_factory=list)

    def _rec(self, *event) -> None:
        self.events.append(event)
        log.debug("fake input: %s", event)

    def touch_down(self, slot: int, x: float, y: float) -> None:
        self._rec("touch_down", slot, clamp01(x), clamp01(y))

    def touch_move(self, slot: int, x: float, y: float) -> None:
        self._rec("touch_move", slot, clamp01(x), clamp01(y))

    def touch_up(self, slot: int) -> None:
        self._rec("touch_up", slot)

    def touch_cancel(self, slot: int) -> None:
        self._rec("touch_cancel", slot)

    def mouse_move(self, x: float, y: float) -> None:
        self._rec("mouse_move", clamp01(x), clamp01(y))

    def mouse_button(self, button: str, pressed: bool) -> None:
        self._rec("mouse_button", button, pressed)

    def scroll(self, dx: float, dy: float) -> None:
        self._rec("scroll", dx, dy)

    def key(self, evdev_code: int, pressed: bool) -> None:
        self._rec("key", evdev_code, pressed)

    def text(self, s: str) -> None:
        self._rec("text", s)

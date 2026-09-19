"""Input backend interface.

Coordinates arrive normalized (0..1 relative to the video area); the backend
maps them to whatever coordinate space it needs. All methods must be cheap
and non-blocking from the caller's perspective (dispatch to GLib.idle_add or
similar when the underlying API needs the GLib main context).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# evdev button codes
BTN_LEFT = 272
BTN_RIGHT = 273
BTN_MIDDLE = 274

BUTTON_CODES = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

AXIS_VERTICAL = 0
AXIS_HORIZONTAL = 1


def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


class InputBackend(ABC):
    @abstractmethod
    def touch_down(self, slot: int, x: float, y: float) -> None: ...

    @abstractmethod
    def touch_move(self, slot: int, x: float, y: float) -> None: ...

    @abstractmethod
    def touch_up(self, slot: int) -> None: ...

    @abstractmethod
    def touch_cancel(self, slot: int) -> None: ...

    @abstractmethod
    def mouse_move(self, x: float, y: float) -> None: ...

    @abstractmethod
    def mouse_button(self, button: str, pressed: bool) -> None: ...

    @abstractmethod
    def scroll(self, dx: float, dy: float) -> None: ...

    @abstractmethod
    def key(self, evdev_code: int, pressed: bool) -> None: ...

    @abstractmethod
    def text(self, s: str) -> None: ...

    def close(self) -> None:  # noqa: B027  # pragma: no cover
        """Release resources (optional)."""

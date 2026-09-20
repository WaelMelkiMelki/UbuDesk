"""Capture backend interface.

A ``VideoSource`` produces H.264 access units (Annex-B) and hands them to a
callback ``on_frame(data, pts_us, keyframe, has_config)``, called from an
arbitrary thread. The network layer is responsible for hopping back onto the
asyncio loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

OnFrame = Callable[[bytes, int, bool, bool], None]


@dataclass
class StreamSettings:
    width: int
    height: int
    fps: int
    bitrate_kbps: int
    mode: str = "extend"  # extend | mirror


@dataclass
class StreamInfo:
    """What the source actually delivers (may differ from the request)."""

    width: int
    height: int
    fps: int
    encoder: str


class CaptureError(Exception):
    """Raised when a capture backend cannot start."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class VideoSource(ABC):
    """One video source per streaming client session."""

    @abstractmethod
    def start(self, settings: StreamSettings, on_frame: OnFrame) -> StreamInfo:
        """Start producing frames. Blocking; may take a moment (portal dialog)."""

    def cancel_start(self) -> None:  # noqa: B027
        """Thread-safe, non-blocking cancellation signal for an in-flight start.

        Backends with permission dialogs should override this. The session
        always calls stop() AFTER start() returns, including cancelled/failed
        starts; cancel_start() must not tear resources down concurrently.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop and release everything (idempotent)."""

    @abstractmethod
    def request_keyframe(self) -> None:
        """Force an IDR as soon as possible."""

    def set_bitrate(self, kbps: int) -> None:  # noqa: B027  # pragma: no cover
        """Change the encoder bitrate live (best effort; optional)."""


def even(n: int) -> int:
    """Round down to an even number (H.264 4:2:0 requires even dimensions)."""
    return max(2, n & ~1)

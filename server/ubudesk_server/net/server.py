"""The asyncio TCP/TLS server that accepts UbuDesk clients."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import uuid

from ..capture.base import StreamSettings, VideoSource
from ..config import Config, state_dir
from ..input.base import InputBackend
from ..input.fake import FakeInput
from .auth import DeviceStore, PinManager
from .discovery import Advertiser
from .session import Session, SourceFactory
from .tls import cert_fingerprint, ensure_cert, server_ssl_context

log = logging.getLogger(__name__)


class UbuDeskServer:
    def __init__(self, config: Config, *, source_factory: SourceFactory | None = None):
        self.config = config
        self.devices = DeviceStore(state_dir() / "devices.json")
        self.pins = PinManager()
        self.server_id = self._load_server_id()
        self._server: asyncio.Server | None = None
        self._advertiser: Advertiser | None = None
        self._sessions: set[Session] = set()
        self._stopping = False
        self.fingerprint = ""
        self._source_factory = source_factory or SourceFactory(self._default_factory)

    def _load_server_id(self) -> str:
        path = state_dir() / "server_id"
        if path.exists():
            return path.read_text().strip()
        sid = str(uuid.uuid4())
        path.write_text(sid)
        return sid

    # ------------------------------------------------------------- factories

    def _default_factory(self, settings: StreamSettings) -> tuple[VideoSource, InputBackend]:
        """Build source+input according to config (test / portal / x11 / auto)."""
        source_kind = self.config.source
        if source_kind == "auto":
            import os

            session_type = os.environ.get("XDG_SESSION_TYPE", "")
            if session_type == "x11":
                source_kind = "x11"
            elif session_type == "wayland":
                source_kind = "portal"
            elif os.environ.get("DISPLAY"):
                source_kind = "x11"
            else:
                source_kind = "portal"  # portal will fail with a clear message
            log.info("source=auto resolved to %s (XDG_SESSION_TYPE=%r)", source_kind, session_type)

        if source_kind == "test":
            from ..capture.test_source import TestPatternSource

            return TestPatternSource(), FakeInput()

        if source_kind == "x11":
            from ..capture.x11 import X11Source

            source_x11: VideoSource = X11Source(settings.mode, encoder_pref=self.config.encoder)
            input_backend = self._make_uinput_or_view_only()
            return source_x11, input_backend

        # Wayland: portal capture + portal input on the same session.
        from ..capture.pipeline import PortalSource
        from ..input.portal_input import PortalInput

        source = PortalSource(
            settings.mode,
            encoder_pref=self.config.encoder,
            restore_token=self.config.restore_token,
        )

        # PortalInput needs the live session; wrap start() so input is built
        # after the portal session exists.
        original_start = source.start

        holder: dict[str, InputBackend] = {}
        proxy = _LateInput(holder)

        def start(settings_, on_frame):
            info = original_start(settings_, on_frame)
            assert source.session is not None
            holder["real"] = PortalInput(source.session, info.width, info.height)
            if source.session.new_restore_token:
                self.config.restore_token = source.session.new_restore_token
                self.config.save()
            return info

        source.start = start  # type: ignore[method-assign]
        return source, proxy

    def _make_uinput_or_view_only(self) -> InputBackend:
        """uinput input for X11; falls back to view-only (logged) if denied."""
        try:
            from ..input.uinput_input import UinputInput

            return UinputInput()
        except (RuntimeError, OSError) as exc:
            log.warning(
                "input disabled (view-only stream): %s -- fix: install "
                "server/packaging/99-ubudesk.rules and add your user to the "
                "'input' group, then re-login",
                exc,
            )
            return FakeInput()

    # ------------------------------------------------------------------- run

    async def start(self) -> None:
        cfg = self.config
        ssl_ctx = None
        if cfg.tls:
            cert_path, key_path = ensure_cert(state_dir(), cfg.server_name)
            self.fingerprint = cert_fingerprint(cert_path)
            ssl_ctx = server_ssl_context(cert_path, key_path)
            log.info("TLS enabled; certificate SHA-256: %s", self.fingerprint)
            log.info("pairing short code: %s", self.fingerprint[:8].upper())
        else:
            log.warning("TLS DISABLED (--no-tls). Use only on trusted networks or for tests.")

        self._server = await asyncio.start_server(
            self._on_client,
            host=cfg.bind,
            port=cfg.port,
            ssl=ssl_ctx,
            reuse_address=True,
        )
        log.info("listening on %s:%d (source=%s mode=%s)", cfg.bind, cfg.port, cfg.source, cfg.mode)

        if cfg.bind not in ("127.0.0.1", "::1", "localhost"):
            self._advertiser = Advertiser(cfg.port, self.server_id, cfg.server_name)
            self._advertiser.start()
        else:
            log.info("bound to loopback; skipping mDNS advertising")

    def issue_pin_if_needed(self, force: bool = False) -> str | None:
        if force or len(self.devices) == 0:
            pin = self.pins.issue()
            log.info("pairing PIN: %s (valid 10 minutes, single use)", pin)
            _notify_send(f"UbuDesk pairing PIN: {pin}")
            return pin
        return None

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._stopping:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        session = Session(
            reader,
            writer,
            server_id=self.server_id,
            server_name=self.config.server_name,
            devices=self.devices,
            pins=self.pins,
            source_factory=self._source_factory,
        )
        self._sessions.add(session)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._advertiser is not None:
            self._advertiser.stop()
        await asyncio.gather(*(session.close() for session in list(self._sessions)))
        log.info("server stopped")

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()


class _LateInput(InputBackend):
    """Input proxy that becomes live once the portal session exists."""

    def __init__(self, holder: dict[str, InputBackend]):
        self._holder = holder

    def _real(self) -> InputBackend | None:
        return self._holder.get("real")

    def touch_down(self, slot, x, y):
        if (r := self._real()) is not None:
            r.touch_down(slot, x, y)

    def touch_move(self, slot, x, y):
        if (r := self._real()) is not None:
            r.touch_move(slot, x, y)

    def touch_up(self, slot):
        if (r := self._real()) is not None:
            r.touch_up(slot)

    def touch_cancel(self, slot):
        if (r := self._real()) is not None:
            r.touch_cancel(slot)

    def mouse_move(self, x, y):
        if (r := self._real()) is not None:
            r.mouse_move(x, y)

    def mouse_button(self, button, pressed):
        if (r := self._real()) is not None:
            r.mouse_button(button, pressed)

    def scroll(self, dx, dy):
        if (r := self._real()) is not None:
            r.scroll(dx, dy)

    def key(self, code, pressed):
        if (r := self._real()) is not None:
            r.key(code, pressed)

    def text(self, s):
        if (r := self._real()) is not None:
            r.text(s)

    def close(self):
        if (r := self._real()) is not None:
            r.close()


def _notify_send(message: str) -> None:
    import shutil
    import subprocess

    exe = shutil.which("notify-send")
    if exe:
        with contextlib.suppress(OSError):
            subprocess.Popen(  # noqa: S603
                [exe, "UbuDesk", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


async def run_server(config: Config, *, pair: bool = False) -> None:
    server = UbuDeskServer(config)
    await server.start()
    server.issue_pin_if_needed(force=pair)
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()

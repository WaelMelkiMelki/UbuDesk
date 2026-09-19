"""UbuDesk server command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from . import DEFAULT_PORT, __version__
from .config import Config, state_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ubudesk",
        description="UbuDesk: use an Android device as an extra monitor for this PC",
    )
    parser.add_argument("--version", action="version", version=f"ubudesk {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the streaming server")
    serve.add_argument("--port", type=int, default=None, help=f"TCP port (default {DEFAULT_PORT})")
    serve.add_argument("--bind", default=None, help="bind address (default 0.0.0.0)")
    serve.add_argument(
        "--source",
        choices=["portal", "test"],
        default=None,
        help="video source: portal = real screen, test = moving test pattern",
    )
    serve.add_argument(
        "--mode",
        choices=["extend", "mirror"],
        default=None,
        help="extend = virtual monitor, mirror = share an existing monitor",
    )
    serve.add_argument("--encoder", choices=["auto", "x264", "va", "nvenc"], default=None)
    serve.add_argument("--no-tls", action="store_true", help="disable TLS (tests/dev only)")
    serve.add_argument("--pair", action="store_true", help="print a pairing PIN on start")
    serve.add_argument("--log-level", default=None, choices=["debug", "info", "warning", "error"])

    doctor = sub.add_parser("doctor", help="diagnose the environment")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")

    usb = sub.add_parser("usb", help="set up USB mode (adb reverse)")
    usb.add_argument("--port", type=int, default=DEFAULT_PORT)

    devices = sub.add_parser("devices", help="list or revoke paired devices")
    devices.add_argument("--revoke", metavar="CLIENT_ID", help="revoke a paired device")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "usb":
        return _cmd_usb(args)
    if args.command == "devices":
        return _cmd_devices(args)
    return 2


def _cmd_serve(args) -> int:
    from . import log as logmod

    config = Config.load()
    if args.port is not None:
        config.port = args.port
    if args.bind is not None:
        config.bind = args.bind
    if args.source is not None:
        config.source = args.source
    if args.mode is not None:
        config.mode = args.mode
    if args.encoder is not None:
        config.encoder = args.encoder
    if args.no_tls:
        config.tls = False
    if args.log_level is not None:
        config.log_level = args.log_level

    logmod.setup(config.log_level)

    from .net.server import run_server

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(run_server(config, pair=args.pair))
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
    return 0


def _cmd_doctor(args) -> int:
    from .doctor import format_report, run_doctor

    report = run_doctor()
    print(format_report(report, as_json=args.json))
    return 1 if report.has_fail else 0


def _cmd_usb(args) -> int:
    from .net.usb import setup_adb_reverse

    ok, message = setup_adb_reverse(args.port)
    print(message)
    return 0 if ok else 1


def _cmd_devices(args) -> int:
    from .net.auth import DeviceStore

    store = DeviceStore(state_dir() / "devices.json")
    if args.revoke:
        if store.remove(args.revoke):
            print(f"revoked {args.revoke}")
            return 0
        print(f"no paired device with id {args.revoke}", file=sys.stderr)
        return 1
    devices = store.list()
    if not devices:
        print("no paired devices")
        return 0
    import datetime

    for d in devices:
        seen = datetime.datetime.fromtimestamp(d.last_seen).strftime("%Y-%m-%d %H:%M")
        print(f"{d.client_id}  {d.name!r}  last seen {seen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

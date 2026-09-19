#!/usr/bin/env python3
"""Headless UbuDesk test client.

Connects, pairs (PIN) or authenticates (token), starts a stream, verifies the
H.264 output, optionally dumps it to a file playable with
`ffplay -f h264 dump.h264`, and can send fake touch input.

Used by the CI integration tests and for manual protocol debugging:

    python tools/test_client.py --host 127.0.0.1 --port 7777 --no-tls \
        --pin 123456 --seconds 5 --dump out.h264
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import struct
import sys
import time
import uuid

HEADER = struct.Struct(">BI")
VIDEO_HEADER = struct.Struct(">QB")
TYPE_CONTROL = 0x01
TYPE_VIDEO = 0x02


class Client:
    def __init__(self, host: str, port: int, use_tls: bool, fingerprint: str | None = None):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.fingerprint = fingerprint
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.client_id = str(uuid.uuid4())
        self.token: str | None = None
        self.stats = {
            "frames": 0, "keyframes": 0, "bytes": 0,
            "first_frame_key": None, "first_frame_has_config": None,
        }

    async def connect(self) -> None:
        ssl_ctx = None
        if self.use_tls:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE  # fingerprint checked below
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx
        )
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.use_tls and self.fingerprint:
            import hashlib

            sslobj = self.writer.get_extra_info("ssl_object")
            der = sslobj.getpeercert(binary_form=True)
            actual = hashlib.sha256(der).hexdigest()
            if actual != self.fingerprint.lower():
                raise RuntimeError(
                    f"certificate fingerprint mismatch: got {actual}, expected {self.fingerprint}"
                )

    async def send(self, msg: dict) -> None:
        payload = json.dumps(msg, separators=(",", ":")).encode()
        assert self.writer is not None
        self.writer.write(HEADER.pack(TYPE_CONTROL, len(payload)) + payload)
        await self.writer.drain()

    async def read_frame(self) -> tuple[int, bytes]:
        assert self.reader is not None
        header = await self.reader.readexactly(HEADER.size)
        ftype, length = HEADER.unpack(header)
        if length > 8 * 1024 * 1024:
            raise RuntimeError("oversized frame from server")
        return ftype, await self.reader.readexactly(length)

    async def expect(self, expected_t: str, skip_video: bool = True) -> dict:
        while True:
            ftype, payload = await self.read_frame()
            if ftype == TYPE_VIDEO and skip_video:
                continue
            if ftype == TYPE_CONTROL:
                msg = json.loads(payload)
                if msg.get("t") == expected_t:
                    return msg
                if msg.get("t") in ("error", "auth_fail"):
                    raise RuntimeError(f"server said: {msg}")
                # ignore other control messages (pong etc.)

    async def handshake(self, pin: str | None, token: str | None) -> None:
        await self.send({
            "t": "hello", "proto": 1, "client_id": self.client_id,
            "name": "test_client.py", "app_version": "0.1.0",
            "screen": {"w": 1920, "h": 1200, "dpi": 240, "refresh": 60},
            "codecs": ["h264"],
        })
        await self.expect("auth_required")
        if token:
            await self.send({"t": "auth", "method": "token", "token": token})
        else:
            await self.send({"t": "auth", "method": "pin", "pin": pin or ""})
        ok = await self.expect("auth_ok")
        self.token = ok.get("token")

    async def start_stream(self, width: int, height: int, fps: int, mode: str) -> dict:
        await self.send({
            "t": "start", "width": width, "height": height, "fps": fps,
            "bitrate_kbps": 8000, "mode": mode, "touch_mode": "touch",
        })
        return await self.expect("started")

    async def close(self) -> None:
        if self.writer is not None:
            try:
                await self.send({"t": "bye"})
            except (ConnectionError, RuntimeError):
                pass
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass


def annexb_nal_types(data: bytes) -> list[int]:
    types, i, n = [], 0, len(data)
    while i + 3 < n:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                types.append(data[i + 3] & 0x1F)
                i += 4
                continue
            if data[i + 2] == 0 and i + 4 < n and data[i + 3] == 1:
                types.append(data[i + 4] & 0x1F)
                i += 5
                continue
        i += 1
    return types


async def run(args) -> int:
    client = Client(args.host, args.port, not args.no_tls, args.fingerprint)
    await client.connect()
    await client.handshake(args.pin, args.token)
    print(f"authenticated; token={'yes' if client.token else 'reused'}", file=sys.stderr)
    started = await client.start_stream(args.width, args.height, args.fps, args.mode)
    print(f"started: {started}", file=sys.stderr)

    dump = open(args.dump, "wb") if args.dump else None
    idr_requested_at: float | None = None
    idr_seen_after_request = False
    deadline = time.monotonic() + args.seconds
    first_frame_at: float | None = None
    last_ping = 0.0

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_ping > 2.0:
                await client.send({"t": "ping", "seq": int(now), "ts": int(now * 1000)})
                last_ping = now
            if args.request_idr and idr_requested_at is None and client.stats["frames"] >= 10:
                await client.send({"t": "idr"})
                idr_requested_at = now
            if args.touch and client.stats["frames"] == 20:
                for a, x in (("down", 0.3), ("move", 0.5), ("up", 0.5)):
                    await client.send({"t": "touch", "a": a, "id": 0, "x": x, "y": 0.5})

            try:
                ftype, payload = await asyncio.wait_for(client.read_frame(), timeout=3.0)
            except asyncio.TimeoutError:
                print("WARNING: no frame for 3 s", file=sys.stderr)
                continue
            if ftype != TYPE_VIDEO:
                continue
            pts_us, flags = VIDEO_HEADER.unpack_from(payload)
            data = payload[VIDEO_HEADER.size:]
            key = bool(flags & 1)
            has_config = bool(flags & 2)
            s = client.stats
            if s["frames"] == 0:
                first_frame_at = time.monotonic()
                s["first_frame_key"] = key
                nals = annexb_nal_types(data)
                s["first_frame_has_config"] = (7 in nals and 8 in nals)
            s["frames"] += 1
            s["bytes"] += len(data)
            if key:
                s["keyframes"] += 1
                if idr_requested_at is not None and not idr_seen_after_request:
                    latency = time.monotonic() - idr_requested_at
                    print(f"IDR after request: {latency * 1000:.0f} ms", file=sys.stderr)
                    s["idr_latency_s"] = latency
                    idr_seen_after_request = True
            if dump:
                dump.write(data)
    finally:
        if dump:
            dump.close()
        await client.close()

    s = client.stats
    elapsed = args.seconds
    if first_frame_at is not None:
        elapsed = max(0.001, time.monotonic() - first_frame_at)
    fps = s["frames"] / elapsed
    result = {
        "frames": s["frames"], "keyframes": s["keyframes"],
        "fps": round(fps, 1), "mbps": round(s["bytes"] * 8 / elapsed / 1e6, 2),
        "first_frame_key": s["first_frame_key"],
        "first_frame_has_config": s["first_frame_has_config"],
        "idr_latency_s": s.get("idr_latency_s"),
        "token": client.token,
    }
    print(json.dumps(result))

    ok = (
        s["frames"] > 0
        and s["first_frame_key"] is True
        and s["first_frame_has_config"] is True
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--no-tls", action="store_true")
    parser.add_argument("--fingerprint", help="expected cert SHA-256 (hex)")
    parser.add_argument("--pin", help="pairing PIN")
    parser.add_argument("--token", help="existing auth token")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=400)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--mode", default="extend")
    parser.add_argument("--dump", help="write raw Annex-B H.264 to this file")
    parser.add_argument("--request-idr", action="store_true")
    parser.add_argument("--touch", action="store_true", help="send a fake touch sequence")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

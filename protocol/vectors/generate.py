#!/usr/bin/env python3
"""Regenerates the golden protocol vectors. Run from the repo root:

    python protocol/vectors/generate.py

Each vector is a pair of files:
    <name>.bin    - the exact bytes on the wire (one frame)
    <name>.json   - expected decode result / metadata

Both the Python tests (server/tests/test_protocol.py) and the Kotlin tests
(android/app/src/test/.../ProtocolVectorTest.kt) consume these files, which is
what keeps the two implementations in lockstep.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

HERE = Path(__file__).parent
HEADER = struct.Struct(">BI")
VIDEO_HEADER = struct.Struct(">QB")


def control(msg: dict) -> bytes:
    payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return HEADER.pack(0x01, len(payload)) + payload


def video(pts_us: int, flags: int, data: bytes) -> bytes:
    return HEADER.pack(0x02, VIDEO_HEADER.size + len(data)) + VIDEO_HEADER.pack(pts_us, flags) + data


def write(name: str, wire: bytes, expect: dict) -> None:
    (HERE / f"{name}.bin").write_bytes(wire)
    (HERE / f"{name}.json").write_text(json.dumps(expect, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    control_msgs = {
        "hello": {
            "t": "hello", "proto": 1,
            "client_id": "3f2a1b7c-9d4e-4f60-8a2b-5c6d7e8f9a0b",
            "name": "Pixel Tablet", "app_version": "0.1.0",
            "screen": {"w": 2560, "h": 1600, "dpi": 320, "refresh": 60},
            "codecs": ["h264"],
        },
        "auth_required": {
            "t": "auth_required",
            "server_id": "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9",
            "server_name": "my-pc", "methods": ["token", "pin"],
        },
        "auth_pin": {"t": "auth", "method": "pin", "pin": "123456"},
        "auth_token": {"t": "auth", "method": "token",
                       "token": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"},
        "auth_ok": {"t": "auth_ok", "token": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"},
        "auth_fail": {"t": "auth_fail", "reason": "bad_pin", "retry_after_s": 60},
        "start": {"t": "start", "width": 1920, "height": 1200, "fps": 60,
                  "bitrate_kbps": 15000, "mode": "extend", "touch_mode": "touch"},
        "started": {"t": "started", "width": 1920, "height": 1200, "fps": 60,
                    "codec": "h264", "encoder": "x264enc"},
        "touch": {"t": "touch", "a": "down", "id": 0, "x": 0.51, "y": 0.25},
        "mouse": {"t": "mouse", "a": "down", "b": "left", "x": 0.4, "y": 0.7},
        "scroll": {"t": "scroll", "dx": 0.0, "dy": 1.0},
        "key": {"t": "key", "code": 30, "down": True},
        "text": {"t": "text", "s": "héllo 😀"},
        "ping": {"t": "ping", "seq": 1, "ts": 123456789},
        "pong": {"t": "pong", "seq": 1, "ts": 123456789},
        "idr": {"t": "idr"},
        "bitrate": {"t": "bitrate", "kbps": 8000},
        "stats": {"t": "stats", "fps": 58, "decode_ms": 4.1, "dropped": 0},
        "error": {"t": "error", "code": "capture_failed", "message": "portal denied"},
        "bye": {"t": "bye"},
    }
    for name, msg in control_msgs.items():
        write(f"control_{name}", control(msg), {"kind": "control", "message": msg})

    # video frames: fake Annex-B payloads (start code + NAL header + junk)
    sps = b"\x00\x00\x00\x01\x67\x42\xc0\x1f\xda\x01\x40\x16\xec\x04"
    pps = b"\x00\x00\x00\x01\x68\xce\x3c\x80"
    idr = b"\x00\x00\x00\x01\x65\x88\x84\x00\x33\xff\xfe\xf6\xf0\xfe\x05"
    p = b"\x00\x00\x00\x01\x41\x9a\x24\x6c\x41\x4f\xfe\xd6\x8c\xb0"

    key_au = sps + pps + idr
    write(
        "video_keyframe",
        video(1_000_000, 0b11, key_au),
        {"kind": "video", "pts_us": 1000000, "flags": 3, "keyframe": True,
         "has_config": True, "data_len": len(key_au), "nal_types": [7, 8, 5]},
    )
    write(
        "video_delta",
        video(1_033_333, 0, p),
        {"kind": "video", "pts_us": 1033333, "flags": 0, "keyframe": False,
         "has_config": False, "data_len": len(p), "nal_types": [1]},
    )
    # pts that exceeds u32 to catch 32-bit truncation bugs
    write(
        "video_large_pts",
        video(5_000_000_000, 1, idr),
        {"kind": "video", "pts_us": 5000000000, "flags": 1, "keyframe": True,
         "has_config": False, "data_len": len(idr), "nal_types": [5]},
    )

    # malformed frames
    oversized = HEADER.pack(0x02, 8 * 1024 * 1024 + 1)
    write("bad_oversized", oversized,
          {"kind": "invalid", "reason": "length exceeds 8 MiB; close the connection"})
    truncated = control({"t": "ping", "seq": 1, "ts": 2})[:-4]
    write("bad_truncated", truncated,
          {"kind": "invalid", "reason": "stream ends mid-frame; treat as disconnect"})
    write("bad_json", HEADER.pack(0x01, 5) + b"{oops",
          {"kind": "invalid", "reason": "control payload is not valid JSON"})

    print(f"wrote vectors to {HERE}", file=sys.stderr)


if __name__ == "__main__":
    main()

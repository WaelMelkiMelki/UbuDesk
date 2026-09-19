"""UbuDesk wire protocol v1: framing and message helpers.

Framing (see docs/PROTOCOL.md):

    | type: u8 | length: u32 big-endian | payload: `length` bytes |

Types:
    0x01 CONTROL  - UTF-8 JSON object with a "t" field.
    0x02 VIDEO    - | pts_us: u64 BE | flags: u8 | H.264 Annex-B access unit |
                    flags bit0 = keyframe (IDR), bit1 = contains SPS/PPS.

Max payload is 8 MiB; a peer announcing more must be disconnected.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

MAX_PAYLOAD = 8 * 1024 * 1024

TYPE_CONTROL = 0x01
TYPE_VIDEO = 0x02

VIDEO_FLAG_KEYFRAME = 0x01
VIDEO_FLAG_CONFIG = 0x02

_HEADER = struct.Struct(">BI")
_VIDEO_HEADER = struct.Struct(">QB")


class ProtocolError(Exception):
    """Fatal protocol violation; the connection must be closed."""


def encode_control(msg: dict[str, Any]) -> bytes:
    payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("control message too large")
    return _HEADER.pack(TYPE_CONTROL, len(payload)) + payload


def encode_video(pts_us: int, flags: int, data: bytes) -> bytes:
    payload_len = _VIDEO_HEADER.size + len(data)
    if payload_len > MAX_PAYLOAD:
        raise ProtocolError("video frame too large")
    return _HEADER.pack(TYPE_VIDEO, payload_len) + _VIDEO_HEADER.pack(pts_us, flags) + data


def decode_control(payload: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"bad control payload: {exc}") from exc
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("control message must be a JSON object with a string 't'")
    return msg


def decode_video(payload: bytes) -> tuple[int, int, bytes]:
    """Return (pts_us, flags, annexb_data)."""
    if len(payload) < _VIDEO_HEADER.size:
        raise ProtocolError("video payload too short")
    pts_us, flags = _VIDEO_HEADER.unpack_from(payload)
    return pts_us, flags, payload[_VIDEO_HEADER.size :]


def parse_header(header: bytes) -> tuple[int, int]:
    """Parse the 5-byte frame header; returns (type, length)."""
    if len(header) != _HEADER.size:
        raise ProtocolError("truncated header")
    ftype, length = _HEADER.unpack(header)
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"payload length {length} exceeds 8 MiB limit")
    return ftype, length


HEADER_SIZE = _HEADER.size  # 5


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one frame; returns (type, payload). Raises ProtocolError / IncompleteReadError."""
    header = await reader.readexactly(_HEADER.size)
    ftype, length = parse_header(header)
    payload = await reader.readexactly(length) if length else b""
    return ftype, payload


# ---------------------------------------------------------------------------
# Annex-B helpers (shared by tests and the test source)


def annexb_nal_types(data: bytes) -> list[int]:
    """Return the NAL unit types found in an Annex-B byte stream, in order."""
    types: list[int] = []
    i = 0
    n = len(data)
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


def contains_sps_pps(data: bytes) -> bool:
    ts = annexb_nal_types(data)
    return 7 in ts and 8 in ts


def contains_idr(data: bytes) -> bool:
    return 5 in annexb_nal_types(data)

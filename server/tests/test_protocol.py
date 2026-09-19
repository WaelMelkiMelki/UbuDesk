"""Protocol framing tests against the shared golden vectors."""

import asyncio
import json

import pytest

from ubudesk_server import protocol


def load_vectors(vector_dir, kind):
    for json_path in sorted(vector_dir.glob("*.json")):
        expect = json.loads(json_path.read_text())
        if expect["kind"] != kind:
            continue
        yield json_path.stem, json_path.with_suffix(".bin").read_bytes(), expect


def test_control_vectors_roundtrip(vector_dir):
    count = 0
    for name, wire, expect in load_vectors(vector_dir, "control"):
        ftype, length = protocol.parse_header(wire[:5])
        assert ftype == protocol.TYPE_CONTROL, name
        payload = wire[5:]
        assert len(payload) == length, name
        msg = protocol.decode_control(payload)
        assert msg == expect["message"], name
        # re-encode must produce identical bytes (key order is preserved)
        assert protocol.encode_control(msg) == wire, name
        count += 1
    assert count >= 15


def test_video_vectors(vector_dir):
    count = 0
    for name, wire, expect in load_vectors(vector_dir, "video"):
        ftype, length = protocol.parse_header(wire[:5])
        assert ftype == protocol.TYPE_VIDEO, name
        pts_us, flags, data = protocol.decode_video(wire[5:])
        assert pts_us == expect["pts_us"], name
        assert flags == expect["flags"], name
        assert len(data) == expect["data_len"], name
        assert bool(flags & protocol.VIDEO_FLAG_KEYFRAME) == expect["keyframe"], name
        assert bool(flags & protocol.VIDEO_FLAG_CONFIG) == expect["has_config"], name
        assert protocol.annexb_nal_types(data) == expect["nal_types"], name
        assert protocol.encode_video(pts_us, flags, data) == wire, name
        count += 1
    assert count == 3


def test_oversized_header_rejected(vector_dir):
    wire = (vector_dir / "bad_oversized.bin").read_bytes()
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_header(wire[:5])


def test_bad_json_rejected(vector_dir):
    wire = (vector_dir / "bad_json.bin").read_bytes()
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_control(wire[5:])


async def test_truncated_stream(vector_dir):
    wire = (vector_dir / "bad_truncated.bin").read_bytes()
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    reader.feed_eof()
    with pytest.raises(asyncio.IncompleteReadError):
        await protocol.read_frame(reader)


async def test_read_frame_roundtrip():
    msg = {"t": "ping", "seq": 42, "ts": 5_000_000_000}
    wire = protocol.encode_control(msg)
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    ftype, payload = await protocol.read_frame(reader)
    assert ftype == protocol.TYPE_CONTROL
    assert protocol.decode_control(payload) == msg


def test_control_not_dict_rejected():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_control(b"[1,2,3]")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_control(b'{"x": 1}')


def test_annexb_helpers():
    sps_pps_idr = (
        b"\x00\x00\x00\x01\x67\x42" + b"\x00\x00\x00\x01\x68\xce" + b"\x00\x00\x01\x65\x88"
    )
    assert protocol.annexb_nal_types(sps_pps_idr) == [7, 8, 5]
    assert protocol.contains_sps_pps(sps_pps_idr)
    assert protocol.contains_idr(sps_pps_idr)
    p_frame = b"\x00\x00\x00\x01\x41\x9a"
    assert protocol.annexb_nal_types(p_frame) == [1]
    assert not protocol.contains_sps_pps(p_frame)

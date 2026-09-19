"""Tests for the headless test-pattern H.264 source."""

import threading
import time

import pytest

from ubudesk_server.capture.base import StreamSettings
from ubudesk_server.capture.test_source import TestPatternSource as PatternSource
from ubudesk_server.protocol import annexb_nal_types, contains_idr, contains_sps_pps

pytest.importorskip("av")


class Collector:
    def __init__(self):
        self.frames = []
        self.event = threading.Event()

    def __call__(self, data, pts_us, key, has_config):
        self.frames.append((data, pts_us, key, has_config))
        if len(self.frames) >= 30:
            self.event.set()


def test_stream_produces_valid_h264():
    src = PatternSource()
    col = Collector()
    info = src.start(StreamSettings(321, 201, 30, 4000), col)  # odd sizes get rounded
    try:
        assert info.width == 320 and info.height == 200
        assert col.event.wait(10), "did not receive 30 frames in 10 s"
    finally:
        src.stop()

    data, pts, key, has_config = col.frames[0]
    assert key and has_config
    assert contains_sps_pps(data)
    assert contains_idr(data)
    assert pts == 0
    # subsequent deltas are P frames
    ts = annexb_nal_types(col.frames[1][0])
    assert 5 not in ts

    # pts increases monotonically
    ptss = [f[1] for f in col.frames]
    assert ptss == sorted(ptss)


def test_request_keyframe_mid_stream():
    src = PatternSource()
    col = Collector()
    src.start(StreamSettings(320, 200, 30, 4000), col)
    try:
        assert col.event.wait(10)
        n = len(col.frames)
        src.request_keyframe()
        deadline = time.monotonic() + 3
        got_idr = False
        while time.monotonic() < deadline and not got_idr:
            time.sleep(0.05)
            for data, _, key, _ in col.frames[n:]:
                if key and contains_idr(data):
                    got_idr = True
                    break
        assert got_idr, "no IDR within 3 s of request_keyframe()"
    finally:
        src.stop()


def test_approximate_frame_rate():
    src = PatternSource()
    col = Collector()
    src.start(StreamSettings(320, 200, 30, 3000), col)
    try:
        assert col.event.wait(10)
    finally:
        src.stop()
    # 30 frames at 30 fps should span ~1 s of pts
    span_us = col.frames[29][1] - col.frames[0][1]
    assert 0.8e6 < span_us < 1.4e6


def test_stop_is_idempotent_and_fast():
    src = PatternSource()
    col = Collector()
    src.start(StreamSettings(320, 200, 30, 3000), col)
    t0 = time.monotonic()
    src.stop()
    src.stop()
    assert time.monotonic() - t0 < 5

"""Tests for encoder fragment/tail construction (no GStreamer required)."""

from __future__ import annotations

from ubudesk_server.capture import encoders


def test_x264_fragment_is_low_latency_no_bframes():
    frag = encoders.encoder_fragment("x264enc", 8000, 30)
    assert "name=enc" in frag
    assert "tune=zerolatency" in frag
    assert "bframes=0" in frag
    assert "key-int-max=60" in frag  # 2s GOP at 30 fps
    assert "bitrate=8000" in frag
    assert "byte-stream=true" in frag


def test_hw_fragments_are_bare_named_elements():
    for name in ("vah264enc", "vaapih264enc", "nvh264enc"):
        assert encoders.encoder_fragment(name, 8000, 30) == f"{name} name=enc"


def test_pipeline_tail_shape():
    tail = encoders.pipeline_tail("x264enc", 1280, 800, 30, 8000)
    assert tail.startswith("videorate drop-only=true max-rate=30 ")
    assert "video/x-raw,format=I420,width=1280,height=800" in tail
    assert "stream-format=byte-stream,alignment=au" in tail
    assert "h264parse config-interval=-1" in tail
    assert tail.endswith("appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true")
    # order: rate -> convert/scale -> caps -> enc -> caps -> parse -> sink
    assert tail.index("videoconvert") < tail.index("x264enc") < tail.index("h264parse")


class FakeElement:
    """find_property/set_property double with a configurable property set."""

    def __init__(self, props: set[str]):
        self._props = props
        self.set: dict[str, object] = {}

    def find_property(self, name: str):
        if name in self._props:
            return object()  # plain pspec: not an enum, no value_type attr
        return None

    def set_property(self, name: str, value: object) -> None:
        self.set[name] = value


def test_apply_properties_only_sets_existing_props():
    el = FakeElement({"bitrate", "key-int-max"})
    applied = encoders.apply_properties(el, "vah264enc", 6000, 30)
    assert applied == {"bitrate": 6000, "key-int-max": 60}
    assert el.set == {"bitrate": 6000, "key-int-max": 60}


def test_apply_properties_x264_noop():
    el = FakeElement({"bitrate"})
    assert encoders.apply_properties(el, "x264enc", 6000, 30) == {}
    assert el.set == {}


def test_set_bitrate_reports_presence():
    el = FakeElement({"bitrate"})
    assert encoders.set_bitrate(el, "vah264enc", 4000) is True
    assert el.set == {"bitrate": 4000}
    assert encoders.set_bitrate(FakeElement(set()), "vah264enc", 4000) is False

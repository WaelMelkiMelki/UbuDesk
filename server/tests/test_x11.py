"""Tests for the X11 xrandr ladder (pure parsing + ladder driver with a fake runner)."""

from __future__ import annotations

import pytest

from ubudesk_server.capture.base import CaptureError
from ubudesk_server.capture.x11 import (
    MODE_PREFIX,
    MONITOR_NAME,
    CmdResult,
    XrandrExtend,
    cvt_modeline_fallback,
    parse_cvt_output,
    parse_xrandr_query,
    pick_extend_output,
)

XRANDR_LAPTOP_WITH_VIRTUAL = """\
Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     60.05*+  59.93
   1680x1050     59.95
HDMI-1 disconnected (normal left inverted right x axis y axis)
VIRTUAL1 disconnected (normal left inverted right x axis y axis)
"""

XRANDR_DESKTOP_NO_SPARE = """\
Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 16384 x 16384
DP-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
   2560x1440     59.95*+
"""

XRANDR_DUAL_HEAD = """\
Screen 0: minimum 8 x 8, current 3840 x 1080, maximum 32767 x 32767
DP-1 connected primary 1920x1080+0+0 (normal left) 527mm x 296mm
   1920x1080     60.00*+
DP-2 connected 1920x1080+1920+0 (normal left) 527mm x 296mm
   1920x1080     60.00*+
DVI-I-1-1 disconnected (normal left inverted right x axis y axis)
"""

XRANDR_STALE_MODE = """\
Screen 0: minimum 8 x 8, current 3200 x 1080, maximum 32767 x 32767
eDP-1 connected primary 1920x1080+0+0 (normal left) 344mm x 194mm
   1920x1080     60.05*+
HDMI-1 disconnected 1280x800+1920+0 (normal left) 0mm x 0mm
   ubudesk_1280x800_30  30.00*
"""

CVT_OUTPUT = """\
# 1280x800 59.81 Hz (CVT 1.02MA-R) hsync: 49.31 kHz; pclk: 83.50 MHz
Modeline "1280x800R"   83.50  1280 1328 1360 1440  800 803 809 823 +hsync -vsync
"""


# ---------------------------------------------------------------- parsing


def test_parse_xrandr_query_screen_and_outputs():
    screen = parse_xrandr_query(XRANDR_LAPTOP_WITH_VIRTUAL)
    assert (screen.fb_width, screen.fb_height) == (1920, 1080)
    assert [o.name for o in screen.outputs] == ["eDP-1", "HDMI-1", "VIRTUAL1"]
    edp = screen.find("eDP-1")
    assert edp is not None and edp.connected and edp.active
    assert edp.geometry == (1920, 1080, 0, 0)
    assert "1920x1080" in edp.modes
    hdmi = screen.find("HDMI-1")
    assert hdmi is not None and not hdmi.connected and not hdmi.active


def test_parse_xrandr_query_dual_head_geometry():
    screen = parse_xrandr_query(XRANDR_DUAL_HEAD)
    dp2 = screen.find("DP-2")
    assert dp2 is not None and dp2.geometry == (1920, 1080, 1920, 0)
    assert screen.primary_geometry() == (1920, 1080, 0, 0)


def test_pick_extend_output_prefers_virtual():
    screen = parse_xrandr_query(XRANDR_LAPTOP_WITH_VIRTUAL)
    out = pick_extend_output(screen)
    assert out is not None and out.name == "VIRTUAL1"


def test_pick_extend_output_falls_back_to_disconnected():
    screen = parse_xrandr_query(XRANDR_DUAL_HEAD)
    out = pick_extend_output(screen)
    assert out is not None and out.name == "DVI-I-1-1"


def test_pick_extend_output_none_when_all_busy():
    screen = parse_xrandr_query(XRANDR_DESKTOP_NO_SPARE)
    assert pick_extend_output(screen) is None


def test_parse_cvt_output():
    parsed = parse_cvt_output(CVT_OUTPUT, 1280, 800, 60)
    assert parsed is not None
    name, args = parsed
    assert name == f"{MODE_PREFIX}1280x800_60"
    assert args[0] == "83.50"
    assert args[1:5] == ["1280", "1328", "1360", "1440"]
    assert args[-2:] == ["+hsync", "-vsync"]


def test_cvt_modeline_fallback_shape():
    name, args = cvt_modeline_fallback(1280, 800, 30)
    assert name == f"{MODE_PREFIX}1280x800_30"
    # pclk, 4 horizontal, 4 vertical, 2 sync flags
    assert len(args) == 11
    float(args[0])  # parses as a number
    assert int(args[1]) == 1280 and int(args[5]) == 800
    assert int(args[4]) > 1280 and int(args[8]) > 800  # totals exceed active


# ---------------------------------------------------------------- ladder


class FakeXrandr:
    """Scriptable xrandr: records commands, serves canned query output."""

    def __init__(self, query_outputs: list[str], fail_on: set[str] | None = None):
        self.queries = list(query_outputs)  # consumed one per --query (last one sticks)
        self.fail_on = fail_on or set()
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CmdResult:
        self.commands.append(cmd)
        if cmd[:2] == ["xrandr", "--query"]:
            out = self.queries.pop(0) if len(self.queries) > 1 else self.queries[0]
            return CmdResult(0, out)
        verb = cmd[1] if len(cmd) > 1 else ""
        if verb in self.fail_on:
            return CmdResult(1, "", f"fake failure for {verb}")
        return CmdResult(0, "")

    def calls(self, verb: str) -> list[list[str]]:
        return [c for c in self.commands if len(c) > 1 and c[1] == verb]


XRANDR_AFTER_ENABLE = """\
Screen 0: minimum 8 x 8, current 3200 x 1080, maximum 32767 x 32767
eDP-1 connected primary 1920x1080+0+0 (normal left) 344mm x 194mm
   1920x1080     60.05*+
HDMI-1 disconnected (normal left)
VIRTUAL1 connected 1280x800+1920+0 (normal left) 0mm x 0mm
   ubudesk_1280x800_30  30.00*
"""


def test_ladder_virtual_output_happy_path():
    fake = FakeXrandr([XRANDR_LAPTOP_WITH_VIRTUAL, XRANDR_LAPTOP_WITH_VIRTUAL, XRANDR_AFTER_ENABLE])
    ext = XrandrExtend(runner=fake, cvt_path="")
    region = ext.create(1280, 800, 30)
    assert ext.method == "output"
    assert ext.output_name == "VIRTUAL1"
    assert region == (1280, 800, 1920, 0)  # geometry read back from xrandr
    assert fake.calls("--newmode") and fake.calls("--addmode")
    enable = fake.calls("--output")[-1]
    assert "--right-of" in enable and "eDP-1" in enable


def test_ladder_setmonitor_when_no_connector():
    fake = FakeXrandr([XRANDR_DESKTOP_NO_SPARE])
    ext = XrandrExtend(runner=fake, cvt_path="")
    region = ext.create(1280, 800, 30)
    assert ext.method == "setmonitor"
    assert region == (1280, 800, 2560, 0)
    fb = fake.calls("--fb")
    assert fb and fb[0][2] == "3840x1440"  # 2560+1280 wide
    setmon = fake.calls("--setmonitor")[0]
    assert setmon[2] == MONITOR_NAME
    assert "+2560+0" in setmon[3]


def test_ladder_addmode_refused_raises_no_virtual_monitor():
    fake = FakeXrandr([XRANDR_LAPTOP_WITH_VIRTUAL], fail_on={"--addmode"})
    ext = XrandrExtend(runner=fake, cvt_path="")
    with pytest.raises(CaptureError) as ei:
        ext.create(1280, 800, 30)
    assert ei.value.code == "no_virtual_monitor"
    # mode must have been rolled back
    assert fake.calls("--rmmode")


def test_ladder_setmonitor_failure_restores_fb():
    fake = FakeXrandr([XRANDR_DESKTOP_NO_SPARE], fail_on={"--setmonitor"})
    ext = XrandrExtend(runner=fake, cvt_path="")
    with pytest.raises(CaptureError) as ei:
        ext.create(1280, 800, 30)
    assert ei.value.code == "no_virtual_monitor"
    fbs = fake.calls("--fb")
    assert fbs[-1][2] == "2560x1440"  # original framebuffer restored


def test_clean_stale_removes_leftovers():
    fake = FakeXrandr([XRANDR_STALE_MODE])
    ext = XrandrExtend(runner=fake, cvt_path="")
    ext.clean_stale()
    assert fake.calls("--delmonitor")[0][2] == MONITOR_NAME
    off = fake.calls("--output")
    assert off and off[0][2] == "HDMI-1" and off[0][3] == "--off"
    delmode = fake.calls("--delmode")[0]
    assert delmode[2] == "HDMI-1" and delmode[3].startswith(MODE_PREFIX)


def test_cleanup_output_method():
    fake = FakeXrandr([XRANDR_LAPTOP_WITH_VIRTUAL, XRANDR_LAPTOP_WITH_VIRTUAL, XRANDR_AFTER_ENABLE])
    ext = XrandrExtend(runner=fake, cvt_path="")
    ext.create(1280, 800, 30)
    fake.commands.clear()
    ext.cleanup()
    assert fake.calls("--output")[0][2:] == ["VIRTUAL1", "--off"]
    assert fake.calls("--delmode") and fake.calls("--rmmode")
    fake.commands.clear()
    ext.cleanup()  # idempotent
    assert not fake.commands


def test_cleanup_setmonitor_method():
    fake = FakeXrandr([XRANDR_DESKTOP_NO_SPARE])
    ext = XrandrExtend(runner=fake, cvt_path="")
    ext.create(1280, 800, 30)
    fake.commands.clear()
    ext.cleanup()
    assert fake.calls("--delmonitor")[0][2] == MONITOR_NAME
    assert fake.calls("--fb")[0][2] == "2560x1440"


def test_modeline_uses_cvt_when_available():
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> CmdResult:
        calls.append(cmd)
        if cmd[0] == "/usr/bin/cvt":
            return CmdResult(0, CVT_OUTPUT)
        return CmdResult(0, "")

    ext = XrandrExtend(runner=runner, cvt_path="/usr/bin/cvt")
    name, args = ext.modeline(1280, 800, 60)
    assert name == f"{MODE_PREFIX}1280x800_60"
    assert args[0] == "83.50"
    assert calls[0][0] == "/usr/bin/cvt"


def test_modeline_falls_back_without_cvt():
    ext = XrandrExtend(runner=lambda cmd: CmdResult(127, "", "nope"), cvt_path="")
    name, args = ext.modeline(1920, 1080, 30)
    assert name == f"{MODE_PREFIX}1920x1080_30"
    assert len(args) == 11

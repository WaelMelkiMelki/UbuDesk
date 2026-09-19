"""GStreamer H.264 encoder selection.

Order of preference (``--encoder auto``): vah264enc (VA-API, Intel/AMD),
nvh264enc (NVENC), x264enc (software, always the safety net).

Property names differ between plugins and GStreamer versions, so properties
are applied through introspection: a property is set only if the element
actually has it.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# name -> (element factory, {property: value builder})
_CANDIDATES: dict[str, str] = {
    "va": "vah264enc",
    "nvenc": "nvh264enc",
    "x264": "x264enc",
}

_AUTO_ORDER = ["va", "nvenc", "x264"]


def gst_available() -> bool:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: F401

        return True
    except (ImportError, ValueError):
        return False


def find_encoder(preference: str = "auto") -> str | None:
    """Return the gst factory name of the first usable encoder, or None."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)

    order = _AUTO_ORDER if preference == "auto" else [preference]
    for key in order:
        factory_name = _CANDIDATES.get(key)
        if factory_name and Gst.ElementFactory.find(factory_name):
            return factory_name
    return None


def encoder_fragment(factory_name: str, bitrate_kbps: int, fps: int) -> str:
    """Pipeline-description fragment for the chosen encoder."""
    gop = 2 * fps
    if factory_name == "x264enc":
        return (
            f"x264enc name=enc tune=zerolatency speed-preset=ultrafast bframes=0 "
            f"key-int-max={gop} bitrate={bitrate_kbps} byte-stream=true"
        )
    # VA / NVENC: set common properties by name at runtime (see apply_properties)
    return f"{factory_name} name=enc"


def apply_properties(element, factory_name: str, bitrate_kbps: int, fps: int) -> dict[str, object]:
    """Best-effort low-latency configuration via introspection.

    Returns the properties that were actually applied (for logging).
    """
    gop = 2 * fps
    wanted: dict[str, object] = {}
    if factory_name == "vah264enc":
        wanted = {
            "bitrate": bitrate_kbps,
            "key-int-max": gop,
            "rate-control": "cbr",
            "b-frames": 0,
            "target-usage": 6,  # speed
        }
    elif factory_name == "nvh264enc":
        wanted = {
            "bitrate": bitrate_kbps,
            "gop-size": gop,
            "rc-mode": "cbr",
            "bframes": 0,
            "preset": "low-latency-hq",
            "zerolatency": True,
        }
    elif factory_name == "x264enc":
        return {}  # set in the fragment already

    applied: dict[str, object] = {}
    for prop, value in wanted.items():
        pspec = element.find_property(prop)
        if pspec is None:
            continue
        try:
            if hasattr(pspec, "value_type") and pspec.value_type.is_a(_enum_type()):
                _set_enum_by_nick(element, prop, str(value))
            else:
                element.set_property(prop, value)
            applied[prop] = value
        except Exception as exc:  # noqa: BLE001 - property sets vary per driver
            log.debug("could not set %s.%s=%r: %s", factory_name, prop, value, exc)
    return applied


def set_bitrate(element, factory_name: str, bitrate_kbps: int) -> bool:
    """Live bitrate change; returns True if the property existed."""
    if element.find_property("bitrate") is None:
        return False
    element.set_property("bitrate", bitrate_kbps)
    return True


def _enum_type():
    from gi.repository import GObject

    return GObject.GEnum.__gtype__


def _set_enum_by_nick(element, prop: str, nick: str) -> None:
    pspec = element.find_property(prop)
    enum_class = pspec.enum_class
    for value in enum_class.__enum_values__.values():
        if value.value_nick == nick:
            element.set_property(prop, value)
            return
    raise ValueError(f"enum nick {nick!r} not found for {prop}")

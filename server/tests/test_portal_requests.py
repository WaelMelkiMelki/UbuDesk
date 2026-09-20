"""Portal request construction/cancellation with fake GI; no D-Bus desktop needed."""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace

import pytest

from ubudesk_server.capture.pipeline import PortalSource
from ubudesk_server.capture.portal_session import (
    IFACE_REMOTEDESKTOP,
    IFACE_REQUEST,
    PortalError,
    PortalSession,
)
from ubudesk_server.capture.x11 import X11Source


class Variant:
    def __init__(self, signature, value):
        self.signature = signature
        self.value = value
        if signature.startswith("(") and signature.endswith("a{sv})"):
            assert isinstance(value[-1], dict), "a{sv} requires a native mapping"
            assert all(not isinstance(p, Variant) for p in value[:-1])

    def unpack(self):
        return self.value

    def get_type_string(self):
        return self.signature


class Cancellable:
    def __init__(self):
        self.event = threading.Event()

    def cancel(self):
        self.event.set()


class FakeBus:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.unsubscribed = []
        self.called = threading.Event()
        self.reply = True
        self.block_call = False

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, *args):
        self.callback = args[-1]
        return 42

    def signal_unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)

    def call_sync(self, *args):
        self.calls.append(args)
        method = args[3]
        if method == "Close":
            return None
        self.called.set()
        if self.block_call:
            assert args[-1].event.wait(2)
            raise RuntimeError("D-Bus call cancelled")
        if self.reply:
            # An immediate response exercises subscribe-before-call ordering.
            self.callback(None, None, None, None, None, Variant("(ua{sv})", (0, {})))
        return None


@pytest.fixture
def portal_env(monkeypatch):
    bus = FakeBus()
    glib = SimpleNamespace(
        Variant=Variant,
        MainContext=SimpleNamespace(default=lambda: SimpleNamespace(iteration=lambda _: False)),
        get_monotonic_time=lambda: time.monotonic_ns() // 1000,
    )
    gio = SimpleNamespace(
        Cancellable=Cancellable,
        BusType=SimpleNamespace(SESSION=0),
        DBusCallFlags=SimpleNamespace(NONE=0),
        DBusSignalFlags=SimpleNamespace(NO_MATCH_RULE=0),
        bus_get_sync=lambda *_: bus,
    )
    gi = ModuleType("gi")
    gi.require_version = lambda *_: None
    repository = ModuleType("gi.repository")
    repository.GLib = glib
    repository.Gio = gio
    repository.Gst = SimpleNamespace(State=SimpleNamespace(NULL=0))
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    return bus


@pytest.mark.parametrize(
    ("params", "expected_signature", "expected_values"),
    [
        ([], "(a{sv})", ()),
        ([Variant("o", "/session")], "(oa{sv})", ("/session",)),
        ([Variant("o", "/session"), Variant("s", "")], "(osa{sv})", ("/session", "")),
    ],
)
def test_request_uses_native_outer_values_and_variant_dictionary_values(
    portal_env, params, expected_signature, expected_values
):
    session = PortalSession()
    assert session._call_with_response(IFACE_REMOTEDESKTOP, "Start", params, {}) == {}
    args = portal_env.calls[0][4]
    assert args.get_type_string() == expected_signature
    assert args.unpack()[:-1] == expected_values
    assert isinstance(args.unpack()[-1]["handle_token"], Variant)
    assert portal_env.unsubscribed == [42]


@pytest.mark.parametrize("during_dbus_call", [False, True])
def test_cancel_dismisses_request_and_unsubscribes(portal_env, during_dbus_call):
    portal_env.reply = False
    portal_env.block_call = during_dbus_call
    session = PortalSession()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(session._call_with_response, IFACE_REMOTEDESKTOP, "Start", [], {})
        assert portal_env.called.wait(1)
        session.cancel()
        with pytest.raises(PortalError, match="cancelled"):
            result.result(timeout=1)
    close = portal_env.calls[-1]
    assert close[2:4] == (IFACE_REQUEST, "Close")
    assert close[-1] is None  # do not reuse the cancelled Gio.Cancellable for cleanup
    assert portal_env.unsubscribed == [42]


def test_cancel_before_publication_does_not_open_a_request(portal_env):
    cancelled = threading.Event()
    cancelled.set()
    session = PortalSession(cancelled=cancelled)
    with pytest.raises(PortalError, match="cancelled"):
        session._call_with_response(IFACE_REMOTEDESKTOP, "CreateSession", [], {})
    assert portal_env.calls == []


def test_timed_out_request_is_closed(portal_env):
    portal_env.reply = False
    session = PortalSession()
    with pytest.raises(PortalError, match="timed out"):
        session._call_with_response(IFACE_REMOTEDESKTOP, "Start", [], {}, timeout_s=0.01)
    assert portal_env.calls[-1][2:4] == (IFACE_REQUEST, "Close")
    assert portal_env.unsubscribed == [42]


def test_sessions_on_shared_bus_have_unique_request_paths(portal_env):
    first, second = PortalSession(), PortalSession()
    assert first._request_path(first._next_token("ubudesk")) != second._request_path(
        second._next_token("ubudesk")
    )


def test_source_propagates_cancellation_without_concurrent_teardown(portal_env):
    source = PortalSource("mirror")
    source.session = PortalSession(cancelled=source._cancelled)
    source.cancel_start()
    assert source._cancelled.is_set()
    assert source.session._cancellable.event.is_set()
    assert not source._stopped.is_set()
    assert portal_env.calls == []


@pytest.mark.parametrize("source_kind", ["portal", "x11"])
@pytest.mark.parametrize("failing_step", ["pipeline", "loop"])
def test_native_stop_errors_do_not_skip_monitor_cleanup(portal_env, source_kind, failing_step):
    cleaned = []

    def stop_pipeline(_state):
        if failing_step == "pipeline":
            raise RuntimeError("pipeline error")

    def stop_loop():
        if failing_step == "loop":
            raise RuntimeError("loop error")

    if source_kind == "portal":
        source = PortalSource("extend")
        source.session = SimpleNamespace(close=lambda: cleaned.append("portal"))
    else:
        source = X11Source("extend")
        source._extend = SimpleNamespace(cleanup=lambda: cleaned.append("x11"))
    source._pipeline = SimpleNamespace(set_state=stop_pipeline)
    source._glib_loop = SimpleNamespace(quit=stop_loop)
    with pytest.raises(RuntimeError):
        source.stop()
    assert cleaned == [source_kind]
    source.stop()  # still idempotent after an exceptional teardown
    assert cleaned == [source_kind]


def test_cancelled_create_session_retains_handle_for_cleanup(portal_env, monkeypatch):
    session = PortalSession()

    def cancelled(*_args, **_kwargs):
        raise PortalError("capture startup cancelled")

    monkeypatch.setattr(session, "_call_with_response", cancelled)
    with pytest.raises(PortalError, match="cancelled"):
        session.open("mirror")
    handle = session.session_handle
    assert handle is not None
    session.close()
    assert portal_env.calls[-1][1] == handle
    assert portal_env.calls[-1][3] == "Close"
    assert session.session_handle is None

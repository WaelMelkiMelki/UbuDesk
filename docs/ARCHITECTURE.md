# UbuDesk architecture

```
 Ubuntu PC                                                   Android device
┌──────────────────────────────────────────────┐            ┌───────────────────────────────┐
│ GNOME/Mutter                                 │            │ Compose UI (connect/settings) │
│  └─ virtual monitor  ◄── xdg-desktop-portal  │            │ NSD/mDNS discovery            │
│         │   (ScreenCast VIRTUAL + RemoteDesktop)          │ TLS socket (TCP_NODELAY)      │
│         ▼ PipeWire node                      │            │  ├─ frame reader thread       │
│  GStreamer: pipewiresrc → convert/scale →    │  H.264 AU  │  ├─ MediaCodec (low-latency)  │
│   encoder (x264 / VA / NVENC) → h264parse →  │ ─────────► │  └─ SurfaceView (ASAP render) │
│   appsink                                    │  TCP/TLS   │ Touch/Key mapper              │
│         │                                    │ ◄───────── │  (normalized coords, JSON)    │
│  asyncio TLS server ── auth/pairing ─────────┼─ input msgs┤                               │
│         ▼                                    │            └───────────────────────────────┘
│  InputBackend: RemoteDesktop portal          │
│   (NotifyTouch*/Pointer*/Keyboard*)          │   USB mode: `ubudesk usb` = adb reverse,
│  mDNS advertise `_ubudesk._tcp`              │   app connects to 127.0.0.1:7777
└──────────────────────────────────────────────┘
```

## Server (Python 3.11+)

### Package layout

| Module | Responsibility |
|---|---|
| `protocol.py` | framing + JSON codec, Annex-B helpers; mirrored by Kotlin `Protocol.kt` |
| `net/server.py` | asyncio TCP/TLS accept loop, wiring config → source factory |
| `net/session.py` | per-client state machine `HELLO → AUTH → READY → STREAMING` |
| `net/auth.py` | PIN manager (single use / expiry / lockout), hashed token store |
| `net/tls.py` | self-signed EC P-256 cert, SHA-256 fingerprint |
| `net/discovery.py` | zeroconf advertising of `_ubudesk._tcp` |
| `net/usb.py` | `adb reverse` helper |
| `capture/base.py` | `VideoSource` interface (`start/stop/request_keyframe/set_bitrate`) |
| `capture/test_source.py` | headless H.264 test pattern (PyAV/libx264) — the CI path |
| `capture/portal_session.py` | xdg-desktop-portal RemoteDesktop+ScreenCast session |
| `capture/pipeline.py` | GStreamer `pipewiresrc → encoder → appsink` |
| `capture/encoders.py` | encoder auto-detection (va → nvenc → x264) + introspected properties |
| `input/base.py` | `InputBackend` interface |
| `input/fake.py` | event-recording backend (CI + `--source test`) |
| `input/portal_input.py` | RemoteDesktop portal Notify* calls |
| `input/uinput_input.py` | /dev/uinput fallback (X11) |
| `doctor.py` | environment diagnostics with fix commands |

### Threading model

- The **asyncio event loop** owns the sockets and the per-client send queue.
- Each **VideoSource** produces frames on its own thread (PyAV encode thread
  for the test source; GStreamer streaming threads for real capture) and calls
  `on_frame(...)`, which hops onto the loop with `loop.call_soon_threadsafe`.
- Portal D-Bus calls happen in a worker thread (`run_in_executor`) because the
  permission dialog can block for minutes; that thread pumps the default GLib
  main context while waiting for `Request.Response` signals.
- A small dedicated GLib `MainLoop` thread watches the GStreamer bus.

### Backpressure (the latency guarantee)

Per client there is a bounded queue (~5 frames). If the client cannot keep
up, the session drops **all delta frames**, keeps/waits for a keyframe,
forces an IDR upstream, and resumes from it. The encoder is configured with
`key-int-max = 2*fps` so waits are bounded even without the explicit force.
The same policy exists client-side in `VideoDecoder.feed()` (max 3 queued
inputs, drop-to-key + `{"t":"idr"}`).

### Why one combined portal session?

`RemoteDesktop.CreateSession` + `ScreenCast.SelectSources` on the same session
means input coordinates from `NotifyPointerMotionAbsolute(stream, x, y)` map
directly onto the streamed (virtual) monitor — no manual monitor-geometry math
and no layout races. Closing the session removes the virtual monitor and
Mutter moves the windows back; cleanup is guaranteed even after `kill -9`
because the session dies with the D-Bus connection.

### Capture fallback ladder

1. Portal `VIRTUAL` source (extend). `doctor` reports whether
   `AvailableSourceTypes` has bit 4.
2. Portal `MONITOR` source (mirror) — works on every Wayland desktop.
3. X11 sessions: planned `xrandr` virtual output + `ximagesrc` + uinput
   (milestone M8, not implemented yet); mirror-via-portal generally still
   works on X11 GNOME because xdg-desktop-portal-gnome runs there too.

The server never falls back silently: the client receives
`error.code = no_virtual_monitor` with instructions and the user chooses
mirror mode explicitly.

### Virtual monitor resolution

The client asks for `width × height` in `start`. The portal decides the
actual stream size (returned in `Start`'s `streams[0].size`). The pipeline
contains `videoscale ! video/x-raw,width=W,height=H`, so whatever Mutter
creates is scaled to the negotiated size, and `started` always tells the
client the true encoded dimensions. Both sizes are logged.

### Non-obvious decisions

- **PyAV for the test source** instead of GStreamer: CI containers and dev
  sandboxes often lack GI/GStreamer; PyAV ships libx264 in its wheel, so the
  *entire protocol + session + backpressure stack* is testable headless. The
  GStreamer path shares everything above `VideoSource`.
- **`keepalive-time=100` on pipewiresrc**: PipeWire screen casts emit frames
  only on damage. Without keepalive a static desktop starves the client and
  trips the 6 s idle timeout.
- **Encoder properties by introspection** (`encoders.apply_properties`):
  VA/NVENC property names drift between GStreamer versions; we set a property
  only if the element exposes it and log what was applied.
- **Tokens hashed, PIN comparisons constant-time** — see PROTOCOL.md §6.
- **One `start` restarts the stream** rather than a separate `stop` message:
  fewer states, and rotation/resolution changes need exactly this.

## Android client (Kotlin, minSdk 26)

- **Compose** for Connect / Pair / Settings; the stream itself is a plain
  `SurfaceView` inside `AndroidView` (a `TextureView` adds a copy and ~1 frame
  of latency).
- `UbuDeskClient`: dedicated reader **thread** (hot path), writer coroutine
  fed by a channel, ping every 2 s, 6 s silence ⇒ disconnect.
- `VideoDecoder`: `MediaCodec` on its own `HandlerThread`,
  `KEY_LOW_LATENCY` (API 30+) + `KEY_PRIORITY=0` + `KEY_OPERATING_RATE`,
  render with `releaseOutputBuffer(i, true)` immediately. A
  "compatibility decode" setting disables the vendor hints for problem SoCs.
- `TouchMapper` (pure JVM, unit tested) computes the letterboxed video rect
  and normalizes coordinates; `TouchForwarder` implements touch mode and
  mouse mode (two-finger tap = right click, two-finger drag = scroll).
- Pinning: custom `X509TrustManager` that accepts only the stored SHA-256
  leaf fingerprint; during first pairing it captures the fingerprint and the
  UI shows the 8-char security code.

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

## Server (Python 3.10+)

### Package layout

| Module | Responsibility |
|---|---|
| `protocol.py` | framing + JSON codec, Annex-B helpers; mirrored by Kotlin `Protocol.kt` |
| `net/server.py` | asyncio TCP/TLS accept loop, wiring config → source factory |
| `net/session.py` | per-client state machine `HELLO → AUTH → READY → STARTING → STREAMING → CLOSED` |
| `net/auth.py` | PIN manager (single use / expiry / lockout), hashed token store |
| `net/tls.py` | self-signed EC P-256 cert, SHA-256 fingerprint |
| `net/discovery.py` | zeroconf advertising of `_ubudesk._tcp` |
| `net/usb.py` | `adb reverse` helper |
| `capture/base.py` | `VideoSource` interface (`start/cancel_start/stop/request_keyframe/set_bitrate`) |
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
  main context while waiting for `Request.Response` signals. A separate
  startup task awaits the worker, leaving the socket reader free to answer pings.
  Cancellation signals the worker (`Gio.Cancellable` and `Request.Close` for
  portals); it never tears down a source concurrently with `start()`. Workers
  that cannot stop immediately retain ownership and dispose their late results.
  `CLOSED` is terminal. Each stream's callbacks carry a generation so frames
  from a replaced source cannot enter the new stream.
- Capture and input cleanup run off the event loop. Held touches, keys, and
  mouse buttons are released before closing input and the capture/portal.
- Device-store operations reload under a process-safe sidecar file lock. Each
  authenticated session keeps only its token hash and checks the registry once
  per second, so CLI revocation also terminates an already-open connection.
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

### Capture backends and the fallback ladder

`--source auto` (the default) picks the backend from `XDG_SESSION_TYPE`:
`wayland` → portal, `x11` → X11. Both implement the same `VideoSource`
interface and share the encode tail (`encoders.pipeline_tail`).

**Wayland (portal):**

1. Portal `VIRTUAL` source (extend). `doctor` reports whether
   `AvailableSourceTypes` has bit 4.
2. Portal `MONITOR` source (mirror) — works on every Wayland desktop.

**X11 (`capture/x11.py`) — extend ladder, first rung that works wins:**

1. `xrandr` `VIRTUALn` output (xf86-video-dummy / intel virtual heads):
   `--newmode` + `--addmode` + `--output VIRTUALn --mode … --right-of primary`.
2. Any **disconnected** physical connector forced on the same way (works on
   modesetting/amdgpu/intel for most connector types). EVDI note: when the
   `evdi` kernel module is loaded, its virtual connector shows up as a
   disconnected output and is picked up by this rung.
3. `xrandr --fb` enlarge + `--setmonitor UbuDesk` region — no real CRTC, so
   some compositors won't render there; best effort only, flagged in the log.
4. Nothing works → `CaptureError("no_virtual_monitor")`; the client is told
   to use mirror.

Mirror on X11 is plain `ximagesrc` over the primary output's geometry — no
ladder involved, works everywhere. All xrandr interaction goes through an
injectable `Runner`, so the whole ladder is unit-tested headlessly with a
scripted fake (`tests/test_x11.py`). Stale `ubudesk_*` modes / `UbuDesk`
monitors from a crashed run are cleaned on every start, and teardown is
registered with `atexit` as well as `stop()`.

X11 input is injected with `uinput` (python-evdev). If `/dev/uinput` is not
writable the stream continues **view-only** and the log says exactly how to
fix it (udev rule + `input` group).

The server never falls back silently between modes: the client receives
`error.code = no_virtual_monitor` with instructions and the user chooses
mirror mode explicitly. The active backend/mode/encoder is always logged.

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
- **`keepalive-time=100` on pipewiresrc**: PipeWire screen casts can emit
  frames only on damage; keepalive keeps the encoder fed on a static desktop.
  Connection liveness is checked by ping/pong independently of video delivery.
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
  fed by a channel. Pairing allows 120 s with no heartbeats; only after
  `auth_ok` do pings run every 2 s with a 6 s idle timeout. EOF/error/cancellation
  share an idempotent socket cleanup path. The ViewModel ignores superseded
  connection callbacks and serializes control events on its main-thread scope.
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

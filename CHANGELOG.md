# Changelog

## Unreleased (v0.1.0 in progress)

### M0 — Scaffold
- Monorepo layout (server / android / protocol / docs / tools).
- Wire protocol v1 spec (`docs/PROTOCOL.md`) and golden vectors shared by the
  Python and Kotlin test suites.
- `ubudesk doctor` diagnostics, `install-deps.sh`, CI workflow definitions
  (`ci/workflows/`, installed via `scripts/enable-ci.sh`).

### M1 — Test-pattern streaming
- Headless H.264 test source (PyAV/libx264): moving ball + frame counter,
  IDR-on-demand, live bitrate change.
- asyncio server with per-client session state machine, backpressure
  (drop-to-keyframe), `tools/test_client.py`, full integration tests.
- Android app: connect screen (NSD discovery + manual IP + USB), low-latency
  MediaCodec decoder onto SurfaceView, settings, stream overlay.

### M2/M3 — Real capture (implemented, needs on-hardware verification)
- xdg-desktop-portal RemoteDesktop+ScreenCast combined session; VIRTUAL
  (extend) and MONITOR (mirror) sources; restore-token persistence;
  `scripts/portal_probe.py`.
- GStreamer pipeline `pipewiresrc → videoconvert/scale → {vah264enc |
  nvh264enc | x264enc} → h264parse → appsink` with keepalive and leaky queues.

### M4 — Security & discovery
- TLS (self-signed EC P-256), SHA-256 fingerprint pinning (TOFU) on Android,
  PIN pairing with expiry/lockout, hashed token store, `ubudesk devices`,
  zeroconf advertising, `ubudesk usb`.

### M5 — Input (implemented, needs on-hardware verification)
- Portal input injection (touch/pointer/scroll/keycode/keysym text) mapped to
  the streamed monitor; uinput fallback backend; touch & mouse modes with
  two-finger scroll / right-click on Android.

### M7 — Packaging
- systemd --user unit + installer, issue template, troubleshooting guide.

### M8 — X11 backend & broad-compatibility pass
- X11 capture backend (`capture/x11.py`): mirror via `ximagesrc`; extend via
  the xrandr ladder (VIRTUAL output → forced disconnected connector [EVDI
  lands here] → `--setmonitor` region → clear `no_virtual_monitor` error).
  Ladder fully unit-tested with an injected fake xrandr runner; stale-state
  cleanup on start, teardown on stop and atexit.
- `--source auto|portal|x11|test` (auto = pick by `XDG_SESSION_TYPE`); X11
  input via uinput with an explicit view-only warning when unavailable.
- Encoder selection extracted to `capture/encoders.py`:
  `vah264enc → vaapih264enc → nvh264enc → x264enc`, properties applied by
  introspection; shared pipeline tail for portal and X11 sources.
- Compatibility floor lowered to Python 3.10 / GStreamer 1.20 (tomli
  fallback, `vaapih264enc` support) for Ubuntu 22.04; CI matrix now runs on
  ubuntu-22.04 and ubuntu-24.04.
- `ubudesk doctor` prints a per-machine support-matrix verdict (OS, session
  type, mirror yes/no, extend yes/no + why, input path) in text and
  `--json`; new xrandr/ximagesrc/evdi checks.
- Android: `auto` resolution preset (1920-wide on ≥600 dp tablets, 1280-wide
  on phones, device aspect kept), landscape default with optional portrait
  ("Landscape only" switch), README per-Ubuntu support table.

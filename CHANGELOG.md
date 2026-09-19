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

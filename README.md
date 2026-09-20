# UbuDesk

Turn an Android phone or tablet into an **extra (or mirrored) monitor for an
Ubuntu PC** — over Wi-Fi or a USB cable — with touch and keyboard input sent
back to the desktop.

- **Server:** Python 3.10+, GStreamer 1.20+, Wayland (portal) **and** X11
  (ximagesrc + xrandr) capture backends, auto-selected per session
- **Client:** Kotlin / Jetpack Compose, MediaCodec low-latency H.264
- **Security:** PIN pairing, TLS with trust-on-first-use certificate pinning,
  hashed tokens. LAN/USB only — no cloud, no telemetry.

> Status: v0.1 development, **not a stable release**. Automated tests cover the
> protocol, authentication, capture lifecycle, and headless streaming pipeline.
> [CI workflows](#ci-note) run these checks when this branch is pushed to GitHub.
> Desktop capture and real-device behavior still require the checks in
> [docs/QUICK_TEST.md](docs/QUICK_TEST.md) and [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

## Quick start

### 1. Ubuntu server (Ubuntu 22.04 / 24.04 / 24.10 / 25.x; 20.04 best-effort)

```bash
git clone https://github.com/WaelMelkiMelki/UbuDesk.git
cd UbuDesk/server
./scripts/install-deps.sh          # apt packages + venv + pip install
source .venv/bin/activate
ubudesk doctor                     # fix anything it flags
ubudesk serve --pair               # prints a 6-digit PIN + security code
```

Firewall: `sudo ufw allow 7777/tcp` (if ufw is enabled).

### 2. Android app

Download `app-debug.apk` from the latest GitHub Actions run (Artifacts) or a
Release, or build it yourself:

```bash
cd android
./gradlew assembleDebug            # -> app/build/outputs/apk/debug/app-debug.apk
```

Install it (allow unknown sources), open UbuDesk, pick the discovered server
(or type `PC_IP:7777`), and enter the PIN. Compare the 8-character security
code with the one in the server terminal.

### 3. USB mode (no Wi-Fi needed)

Enable USB debugging on the phone, connect the cable, then:

```bash
ubudesk usb        # sets up: adb reverse tcp:7777 tcp:7777
```

Tap **USB** on the app's connect screen.

### 4. Modes

- **Extend** (default): a new virtual monitor appears; drag windows onto it.
  - *Wayland*: needs a portal that supports VIRTUAL sources (GNOME does).
  - *X11*: created with the xrandr ladder — a `VIRTUAL` output, a forced
    disconnected connector (this is also how EVDI virtual displays appear),
    or a `--setmonitor` region as last resort.
- **Mirror**: streams an existing monitor. Works on **every** supported
  combination (portal MONITOR on Wayland, `ximagesrc` on X11).

The server never falls back silently: the mode actually in use is logged and
reported to the client, and `ubudesk doctor` prints a per-machine verdict
(mirror yes/no, extend yes/no, and why) before you even connect.

## How it works

On **Wayland**, capture uses the **xdg-desktop-portal**
ScreenCast+RemoteDesktop APIs: the portal creates a virtual monitor (or
shares a real one) as a PipeWire stream and input is injected through the
same portal session. On **X11**, capture uses `ximagesrc` (with the xrandr
ladder creating the extend region) and input is injected via
`uinput`/python-evdev. Either way GStreamer encodes to H.264
(`vah264enc` → `vaapih264enc` → `nvh264enc` → `x264enc`, picked at runtime),
and a single TLS TCP connection carries video one way and JSON input events
the other. Details in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), wire format in
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## CLI

```
ubudesk serve  [--port 7777] [--bind 0.0.0.0] [--source auto|portal|x11|test]
               [--mode extend|mirror] [--encoder auto|x264|va|nvenc]
               [--no-tls] [--pair] [--log-level debug|info|warning|error]
ubudesk doctor [--json]     # environment diagnostics with fix commands
ubudesk usb                 # adb reverse for USB mode
ubudesk devices [--revoke CLIENT_ID]
```

`--no-tls` is for tests/dev only. For USB-only use, `--bind 127.0.0.1`
keeps the server off the network entirely.

## Development

```bash
# server: lint + types + full test suite (headless, no desktop needed)
cd server && source .venv/bin/activate
ruff check . && mypy ubudesk_server && pytest tests -v

# quick end-to-end without any hardware:
./scripts/run-dev.sh                       # terminal 1
python ../tools/test_client.py --no-tls --pin <PIN> --dump out.h264   # terminal 2
ffplay -f h264 out.h264                    # watch the test pattern

# android
cd android && ./gradlew lintDebug testDebugUnitTest assembleDebug
```

The Python and Kotlin protocol implementations are kept in lockstep by the
shared golden vectors in `protocol/vectors/` — both test suites consume the
same files.

### CI note

The server and Android test/build workflows are installed under
`.github/workflows/`. They run on relevant pushes and pull requests, and can
also be started with **Run workflow** once present on the default branch.
Changes on a working branch must be pushed before GitHub can run them.

- Server: Python 3.10 on Ubuntu 22.04 and Python 3.12 on Ubuntu 24.04, lint,
  type checks, tests, golden vectors, and a decoded-video CLI smoke test.
- Android: Java 17 / SDK 36, JVM tests, lint, and a signed **debug** APK.
  A successful run uploads `ubudesk-debug-<commit>` with the APK, matching
  source archive, checksums, and [first-device-test instructions](docs/QUICK_TEST.md).
  This is a test build, **not a production release**.

The maintained definitions remain in `ci/workflows/`; after editing them,
run `./scripts/enable-ci.sh` to refresh the installed copies. CI checks for
drift with `./scripts/enable-ci.sh --check`. The tag-triggered release publisher
is **not enabled** by default (`--include-release` is a separate opt-in).


## Support matrix (honest)

What *should* work per Ubuntu version and session type — and what has
actually been **verified on real hardware** so far. Run `ubudesk doctor` for
the authoritative verdict on *your* machine.

| Ubuntu | Session | Mirror | Extend | Input | Verified on hardware |
|---|---|---|---|---|---|
| 24.04 / 24.10 / 25.x | GNOME Wayland | ✅ portal | ✅ portal VIRTUAL | portal | ❌ not yet |
| 24.04 / 24.10 / 25.x | X11 (any DE) | ✅ ximagesrc | ⚠️ xrandr ladder¹ | uinput | ❌ not yet |
| 22.04 | GNOME Wayland | ✅ portal | ⚠️ portal-dependent² | portal | ❌ not yet |
| 22.04 | X11 (any DE) | ✅ ximagesrc | ⚠️ xrandr ladder¹ | uinput | ❌ not yet |
| 20.04 (best-effort) | X11 | ✅ ximagesrc | ⚠️ xrandr ladder¹ | uinput | ❌ not yet |
| Other Wayland DEs (KDE…) | Wayland | ✅ usually | portal-dependent² | portal-dependent | ❌ not yet |

¹ X11 extend depends on the GPU driver: works out of the box when a
`VIRTUAL` output or a spare disconnected connector exists (or with
`evdi-dkms` installed); otherwise falls back to a `--setmonitor` region,
which some compositors do not render. `ubudesk doctor` tells you which rung
of the ladder applies. Mirror always works.
² The portal must advertise VIRTUAL source types; GNOME ≥ 42 on Wayland
does, most others don't (yet). Mirror always works.

The headless CI definition (protocol, auth, TLS, encode pipeline with test
source) targets **ubuntu-22.04 and ubuntu-24.04**.
A passing headless run is not proof of desktop capture or Android device behavior.
Per-feature hardware status is tracked in
[docs/MANUAL_TEST.md](docs/MANUAL_TEST.md); the table above will be updated
as real machines confirm each row.

## License

[MIT](LICENSE)

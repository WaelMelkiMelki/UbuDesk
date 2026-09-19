# UbuDesk

Turn an Android phone or tablet into an **extra (or mirrored) monitor for an
Ubuntu PC** — over Wi-Fi or a USB cable — with touch and keyboard input sent
back to the desktop.

- **Server:** Python 3.11+, GStreamer, xdg-desktop-portal (Wayland/GNOME first-class)
- **Client:** Kotlin / Jetpack Compose, MediaCodec low-latency H.264
- **Security:** PIN pairing, TLS with trust-on-first-use certificate pinning,
  hashed tokens. LAN/USB only — no cloud, no telemetry.

> Status: v0.1 development. The protocol, auth, and the headless streaming
> pipeline are covered by CI; desktop capture and real-device behavior are
> tracked honestly in [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

## Quick start

### 1. Ubuntu server (Ubuntu 24.04+, GNOME on Wayland recommended)

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

- **Extend** (default): a new virtual monitor appears in *Settings →
  Displays*; drag windows onto it. Requires GNOME on Wayland with a portal
  that supports VIRTUAL sources (`ubudesk doctor` tells you).
- **Mirror**: streams an existing monitor. Works on any Wayland desktop
  (you pick the monitor in the GNOME dialog).

## How it works

Capture uses the **xdg-desktop-portal** ScreenCast+RemoteDesktop APIs: the
portal creates a virtual monitor (or shares a real one) as a PipeWire stream,
GStreamer encodes it to H.264 (VA-API/NVENC when available, x264 otherwise),
and a single TLS TCP connection carries video one way and JSON input events
the other. Touch/mouse/keyboard are injected through the same portal session,
so coordinates always land on the streamed monitor. Details in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), wire format in
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## CLI

```
ubudesk serve  [--port 7777] [--bind 0.0.0.0] [--source portal|test]
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

GitHub Actions workflows live in `ci/workflows/`. The automation account
that created this branch cannot push `.github/workflows/` (missing
`workflows` permission), so enable them once with:

```bash
./scripts/enable-ci.sh
git add .github/workflows && git commit -m "ci: enable workflows" && git push
```

## Support matrix (honest)

| Environment | Extend | Mirror | Input |
|---|---|---|---|
| Ubuntu 24.04+ GNOME Wayland | ✅ designed for (portal VIRTUAL) | ✅ | ✅ portal |
| Other Wayland desktops | portal-dependent | ✅ usually | portal-dependent |
| X11 sessions | ❌ planned (M8) | ⚠️ via portal if present | ⚠️ uinput fallback |

“Verified on real hardware” per feature: see
[docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

## License

[MIT](LICENSE)
